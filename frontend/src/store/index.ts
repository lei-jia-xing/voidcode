import { create } from "zustand";
import { persist } from "zustand/middleware";
import { RuntimeClient } from "../lib/runtime/client";
import i18n from "../i18n";
import { errorMessage } from "../lib/errorMessage";
import {
  deeplyEqual,
  failureMessageFromEvent,
  isRuntimeCancellationEvent,
  preferFailureMessage,
} from "../lib/runtime/event-parser";
import {
  currentWorkspaceScope,
  queryClient,
  queryKeys,
  readProviderCatalog,
  refreshAfterMutation,
  resolveProviderModelReference,
  type WorkspaceScope,
} from "../lib/queries";
import {
  AgentSummary,
  ApprovalDecision,
  AsyncStatus,
  EventEnvelope,
  ProviderModelsResult,
  ProviderSummary,
  QuestionAnswer,
  RuntimeRequest,
  RuntimeSessionDebugSnapshot,
  SessionState,
  StoredSessionSummary,
} from "../lib/runtime/types";

/**
 * Client state and the runtime's execution lifecycle.
 *
 * What lives here: the persisted user preferences (language, agent preset,
 * model, reasoning effort, sidebar width, review mode), the panel/selection
 * state (the selected session, the selected review path, the selected background
 * task, the child-session parent, the workspace selection reset), and the
 * *streamed-run projection* — the session row, event list and output the run and
 * follow streams write as they arrive, together with the run/cancel, approval,
 * question, replay and resume state machines that own them.
 *
 * What does not live here: every plain-HTTP payload. Providers, agents, skills,
 * commands, sessions, status, review, workspaces, tasks, notifications, settings
 * and the per-session debug snapshot are read from the TanStack Query cache (see
 * `lib/queries`), so a payload has exactly one home. This module reads those
 * entries where its own logic needs them (the session list for the
 * delegated-context routing decision, the provider catalog to build a run
 * request) and writes them where its lifecycle produces one (the task-output
 * entry a delegated-context probe selects) — it never mirrors them into React
 * state.
 */
const DEFAULT_SESSION_SIDEBAR_WIDTH = 344;

// The active run's AbortController. runTask creates one per run and passes its
// signal to RuntimeClient.runStream; cancelCurrentRun aborts it to
// deterministically tear down the stream. Module scope (not store state) keeps
// the controller out of the persisted store.
let activeRunAbortController: AbortController | null = null;
let activeRunIdentity: { sessionId: string; runId: string } | null = null;
// The transcript read of the current selection (child-context probe + replay).
// A newer selection aborts it: the superseded request is really cancelled
// instead of being decoded and discarded, and a workspace switch aborts it too.
let activeSelectionAbortController: AbortController | null = null;

type PersistedAppState = Pick<
  AppState,
  | "language"
  | "agentPreset"
  | "providerModel"
  | "reasoningEffort"
  | "currentSessionId"
  | "sessionSidebarWidth"
  | "reviewMode"
>;
interface AppState {
  language: "en" | "zh-CN";

  agentPreset: string;
  providerModel: string;
  reasoningEffort: string;

  reviewMode: "changes" | "files";
  reviewSelectedPath: string | null;
  selectedBackgroundTaskOutputId: string | null;

  currentSessionId: string | null;
  sessionSidebarWidth: number;
  currentSessionState: SessionState | null;
  currentSessionEvents: EventEnvelope[];
  currentSessionOutput: string | null;
  childSessionParentId: string | null;

  replayStatus: AsyncStatus;
  replayError: string | null;
  replayRequestId: number;
  replayTargetSessionId: string | null;
  resumeStatus: AsyncStatus;
  resumeError: string | null;

  runStatus: "idle" | "running" | "cancelling" | "success" | "error";
  runOrigin: "local" | "external" | null;
  runError: string | null;
  cancelRequested: boolean;
  approvalStatus: "idle" | "submitting" | "success" | "error";
  approvalError: string | null;
  questionStatus: "idle" | "submitting" | "success" | "error";
  questionError: string | null;

