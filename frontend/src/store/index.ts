import { create } from "zustand";
import { persist } from "zustand/middleware";
import { RuntimeClient } from "../lib/runtime/client";
import {
  AgentSummary,
  ApprovalDecision,
  AsyncStatus,
  BackgroundTaskSummary,
  StoredSessionSummary,
  SessionState,
  EventEnvelope,
  ProviderModelsResult,
  ProviderSummary,
  ProviderValidationResult,
  QuestionAnswer,
  RuntimeNotification,
  RuntimeSessionDebugSnapshot,
  RuntimeStatusSnapshot,
  RuntimeSettings,
  RuntimeSettingsUpdate,
  ReviewFileDiff,
  WorkspaceRegistrySnapshot,
  WorkspaceReviewSnapshot,
} from "../lib/runtime/types";

const DEFAULT_SESSION_SIDEBAR_WIDTH = 344;
const APPROVAL_REPLAY_POLL_DELAY_MS = 700;

interface AppState {
  language: "en" | "zh-CN";

  agentPreset: string;
  providerModel: string;
  reasoningEffort: string;
  workspaces: WorkspaceRegistrySnapshot | null;
  workspacesStatus: AsyncStatus;
  workspacesError: string | null;
  workspaceSwitchStatus: AsyncStatus;
  workspaceSwitchError: string | null;
  providers: ProviderSummary[];
  providersStatus: AsyncStatus;
  providersError: string | null;
  providerModels: Record<string, ProviderModelsResult>;
  providerValidationResults: Record<string, ProviderValidationResult>;
  providerValidationStatus: Record<string, AsyncStatus>;
  providerValidationError: Record<string, string | null>;
  agentPresets: AgentSummary[];
  agentsStatus: AsyncStatus;
  agentsError: string | null;
  statusSnapshot: RuntimeStatusSnapshot | null;
  statusStatus: AsyncStatus;
  statusError: string | null;
  mcpRetryStatus: AsyncStatus;
  mcpRetryError: string | null;
  reviewSnapshot: WorkspaceReviewSnapshot | null;
  reviewStatus: AsyncStatus;
  reviewError: string | null;
  reviewSelectedPath: string | null;
  reviewDiff: ReviewFileDiff | null;
  reviewDiffStatus: AsyncStatus;
  reviewDiffError: string | null;
  reviewMode: "changes" | "files";

  sessions: StoredSessionSummary[];
  currentSessionId: string | null;
  sessionSidebarWidth: number;
  currentSessionState: SessionState | null;
  currentSessionEvents: EventEnvelope[];
  currentSessionOutput: string | null;

  sessionsStatus: "idle" | "loading" | "success" | "error";
  sessionsError: string | null;
  replayStatus: "idle" | "loading" | "success" | "error";
  replayError: string | null;
  runStatus: "idle" | "running" | "success" | "error";
  runError: string | null;
  approvalStatus: "idle" | "submitting" | "success" | "error";
  approvalError: string | null;
  questionStatus: "idle" | "submitting" | "success" | "error";
  questionError: string | null;
  notifications: RuntimeNotification[];
  notificationsStatus: AsyncStatus;
  notificationsError: string | null;
  backgroundTasks: BackgroundTaskSummary[];
  backgroundTasksStatus: AsyncStatus;
  backgroundTasksError: string | null;
  sessionDebug: RuntimeSessionDebugSnapshot | null;
  sessionDebugStatus: AsyncStatus;
  sessionDebugError: string | null;
  replayRequestId: number;

  settings: RuntimeSettings | null;
  settingsStatus: "idle" | "loading" | "success" | "error";
  settingsError: string | null;

  setLanguage: (lang: "en" | "zh-CN") => void;
  setAgentPreset: (preset: string) => void;
  setProviderModel: (model: string) => void;
  setReasoningEffort: (effort: string) => void;
  loadWorkspaces: () => Promise<void>;
  switchWorkspace: (path: string) => Promise<void>;
  loadProviders: () => Promise<void>;
  validateProviderCredentials: (providerName: string) => Promise<void>;
  loadAgents: () => Promise<void>;
  loadStatus: () => Promise<void>;
  retryMcpConnections: () => Promise<void>;
  loadReview: () => Promise<void>;
  selectReviewPath: (path: string | null) => Promise<void>;
  setReviewMode: (mode: "changes" | "files") => void;
  setSessionSidebarWidth: (width: number) => void;
  loadSessions: () => Promise<void>;
  selectSession: (sessionId: string) => Promise<void>;
  runTask: (
    prompt: string,
    options?: {
      sessionId?: string | null;
      metadata?: {
        skills?: string[];
        provider_stream?: boolean;
        [key: string]: unknown;
      };
    },
  ) => Promise<void>;
  resolveApproval: (decision: ApprovalDecision) => Promise<void>;
  answerQuestion: (answers: QuestionAnswer[]) => Promise<void>;
  loadNotifications: () => Promise<void>;
  acknowledgeNotification: (notificationId: string) => Promise<void>;
  loadBackgroundTasks: () => Promise<void>;
  cancelBackgroundTask: (taskId: string) => Promise<void>;
  loadSessionDebug: (sessionId?: string | null) => Promise<void>;
  loadSettings: () => Promise<void>;
  updateSettings: (settings: RuntimeSettingsUpdate) => Promise<void>;
}

