// The query layer's own suite: it drives the hooks the shell renders, against the
// process-wide client, so it pins what the *components* observe — which key an
// answer lands under, when a request is cancelled, and which surface a mutation
// refreshes.
import "../../test-local-storage";
import type { ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RuntimeClientError } from "../runtime/client";
import type * as RuntimeClientModule from "../runtime/client";
import type {
  BackgroundTaskOutput,
  BackgroundTaskResultPayload,
  BackgroundTaskSummary,
  ProviderModelsResult,
  ProviderSummary,
  RuntimeStatusSnapshot,
  StoredSessionSummary,
} from "../runtime/types";
import { queryClient, shouldRetryQuery } from "./client";
import { queryKeys } from "./keys";
import { asyncStatusFromQuery } from "./state";
import { useProviderCatalogQuery } from "./providers";
import { useBackgroundTasksQuery } from "./tasks";
import { useSessionsQuery } from "./sessions";
import { useReviewDiffQuery } from "./review";
import { useRuntimeStatusQuery } from "./status";
import { useSettingsQuery, useUpdateSettings } from "./settings";
import { currentWorkspaceScope } from "./scope";
import { useSwitchWorkspace, useWorkspacesQuery } from "./workspaces";

const runtimeClientMocks = vi.hoisted(() => ({
  listWorkspacesMock: vi.fn(),
  openWorkspaceMock: vi.fn(),
  listProvidersMock: vi.fn(),
  listProviderModelsMock: vi.fn(),
  listAgentsMock: vi.fn(),
  listSkillsMock: vi.fn(),
  listCommandsMock: vi.fn(),
  listSessionsMock: vi.fn(),
  listNotificationsMock: vi.fn(),
  ackNotificationMock: vi.fn(),
  getStatusMock: vi.fn(),
  retryMcpConnectionsMock: vi.fn(),
  getReviewMock: vi.fn(),
  getReviewDiffMock: vi.fn(),
  listBackgroundTasksMock: vi.fn(),
  listSessionBackgroundTasksMock: vi.fn(),
  getBackgroundTaskOutputMock: vi.fn(),
  getSessionDebugMock: vi.fn(),
  getSettingsMock: vi.fn(),
  updateSettingsMock: vi.fn(),
  validateProviderCredentialsMock: vi.fn(),
}));

vi.mock("../runtime/client", async () => {
  const actual =
    await vi.importActual<typeof RuntimeClientModule>("../runtime/client");
  return {
    RuntimeClientError: actual.RuntimeClientError,
    RuntimeClient: {
      listWorkspaces: runtimeClientMocks.listWorkspacesMock,
      openWorkspace: runtimeClientMocks.openWorkspaceMock,
      listProviders: runtimeClientMocks.listProvidersMock,
      listProviderModels: runtimeClientMocks.listProviderModelsMock,
      listAgents: runtimeClientMocks.listAgentsMock,
      listSkills: runtimeClientMocks.listSkillsMock,
      listCommands: runtimeClientMocks.listCommandsMock,
      listSessions: runtimeClientMocks.listSessionsMock,
      listNotifications: runtimeClientMocks.listNotificationsMock,
      ackNotification: runtimeClientMocks.ackNotificationMock,
      getStatus: runtimeClientMocks.getStatusMock,
      retryMcpConnections: runtimeClientMocks.retryMcpConnectionsMock,
      getReview: runtimeClientMocks.getReviewMock,
      getReviewDiff: runtimeClientMocks.getReviewDiffMock,
      listBackgroundTasks: runtimeClientMocks.listBackgroundTasksMock,
      listSessionBackgroundTasks:
        runtimeClientMocks.listSessionBackgroundTasksMock,
      getBackgroundTaskOutput: runtimeClientMocks.getBackgroundTaskOutputMock,
      getSessionDebug: runtimeClientMocks.getSessionDebugMock,
      getSettings: runtimeClientMocks.getSettingsMock,
      updateSettings: runtimeClientMocks.updateSettingsMock,
      validateProviderCredentials:
        runtimeClientMocks.validateProviderCredentialsMock,
    },
  };
});

const WORKSPACE_A = "/workspace-a";
const WORKSPACE_B = "/workspace-b";

