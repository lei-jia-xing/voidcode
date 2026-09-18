// Must precede the store/App imports so the persisted store sees a real
// localStorage during hydration.
import "./test-local-storage";
import {
  render,
  screen,
  waitFor,
  act,
  fireEvent,
} from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import { useAppStore } from "./store";
import { currentWorkspaceScope, queryClient, queryKeys } from "./lib/queries";
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

// The workspace the mocked registry opens, i.e. the scope every seeded
// payload belongs to.
const WORKSPACE_PATH = "/workspace";

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
  listBackgroundTasksMock: vi.fn(),
  listSessionBackgroundTasksMock: vi.fn(),
  listNotificationsMock: vi.fn(),
  cancelSessionMock: vi.fn(),
  getBackgroundTaskOutputMock: vi.fn(),
  getChildSessionContextMock: vi.fn(),
  getSessionDebugMock: vi.fn(),
  getSettingsMock: vi.fn(),
  updateSettingsMock: vi.fn(),
  validateProviderCredentialsMock: vi.fn(),
  retryMcpConnectionsMock: vi.fn(),
  runStreamMock: vi.fn(),
  sessionEventsMock: vi.fn(),
  steerSessionMock: vi.fn(),
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
    listBackgroundTasks: runtimeClientMocks.listBackgroundTasksMock,
    listSessionBackgroundTasks:
      runtimeClientMocks.listSessionBackgroundTasksMock,
    listNotifications: runtimeClientMocks.listNotificationsMock,
    cancelSession: runtimeClientMocks.cancelSessionMock,
    getBackgroundTaskOutput: runtimeClientMocks.getBackgroundTaskOutputMock,
    getChildSessionContext: runtimeClientMocks.getChildSessionContextMock,
    getSessionDebug: runtimeClientMocks.getSessionDebugMock,
    getSettings: runtimeClientMocks.getSettingsMock,
    updateSettings: runtimeClientMocks.updateSettingsMock,
    validateProviderCredentials:
      runtimeClientMocks.validateProviderCredentialsMock,
    retryMcpConnections: runtimeClientMocks.retryMcpConnectionsMock,
    runStream: runtimeClientMocks.runStreamMock,
    sessionEvents: runtimeClientMocks.sessionEventsMock,
    steerSession: runtimeClientMocks.steerSessionMock,
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

const NO_DELEGATED_CONTEXT = {
  status: 404,
  code: "delegated_context_missing",
  message: "no delegated child context",
};

// The shell reads server data from the query cache, so a view assertion reads the
// entry the component reads. The cache is the process-wide client the shell is
// provided with (the same instance the store reads through).
function taskOutput(): { output: string | null } | undefined {
  const scope = currentWorkspaceScope();
  const taskId = useAppStore.getState().selectedBackgroundTaskOutputId;
  return taskId === null
    ? undefined
    : queryClient.getQueryData(queryKeys.taskOutput(scope, taskId));
}

function queryStatus(key: readonly unknown[]): string | undefined {
  return queryClient.getQueryState(key)?.status;
}