  setLanguage: (lang: "en" | "zh-CN") => void;
  setAgentPreset: (preset: string) => void;
  setProviderModel: (model: string) => void;
  setReasoningEffort: (effort: string) => void;
  setSessionSidebarWidth: (width: number) => void;
  setReviewMode: (mode: "changes" | "files") => void;
  setReviewSelectedPath: (path: string | null) => void;
  /** Select a background task's output view; `null` returns to the plain session. */
  selectBackgroundTaskOutput: (taskId: string | null) => void;
  /** Adopt a loaded agent catalog over the stored preset preference. */
  reconcileAgentPreset: (agents: AgentSummary[]) => void;
  /** Adopt the runtime settings' model while the user has not chosen one. */
  hydrateModelFromSettings: (model: string | null | undefined) => void;
  /** Drop everything the previous workspace owned, ahead of a switch. */
  prepareWorkspaceSwitch: () => void;
  /** Apply the freshly loaded session list to the current selection. */
  reconcileSessionList: (sessions: StoredSessionSummary[]) => void;
  /** Merge one pushed session-event frame; false when it was already stored. */
  mergeSessionEvent: (event: EventEnvelope) => boolean;
  /** Adopt the session row pushed by a stream; false when nothing moved. */
  mergeSessionState: (session: SessionState) => boolean;
  /**
   * Select a session and read its transcript.
   *
   * `workspaceScope` is the workspace the caller is acting in. It is an explicit
   * argument because this action reads and writes the query cache: the shell
   * always knows which workspace it is showing, and an action must not resolve
   * that from a hidden global read.
   */
  selectSession: (
    sessionId: string,
    workspaceScope: WorkspaceScope,
  ) => Promise<void>;
  /**
   * Start a run in `workspaceScope`, whose provider catalog supplies the model
   * metadata the request is built from.
   */
  runTask: (
    prompt: string,
    workspaceScope: WorkspaceScope,
    options?: {
      sessionId?: string | null;
      /**
       * The metadata the request is built with. The two named keys are bound to
       * the generated request type (there is one definition of the metadata
       * shape); the index signature keeps the pass-through open, because the
       * runtime owns the rest of the blob.
       */
      metadata?: Pick<
        NonNullable<RuntimeRequest["metadata"]>,
        "skills" | "provider_stream"
      > & { [key: string]: unknown };
    },
  ) => Promise<void>;
  cancelCurrentRun: () => Promise<void>;
  resolveApproval: (decision: ApprovalDecision) => Promise<void>;
  answerQuestion: (answers: QuestionAnswer[]) => Promise<void>;
  resumeSession: (sessionId?: string | null) => Promise<void>;
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

function getPendingQuestionRequestId(events: EventEnvelope[]): string | null {
  const answeredRequestIds = new Set<string>();

  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    const requestId = event.payload.request_id;

    if (event.event_type === "runtime.question_answered") {
      if (typeof requestId === "string" && requestId.length > 0) {
        answeredRequestIds.add(requestId);
      }
      continue;
    }

    if (event.event_type !== "runtime.question_requested") {
      continue;
    }

    if (
      typeof requestId === "string" &&
      requestId.length > 0 &&
      !answeredRequestIds.has(requestId)
    ) {
      return requestId;
    }
  }

  return null;
}

function runStatusForReplay(session: SessionState): AppState["runStatus"] {
  return session.status === "running" ? "running" : "idle";
}

function isRunLocked(runStatus: AppState["runStatus"]): boolean {
  return runStatus === "running" || runStatus === "cancelling";
}

// Live-only provider output: the runtime never persists these events, so they are
// the client's in-flight projection of the current attempt. A transient retry or
// provider fallback that restarts the attempt must drop them; the persisted
// transcript (and its single graph.response_ready) is untouched.
const LIVE_STREAM_EVENT_TYPES: Record<string, true> = {
  "graph.provider_stream": true,
  "graph.tool_call_start": true,
  "graph.tool_call_delta": true,
  "graph.tool_call_end": true,
};

function applyLiveStreamEvent(
  events: EventEnvelope[],
  event: EventEnvelope,
): EventEnvelope[] {
  const restarted =
    (event.event_type === "runtime.provider_transient_retry" ||
      event.event_type === "runtime.provider_fallback") &&
    event.payload.discarded_streamed_output === true;
  if (!restarted) {
    return [...events, event];
  }
  let end = events.length;
  while (end > 0 && LIVE_STREAM_EVENT_TYPES[events[end - 1].event_type]) {
    end -= 1;
  }
  return [...events.slice(0, end), event];
}

/**
 * Whether the runtime's own session list says this id is a main session.
 *
 * The flat session list is the main-session surface: it filters every delegated
 * child out (see the runtime's transport). A session found in it therefore has
 * no parent, so the delegated-context lookup can only answer 404 — this reads the
 * cached list to skip the guaranteed miss. Unloaded list (no workspace yet) means
 * "unknown", which keeps the lookup-then-fallback path.
 */
function knownMainSessionInCache(
  sessionId: string,
  workspaceScope: WorkspaceScope,
): boolean {
  if (workspaceScope === null) return false;
  const sessions = queryClient.getQueryData<StoredSessionSummary[]>(
    queryKeys.sessions(workspaceScope),
  );
  const summary = sessions?.find((item) => item.session.id === sessionId);
  return summary !== undefined && (summary.session.parent_id ?? null) === null;
}