const statusSnapshot: RuntimeStatusSnapshot = {
  git: { state: "git_ready", root: WORKSPACE_A, error: null },
  lsp: { state: "stopped", error: null, details: {} },
  mcp: { state: "stopped", error: null, details: {} },
  acp: { state: "unconfigured", error: null, details: {} },
  background_tasks: {
    active_worker_slots: 0,
    queued_count: 0,
    running_count: 0,
    terminal_count: 0,
    default_concurrency: 1,
    provider_concurrency: {},
    model_concurrency: {},
    status_counts: {},
  },
};

const retriedStatusSnapshot: RuntimeStatusSnapshot = {
  ...statusSnapshot,
  git: {
    state: "git_ready",
    root: WORKSPACE_A,
    branch: "retried",
    error: null,
  },
};

function workspaceRegistry(path: string) {
  return {
    current: {
      path,
      label: path,
      available: true,
      current: true,
      last_opened_at: 1,
    },
    recent: [],
    candidates: [],
  };
}

function storedSession(id: string, prompt: string): StoredSessionSummary {
  return {
    session: { id },
    status: "completed",
    turn: 1,
    prompt,
    updated_at: 1,
  };
}

/**
 * One delegated task result as `GET /api/tasks/{id}/output` reports it.
 *
 * The transport always writes the task's delegated view, its lifecycle message
 * and its tool-call count, so the fixture does too; only the lineage varies
 * between cases.
 */
function makeTaskResult(
  taskId: string,
  fields: Partial<BackgroundTaskResultPayload> = {},
): BackgroundTaskResultPayload {
  return {
    task_id: taskId,
    status: "completed",
    approval_blocked: false,
    result_available: true,
    tool_call_count: 0,
    delegation: { approval_blocked: false, result_available: true },
    message: {
      kind: "delegated_lifecycle",
      approval_blocked: false,
      result_available: true,
    },
    ...fields,
  };
}

function taskSummary(
  id: string,
  sessionId: string | null,
): BackgroundTaskSummary {
  return {
    task: { id },
    status: "completed",
    prompt: `prompt ${id}`,
    session_id: sessionId,
    error: null,
    created_at: 1,
    updated_at: 1,
    keep_alive: false,
    schema_mode: "permissive",
  };
}

function taskOutput(id: string, output: string): BackgroundTaskOutput {
  return {
    task: makeTaskResult(id, {
      parent_session_id: "session-1",
      requested_child_session_id: null,
      child_session_id: null,
      approval_request_id: null,
      question_request_id: null,
      summary_output: null,
      routing: { mode: "background", subagent_type: "explore" },
    }),
    session_result: null,
    output,
  };
}

/** A request that rejects the way `fetch` does when its signal aborts. */
function abortableRequest<T>(
  signal: AbortSignal | undefined,
  aborted: () => void,
) {
  return new Promise<T>((_resolve, reject) => {
    if (signal === undefined) return;
    if (signal.aborted) {
      aborted();
      reject(new DOMException("aborted", "AbortError"));
      return;
    }
    signal.addEventListener(
      "abort",
      () => {
        aborted();
        reject(new DOMException("aborted", "AbortError"));
      },
      { once: true },
    );
  });
}