function renderApp() {
  return render(
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>,
  );
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
  // Client state only: the server payloads the shell paints live in the query
  // cache, which each test starts from empty and repopulates through the mocked
  // runtime client.
  queryClient.clear();
  useAppStore.setState({
    language: "en",
    agentPreset: "leader",
    providerModel: "deepseek/deepseek-v4-pro",
    reasoningEffort: "",
    reviewMode: "changes",
    reviewSelectedPath: null,
    selectedBackgroundTaskOutputId: null,
    currentSessionId: null,
    childSessionParentId: null,
    sessionSidebarWidth: 344,
    currentSessionState: null,
    currentSessionEvents: [],
    currentSessionOutput: null,
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
  runtimeClientMocks.getChildSessionContextMock.mockRejectedValue(
    NO_DELEGATED_CONTEXT,
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
    await useAppStore
      .getState()
      .selectSession("session-parent", WORKSPACE_PATH);
  });
  await waitFor(() => {
    expect(useAppStore.getState().replayStatus).toBe("success");
  });
  expect(useAppStore.getState().currentSessionId).toBe("session-parent");
}

function installDefaultRuntimeClientMocks() {
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
  runtimeClientMocks.listNotificationsMock.mockResolvedValue([]);
  runtimeClientMocks.getBackgroundTaskOutputMock.mockResolvedValue(
    makeChildOutput("interrupted"),
  );
}

describe("App follow stream with delegated child sessions", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    resetStore();
    installDefaultRuntimeClientMocks();
  });

  it("shows an interrupted child once without opening a redundant follow stream", async () => {
    renderApp();
    await browseParentSession();

    // Override the child behavior after browsing the parent (browseParentSession
    // owns the parent-side implementation).
    runtimeClientMocks.getChildSessionContextMock.mockImplementation(
      (sessionId: string) =>
        sessionId === "child-session"
          ? Promise.resolve(makeChildOutput("interrupted"))
          : Promise.reject(NO_DELEGATED_CONTEXT),
    );

    await act(async () => {
      await useAppStore
        .getState()
        .selectSession("child-session", WORKSPACE_PATH);
    });

    // The child view is populated once and stably.
    await waitFor(() => {
      expect(useAppStore.getState().selectedBackgroundTaskOutputId).toBe(
        "task-child",
      );
      expect(useAppStore.getState().childSessionParentId).toBe(
        "session-parent",
      );
      expect(taskOutput()?.output).toBe("child output");
      // An interrupted (unsealed) child is terminal for display: it must not
      // be treated as a live run.
      expect(useAppStore.getState().runStatus).toBe("idle");
    });

    // Loading the terminal child already supplies its complete transcript.

    // Give any loop a chance to fire: the child must not be re-selected or
    // re-followed.
    await flushAsync();
    expect(childContextCalls()).toBe(1);
    expect(childStreamCalls()).toBe(0);
    expect(useAppStore.getState().currentSessionId).toBe("child-session");
    expect(
      queryStatus(queryKeys.taskOutput(currentWorkspaceScope(), "task-child")),
    ).toBe("success");
  });

  it("does not follow or re-select a child while its context fetch is in flight", async () => {
    runtimeClientMocks.sessionEventsMock.mockImplementation(async function* (
      sessionId: string,
    ) {
      yield makeSnapshotChunk(sessionId, "completed");
    });
    const childContextDeferred = createDeferred<BackgroundTaskOutput>();

    renderApp();
    await browseParentSession();

    // Override the child behavior after browsing the parent (browseParentSession
    // owns the parent-side implementation).
    runtimeClientMocks.getChildSessionContextMock.mockImplementation(
      (sessionId: string) =>
        sessionId === "child-session"
          ? childContextDeferred.promise
          : Promise.reject(NO_DELEGATED_CONTEXT),
    );

    let selectPromise!: Promise<void>;
    await act(async () => {
      selectPromise = useAppStore
        .getState()
        .selectSession("child-session", WORKSPACE_PATH);
      await Promise.resolve();
    });

    // A pending context fetch owns the view until it completes.
    expect(childStreamCalls()).toBe(0);
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
    expect(childStreamCalls()).toBe(0);
    expect(taskOutput()?.output).toBe("child output");
  });

  it("shows a locally interrupted run as Interrupted and never as Failed", async () => {
    const sessionId = "session-1";
    async function* stream() {
      yield {
        kind: "session",
        session: makeSessionState(sessionId, "running"),
        event: makeEvent(
          1,
          "runtime.request_received",
          { prompt: "do it" },
          "runtime",
          sessionId,
        ),
        output: null,
      };
      yield {
        kind: "session",
        session: makeSessionState(sessionId, "interrupted"),
        event: makeEvent(
          2,
          "runtime.failed",
          { cancelled: true, error: "provider stream cancelled" },
          "runtime",
          sessionId,
        ),
        output: null,
      };
    }
    runtimeClientMocks.runStreamMock.mockReturnValue(stream());
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      {
        session: { id: sessionId },
        status: "interrupted",
        turn: 1,
        prompt: "do it",
        updated_at: 1,
      },
    ]);

    renderApp();
    await flushAsync();
    await act(async () => {
      await useAppStore.getState().runTask("do it", WORKSPACE_PATH);
    });
    await flushAsync();

    // The run is not treated as a failure: transient run status settles to
    // idle (no error banner) and the transcript renders an Interrupted badge.
    expect(useAppStore.getState().runStatus).toBe("idle");
    expect(useAppStore.getState().runError).toBeNull();
    expect(useAppStore.getState().currentSessionState?.status).toBe(
      "interrupted",
    );
    expect(screen.getByText("Interrupted")).toBeInTheDocument();
    expect(screen.queryByText("Failed")).not.toBeInTheDocument();
  });

  it("shows no error banner and an Interrupted session when Stop aborts the stream mid-run", async () => {
    const sessionId = "session-1";
    runtimeClientMocks.runStreamMock.mockImplementation(async function* (
      _request: unknown,
      signal?: AbortSignal,
    ) {
      yield {
        kind: "session",
        session: makeSessionState(sessionId, "running"),
        event: makeEvent(
          1,
          "runtime.request_received",
          { prompt: "do it" },
          "runtime",
          sessionId,
        ),
        output: null,
      };
      // The backend lands the session row as interrupted, then the
      // frontend's aborted fetch tears the stream down: the next read
      // rejects with AbortError before any SSE cancellation event arrives.
      yield {
        kind: "session",
        session: makeSessionState(sessionId, "interrupted"),
        event: null,
        output: null,
      };
      await new Promise<never>((_, reject) => {
        signal?.addEventListener(
          "abort",
          () =>
            reject(
              new DOMException("The operation was aborted.", "AbortError"),
            ),
          { once: true },
        );
      });
    });
    runtimeClientMocks.cancelSessionMock.mockResolvedValue({
      session_id: sessionId,
      status: "interrupted",
      interrupted: true,
      cancelled: true,
      run_id: "run-1",
      reason: "web user interrupt",
    });
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      {
        session: { id: sessionId },
        status: "interrupted",
        turn: 1,
        prompt: "do it",
        updated_at: 1,
      },
    ]);

    renderApp();
    await flushAsync();

    let runPromise!: Promise<void>;
    await act(async () => {
      runPromise = useAppStore.getState().runTask("do it", WORKSPACE_PATH);
      await Promise.resolve();
    });
    await waitFor(() => {
      expect(useAppStore.getState().runStatus).toBe("running");
    });

    fireEvent.click(screen.getByRole("button", { name: "Stop generation" }));
    await act(async () => {
      await runPromise;
    });
    await flushAsync();

    // The torn-down stream is not a failure: no error banner, the run
    // settles to idle, and the session is the backend's authoritative
    // interrupted row (even though no SSE cancellation event was emitted).
    expect(useAppStore.getState().runStatus).toBe("idle");
    expect(useAppStore.getState().runError).toBeNull();
    expect(useAppStore.getState().currentSessionState?.status).toBe(
      "interrupted",
    );
    expect(screen.queryByText(/^Error:/)).not.toBeInTheDocument();
    expect(screen.queryByText("Failed")).not.toBeInTheDocument();
  });

  it("does not re-follow or re-select the session right after a locally completed run", async () => {
    const sessionId = "session-1";
    async function* stream() {
      yield {
        kind: "session",
        session: makeSessionState(sessionId, "running"),
        event: makeEvent(
          1,
          "runtime.request_received",
          { prompt: "do it" },
          "runtime",
          sessionId,
        ),
        output: null,
      };
      yield {
        kind: "session",
        session: makeSessionState(sessionId, "completed"),
        event: makeEvent(
          2,
          "graph.response_ready",
          { output: "done" },
          "graph",
          sessionId,
        ),
        output: null,
      };
    }
    runtimeClientMocks.runStreamMock.mockReturnValue(stream());
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      {
        session: { id: sessionId },
        status: "completed",
        turn: 1,
        prompt: "do it",
        updated_at: 1,
      },
    ]);

    renderApp();
    await flushAsync();
    await act(async () => {
      await useAppStore.getState().runTask("do it", WORKSPACE_PATH);
    });
    await flushAsync();

    expect(useAppStore.getState().runStatus).toBe("success");
    expect(useAppStore.getState().currentSessionState?.status).toBe(
      "completed",
    );
    // The just-completed run already holds terminal data: the follow-stream
    // effect must not open a follow stream nor trigger a redundant full
    // reload (selectSession) for it.
    expect(runtimeClientMocks.sessionEventsMock).not.toHaveBeenCalled();
    expect(runtimeClientMocks.getSessionReplayMock).not.toHaveBeenCalled();
  });

  it("routes an Enter submit to steering while running instead of starting a new run", async () => {
    const sessionId = "session-parent";
    runtimeClientMocks.steerSessionMock.mockResolvedValue({
      session_id: sessionId,
      queued: 2,
    });
    useAppStore.setState({
      runStatus: "running",
      currentSessionId: sessionId,
    });

    renderApp();
    await flushAsync();

    const textarea = screen.getByPlaceholderText(
      "Ask VoidCode to do something...",
    );
    expect(textarea).not.toBeDisabled();
    fireEvent.change(textarea, { target: { value: "hold on" } });
    fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });

    await waitFor(() => {
      expect(runtimeClientMocks.steerSessionMock).toHaveBeenCalledWith(
        sessionId,
        "hold on",
      );
    });
    // A running session must queue via steer, never fire a new run stream.
    expect(runtimeClientMocks.runStreamMock).not.toHaveBeenCalled();
    await waitFor(() => {
      expect(screen.getByText("Queued (2)")).toBeInTheDocument();
    });
  });
  it("consumes replay after a terminal snapshot before refreshing an external run", async () => {
    let replayConsumed = false;
    runtimeClientMocks.getChildSessionContextMock.mockRejectedValue(
      NO_DELEGATED_CONTEXT,
    );
    const event = makeEvent(
      2,
      "graph.response_ready",
      { output: "completed externally" },
      "graph",
      "external-session",
    );
    runtimeClientMocks.sessionEventsMock.mockImplementation(async function* () {
      yield makeSnapshotChunk("external-session", "completed");
      replayConsumed = true;
      yield {
        kind: "event",
        session: makeSessionState("external-session", "completed"),
        event,
        output: null,
      };
    });
    runtimeClientMocks.getSessionReplayMock.mockResolvedValue(
      makeRuntimeResponse(
        "external-session",
        "completed",
        [event],
        "completed externally",
      ),
    );
    renderApp();
    await flushAsync();
    useAppStore.setState({
      currentSessionId: "external-session",
      currentSessionState: makeSessionState("external-session", "running"),
      currentSessionEvents: [],
      runStatus: "running",
      runOrigin: "external",
      replayStatus: "success",
    });
    await waitFor(() =>
      expect(useAppStore.getState().currentSessionState?.status).toBe(
        "completed",
      ),
    );
    expect(replayConsumed).toBe(true);
    expect(useAppStore.getState().currentSessionOutput).toBe(
      "completed externally",
    );
  });
});