function getPendingApprovalRequestId(events: EventEnvelope[]): string | null {
  const resolvedRequestIds = new Set<string>();

  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    const requestId = event.payload.request_id;

    if (event.event_type === "runtime.approval_resolved") {
      if (typeof requestId === "string" && requestId.length > 0) {
        resolvedRequestIds.add(requestId);
      }
      continue;
    }

    if (event.event_type !== "runtime.approval_requested") {
      continue;
    }

    if (
      typeof requestId === "string" &&
      requestId.length > 0 &&
      !resolvedRequestIds.has(requestId)
    ) {
      return requestId;
    }
  }

  return null;
}

function appendLocalApprovalResolution(
  events: EventEnvelope[],
  sessionId: string,
  requestId: string,
  decision: ApprovalDecision,
): EventEnvelope[] {
  if (
    events.some(
      (event) =>
        event.event_type === "runtime.approval_resolved" &&
        event.payload.request_id === requestId,
    )
  ) {
    return events;
  }

  const maxSequence = events.reduce(
    (max, event) => Math.max(max, event.sequence),
    0,
  );

  return [
    ...events,
    {
      session_id: sessionId,
      sequence: maxSequence + 1,
      event_type: "runtime.approval_resolved",
      source: "runtime",
      payload: { request_id: requestId, decision },
      received_at: Date.now(),
    },
  ];
}

function replayStillShowsSamePendingApproval(
  replayEvents: EventEnvelope[],
  requestId: string,
): boolean {
  return getPendingApprovalRequestId(replayEvents) === requestId;
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function runStatusForReplay(session: SessionState): AppState["runStatus"] {
  return session.status === "running" ? "running" : "idle";
}

function getPendingQuestionRequestId(events: EventEnvelope[]): string | null {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event.event_type !== "runtime.question_requested") {
      continue;
    }

    const requestId = event.payload.request_id;
    if (typeof requestId === "string" && requestId.length > 0) {
      return requestId;
    }
  }

  return null;
}

function firstTreeFilePath(
  nodes: WorkspaceReviewSnapshot["tree"],
): string | null {
  for (const node of nodes) {
    if (node.kind === "file") {
      return node.path;
    }
    const childPath = firstTreeFilePath(node.children);
    if (childPath) {
      return childPath;
    }
  }

  return null;
}

function treeContainsPath(
  nodes: WorkspaceReviewSnapshot["tree"],
  targetPath: string,
): boolean {
  for (const node of nodes) {
    if (node.path === targetPath) {
      return true;
    }
    if (treeContainsPath(node.children, targetPath)) {
      return true;
    }
  }

  return false;
}

function normalizeProviderModelReference(
  model: string,
  providers: ProviderSummary[],
  providerModels: Record<string, ProviderModelsResult>,
): string {
  if (!model || model.includes("/")) {
    return model;
  }

  const currentProviderName = providers.find(
    (provider) => provider.current && provider.configured,
  )?.name;
  if (
    currentProviderName &&
    (providerModels[currentProviderName]?.models ?? []).includes(model)
  ) {
    return `${currentProviderName}/${model}`;
  }

  const matchingProviderNames = Object.entries(providerModels)
    .filter(([, result]) => result.models.includes(model))
    .map(([providerName]) => providerName);
  if (matchingProviderNames.length === 1) {
    return `${matchingProviderNames[0]}/${model}`;
  }

  return model;
}

function selectedModelMetadata(
  model: string,
  providers: ProviderSummary[],
  providerModels: Record<string, ProviderModelsResult>,
) {
  const normalized = normalizeProviderModelReference(
    model,
    providers,
    providerModels,
  );
  const [providerName, ...modelParts] = normalized.split("/");
  const modelName = modelParts.join("/");
  if (!providerName || !modelName) return undefined;
  const metadata = providerModels[providerName]?.model_metadata ?? {};
  return metadata[modelName] ?? metadata[normalized];
}

