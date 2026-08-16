// Must precede the store/App imports so the persisted store sees a real
// localStorage during hydration.
import "./test-local-storage";
import { render, waitFor, act } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import App from "./App";
import { useAppStore } from "./store";
import type {
  BackgroundTaskOutput,
  BackgroundTaskSummary,
  EventEnvelope,
  RuntimeResponse,
  RuntimeStreamChunk,
  SessionState,
} from "./lib/runtime/types";
import "./i18n";

vi.mock("./components/SettingsPanel", () => ({
  SettingsPanel: () => <div data-testid="settings-panel-mock" />,
}));

vi.mock("./components/OpenProjectModal", () => ({
  OpenProjectModal: () => <div data-testid="open-project-modal-mock" />,
}));

const runtimeClientMocks = vi.hoisted(() => ({
  listWorkspacesMock: vi.fn(),
  openWorkspaceMock: vi.fn(),
  listProvidersMock: vi.fn(),
  listProviderModelsMock: vi.fn(),
  listAgentsMock: vi.fn(),
  listSkillsMock: vi.fn(),
  listCommandsMock: vi.fn(),
  listSessionsMock: vi.fn(),
  getSessionReplayMock: vi.fn(),
  getStatusMock: vi.fn(),
  getReviewMock: vi.fn(),
  getReviewDiffMock: vi.fn(),
  resolveApprovalMock: vi.fn(),
  answerQuestionMock: vi.fn(),
  listNotificationsMock: vi.fn(),
  acknowledgeNotificationMock: vi.fn(),
  listBackgroundTasksMock: vi.fn(),
  listSessionBackgroundTasksMock: vi.fn(),
  cancelSessionMock: vi.fn(),
  cancelBackgroundTaskMock: vi.fn(),
  getBackgroundTaskMock: vi.fn(),
  getBackgroundTaskOutputMock: vi.fn(),
  getChildSessionContextMock: vi.fn(),
  getSessionDebugMock: vi.fn(),
  getSettingsMock: vi.fn(),
  updateSettingsMock: vi.fn(),
  validateProviderCredentialsMock: vi.fn(),
  retryBackgroundTaskMock: vi.fn(),
  retryMcpConnectionsMock: vi.fn(),
  runStreamMock: vi.fn(),
  sessionEventsMock: vi.fn(),
}));

vi.mock("./lib/runtime/client", () => ({
  RuntimeClient: {
    listWorkspaces: runtimeClientMocks.listWorkspacesMock,
    openWorkspace: runtimeClientMocks.openWorkspaceMock,
    listProviders: runtimeClientMocks.listProvidersMock,
    listProviderModels: runtimeClientMocks.listProviderModelsMock,
    listAgents: runtimeClientMocks.listAgentsMock,
    listSkills: runtimeClientMocks.listSkillsMock,
    listCommands: runtimeClientMocks.listCommandsMock,
    listSessions: runtimeClientMocks.listSessionsMock,
    getSessionReplay: runtimeClientMocks.getSessionReplayMock,
    getStatus: runtimeClientMocks.getStatusMock,
    getReview: runtimeClientMocks.getReviewMock,
    getReviewDiff: runtimeClientMocks.getReviewDiffMock,
    resolveApproval: runtimeClientMocks.resolveApprovalMock,
    answerQuestion: runtimeClientMocks.answerQuestionMock,
    listNotifications: runtimeClientMocks.listNotificationsMock,
    acknowledgeNotification: runtimeClientMocks.acknowledgeNotificationMock,
    listBackgroundTasks: runtimeClientMocks.listBackgroundTasksMock,
    listSessionBackgroundTasks:
      runtimeClientMocks.listSessionBackgroundTasksMock,
    cancelSession: runtimeClientMocks.cancelSessionMock,
    cancelBackgroundTask: runtimeClientMocks.cancelBackgroundTaskMock,
    getBackgroundTask: runtimeClientMocks.getBackgroundTaskMock,
    getBackgroundTaskOutput: runtimeClientMocks.getBackgroundTaskOutputMock,
    getChildSessionContext: runtimeClientMocks.getChildSessionContextMock,
    getSessionDebug: runtimeClientMocks.getSessionDebugMock,
    getSettings: runtimeClientMocks.getSettingsMock,
    updateSettings: runtimeClientMocks.updateSettingsMock,
    validateProviderCredentials:
      runtimeClientMocks.validateProviderCredentialsMock,
    retryBackgroundTask: runtimeClientMocks.retryBackgroundTaskMock,
    retryMcpConnections: runtimeClientMocks.retryMcpConnectionsMock,
    runStream: runtimeClientMocks.runStreamMock,
    sessionEvents: runtimeClientMocks.sessionEventsMock,
  },
}));