function sequenceList(): number[] {
  return useAppStore
    .getState()
    .currentSessionEvents.map((event) => event.sequence);
}

function delegatedRequestCount(): number {
  return (
    runtimeClientMocks.listBackgroundTasksMock.mock.calls.length +
    runtimeClientMocks.listSessionBackgroundTasksMock.mock.calls.length +
    runtimeClientMocks.getBackgroundTaskOutputMock.mock.calls.length
  );
}

/**
 * Put the app in the state an externally started run leaves the client in:
 * selected session, non-terminal status, no local run in flight.
 */
async function startExternalRunSession(
  sessionId: string,
  events: EventEnvelope[],
) {
  // Settle mount effects (their own task/notification refreshes) so a test can
  // count only the requests the follow stream itself causes.
  await flushAsync();
  runtimeClientMocks.listBackgroundTasksMock.mockClear();
  runtimeClientMocks.listSessionBackgroundTasksMock.mockClear();
  runtimeClientMocks.getBackgroundTaskOutputMock.mockClear();
  useAppStore.setState({
    currentSessionId: sessionId,
    currentSessionState: makeSessionState(sessionId, "running"),
    currentSessionEvents: events,
    currentSessionOutput: null,
    runStatus: "running",
    runOrigin: "external",
    replayStatus: "success",
  });
}