export const useAppStore = create<AppState>()(
  persist(
    (set, get) => ({
      language: "en",
      agentPreset: "leader",
      providerModel: "opencode-go/glm-5.1",
      reasoningEffort: "",
      workspaces: null,
      workspacesStatus: "idle",
      workspacesError: null,
      workspaceSwitchStatus: "idle",
      workspaceSwitchError: null,
      providers: [],
      providersStatus: "idle",
      providersError: null,
      providerModels: {},
      providerValidationResults: {},
      providerValidationStatus: {},
      providerValidationError: {},
      agentPresets: [],
      agentsStatus: "idle",
      agentsError: null,
      statusSnapshot: null,
      statusStatus: "idle",
      statusError: null,
      mcpRetryStatus: "idle",
      mcpRetryError: null,
      reviewSnapshot: null,
      reviewStatus: "idle",
      reviewError: null,
      reviewSelectedPath: null,
      reviewDiff: null,
      reviewDiffStatus: "idle",
      reviewDiffError: null,
      reviewMode: "changes",

      sessions: [],
      currentSessionId: null,
      sessionSidebarWidth: DEFAULT_SESSION_SIDEBAR_WIDTH,
      currentSessionState: null,
      currentSessionEvents: [],
      currentSessionOutput: null,

      sessionsStatus: "idle",
      sessionsError: null,
      replayStatus: "idle",
      replayError: null,
      runStatus: "idle",
      runError: null,
      approvalStatus: "idle",
      approvalError: null,
      questionStatus: "idle",
      questionError: null,
      notifications: [],
      notificationsStatus: "idle",
      notificationsError: null,
      backgroundTasks: [],
      backgroundTasksStatus: "idle",
      backgroundTasksError: null,
      sessionDebug: null,
      sessionDebugStatus: "idle",
      sessionDebugError: null,
      replayRequestId: 0,

      settings: null,
      settingsStatus: "idle",
      settingsError: null,

      setLanguage: (language) => set({ language }),
      setAgentPreset: (agentPreset) => set({ agentPreset }),
      setProviderModel: (providerModel) => set({ providerModel }),
      setReasoningEffort: (reasoningEffort) => set({ reasoningEffort }),

      loadWorkspaces: async () => {
        set({ workspacesStatus: "loading", workspacesError: null });
        try {
          const workspaces = await RuntimeClient.listWorkspaces();
          set({
            workspaces,
            workspacesStatus: "success",
            workspaceSwitchStatus:
              get().workspaceSwitchStatus === "loading"
                ? "success"
                : get().workspaceSwitchStatus,
            workspaceSwitchError: null,
          });
        } catch (err) {
          set({
            workspacesStatus: "error",
            workspacesError: (err as Error).message,
            workspaceSwitchStatus:
              get().workspaceSwitchStatus === "loading"
                ? "error"
                : get().workspaceSwitchStatus,
            workspaceSwitchError:
              get().workspaceSwitchStatus === "loading"
                ? (err as Error).message
                : get().workspaceSwitchError,
          });
        }
      },

      switchWorkspace: async (path) => {
        set({
          workspaceSwitchStatus: "loading",
          workspaceSwitchError: null,
        });
        try {
          const workspaces = await RuntimeClient.openWorkspace(path);
          set({
            workspaces,
            workspacesStatus: "success",
            workspacesError: null,
            workspaceSwitchStatus: "success",
            workspaceSwitchError: null,
            providers: [],
            providersStatus: "idle",
            providersError: null,
            providerModels: {},
            providerValidationResults: {},
            providerValidationStatus: {},
            providerValidationError: {},
            agentPresets: [],
            agentsStatus: "idle",
            agentsError: null,
            currentSessionId: null,
            currentSessionState: null,
            currentSessionEvents: [],
            currentSessionOutput: null,
            replayStatus: "idle",
            replayError: null,
            runStatus: "idle",
            runError: null,
            approvalStatus: "idle",
            approvalError: null,
            questionStatus: "idle",
            questionError: null,
            reviewSnapshot: null,
            reviewStatus: "idle",
            reviewError: null,
            reviewSelectedPath: null,
            reviewDiff: null,
            reviewDiffStatus: "idle",
            reviewDiffError: null,
            statusSnapshot: null,
            statusStatus: "idle",
            statusError: null,
            mcpRetryStatus: "idle",
            mcpRetryError: null,
            sessions: [],
            sessionsStatus: "idle",
            sessionsError: null,
            notifications: [],
            notificationsStatus: "idle",
            notificationsError: null,
            backgroundTasks: [],
            backgroundTasksStatus: "idle",
            backgroundTasksError: null,
            sessionDebug: null,
            sessionDebugStatus: "idle",
            sessionDebugError: null,
          });
          await Promise.all([
            get().loadSessions(),
            get().loadProviders(),
            get().loadAgents(),
            get().loadStatus(),
            get().loadReview(),
            get().loadNotifications(),
            get().loadBackgroundTasks(),
          ]);
        } catch (err) {
          set({
            workspaceSwitchStatus: "error",
            workspaceSwitchError: (err as Error).message,
          });
        }
      },

      loadProviders: async () => {
        set({ providersStatus: "loading", providersError: null });
        try {
          const providers = await RuntimeClient.listProviders();
          const configuredProviders = providers.filter(
            (provider) => provider.configured,
          );
          const providerModelsEntries = await Promise.all(
            configuredProviders.map(async (provider) => {
              const result = await RuntimeClient.listProviderModels(
                provider.name,
              );
              return [provider.name, result] as const;
            }),
          );
          const providerModels = Object.fromEntries(providerModelsEntries);
          const normalizedProviderModel = normalizeProviderModelReference(
            get().providerModel,
            providers,
            providerModels,
          );
          set({
            providers,
            providersStatus: "success",
            providersError: null,
            providerModels,
            providerModel: normalizedProviderModel,
          });
        } catch (err) {
          set({
            providersStatus: "error",
            providersError: (err as Error).message,
          });
        }
      },

      loadAgents: async () => {
        set({ agentsStatus: "loading", agentsError: null });
        try {
          const agentPresets = await RuntimeClient.listAgents();
          const selectableAgentPresets = agentPresets.filter(
            (agent) => agent.selectable !== false,
          );
          set({
            agentPresets,
            agentsStatus: "success",
            agentsError: null,
            agentPreset: selectableAgentPresets.some(
              (agent) => agent.id === get().agentPreset,
            )
              ? get().agentPreset
              : (selectableAgentPresets[0]?.id ?? "leader"),
          });
        } catch (err) {
          set({
            agentsStatus: "error",
            agentsError: (err as Error).message,
          });
        }
      },

      validateProviderCredentials: async (providerName) => {
        if (!providerName) return;
        set((state) => ({
          providerValidationStatus: {
            ...state.providerValidationStatus,
            [providerName]: "loading",
          },
          providerValidationError: {
            ...state.providerValidationError,
            [providerName]: null,
          },
        }));
        try {
          const result =
            await RuntimeClient.validateProviderCredentials(providerName);
          set((state) => ({
            providerValidationResults: {
              ...state.providerValidationResults,
              [providerName]: result,
            },
            providerValidationStatus: {
              ...state.providerValidationStatus,
              [providerName]: result.ok ? "success" : "error",
            },
            providerValidationError: {
              ...state.providerValidationError,
              [providerName]: result.ok ? null : result.message,
            },
          }));
        } catch (err) {
          set((state) => ({
            providerValidationStatus: {
              ...state.providerValidationStatus,
              [providerName]: "error",
            },
            providerValidationError: {
              ...state.providerValidationError,
              [providerName]: (err as Error).message,
            },
          }));
        }
      },

      loadStatus: async () => {
        set({ statusStatus: "loading", statusError: null });
        try {
          const statusSnapshot = await RuntimeClient.getStatus();
          set({
            statusSnapshot,
            statusStatus: "success",
            statusError: null,
            mcpRetryStatus:
              get().mcpRetryStatus === "loading"
                ? "success"
                : get().mcpRetryStatus,
            mcpRetryError: null,
          });
        } catch (err) {
          set({
            statusStatus: "error",
            statusError: (err as Error).message,
            mcpRetryStatus:
              get().mcpRetryStatus === "loading"
                ? "error"
                : get().mcpRetryStatus,
            mcpRetryError:
              get().mcpRetryStatus === "loading"
                ? (err as Error).message
                : get().mcpRetryError,
          });
        }
      },

      retryMcpConnections: async () => {
        set({ mcpRetryStatus: "loading", mcpRetryError: null });
        try {
          const statusSnapshot = await RuntimeClient.retryMcpConnections();
          set({
            statusSnapshot,
            statusStatus: "success",
            statusError: null,
            mcpRetryStatus: "success",
            mcpRetryError: null,
          });
        } catch (err) {
          set({
            mcpRetryStatus: "error",
            mcpRetryError: (err as Error).message,
          });
        }
      },

      loadReview: async () => {
        set({ reviewStatus: "loading", reviewError: null });
        try {
          const reviewSnapshot = await RuntimeClient.getReview();
          const selectedPath = get().reviewSelectedPath;
          const treeFallbackPath = firstTreeFilePath(reviewSnapshot.tree);
          const nextSelectedPath =
            selectedPath &&
            (reviewSnapshot.changed_files.some(
              (item) => item.path === selectedPath,
            ) ||
              treeContainsPath(reviewSnapshot.tree, selectedPath))
              ? selectedPath
              : (reviewSnapshot.changed_files[0]?.path ?? treeFallbackPath);
          set({
            reviewSnapshot,
            reviewStatus: "success",
            reviewError: null,
            reviewSelectedPath: nextSelectedPath,
            reviewDiff: null,
            reviewDiffStatus: nextSelectedPath ? "idle" : "success",
            reviewDiffError: null,
          });
          if (nextSelectedPath) {
            await get().selectReviewPath(nextSelectedPath);
          }
        } catch (err) {
          set({
            reviewStatus: "error",
            reviewError: (err as Error).message,
          });
        }
      },

      selectReviewPath: async (path) => {
        if (!path) {
          set({
            reviewSelectedPath: null,
            reviewDiff: null,
            reviewDiffStatus: "idle",
            reviewDiffError: null,
          });
          return;
        }
        set({
          reviewSelectedPath: path,
          reviewDiffStatus: "loading",
          reviewDiffError: null,
        });
        try {
          const reviewDiff = await RuntimeClient.getReviewDiff(path);
          if (get().reviewSelectedPath !== path) {
            return;
          }
          set({
            reviewDiff,
            reviewDiffStatus: "success",
            reviewDiffError: null,
          });
        } catch (err) {
          if (get().reviewSelectedPath !== path) {
            return;
          }
          set({
            reviewDiff: null,
            reviewDiffStatus: "error",
            reviewDiffError: (err as Error).message,
          });
        }
      },

      setReviewMode: (reviewMode) => set({ reviewMode }),

      setSessionSidebarWidth: (sessionSidebarWidth) =>
        set({ sessionSidebarWidth }),

      loadSessions: async () => {
        set({ sessionsStatus: "loading", sessionsError: null });
        try {
          const sessions = await RuntimeClient.listSessions();
          const { currentSessionId } = get();

          if (
            currentSessionId &&
            !sessions.some((s) => s.session.id === currentSessionId)
          ) {
            set({
              sessions,
              sessionsStatus: "success",
              currentSessionId: null,
              currentSessionState: null,
              currentSessionEvents: [],
              currentSessionOutput: null,
              replayStatus: "idle",
              replayError: null,
            });
          } else {
            set({ sessions, sessionsStatus: "success" });
          }
        } catch (err) {
          set({
            sessionsStatus: "error",
            sessionsError: (err as Error).message,
          });
        }
      },

      selectSession: async (sessionId: string) => {
        if (get().runStatus === "running") {
          return;
        }

        if (!sessionId) {
          set({
            currentSessionId: null,
            currentSessionState: null,
            currentSessionEvents: [],
            currentSessionOutput: null,
            replayStatus: "idle",
            replayError: null,
            runStatus: "idle",
            runError: null,
            approvalStatus: "idle",
            approvalError: null,
            questionStatus: "idle",
            questionError: null,
            sessionDebug: null,
            sessionDebugStatus: "idle",
            sessionDebugError: null,
          });
          await get().loadBackgroundTasks();
          return;
        }

        const requestId = get().replayRequestId + 1;
        set({
          currentSessionId: sessionId,
          currentSessionState: null,
          currentSessionEvents: [],
          currentSessionOutput: null,
          replayStatus: "loading",
          replayError: null,
          replayRequestId: requestId,
          runStatus: "idle",
          runError: null,
          approvalStatus: "idle",
          approvalError: null,
          questionStatus: "idle",
          questionError: null,
          sessionDebug: null,
          sessionDebugStatus: "idle",
          sessionDebugError: null,
        });

        try {
          const replay = await RuntimeClient.getSessionReplay(sessionId);
          if (
            get().replayRequestId !== requestId ||
            get().currentSessionId !== sessionId
          ) {
            return;
          }

          set({
            currentSessionState: replay.session,
            currentSessionEvents: replay.events,
            currentSessionOutput: replay.output,
            replayStatus: "success",
          });
          await get().loadBackgroundTasks();
        } catch {
          if (
            get().replayRequestId !== requestId ||
            get().currentSessionId !== sessionId
          ) {
            return;
          }

          set({
            currentSessionId: null,
            currentSessionState: null,
            currentSessionEvents: [],
            currentSessionOutput: null,
            replayStatus: "idle",
            replayError: null,
          });
        }
      },

      runTask: async (prompt: string, options) => {
        if (get().replayStatus === "loading") {
          return;
        }

        const nextReplayRequestId = get().replayRequestId + 1;
        set({
          runStatus: "running",
          runError: null,
          currentSessionOutput: null,
          approvalStatus: "idle",
          approvalError: null,
          questionStatus: "idle",
          questionError: null,
          sessionDebug: null,
          sessionDebugStatus: "idle",
          sessionDebugError: null,
        });
        const effectiveSessionId =
          options?.sessionId !== undefined
            ? options.sessionId
            : get().currentSessionId;
        set({
          replayStatus: "idle",
          replayError: null,
          replayRequestId: nextReplayRequestId,
        });

        const rawMetadata = options?.metadata ?? {};
        const rawAgentMetadata =
          rawMetadata.agent && typeof rawMetadata.agent === "object"
            ? (rawMetadata.agent as Record<string, unknown>)
            : {};
        const requestedMaxSteps = rawMetadata.max_steps;
        const maxStepsOverride =
          typeof requestedMaxSteps === "number" &&
          Number.isInteger(requestedMaxSteps) &&
          requestedMaxSteps > 0
            ? requestedMaxSteps
            : undefined;
        const forwardMetadata = Object.fromEntries(
          Object.entries(rawMetadata).filter(
            ([key]) =>
              key !== "agent" &&
              key !== "max_steps" &&
              key !== "reasoning_effort",
          ),
        );
        const forwardAgentMetadata = Object.fromEntries(
          Object.entries(rawAgentMetadata),
        );

        const modelMetadata = selectedModelMetadata(
          get().providerModel,
          get().providers,
          get().providerModels,
        );
        const requestedReasoningEffort =
          typeof rawMetadata.reasoning_effort === "string" &&
          rawMetadata.reasoning_effort.trim()
            ? rawMetadata.reasoning_effort.trim()
            : get().reasoningEffort.trim() ||
              modelMetadata?.default_reasoning_effort ||
              "";
        const metadata = {
          ...forwardMetadata,
          ...(maxStepsOverride !== undefined
            ? { max_steps: maxStepsOverride }
            : {}),
          ...(modelMetadata?.supports_reasoning_effort === true &&
          requestedReasoningEffort
            ? { reasoning_effort: requestedReasoningEffort }
            : {}),
          agent: {
            preset: get().agentPreset,
            model: normalizeProviderModelReference(
              get().providerModel,
              get().providers,
              get().providerModels,
            ),
            ...forwardAgentMetadata,
            execution_engine: "provider",
          },
        };

        try {
          const stream = RuntimeClient.runStream({
            prompt,
            session_id: effectiveSessionId,
            metadata: metadata,
          });

          for await (const chunk of stream) {
            set((state) => {
              const newEvents = chunk.event
                ? [...state.currentSessionEvents, chunk.event]
                : state.currentSessionEvents;
              return {
                currentSessionState: chunk.session,
                currentSessionEvents: newEvents,
                currentSessionId: chunk.session.session
                  ? chunk.session.session.id
                  : state.currentSessionId,
                currentSessionOutput:
                  chunk.output !== null
                    ? chunk.output
                    : state.currentSessionOutput,
              };
            });
          }

          set({ runStatus: "success" });
          const currentSessionId = get().currentSessionId;
          await Promise.all([
            get().loadSessions(),
            get().loadStatus(),
            get().loadReview(),
            get().loadNotifications(),
            get().loadBackgroundTasks(),
            currentSessionId
              ? get().loadSessionDebug(currentSessionId)
              : Promise.resolve(),
          ]);
        } catch (err) {
          set({ runStatus: "error", runError: (err as Error).message });
        }
      },

      resolveApproval: async (decision) => {
        const {
          currentSessionId,
          currentSessionEvents,
          currentSessionState,
          currentSessionOutput,
          replayStatus,
          replayError,
          runStatus,
          approvalStatus,
          loadSessions,
        } = get();

        if (
          !currentSessionId ||
          replayStatus === "loading" ||
          runStatus === "running" ||
          approvalStatus === "submitting"
        ) {
          return;
        }

        const requestId = getPendingApprovalRequestId(currentSessionEvents);
        if (!requestId) {
          set({
            approvalStatus: "error",
            approvalError: "No pending approval request found.",
          });
          return;
        }

        let shouldPollReplay = true;
        const preOptimisticState = {
          currentSessionEvents,
          currentSessionState,
          currentSessionOutput,
          replayStatus,
          replayError,
          runStatus,
        };
        const locallyResolvedEvents = appendLocalApprovalResolution(
          currentSessionEvents,
          currentSessionId,
          requestId,
          decision,
        );
        const localResolutionEvent =
          locallyResolvedEvents[locallyResolvedEvents.length - 1];
        set((state) => ({
          currentSessionEvents: locallyResolvedEvents,
          currentSessionState: state.currentSessionState
            ? {
                ...state.currentSessionState,
                status: decision === "allow" ? "running" : "failed",
              }
            : state.currentSessionState,
          runStatus: decision === "allow" ? "running" : "idle",
          runError: null,
          approvalStatus: "submitting",
          approvalError: null,
          replayStatus: "success",
          replayError: null,
        }));

        const pollReplayWhileResolving = async () => {
          while (shouldPollReplay) {
            await delay(APPROVAL_REPLAY_POLL_DELAY_MS);
            if (
              !shouldPollReplay ||
              get().currentSessionId !== currentSessionId
            ) {
              return;
            }

            try {
              const replay =
                await RuntimeClient.getSessionReplay(currentSessionId);
              if (
                get().currentSessionId !== currentSessionId ||
                replayStillShowsSamePendingApproval(replay.events, requestId)
              ) {
                continue;
              }
              set({
                currentSessionId: replay.session.session.id,
                currentSessionState: replay.session,
                currentSessionEvents: replay.events,
                currentSessionOutput: replay.output,
                replayStatus: "success",
                replayError: null,
                runStatus: runStatusForReplay(replay.session),
              });
            } catch {
              // Best-effort refresh while the approval POST is still running.
            }
          }
        };
        void pollReplayWhileResolving();

        try {
          const response = await RuntimeClient.resolveApproval(
            currentSessionId,
            requestId,
            decision,
          );
          shouldPollReplay = false;
          set({
            currentSessionId: response.session.session.id,
            currentSessionState: response.session,
            currentSessionEvents: response.events,
            currentSessionOutput: response.output,
            replayStatus: "success",
            replayError: null,
            runStatus: runStatusForReplay(response.session),
            runError: null,
            approvalStatus: "success",
            approvalError: null,
          });
          await Promise.all([
            loadSessions(),
            get().loadStatus(),
            get().loadReview(),
          ]);
          set({ approvalStatus: "idle" });
        } catch (err) {
          shouldPollReplay = false;
          set({
            approvalStatus: "error",
            approvalError: (err as Error).message,
            runStatus: "idle",
          });
          // Reload session from backend so the UI can pick up any
          // re-emitted approval state and the composer is usable again.
          try {
            const replay =
              await RuntimeClient.getSessionReplay(currentSessionId);
            if (get().currentSessionId === currentSessionId) {
              set({
                currentSessionState: replay.session,
                currentSessionEvents: replay.events,
                currentSessionOutput: replay.output,
                replayStatus: "success",
                replayError: null,
                runStatus: runStatusForReplay(replay.session),
              });
            }
          } catch {
            if (
              get().currentSessionId === currentSessionId &&
              localResolutionEvent &&
              get().currentSessionEvents.includes(localResolutionEvent)
            ) {
              set({
                currentSessionState: preOptimisticState.currentSessionState,
                currentSessionEvents: preOptimisticState.currentSessionEvents,
                currentSessionOutput: preOptimisticState.currentSessionOutput,
                replayStatus: preOptimisticState.replayStatus,
                replayError: preOptimisticState.replayError,
                runStatus: preOptimisticState.runStatus,
              });
            }
          }
          await loadSessions();
        }
      },

      answerQuestion: async (answers) => {
        const {
          currentSessionId,
          currentSessionEvents,
          replayStatus,
          runStatus,
          questionStatus,
        } = get();

        if (
          !currentSessionId ||
          replayStatus === "loading" ||
          runStatus === "running" ||
          questionStatus === "submitting"
        ) {
          return;
        }

        const requestId = getPendingQuestionRequestId(currentSessionEvents);
        if (!requestId) {
          set({
            questionStatus: "error",
            questionError: "No pending question request found.",
          });
          return;
        }

        set({ questionStatus: "submitting", questionError: null });

        try {
          const response = await RuntimeClient.answerQuestion(
            currentSessionId,
            requestId,
            answers,
          );
          set({
            currentSessionId: response.session.session.id,
            currentSessionState: response.session,
            currentSessionEvents: response.events,
            currentSessionOutput: response.output,
            replayStatus: "success",
            replayError: null,
            runStatus: "idle",
            runError: null,
            questionStatus: "success",
            questionError: null,
          });
          await Promise.all([
            get().loadSessions(),
            get().loadStatus(),
            get().loadReview(),
            get().loadNotifications(),
            get().loadBackgroundTasks(),
            get().loadSessionDebug(response.session.session.id),
          ]);
          set({ questionStatus: "idle" });
        } catch (err) {
          set({
            questionStatus: "error",
            questionError: (err as Error).message,
          });
        }
      },

      loadNotifications: async () => {
        set({ notificationsStatus: "loading", notificationsError: null });
        try {
          const notifications = await RuntimeClient.listNotifications();
          set({
            notifications,
            notificationsStatus: "success",
            notificationsError: null,
          });
        } catch (err) {
          set({
            notificationsStatus: "error",
            notificationsError: (err as Error).message,
          });
        }
      },

      acknowledgeNotification: async (notificationId) => {
        try {
          const notification =
            await RuntimeClient.acknowledgeNotification(notificationId);
          set((state) => ({
            notifications: state.notifications.map((item) =>
              item.id === notification.id ? notification : item,
            ),
            notificationsError: null,
          }));
        } catch (err) {
          set({ notificationsError: (err as Error).message });
        }
      },

      loadBackgroundTasks: async () => {
        const scopedSessionId = get().currentSessionId;
        set({ backgroundTasksStatus: "loading", backgroundTasksError: null });
        try {
          const backgroundTasks = scopedSessionId
            ? await RuntimeClient.listSessionBackgroundTasks(scopedSessionId)
            : await RuntimeClient.listBackgroundTasks();
          if (get().currentSessionId !== scopedSessionId) {
            return;
          }
          set({
            backgroundTasks,
            backgroundTasksStatus: "success",
            backgroundTasksError: null,
          });
        } catch (err) {
          set({
            backgroundTasksStatus: "error",
            backgroundTasksError: (err as Error).message,
          });
        }
      },

      cancelBackgroundTask: async (taskId) => {
        try {
          await RuntimeClient.cancelBackgroundTask(taskId);
          await get().loadBackgroundTasks();
        } catch (err) {
          set({ backgroundTasksError: (err as Error).message });
        }
      },

      loadSessionDebug: async (sessionId) => {
        const targetSessionId = sessionId ?? get().currentSessionId;
        if (!targetSessionId) {
          set({
            sessionDebug: null,
            sessionDebugStatus: "idle",
            sessionDebugError: null,
          });
          return;
        }
        set({ sessionDebugStatus: "loading", sessionDebugError: null });
        try {
          const sessionDebug =
            await RuntimeClient.getSessionDebug(targetSessionId);
          set({
            sessionDebug,
            sessionDebugStatus: "success",
            sessionDebugError: null,
          });
        } catch (err) {
          set({
            sessionDebug: null,
            sessionDebugStatus: "error",
            sessionDebugError: (err as Error).message,
          });
        }
      },

      loadSettings: async () => {
        set({ settingsStatus: "loading", settingsError: null });
        try {
          const settings = await RuntimeClient.getSettings();
          set({ settings, settingsStatus: "success" });
          if (settings.model) {
            set({ providerModel: settings.model });
          }
        } catch (err) {
          set({
            settingsStatus: "error",
            settingsError: (err as Error).message,
          });
        }
      },

      updateSettings: async (settings) => {
        set({ settingsStatus: "loading", settingsError: null });
        try {
          const updated = await RuntimeClient.updateSettings(settings);
          set({
            settings: updated,
            settingsStatus: "success",
            providerValidationResults: {},
            providerValidationStatus: {},
            providerValidationError: {},
          });
          if (updated.model) {
            set({ providerModel: updated.model });
          }
          await get().loadProviders();
          await get().loadStatus();
        } catch (err) {
          set({
            settingsStatus: "error",
            settingsError: (err as Error).message,
          });
        }
      },
    }),
    {
      name: "app-storage",
      partialize: (state) => ({
        language: state.language,
        agentPreset: state.agentPreset,
        providerModel: state.providerModel,
        reasoningEffort: state.reasoningEffort,
        currentSessionId: state.currentSessionId,
        sessionSidebarWidth: state.sessionSidebarWidth,
        reviewMode: state.reviewMode,
      }),
    },
  ),
);