function makeSessionState(
  sessionId: string,
  status: SessionState["status"],
): SessionState {
  return {
    session: { id: sessionId },
    status,
    turn: 1,
    metadata: {},
  };
}

function makeSnapshotChunk(
  sessionId: string,
  status: SessionState["status"],
): RuntimeStreamChunk {
  return {
    kind: "session",
    session: makeSessionState(sessionId, status),
    event: null,
    output: null,
  };
}

function makeEvent(
  sequence: number,
  eventType: string,
  payload: Record<string, unknown>,
  source: EventEnvelope["source"] = "runtime",
  sessionId = "session-1",
): EventEnvelope {
  return {
    session_id: sessionId,
    sequence,
    event_type: eventType,
    source,
    payload,
  };
}

function makeRuntimeResponse(
  sessionId: string,
  status: SessionState["status"],
  events: EventEnvelope[],
  output: string | null,
): RuntimeResponse {
  return {
    session: makeSessionState(sessionId, status),
    events,
    output,
  };
}

const parentTaskSummary: BackgroundTaskSummary = {
  task: { id: "task-child" },
  status: "completed",
  prompt: "child prompt",
  session_id: "child-session",
  error: null,
  created_at: 1,
  updated_at: 1,
};

function makeChildOutput(status: SessionState["status"]): BackgroundTaskOutput {
  return {
    task: {
      task_id: "task-child",
      status: "completed",
      parent_session_id: "session-parent",
      requested_child_session_id: "requested-child",
      delegated_prompt: "child prompt",
      child_session_id: "child-session",
      approval_request_id: null,
      question_request_id: null,
      approval_blocked: false,
      summary_output: "child summary",
      error: null,
      result_available: true,
      cancellation_cause: null,
      routing: { mode: "subagent", subagent_type: "explore" },
    },
    session_result: {
      session: {
        ...makeSessionState("child-session", status),
        session: { id: "child-session", parent_id: "session-parent" },
      },
      prompt: "child prompt",
      status,
      summary: "child summary",
      output: "child output",
      error: null,
      last_event_sequence: 2,
      transcript: [
        makeEvent(
          1,
          "runtime.request_received",
          { prompt: "child prompt" },
          "runtime",
          "child-session",
        ),
        makeEvent(
          2,
          "graph.response_ready",
          { output: "child output" },
          "graph",
          "child-session",
        ),
      ],
    },
    output: "child output",
  };
}

function createDeferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function childContextCalls(): number {
  return runtimeClientMocks.getChildSessionContextMock.mock.calls.filter(
    ([sessionId]) => sessionId === "child-session",
  ).length;
}

function childStreamCalls(): number {
  return runtimeClientMocks.sessionEventsMock.mock.calls.filter(
    ([sessionId]) => sessionId === "child-session",
  ).length;
}