describe("App session-event follow stream (push contract)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    resetStore();
    installDefaultRuntimeClientMocks();
    runtimeClientMocks.getChildSessionContextMock.mockRejectedValue(
      NO_DELEGATED_CONTEXT,
    );
  });

  it("renders pushed events while the stream is open and reconciles once at close", async () => {
    const held = createDeferred<void>();
    runtimeClientMocks.sessionEventsMock.mockImplementation(async function* () {
      yield makeSnapshotChunk("session-1", "running");
      yield {
        kind: "event",
        session: null,
        event: makeEvent(
          2,
          "graph.provider_stream",
          { channel: "text", text: "first pushed chunk" },
          "graph",
        ),
        output: null,
      };
      await held.promise;
      yield {
        kind: "event",
        session: null,
        event: makeEvent(
          3,
          "graph.provider_stream",
          { channel: "text", text: " second pushed chunk" },
          "graph",
        ),
        output: null,
      };
    });
    runtimeClientMocks.getSessionReplayMock.mockResolvedValue(
      makeRuntimeResponse(
        "session-1",
        "completed",
        [
          makeEvent(
            1,
            "runtime.request_received",
            { prompt: "do it" },
            "runtime",
          ),
          makeEvent(
            2,
            "graph.provider_stream",
            { channel: "text", text: "first pushed chunk" },
            "graph",
          ),
          makeEvent(
            3,
            "graph.provider_stream",
            { channel: "text", text: " second pushed chunk" },
            "graph",
          ),
          makeEvent(
            4,
            "runtime.completed",
            { output: "final output" },
            "runtime",
          ),
        ],
        "final output",
      ),
    );

    renderApp();
    await startExternalRunSession("session-1", [
      makeEvent(1, "runtime.request_received", { prompt: "do it" }, "runtime"),
    ]);

    // The pushed frame is rendered while the stream is still open: seeing it
    // needs no transcript refetch.
    await waitFor(() => {
      expect(screen.getByText(/first pushed chunk/)).toBeInTheDocument();
    });
    expect(runtimeClientMocks.getSessionReplayMock).not.toHaveBeenCalled();
    expect(sequenceList()).toEqual([1, 2]);
    expect(useAppStore.getState().currentSessionState?.status).toBe("running");

    // The stream closes: exactly one authoritative reconciliation follows.
    held.resolve();
    await waitFor(() => {
      expect(runtimeClientMocks.getSessionReplayMock).toHaveBeenCalledTimes(1);
    });
    await flushAsync();
    expect(useAppStore.getState().currentSessionState?.status).toBe(
      "completed",
    );
    expect(useAppStore.getState().currentSessionOutput).toBe("final output");
    expect(sequenceList()).toEqual([1, 2, 3, 4]);
    // The authoritative transcript replaced the incremental view.
    await waitFor(() => {
      expect(screen.getByText(/final output/)).toBeInTheDocument();
    });
  });

  it("does not append an event the transcript already holds", async () => {
    const held = createDeferred<void>();
    runtimeClientMocks.sessionEventsMock.mockImplementation(async function* () {
      yield makeSnapshotChunk("session-1", "running");
      // The cursor the stream resumes from can race a `selectSession` that
      // already delivered this exact event.
      yield {
        kind: "event",
        session: null,
        event: makeEvent(
          2,
          "graph.provider_stream",
          { channel: "text", text: "ALPHA" },
          "graph",
        ),
        output: null,
      };
      yield {
        kind: "event",
        session: null,
        event: makeEvent(
          3,
          "graph.provider_stream",
          { channel: "text", text: "BETA" },
          "graph",
        ),
        output: null,
      };
      await held.promise;
    });

    renderApp();
    await startExternalRunSession("session-1", [
      makeEvent(1, "runtime.request_received", { prompt: "do it" }, "runtime"),
      makeEvent(
        2,
        "graph.provider_stream",
        { channel: "text", text: "ALPHA" },
        "graph",
      ),
    ]);

    await waitFor(() => {
      expect(sequenceList()).toEqual([1, 2, 3]);
    });
    // Delivered once, not twice: the re-pushed event neither grew the
    // transcript nor duplicated its text.
    await waitFor(() => {
      expect(screen.getByText(/ALPHABETA/)).toBeInTheDocument();
    });
    expect(screen.queryByText(/ALPHAALPHA/)).toBeNull();
    held.resolve();
  });

  it("coalesces a burst of delegated frames into one task refresh", async () => {
    const held = createDeferred<void>();
    const delegatedEvents = Array.from({ length: 20 }, (_, index) =>
      makeEvent(
        index + 2,
        "runtime.background_task_progress",
        { task_id: "task-child", chunk: `chunk ${index}` },
        "runtime",
      ),
    );
    runtimeClientMocks.sessionEventsMock.mockImplementation(async function* () {
      yield makeSnapshotChunk("session-1", "running");
      for (const event of delegatedEvents) {
        yield { kind: "event", session: null, event, output: null };
      }
      await held.promise;
    });

    renderApp();
    await startExternalRunSession("session-1", [
      makeEvent(1, "runtime.request_received", { prompt: "do it" }, "runtime"),
    ]);

    await waitFor(() => {
      expect(sequenceList()).toHaveLength(1 + delegatedEvents.length);
    });
    // The whole burst coalesces instead of firing a request pair per frame.
    expect(delegatedRequestCount()).toBeLessThanOrEqual(2);

    // ...and the coalesced refresh lands while the stream stays open.
    await waitFor(() => {
      expect(
        runtimeClientMocks.listSessionBackgroundTasksMock,
      ).toHaveBeenCalled();
    });
    expect(delegatedRequestCount()).toBeLessThanOrEqual(2);
    held.resolve();
  });

  it("refreshes notifications once when a pushed frame enqueues one", async () => {
    const held = createDeferred<void>();
    runtimeClientMocks.sessionEventsMock.mockImplementation(async function* () {
      yield makeSnapshotChunk("session-1", "running");
      yield {
        kind: "event",
        session: null,
        event: makeEvent(
          2,
          "runtime.background_task_progress",
          { task_id: "task-child", chunk: "chunk" },
          "runtime",
        ),
        output: null,
      };
      yield {
        kind: "event",
        session: null,
        event: makeEvent(
          3,
          "runtime.background_task_notification_enqueued",
          { task_id: "task-child", notification_id: "notification-1" },
          "runtime",
        ),
        output: null,
      };
      await held.promise;
    });

    renderApp();
    await startExternalRunSession("session-1", [
      makeEvent(1, "runtime.request_received", { prompt: "do it" }, "runtime"),
    ]);
    runtimeClientMocks.listNotificationsMock.mockClear();

    // The notification list follows the pushed frame (it used to only refresh
    // when the run ended), inside the same coalesced window as the task list.
    await waitFor(() => {
      expect(runtimeClientMocks.listNotificationsMock).toHaveBeenCalledTimes(1);
    });
    expect(delegatedRequestCount()).toBeLessThanOrEqual(2);
    held.resolve();
  });

  it("keeps the delegated child view when frames are pushed for it", async () => {
    runtimeClientMocks.sessionEventsMock.mockImplementation(async function* (
      sessionId: string,
    ) {
      yield makeSnapshotChunk(sessionId, "running");
      yield {
        kind: "event",
        session: null,
        event: makeEvent(
          3,
          "graph.provider_stream",
          { channel: "text", text: "live child chunk" },
          "graph",
          sessionId,
        ),
        output: null,
      };
    });

    renderApp();
    await browseParentSession();
    // Override the child behavior after browsing the parent
    // (browseParentSession owns the parent-side implementation).
    runtimeClientMocks.getChildSessionContextMock.mockImplementation(
      (sessionId: string) =>
        sessionId === "child-session"
          ? Promise.resolve(makeChildOutput("running"))
          : Promise.reject(NO_DELEGATED_CONTEXT),
    );
    const parentReplayCalls =
      runtimeClientMocks.getSessionReplayMock.mock.calls.length;

    await act(async () => {
      await useAppStore
        .getState()
        .selectSession("child-session", WORKSPACE_PATH);
    });
    await waitFor(() => {
      expect(useAppStore.getState().selectedBackgroundTaskOutputId).toBe(
        "task-child",
      );
    });
    await flushAsync();

    // Pushed frames for the child never overwrite the child context view, and
    // the stream settling never yanks the user back to the parent session.
    expect(useAppStore.getState().currentSessionId).toBe("child-session");
    expect(useAppStore.getState().childSessionParentId).toBe("session-parent");
    expect(useAppStore.getState().selectedBackgroundTaskOutputId).toBe(
      "task-child",
    );
    expect(taskOutput()?.output).toBe("child output");
    // Where a pushed frame for the selected session lands is the store's
    // business and is pinned there; what this view guarantees is that the child
    // context still owns the transcript on screen (asserted above and below).
    expect(runtimeClientMocks.getSessionReplayMock).toHaveBeenCalledTimes(
      parentReplayCalls,
    );
    // The child view is refreshed in place instead.
    expect(runtimeClientMocks.getBackgroundTaskOutputMock).toHaveBeenCalledWith(
      "task-child",
      expect.anything(),
    );
    expect(screen.getByText(/child output/)).toBeInTheDocument();
  });
});