function selectedModelMetadata(
  model: string,
  providers: ProviderSummary[],
  providerModels: Record<string, ProviderModelsResult>,
) {
  const normalized = resolveProviderModelReference(
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

/**
 * Whether the loaded debug snapshot says a failed session can be resumed.
 *
 * The debug snapshot is a query payload, so the store reads it out of the cache
 * where it is written, instead of keeping a second copy next to it.
 */
function isResumableDebugSnapshot(
  sessionId: string | null,
  scope: string | null,
): boolean {
  if (scope === null || sessionId === null) return false;
  const snapshot = queryClient.getQueryData<RuntimeSessionDebugSnapshot>(
    queryKeys.sessionDebug(scope, sessionId),
  );
  return snapshot?.resumable === true;
}

export const useAppStore = create<AppState>()(
  persist(
    (set, get) => ({
      language: "en",
      agentPreset: "leader",
      providerModel: "deepseek/deepseek-v4-pro",
      reasoningEffort: "",

      reviewMode: "changes",
      reviewSelectedPath: null,
      selectedBackgroundTaskOutputId: null,

      currentSessionId: null,
      sessionSidebarWidth: DEFAULT_SESSION_SIDEBAR_WIDTH,
      currentSessionState: null,
      currentSessionEvents: [],
      currentSessionOutput: null,
      childSessionParentId: null,

      replayStatus: "idle",
      replayError: null,
      replayRequestId: 0,
      replayTargetSessionId: null,
      resumeStatus: "idle",
      resumeError: null,

      runStatus: "idle",
      runOrigin: null,
      runError: null,
      cancelRequested: false,
      approvalStatus: "idle",
      approvalError: null,
      questionStatus: "idle",
      questionError: null,

      setLanguage: (language) => set({ language }),
      setAgentPreset: (agentPreset) => set({ agentPreset }),
      setProviderModel: (providerModel) => set({ providerModel }),
      setReasoningEffort: (reasoningEffort) => set({ reasoningEffort }),
      setSessionSidebarWidth: (sessionSidebarWidth) =>
        set({ sessionSidebarWidth }),
      setReviewMode: (reviewMode) => set({ reviewMode }),
      setReviewSelectedPath: (reviewSelectedPath) =>
        set({ reviewSelectedPath }),

      selectBackgroundTaskOutput: (taskId) => {
        if (taskId === null) {
          // Returning from a delegated child means returning from the task view
          // that stood in for it, so the parent link is cleared with the output.
          set({
            selectedBackgroundTaskOutputId: null,
            childSessionParentId: null,
          });
          return;
        }
        set({ selectedBackgroundTaskOutputId: taskId });
      },

      reconcileAgentPreset: (agents) => {
        const selectable = agents.filter((agent) => agent.selectable !== false);
        const currentPreset = get().agentPreset;
        if (selectable.some((agent) => agent.id === currentPreset)) return;
        set({ agentPreset: selectable[0]?.id ?? "leader" });
      },

      hydrateModelFromSettings: (model) => {
        if (!model || get().providerModel.trim()) return;
        set({ providerModel: model });
      },

      prepareWorkspaceSwitch: () => {
        // A workspace switch invalidates every in-flight local stream. Abort it
        // before replacing state, and bump the replay token so late chunks and
        // late replay responses cannot leak into the new workspace. The
        // workspace-scoped queries of the old scope are cancelled by the switch
        // mutation itself (they are keyed by that scope).
        activeRunAbortController?.abort();
        activeRunAbortController = null;
        activeSelectionAbortController?.abort();
        activeSelectionAbortController = null;
        set((state) => ({
          replayRequestId: state.replayRequestId + 1,
          currentSessionId: null,
          currentSessionState: null,
          currentSessionEvents: [],
          currentSessionOutput: null,
          childSessionParentId: null,
          replayStatus: "idle",
          replayError: null,
          replayTargetSessionId: null,
          resumeStatus: "idle",
          resumeError: null,
          runStatus: "idle",
          runOrigin: null,
          runError: null,
          cancelRequested: false,
          approvalStatus: "idle",
          approvalError: null,
          questionStatus: "idle",
          questionError: null,
          selectedBackgroundTaskOutputId: null,
        }));
      },

      reconcileSessionList: (sessions) => {
        const { currentSessionId, childSessionParentId, replayStatus } = get();
        const selectionHasTranscript =
          get().currentSessionState !== null ||
          get().currentSessionEvents.length > 0 ||
          get().currentSessionOutput !== null;
        const selectionIsLive =
          get().currentSessionState?.status === "running" ||
          get().currentSessionState?.status === "waiting";

        if (
          currentSessionId &&
          !selectionIsLive &&
          // CONTRACT: the runtime's flat session list is authoritative about
          // *finished* sessions only. A selection whose projection says the
          // session is live (running/waiting) is never judged against a list
          // payload — a payload read before the run started would drop the run
          // off the screen. It is judged on the post-run refresh instead, which
          // re-reads the list once the run has settled.
          // Delegated child sessions are not part of the flat session list;
          // while one is being browsed its absence must not reset the app.
          childSessionParentId === null &&
          // Only a selection with a resolved transcript behind it can be
          // judged against this list. Before the replay lands (and while it is
          // in flight, when `childSessionParentId` has already been cleared for
          // the new selection) nothing here knows whether the selected session
          // is a delegated child, and the list can never contain one. A session
          // that is really gone is reported by the replay itself.
          selectionHasTranscript &&
          replayStatus !== "loading" &&
          !sessions.some((s) => s.session.id === currentSessionId)
        ) {
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

      mergeSessionEvent: (event) => {
        const state = get();
        // Accept a pushed frame when it belongs to the session the client is
        // showing, and drop every other session's. The shell always selects the
        // session it displays (a delegated child is selected by its own id), so
        // this is also what keeps a reloaded child receiving live updates; the
        // parent's frames are simply not its frames.
        if (state.currentSessionId !== event.session_id) {
          return false;
        }
        // Value-based dedupe. The follow stream resumes from the cursor the
        // client already replayed, so a pushed frame can legitimately repeat an
        // event `selectSession` delivered; (session_id, sequence) is the
        // runtime's per-session identity for a persisted event. A live-only
        // frame has no identity of its own — every delta of one attempt carries
        // the same persisted cursor — so it is judged on its whole payload
        // instead: that still catches a re-pushed duplicate without dropping the
        // rest of the burst.
        const alreadyDelivered = state.currentSessionEvents.some(
          (existing) =>
            existing.session_id === event.session_id &&
            existing.sequence === event.sequence &&
            (LIVE_STREAM_EVENT_TYPES[event.event_type] !== true ||
              deeplyEqual(existing.payload, event.payload)),
        );
        if (alreadyDelivered) return false;
        set({
          currentSessionEvents: applyLiveStreamEvent(
            state.currentSessionEvents,
            event,
          ),
        });
        return true;
      },

      mergeSessionState: (session) => {
        const state = get();
        // Same rule as `mergeSessionEvent`: the pushed row is only this client's
        // when it is the row of the session on screen.
        if (state.currentSessionId !== session.session.id) {
          return false;
        }
        const previous = state.currentSessionState;
        // The row is always adopted — it is the runtime's own state — while the
        // return value reports whether the view moved (the stream only pushes a
        // row on open and on real change).
        const moved =
          previous === null ||
          previous.status !== session.status ||
          previous.turn !== session.turn ||
          previous.session.parent_id !== session.session.parent_id;
        set({ currentSessionState: session });
        return moved;
      },

      selectSession: async (sessionId: string, workspaceScope) => {
        const childParentSessionId = get().childSessionParentId;
        const allowChildParentReturn =
          Boolean(sessionId) &&
          childParentSessionId !== null &&
          sessionId === childParentSessionId;
        if (
          isRunLocked(get().runStatus) &&
          get().runOrigin !== "external" &&
          !allowChildParentReturn
        ) {
          return;
        }

        if (
          sessionId === get().currentSessionId &&
          get().selectedBackgroundTaskOutputId &&
          childParentSessionId !== null
        ) {
          // Already browsing this delegated child session: refresh its output
          // in place instead of clearing the child view and re-fetching the
          // context, which would make the transcript flash between views.
          await queryClient.invalidateQueries({
            queryKey: queryKeys.taskOutput(
              workspaceScope,
              get().selectedBackgroundTaskOutputId as string,
            ),
          });
          return;
        }

        if (!sessionId) {
          activeSelectionAbortController?.abort();
          activeSelectionAbortController = null;
          set({
            currentSessionId: null,
            currentSessionState: null,
            currentSessionEvents: [],
            currentSessionOutput: null,
            childSessionParentId: null,
            replayStatus: "idle",
            replayError: null,
            runStatus: "idle",
            runError: null,
            approvalStatus: "idle",
            approvalError: null,
            questionStatus: "idle",
            questionError: null,
            selectedBackgroundTaskOutputId: null,
          });
          return;
        }

        const requestId = get().replayRequestId + 1;
        const previousSessionId = get().currentSessionId;
        const previousSessionState = get().currentSessionState;
        const previousSessionEvents = get().currentSessionEvents;
        const previousSessionOutput = get().currentSessionOutput;
        const previousChildParentId = get().childSessionParentId;
        // A newer selection supersedes the transcript read it interrupts: the
        // request itself is aborted, and the replay token below keeps its late
        // frames out of the newer selection's state.
        activeSelectionAbortController?.abort();
        const selectionAbortController = new AbortController();
        activeSelectionAbortController = selectionAbortController;
        set({
          currentSessionId: sessionId,
          currentSessionState: null,
          currentSessionEvents: [],
          currentSessionOutput: null,
          replayStatus: "loading",
          replayError: null,
          replayRequestId: requestId,
          replayTargetSessionId: sessionId,
          runStatus: "idle",
          runError: null,
          approvalStatus: "idle",
          approvalError: null,
          questionStatus: "idle",
          questionError: null,
          selectedBackgroundTaskOutputId: null,
          childSessionParentId: null,
        });

        const knownMainSession = knownMainSessionInCache(
          sessionId,
          workspaceScope,
        );

        try {
          if (!knownMainSession) {
            try {
              const childContext = await RuntimeClient.getChildSessionContext(
                sessionId,
                selectionAbortController.signal,
              );
              if (
                get().replayRequestId !== requestId ||
                get().currentSessionId !== sessionId
              ) {
                return;
              }
              const parentSessionId =
                childContext.task.parent_session_id ??
                childContext.session_result?.session.session.parent_id ??
                previousSessionId;
              // The probe answered with the same payload the task-output read
              // returns, so it seeds the entry the task view is keyed by: one
              // payload, one key, whichever surface asked for it.
              queryClient.setQueryData(
                queryKeys.taskOutput(workspaceScope, childContext.task.task_id),
                childContext,
              );
              set({
                selectedBackgroundTaskOutputId: childContext.task.task_id,
                childSessionParentId: parentSessionId,
                currentSessionState:
                  childContext.session_result?.session ??
                  get().currentSessionState,
                currentSessionEvents:
                  childContext.session_result?.transcript ??
                  get().currentSessionEvents,
                currentSessionOutput:
                  childContext.session_result?.output ?? childContext.output,
                runStatus: childContext.session_result?.session
                  ? runStatusForReplay(childContext.session_result.session)
                  : "idle",
                runOrigin:
                  childContext.session_result?.session?.status === "running"
                    ? "external"
                    : null,
                replayStatus: "success",
                replayError: null,
              });
              await refreshAfterMutation(
                { backgroundTasks: true, notifications: true },
                workspaceScope,
              );
              return;
            } catch (error) {
              const status =
                typeof error === "object" && error !== null && "status" in error
                  ? error.status
                  : undefined;
              const code =
                typeof error === "object" && error !== null && "code" in error
                  ? error.code
                  : undefined;
              if (status !== 404 || code !== "delegated_context_missing") {
                throw error;
              }
              // A recognised missing delegated context means ordinary session replay.
            }
          }

          const replay = await RuntimeClient.getSessionReplay(
            sessionId,
            selectionAbortController.signal,
          );
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
            // A delegated child that has no background-task row (synchronous
            // delegation) is a plain session whose parent only the session row
            // names, so keep that relationship: the child-session panel then
            // offers the real parent instead of the child's own id.
            childSessionParentId: replay.session.session.parent_id ?? null,
            runStatus: runStatusForReplay(replay.session),
            runOrigin: replay.session.status === "running" ? "external" : null,
            replayStatus: "success",
            replayError: null,
            replayTargetSessionId: null,
          });
          await refreshAfterMutation(
            { backgroundTasks: true, notifications: true },
            workspaceScope,
          );
        } catch (err) {
          if (
            get().replayRequestId !== requestId ||
            get().currentSessionId !== sessionId
          ) {
            return;
          }
          // Only a selection that actually has a transcript on screen is worth
          // falling back to. A persisted id with nothing replayed behind it (the
          // boot after the runtime lost that session) must reach the usable
          // empty state instead of restoring a selection whose replay just
          // failed — which would leave the app pinned to a session that can no
          // longer be opened.
          const hasPreviousTranscript =
            previousSessionState !== null ||
            previousSessionEvents.length > 0 ||
            previousSessionOutput !== null;
          set({
            currentSessionId: hasPreviousTranscript ? previousSessionId : null,
            currentSessionState: hasPreviousTranscript
              ? previousSessionState
              : null,
            currentSessionEvents: hasPreviousTranscript
              ? previousSessionEvents
              : [],
            currentSessionOutput: hasPreviousTranscript
              ? previousSessionOutput
              : null,
            childSessionParentId: hasPreviousTranscript
              ? previousChildParentId
              : null,
            replayStatus: hasPreviousTranscript ? "error" : "idle",
            replayError: hasPreviousTranscript ? errorMessage(err) : null,
            replayTargetSessionId: hasPreviousTranscript ? sessionId : null,
          });
        }
      },

      resumeSession: async (sessionId) => {
        const targetSessionId = sessionId ?? get().currentSessionId;
        const currentSessionState = get().currentSessionState;
        const isResumable =
          currentSessionState?.status === "interrupted" ||
          (currentSessionState?.status === "failed" &&
            isResumableDebugSnapshot(targetSessionId, currentWorkspaceScope()));
        if (
          !targetSessionId ||
          !isResumable ||
          get().resumeStatus === "loading" ||
          isRunLocked(get().runStatus) ||
          get().replayStatus === "loading" ||
          get().currentSessionId !== targetSessionId
        ) {
          return;
        }
        const requestId = get().replayRequestId + 1;
        set({
          resumeStatus: "loading",
          resumeError: null,
          replayRequestId: requestId,
          replayStatus: "loading",
          replayError: null,
          replayTargetSessionId: targetSessionId,
          runError: null,
        });
        try {
          const response = await RuntimeClient.resumeSession(targetSessionId);
          if (
            get().replayRequestId !== requestId ||
            get().currentSessionId !== targetSessionId
          ) {
            return;
          }
          set({
            currentSessionState: response.session,
            currentSessionEvents: response.events,
            currentSessionOutput: response.output,
            replayStatus: "success",
            replayError: null,
            replayTargetSessionId: null,
            runStatus: runStatusForReplay(response.session),
            runOrigin:
              response.session.status === "running" ? "external" : null,
            resumeStatus: "success",
            resumeError: null,
          });
          await refreshAfterMutation({
            sessions: true,
            notifications: true,
            backgroundTasks: true,
          });
        } catch (err) {
          if (
            get().replayRequestId !== requestId ||
            get().currentSessionId !== targetSessionId
          ) {
            return;
          }
          set({
            resumeStatus: "error",
            resumeError: errorMessage(err),
            replayStatus: "success",
            replayError: null,
            replayTargetSessionId: null,
          });
        }
      },

      runTask: async (prompt: string, workspaceScope, options) => {
        if (get().replayStatus === "loading" || isRunLocked(get().runStatus)) {
          return;
        }

        const nextReplayRequestId = get().replayRequestId + 1;
        const abortController = new AbortController();
        activeRunAbortController = abortController;
        activeRunIdentity = null;
        set({
          runStatus: "running",
          runOrigin: "local",
          runError: null,
          cancelRequested: false,
          currentSessionOutput: null,
          approvalStatus: "idle",
          approvalError: null,
          questionStatus: "idle",
          questionError: null,
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
        const forwardMetadata = Object.fromEntries(
          Object.entries(rawMetadata).filter(
            ([key]) => key !== "agent" && key !== "reasoning_effort",
          ),
        );
        const forwardAgentMetadata = Object.fromEntries(
          Object.entries(rawAgentMetadata).filter(
            ([key]) => key !== "execution_engine",
          ),
        );

        const catalog = readProviderCatalog(workspaceScope);
        const modelMetadata = selectedModelMetadata(
          get().providerModel,
          catalog.providers,
          catalog.models,
        );
        // The runtime owns the capability decision and clamps to the model's own
        // levels, so the client only vetoes when the model's catalog metadata says
        // it cannot take an effort hint at all (`false`); an unknown capability
        // (`undefined`/`null`) still forwards the user's choice.
        const reasoningEffortAllowed =
          modelMetadata?.supports_reasoning_effort !== false;
        const requestedReasoningEffort =
          typeof rawMetadata.reasoning_effort === "string" &&
          rawMetadata.reasoning_effort.trim()
            ? rawMetadata.reasoning_effort.trim()
            : get().reasoningEffort.trim() ||
              modelMetadata?.default_reasoning_effort ||
              "";
        const metadata = {
          ...forwardMetadata,
          ...(reasoningEffortAllowed && requestedReasoningEffort
            ? { reasoning_effort: requestedReasoningEffort }
            : {}),
          agent: {
            preset: get().agentPreset,
            model: resolveProviderModelReference(
              get().providerModel,
              catalog.providers,
              catalog.models,
            ),
            ...forwardAgentMetadata,
          },
        };

        try {
          const stream = RuntimeClient.runStream(
            {
              prompt,
              session_id: effectiveSessionId,
              metadata: metadata,
            },
            abortController.signal,
          );

          let streamFailureMessage: string | null = null;
          let streamInterrupted = false;
          for await (const chunk of stream) {
            if (get().replayRequestId !== nextReplayRequestId) return;
            const runtimeState = chunk.session?.metadata.runtime_state;
            if (
              chunk.session &&
              runtimeState &&
              typeof runtimeState === "object" &&
              "run_id" in runtimeState &&
              typeof runtimeState.run_id === "string" &&
              runtimeState.run_id
            ) {
              activeRunIdentity = {
                sessionId: chunk.session.session.id,
                runId: runtimeState.run_id,
              };
            }
            if (chunk.event) {
              streamInterrupted =
                isRuntimeCancellationEvent(chunk.event) || streamInterrupted;
              streamFailureMessage =
                preferFailureMessage(
                  streamFailureMessage,
                  failureMessageFromEvent(chunk.event),
                ) ?? null;
            }
            set((state) => {
              if (state.replayRequestId !== nextReplayRequestId) return state;
              const newEvents = chunk.event
                ? applyLiveStreamEvent(state.currentSessionEvents, chunk.event)
                : state.currentSessionEvents;
              return {
                currentSessionState: chunk.session ?? state.currentSessionState,
                currentSessionEvents: newEvents,
                currentSessionId:
                  chunk.session?.session?.id ?? state.currentSessionId,
                currentSessionOutput:
                  chunk.output !== null
                    ? chunk.output
                    : state.currentSessionOutput,
              };
            });
          }

          // A run is interrupted when any signal agrees: an SSE cancellation
          // event, the user's cancel request (cancelRequested / cancelling),
          // or the authoritative backend session row landed as "interrupted"
          // even if no cancellation event accompanied the stream close.
          if (get().replayRequestId !== nextReplayRequestId) return;
          const sessionStatus = get().currentSessionState?.status;
          const interrupted =
            streamInterrupted ||
            get().runStatus === "cancelling" ||
            get().cancelRequested ||
            sessionStatus === "interrupted";
          const failed =
            !interrupted &&
            (streamFailureMessage !== null || sessionStatus === "failed");
          set({
            runStatus: interrupted ? "idle" : failed ? "error" : "success",
            runError: failed
              ? (streamFailureMessage ?? "runtime session failed")
              : null,
            cancelRequested: false,
          });
          await refreshAfterMutation(
            {
              sessions: true,
              status: true,
              review: true,
              backgroundTasks: true,
              notifications: true,
              debug: true,
              sessionId: get().currentSessionId,
            },
            workspaceScope,
          );
        } catch (err) {
          if (get().replayRequestId !== nextReplayRequestId) return;
          // A torn-down stream during a user interrupt surfaces as an
          // AbortError (or any rejection once the cancel flag is set), and the
          // backend session row may already be interrupted.
          const errName = (err as Error)?.name;
          const interrupted =
            errName === "AbortError" ||
            get().runStatus === "cancelling" ||
            get().cancelRequested ||
            get().currentSessionState?.status === "interrupted";
          set({
            runStatus: interrupted ? "idle" : "error",
            runError: interrupted ? null : errorMessage(err),
            cancelRequested: false,
          });
        }
      },

      cancelCurrentRun: async () => {
        const { currentSessionId, currentSessionState, runStatus, runOrigin } =
          get();
        if (!isRunLocked(runStatus) || runStatus === "cancelling") return;
        const runtimeState = currentSessionState?.metadata.runtime_state;
        const externalRunId =
          runtimeState &&
          typeof runtimeState === "object" &&
          "run_id" in runtimeState &&
          typeof runtimeState.run_id === "string"
            ? runtimeState.run_id
            : null;
        const target =
          runOrigin === "local"
            ? activeRunIdentity
            : currentSessionId && externalRunId
              ? { sessionId: currentSessionId, runId: externalRunId }
              : null;
        set({ runStatus: "cancelling", runError: null, cancelRequested: true });
        if (runOrigin === "local") activeRunAbortController?.abort();
        // Before the first local snapshot, disconnect cancellation owns the run.
        // Never send an unqualified POST that could cancel a subsequent run.
        if (!target) {
          if (runOrigin !== "local")
            set({
              runStatus: "error",
              runError: i18n.t("chat.cancelIdentityUnavailable"),
              cancelRequested: false,
            });
          return;
        }
        const requestId = get().replayRequestId;
        try {
          await RuntimeClient.cancelSession(target.sessionId, target.runId);
          if (
            runOrigin !== "local" &&
            requestId === get().replayRequestId &&
            get().currentSessionId === target.sessionId &&
            get().runStatus === "cancelling"
          ) {
            set({ runStatus: "idle", cancelRequested: false });
          }
        } catch (err) {
          if (
            runOrigin !== "local" &&
            requestId === get().replayRequestId &&
            get().currentSessionId === target.sessionId
          ) {
            set({
              runStatus: "error",
              runError: errorMessage(err),
              cancelRequested: false,
            });
          }
        }
      },

      resolveApproval: async (decision) => {
        const {
          currentSessionId,
          currentSessionEvents,
          replayStatus,
          approvalStatus,
          replayRequestId,
        } = get();

        if (
          !currentSessionId ||
          replayStatus === "loading" ||
          approvalStatus === "submitting"
        ) {
          return;
        }

        // Claim the approval card before any network call. Use the pending
        // request shown by the current event projection for this POST; an
        // authoritative replay here would make the click wait on a slow GET.
        const requestId = getPendingApprovalRequestId(currentSessionEvents);
        set({ approvalStatus: "submitting", approvalError: null });

        if (!requestId) {
          set({
            approvalStatus: "error",
            approvalError: i18n.t("approval.noPending"),
          });
          return;
        }

        // A concurrent replay may advance the generation while the POST is
        // in flight. The response and failure paths below keep that newer
        // session view authoritative and clear submitting on stale exits.

        try {
          const response = await RuntimeClient.resolveApproval(
            currentSessionId,
            requestId,
            decision,
          );
          if (
            get().currentSessionId !== currentSessionId ||
            get().replayRequestId !== replayRequestId
          ) {
            if (get().currentSessionId === currentSessionId) {
              set({ approvalStatus: "idle", approvalError: null });
            }
            return;
          }
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
          await refreshAfterMutation({
            sessions: true,
            status: true,
            review: true,
          });
          set({ approvalStatus: "idle" });
        } catch (err) {
          if (
            get().currentSessionId !== currentSessionId ||
            get().replayRequestId !== replayRequestId
          ) {
            if (get().currentSessionId === currentSessionId) {
              set({ approvalStatus: "idle", approvalError: null });
            }
            return;
          }
          const message = errorMessage(err);
          const isApprovalConflict =
            typeof err === "object" &&
            err !== null &&
            "status" in err &&
            err.status === 409;
          set({
            approvalStatus: "error",
            approvalError: message,
          });
          // A 409 means the local card was stale or another resolver won the
          // CAS claim. Reload the authoritative session so the current
          // pending approval is rendered instead of leaving a dead card.
          try {
            const replay =
              await RuntimeClient.getSessionReplay(currentSessionId);
            if (
              get().currentSessionId === currentSessionId &&
              get().replayRequestId === replayRequestId
            ) {
              const hasPendingApproval =
                replay.session.status === "waiting" &&
                getPendingApprovalRequestId(replay.events) !== null;
              set({
                currentSessionState: replay.session,
                currentSessionEvents: replay.events,
                currentSessionOutput: replay.output,
                replayStatus: "success",
                replayError: null,
                runStatus: runStatusForReplay(replay.session),
                approvalStatus:
                  isApprovalConflict && hasPendingApproval ? "idle" : "error",
                approvalError:
                  isApprovalConflict && hasPendingApproval ? null : message,
              });
            }
          } catch {
            // Preserve the runtime error and last known state if replay fails.
          }
          await refreshAfterMutation({ sessions: true });
        }
      },

      answerQuestion: async (answers) => {
        const requestGeneration = get().replayRequestId;
        const {
          currentSessionId,
          currentSessionEvents,
          replayStatus,
          questionStatus,
        } = get();
        const isCurrent = () =>
          get().replayRequestId === requestGeneration &&
          get().currentSessionId === currentSessionId;

        if (
          !currentSessionId ||
          replayStatus === "loading" ||
          questionStatus === "submitting"
        ) {
          return;
        }

        const requestId = getPendingQuestionRequestId(currentSessionEvents);
        if (!requestId) {
          set({
            questionStatus: "error",
            questionError: i18n.t("question.noPending"),
          });
          return;
        }

        // A pending question pauses the run: the backend emits
        // runtime.question_requested and then closes the run stream, parking the
        // session in the "waiting" state. runStatus can still read "running" in
        // the window between the question event and the stream close, and gating
        // the answer on isRunLocked there would silently drop the submission and
        // strand the user (the composer is disabled while the session is
        // "waiting", so the question card is the only input path). The presence
        // of a pending question is the authoritative signal that the run is
        // paused awaiting input, so the answer is not blocked by a stale
        // run-lock; the backend still rejects answers whose request is no longer
        // pending.
        set({ questionStatus: "submitting", questionError: null });

        try {
          const response = await RuntimeClient.answerQuestion(
            currentSessionId,
            requestId,
            answers,
          );
          if (!isCurrent()) return;
          set({
            currentSessionId: response.session.session.id,
            currentSessionState: response.session,
            currentSessionEvents: response.events,
            currentSessionOutput: response.output,
            replayStatus: "success",
            replayError: null,
            runStatus: runStatusForReplay(response.session),
            runError: null,
            questionStatus: "idle",
            questionError: null,
          });
          await refreshAfterMutation({
            sessions: true,
            status: true,
            review: true,
            backgroundTasks: true,
            debug: true,
            sessionId: response.session.session.id,
          });
        } catch (err) {
          if (!isCurrent()) return;
          set({
            questionStatus: "error",
            questionError: errorMessage(err),
          });
          try {
            const replay =
              await RuntimeClient.getSessionReplay(currentSessionId);
            if (isCurrent()) {
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
            // Preserve the last runtime-owned snapshot when replay is unavailable.
          }
          if (!isCurrent()) return;
          await refreshAfterMutation({
            sessions: true,
            status: true,
            review: true,
            backgroundTasks: true,
          });
        }
      },
    }),
    {
      name: "app-storage",
      partialize: (state): PersistedAppState => ({
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