function QueryWrapper({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

const hookOptions = { wrapper: QueryWrapper };

function installDefaultMocks() {
  runtimeClientMocks.listWorkspacesMock.mockResolvedValue(
    workspaceRegistry(WORKSPACE_A),
  );
  runtimeClientMocks.openWorkspaceMock.mockResolvedValue(
    workspaceRegistry(WORKSPACE_B),
  );
  runtimeClientMocks.listSessionsMock.mockResolvedValue([]);
  runtimeClientMocks.listProvidersMock.mockResolvedValue([]);
  runtimeClientMocks.listProviderModelsMock.mockResolvedValue({
    provider: "opencode-go",
    configured: true,
    models: [],
    model_metadata: {},
  });
  runtimeClientMocks.listAgentsMock.mockResolvedValue([]);
  runtimeClientMocks.listSkillsMock.mockResolvedValue([]);
  runtimeClientMocks.listCommandsMock.mockResolvedValue([]);
  runtimeClientMocks.listNotificationsMock.mockResolvedValue([]);
  runtimeClientMocks.getStatusMock.mockResolvedValue(statusSnapshot);
  runtimeClientMocks.retryMcpConnectionsMock.mockResolvedValue(
    retriedStatusSnapshot,
  );
  runtimeClientMocks.getReviewMock.mockResolvedValue({
    root: WORKSPACE_A,
    git: { state: "git_ready" },
    changed_files: [],
    tree: [],
  });
  runtimeClientMocks.getReviewDiffMock.mockResolvedValue({
    root: WORKSPACE_A,
    path: "README.md",
    state: "clean",
    diff: null,
  });
  runtimeClientMocks.listBackgroundTasksMock.mockResolvedValue([]);
  runtimeClientMocks.listSessionBackgroundTasksMock.mockResolvedValue([]);
  runtimeClientMocks.getBackgroundTaskOutputMock.mockResolvedValue(
    taskOutput("task-1", "output"),
  );
  runtimeClientMocks.getSessionDebugMock.mockResolvedValue({
    session: {
      session: { id: "session-1" },
      status: "completed",
      turn: 1,
      metadata: {},
    },
    prompt: "prompt",
    persisted_status: "completed",
    current_status: "completed",
    active: false,
    resumable: true,
    replayable: true,
    terminal: true,
    pending_approval: null,
    pending_question: null,
    last_relevant_event: null,
    last_failure_event: null,
    failure: null,
    last_tool: null,
    suggested_operator_action: null,
    operator_guidance: null,
  });
  runtimeClientMocks.getSettingsMock.mockResolvedValue({});
  runtimeClientMocks.updateSettingsMock.mockResolvedValue({});
  runtimeClientMocks.validateProviderCredentialsMock.mockResolvedValue({
    provider: "opencode-go",
    configured: true,
    ok: true,
    status: "ok",
    message: "Validation succeeded.",
  });
}

describe("query layer", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    installDefaultMocks();
    queryClient.clear();
  });

  describe("workspace isolation", () => {
    it("cancels a superseded workspace's in-flight read and never shows its answer", async () => {
      const abortedSignals: boolean[] = [];
      runtimeClientMocks.listSessionsMock.mockImplementationOnce(
        (signal: AbortSignal | undefined) =>
          abortableRequest<StoredSessionSummary[]>(signal, () => {
            abortedSignals.push(true);
          }),
      );
      runtimeClientMocks.listSessionsMock.mockResolvedValueOnce([
        storedSession("b-session", "workspace b"),
      ]);

      const { result } = renderHook(() => {
        const workspaces = useWorkspacesQuery();
        const scope = workspaces.data?.current?.path ?? null;
        const sessions = useSessionsQuery(scope);
        const status = useRuntimeStatusQuery(scope);
        const workspaceSwitch = useSwitchWorkspace();
        return {
          scope,
          sessions: sessions.data,
          sessionsStatus: asyncStatusFromQuery(sessions),
          statusRoot: status.data?.git.root,
          switchTo: workspaceSwitch.switchTo,
        };
      }, hookOptions);

      // The first read belongs to workspace A and is still in flight.
      await waitFor(() => expect(result.current.scope).toBe(WORKSPACE_A));
      await waitFor(() =>
        expect(runtimeClientMocks.listSessionsMock).toHaveBeenCalledTimes(1),
      );
      expect(runtimeClientMocks.listSessionsMock).toHaveBeenCalledWith(
        expect.any(AbortSignal),
      );

      await act(async () => {
        await result.current.switchTo(WORKSPACE_B);
      });

      // The superseded read was aborted, not merely discarded, and it was not
      // retried: exactly one request carried workspace A's payload.
      expect(abortedSignals).toEqual([true]);
      await waitFor(() => expect(result.current.scope).toBe(WORKSPACE_B));
      await waitFor(() =>
        expect(result.current.sessions).toEqual([
          storedSession("b-session", "workspace b"),
        ]),
      );
      expect(runtimeClientMocks.listSessionsMock).toHaveBeenCalledTimes(2);

      // Workspace A's payload can never be read under workspace B's key: the
      // keys differ, and A's answer never landed at all.
      expect(
        queryClient.getQueryData(queryKeys.sessions(WORKSPACE_A)),
      ).toBeUndefined();
      expect(queryClient.getQueryData(queryKeys.sessions(WORKSPACE_B))).toEqual(
        [storedSession("b-session", "workspace b")],
      );

      // Every other workspace-scoped payload is re-keyed by the switch, so the
      // shell refetches for the new runtime instead of showing the old one's.
      await waitFor(() =>
        expect(runtimeClientMocks.getStatusMock).toHaveBeenCalledTimes(2),
      );
      expect(runtimeClientMocks.getStatusMock).toHaveBeenCalledWith(
        expect.any(AbortSignal),
      );
      expect(result.current.statusRoot).toBe(WORKSPACE_A);
    });
  });

  describe("documented scope fallback", () => {
    it("answers no scope at all — never a guessed workspace — when the registry has not loaded", async () => {
      const seeded = queryClient.setQueryData(queryKeys.sessions(WORKSPACE_A), [
        storedSession("a-session", "workspace a"),
      ]);
      const invalidated = () =>
        queryClient
          .getQueryCache()
          .findAll({ queryKey: queryKeys.sessions(WORKSPACE_A) })
          .some((query) => query.state.isInvalidated);
      expect(seeded).toBeDefined();

      // No registry payload: the fallback helper answers null, so a caller that
      // could not receive an explicit scope cannot address a real workspace's key.
      expect(currentWorkspaceScope()).toBeNull();
      const { refreshAfterMutation } = await import("./invalidation");
      await refreshAfterMutation({ sessions: true });
      expect(invalidated()).toBe(false);

      // With the registry loaded, the same fallback addresses exactly that
      // workspace, which is what keeps imperative helpers correct in the shell.
      queryClient.setQueryData(
        queryKeys.workspaceRegistry(),
        workspaceRegistry(WORKSPACE_A),
      );
      expect(currentWorkspaceScope()).toBe(WORKSPACE_A);
      await refreshAfterMutation({ sessions: true });
      expect(invalidated()).toBe(true);
    });
  });

  describe("stale-request cancellation", () => {
    it("keys a selected file diff by its path so a superseded read never wins", async () => {
      const abortedSignals: boolean[] = [];
      runtimeClientMocks.getReviewDiffMock.mockImplementation(
        (path: string, signal: AbortSignal | undefined) =>
          path === "src/slow.ts"
            ? abortableRequest(signal, () => {
                abortedSignals.push(true);
              })
            : Promise.resolve({
                root: WORKSPACE_A,
                path,
                state: "clean" as const,
                diff: null,
              }),
      );

      const { result, rerender } = renderHook(
        ({ path }: { path: string }) => useReviewDiffQuery(WORKSPACE_A, path),
        { ...hookOptions, initialProps: { path: "src/slow.ts" } },
      );

      await waitFor(() =>
        expect(runtimeClientMocks.getReviewDiffMock).toHaveBeenCalledWith(
          "src/slow.ts",
          expect.any(AbortSignal),
        ),
      );

      rerender({ path: "src/fast.ts" });

      await waitFor(() =>
        expect(result.current.data?.path).toBe("src/fast.ts"),
      );
      expect(abortedSignals).toEqual([true]);
      // The payload the reader left is not re-fetched under the new path's key,
      // and the new path's answer is the only one in the cache for it.
      expect(
        queryClient.getQueryData(
          queryKeys.reviewDiff(WORKSPACE_A, "src/slow.ts"),
        ),
      ).toBeUndefined();
      expect(runtimeClientMocks.getReviewDiffMock).toHaveBeenCalledTimes(2);
    });

    it("never retries an aborted read, retries an idempotent read once, and never retries a client error", () => {
      expect(
        shouldRetryQuery(0, new DOMException("aborted", "AbortError")),
      ).toBe(false);
      expect(shouldRetryQuery(0, new RuntimeClientError("conflict", 409))).toBe(
        false,
      );
      expect(
        shouldRetryQuery(0, new RuntimeClientError("runtime down", 503)),
      ).toBe(true);
      expect(
        shouldRetryQuery(1, new RuntimeClientError("runtime down", 503)),
      ).toBe(false);
      // Mutations are agent-control calls and never retry a possibly-applied
      // state transition.
      expect(queryClient.getDefaultOptions().mutations?.retry).toBe(false);
      expect(
        queryClient.getDefaultOptions().queries?.refetchOnWindowFocus,
      ).toBe(false);
    });
  });

  describe("mutation invalidation", () => {
    it("refetches an affected query a mounted component is reading", async () => {
      const providers: ProviderSummary[] = [
        {
          name: "opencode-go",
          label: "OpenCode Go",
          configured: true,
          current: true,
        },
      ];
      const models: Record<string, ProviderModelsResult> = {
        "opencode-go": {
          provider: "opencode-go",
          configured: true,
          models: ["original-model"],
          model_metadata: {},
        },
      };
      runtimeClientMocks.listProvidersMock.mockResolvedValue(providers);
      runtimeClientMocks.listProviderModelsMock.mockResolvedValue(
        models["opencode-go"],
      );
      runtimeClientMocks.getStatusMock.mockResolvedValue(statusSnapshot);

      const { result } = renderHook(
        () => ({
          catalog: useProviderCatalogQuery(WORKSPACE_A),
          status: useRuntimeStatusQuery(WORKSPACE_A),
          settingsQuery: useSettingsQuery(WORKSPACE_A),
          settings: useUpdateSettings(WORKSPACE_A),
        }),
        hookOptions,
      );

      await waitFor(() =>
        expect(result.current.catalog.data?.models).toEqual(models),
      );
      expect(runtimeClientMocks.getStatusMock).toHaveBeenCalledTimes(1);

      runtimeClientMocks.listProviderModelsMock.mockResolvedValue({
        provider: "opencode-go",
        configured: true,
        models: ["reloaded-model"],
        model_metadata: {},
      });
      runtimeClientMocks.getStatusMock.mockResolvedValue(retriedStatusSnapshot);
      runtimeClientMocks.updateSettingsMock.mockResolvedValue({
        provider: "opencode-go",
        model: "opencode-go/reloaded-model",
      });

      await act(async () => {
        await result.current.settings.save({ provider: "opencode-go" });
      });

      // The save writes its own answer into the settings entry and invalidates
      // the surfaces it can have moved, so a mounted reader reloads them.
      await waitFor(() =>
        expect(
          result.current.catalog.data?.models["opencode-go"]?.models,
        ).toEqual(["reloaded-model"]),
      );
      await waitFor(() =>
        expect(result.current.status.data?.git.branch).toBe("retried"),
      );
      expect(result.current.settingsQuery.data?.model).toBe(
        "opencode-go/reloaded-model",
      );
      expect(runtimeClientMocks.listProvidersMock).toHaveBeenCalledTimes(2);
      expect(runtimeClientMocks.getStatusMock).toHaveBeenCalledTimes(2);
    });
  });

  describe("session and task scoping", () => {
    it("keeps each task list under its own session scope", async () => {
      runtimeClientMocks.listBackgroundTasksMock.mockResolvedValue([
        taskSummary("task-global", null),
      ]);
      runtimeClientMocks.listSessionBackgroundTasksMock.mockResolvedValue([
        taskSummary("task-session", "session-1"),
      ]);

      const globalTasks = renderHook(
        () => useBackgroundTasksQuery(WORKSPACE_A, null),
        hookOptions,
      );
      const sessionTasks = renderHook(
        () => useBackgroundTasksQuery(WORKSPACE_A, "session-1"),
        hookOptions,
      );

      await waitFor(() =>
        expect(globalTasks.result.current.data?.[0]?.task.id).toBe(
          "task-global",
        ),
      );
      await waitFor(() =>
        expect(sessionTasks.result.current.data?.[0]?.task.id).toBe(
          "task-session",
        ),
      );
      expect(
        runtimeClientMocks.listSessionBackgroundTasksMock,
      ).toHaveBeenCalledWith("session-1", expect.any(AbortSignal));
      expect(
        queryClient.getQueryData(queryKeys.backgroundTasks(WORKSPACE_A, null)),
      ).toEqual([taskSummary("task-global", null)]);
      expect(
        queryClient.getQueryData(
          queryKeys.backgroundTasks(WORKSPACE_A, "session-1"),
        ),
      ).toEqual([taskSummary("task-session", "session-1")]);
    });
  });
});