function resetStore() {
  useAppStore.setState({
    language: "en",
    agentPreset: "leader",
    providerModel: "deepseek/deepseek-v4-pro",
    workspaces: {
      current: {
        path: "/workspace",
        label: "workspace",
        available: true,
        current: true,
        last_opened_at: 1,
      },
      recent: [],
      candidates: [],
    },
    workspacesStatus: "success",
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
    commands: [],
    commandsStatus: "idle",
    commandsError: null,
    skills: [],
    skillsStatus: "idle",
    skillsError: null,
    sessions: [],
    currentSessionId: null,
    childSessionParentId: null,
    sessionSidebarWidth: 344,
    currentSessionState: null,
    currentSessionEvents: [],
    currentSessionOutput: null,
    sessionsStatus: "success",
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
    selectedBackgroundTaskOutputId: null,
    backgroundTaskOutput: null,
    backgroundTaskOutputStatus: "idle",
    backgroundTaskOutputError: null,
    sessionDebug: null,
    sessionDebugStatus: "idle",
    sessionDebugError: null,
    replayRequestId: 0,
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
    settings: null,
    settingsStatus: "idle",
    settingsError: null,
  });
}

async function flushAsync() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 20));
  });
}

async function browseParentSession() {
  // The parent is a plain (non-child) session: delegated-context lookup fails
  // and it falls through to plain replay, which succeeds.
  runtimeClientMocks.getChildSessionContextMock.mockImplementation(
    (sessionId: string) =>
      sessionId === "session-parent"
        ? Promise.reject(new Error("not a delegated child"))
        : Promise.reject(new Error("not found")),
  );
  runtimeClientMocks.getSessionReplayMock.mockResolvedValue(
    makeRuntimeResponse(
      "session-parent",
      "completed",
      [
        makeEvent(
          1,
          "runtime.request_received",
          { prompt: "parent prompt" },
          "runtime",
          "session-parent",
        ),
        makeEvent(
          2,
          "graph.response_ready",
          { output: "parent output" },
          "graph",
          "session-parent",
        ),
      ],
      "parent output",
    ),
  );
  runtimeClientMocks.listSessionBackgroundTasksMock.mockResolvedValue([
    parentTaskSummary,
  ]);
  // Let the mount effects settle first: App's hydrated-session effect
  // re-selects the current session once `loadSessions` reports success, which
  // would otherwise race our explicit selection below.
  await waitFor(() => {
    expect(runtimeClientMocks.listSessionsMock).toHaveBeenCalled();
  });
  await flushAsync();
  await act(async () => {
    await useAppStore.getState().selectSession("session-parent");
  });
  await waitFor(() => {
    expect(useAppStore.getState().replayStatus).toBe("success");
  });
  expect(useAppStore.getState().currentSessionId).toBe("session-parent");
}

describe("App follow stream with delegated child sessions", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    resetStore();
    runtimeClientMocks.listWorkspacesMock.mockResolvedValue({
      current: {
        path: "/workspace",
        label: "workspace",
        available: true,
        current: true,
        last_opened_at: 1,
      },
      recent: [],
      candidates: [],
    });
    runtimeClientMocks.listProvidersMock.mockResolvedValue([]);
    runtimeClientMocks.listAgentsMock.mockResolvedValue([]);
    runtimeClientMocks.listSkillsMock.mockResolvedValue([]);
    runtimeClientMocks.listCommandsMock.mockResolvedValue([]);
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      {
        session: { id: "session-parent" },
        status: "completed",
        turn: 1,
        prompt: "parent prompt",
        updated_at: 1,
      },
    ]);
    runtimeClientMocks.getStatusMock.mockResolvedValue({
      git: { state: "git_ready", root: "/workspace", error: null },
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
    });
    runtimeClientMocks.getReviewMock.mockResolvedValue({
      root: "/workspace",
      git: { state: "git_ready", root: "/workspace" },
      changed_files: [],
      tree: [],
    });
    runtimeClientMocks.getSettingsMock.mockResolvedValue({
      model: "",
    });
    runtimeClientMocks.listBackgroundTasksMock.mockResolvedValue([]);
    runtimeClientMocks.listSessionBackgroundTasksMock.mockResolvedValue([]);
    runtimeClientMocks.getBackgroundTaskOutputMock.mockResolvedValue(
      makeChildOutput("interrupted"),
    );
  });

  it("shows an interrupted child session once and closes the follow stream instead of leaving it open", async () => {
    let childStreamClosed = false;
    runtimeClientMocks.sessionEventsMock.mockImplementation(async function* (
      sessionId: string,
    ) {
      // The backend only closes follow streams for {completed, failed}; an
      // interrupted row keeps polling forever. Simulate that: yield the
      // snapshot and then never end.
      if (sessionId === "child-session") {
        try {
          yield makeSnapshotChunk(sessionId, "interrupted");
          await new Promise(() => {});
        } finally {
          childStreamClosed = true;
        }
      }
      yield makeSnapshotChunk(sessionId, "completed");
    });
    render(<App />);
    await browseParentSession();

    // Override the child behavior after browsing the parent (browseParentSession
    // owns the parent-side implementation).
    runtimeClientMocks.getChildSessionContextMock.mockImplementation(
      (sessionId: string) =>
        sessionId === "child-session"
          ? Promise.resolve(makeChildOutput("interrupted"))
          : Promise.reject(new Error("not a delegated child")),
    );

    await act(async () => {
      await useAppStore.getState().selectSession("child-session");
    });

    // The child view is populated once and stably.
    await waitFor(() => {
      expect(useAppStore.getState().selectedBackgroundTaskOutputId).toBe(
        "task-child",
      );
      expect(useAppStore.getState().childSessionParentId).toBe(
        "session-parent",
      );
      expect(useAppStore.getState().backgroundTaskOutput?.output).toBe(
        "child output",
      );
      // An interrupted (unsealed) child is terminal for display: it must not
      // be treated as a live run.
      expect(useAppStore.getState().runStatus).toBe("idle");
    });

    // The frontend must stop following the interrupted session on its own
    // (the backend stream would otherwise stay open forever).
    expect(childStreamClosed).toBe(true);

    // Give any loop a chance to fire: the child must not be re-selected or
    // re-followed.
    await flushAsync();
    expect(childContextCalls()).toBe(1);
    expect(childStreamCalls()).toBe(1);
    expect(useAppStore.getState().currentSessionId).toBe("child-session");
    expect(useAppStore.getState().backgroundTaskOutputStatus).toBe("success");
  });

  it("does not re-select a child whose context fetch is still in flight when the follow stream ends", async () => {
    // Parent and child streams both close immediately after the snapshot
    // (completed sessions are terminal on the backend too).
    runtimeClientMocks.sessionEventsMock.mockImplementation(async function* (
      sessionId: string,
    ) {
      yield makeSnapshotChunk(sessionId, "completed");
    });
    const childContextDeferred = createDeferred<BackgroundTaskOutput>();

    render(<App />);
    await browseParentSession();

    // Override the child behavior after browsing the parent (browseParentSession
    // owns the parent-side implementation).
    runtimeClientMocks.getChildSessionContextMock.mockImplementation(
      (sessionId: string) =>
        sessionId === "child-session"
          ? childContextDeferred.promise
          : Promise.reject(new Error("not a delegated child")),
    );

    let selectPromise!: Promise<void>;
    await act(async () => {
      selectPromise = useAppStore.getState().selectSession("child-session");
      // Let the follow stream receive the terminal snapshot and reach its
      // post-loop while the child context fetch is still unresolved.
      await new Promise((resolve) => setTimeout(resolve, 20));
    });

    // The stream ended before the context fetch resolved; the post-loop must
    // NOT re-select the child (that would clear the view and discard the
    // in-flight fetch).
    expect(childContextCalls()).toBe(1);

    await act(async () => {
      childContextDeferred.resolve(makeChildOutput("completed"));
      await selectPromise;
    });

    await waitFor(() => {
      expect(useAppStore.getState().selectedBackgroundTaskOutputId).toBe(
        "task-child",
      );
    });
    await flushAsync();
    expect(childContextCalls()).toBe(1);
    expect(childStreamCalls()).toBe(1);
    expect(useAppStore.getState().backgroundTaskOutput?.output).toBe(
      "child output",
    );
  });
});
