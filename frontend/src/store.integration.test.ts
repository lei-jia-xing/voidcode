import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  ApprovalDecision,
  BackgroundTaskSummary,
  EventEnvelope,
  QuestionAnswer,
  RuntimeNotification,
  RuntimeResponse,
  RuntimeSessionDebugSnapshot,
  RuntimeStatusSnapshot,
  RuntimeStreamChunk,
  RuntimeSettings,
  SessionState,
  StoredSessionSummary,
} from "./lib/runtime/types";

type PersistedState = {
  state: {
    language: "en" | "zh-CN";
    currentSessionId: string | null;
    agentPreset?: "leader";
    providerModel?: string;
  };
  version: number;
};

type StorageLike = {
  getItem: (key: string) => string | null;
  setItem: (key: string, value: string) => void;
  removeItem: (key: string) => void;
  clear: () => void;
};

const storageData = new Map<string, string>();
const testStorage: StorageLike = {
  getItem: (key) => storageData.get(key) ?? null,
  setItem: (key, value) => {
    storageData.set(key, value);
  },
  removeItem: (key) => {
    storageData.delete(key);
  },
  clear: () => {
    storageData.clear();
  },
};

Object.defineProperty(globalThis, "localStorage", {
  value: testStorage,
  configurable: true,
});

let useAppStore: typeof import("./store").useAppStore;

const emptyStatusSnapshot: RuntimeStatusSnapshot = {
  git: { state: "git_ready", root: "/workspace", error: null },
  lsp: { state: "stopped", error: null, details: {} },
  mcp: { state: "stopped", error: null, details: {} },
  acp: { state: "unconfigured", error: null, details: {} },
};

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

function makeStoredSessionSummary(
  sessionId: string,
  status: StoredSessionSummary["status"],
  prompt: string,
): StoredSessionSummary {
  return {
    session: { id: sessionId },
    status,
    turn: 1,
    prompt,
    updated_at: 1,
  };
}

function makeBackgroundTaskSummary(
  taskId: string,
  prompt: string,
): BackgroundTaskSummary {
  return {
    task: { id: taskId },
    status: "running",
    prompt,
    session_id: "session-1",
    error: null,
    created_at: 1,
    updated_at: 1,
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

function makeStreamChunk(
  sessionId: string,
  status: SessionState["status"],
  event: EventEnvelope | null,
  output: string | null = null,
): RuntimeStreamChunk {
  return {
    kind: output === null ? "event" : "output",
    session: makeSessionState(sessionId, status),
    event,
    output,
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

const runtimeClientMocks = vi.hoisted(() => ({
  openWorkspaceMock:
    vi.fn<() => Promise<{ current: null; recent: []; candidates: [] }>>(),
  listProvidersMock: vi.fn<() => Promise<[]>>(),
  listProviderModelsMock:
    vi.fn<
      () => Promise<{ provider: string; configured: boolean; models: [] }>
    >(),
  listAgentsMock: vi.fn<() => Promise<[]>>(),
  listSessionsMock: vi.fn<() => Promise<StoredSessionSummary[]>>(),
  getSessionReplayMock:
    vi.fn<(sessionId: string) => Promise<RuntimeResponse>>(),
  getStatusMock: vi.fn<() => Promise<RuntimeStatusSnapshot>>(),
  retryMcpConnectionsMock: vi.fn<() => Promise<RuntimeStatusSnapshot>>(),
  getReviewMock: vi.fn<
    () => Promise<{
      root: string;
      git: { state: string };
      changed_files: [];
      tree: [];
    }>
  >(),
  resolveApprovalMock:
    vi.fn<
      (
        sessionId: string,
        requestId: string,
        decision: ApprovalDecision,
      ) => Promise<RuntimeResponse>
    >(),
  answerQuestionMock:
    vi.fn<
      (
        sessionId: string,
        requestId: string,
        responses: QuestionAnswer[],
      ) => Promise<RuntimeResponse>
    >(),
  listNotificationsMock: vi.fn<() => Promise<RuntimeNotification[]>>(),
  acknowledgeNotificationMock:
    vi.fn<(notificationId: string) => Promise<RuntimeNotification>>(),
  listBackgroundTasksMock: vi.fn<() => Promise<BackgroundTaskSummary[]>>(),
  listSessionBackgroundTasksMock:
    vi.fn<(sessionId: string) => Promise<BackgroundTaskSummary[]>>(),
  cancelBackgroundTaskMock: vi.fn<(taskId: string) => Promise<unknown>>(),
  getSessionDebugMock:
    vi.fn<(sessionId: string) => Promise<RuntimeSessionDebugSnapshot>>(),
  getSettingsMock: vi.fn<() => Promise<RuntimeSettings>>(),
  updateSettingsMock:
    vi.fn<(settings: Record<string, unknown>) => Promise<RuntimeSettings>>(),
  validateProviderCredentialsMock: vi.fn<
    (providerName: string) => Promise<{
      provider: string;
      configured: boolean;
      ok: boolean;
      status: string;
      message: string;
    }>
  >(),
  runStreamMock:
    vi.fn<
      (request: {
        prompt: string;
        session_id?: string | null;
        metadata?: Record<string, unknown>;
      }) => AsyncGenerator<RuntimeStreamChunk, void, unknown>
    >(),
}));

vi.mock("./lib/runtime/client", () => ({
  RuntimeClient: {
    openWorkspace: runtimeClientMocks.openWorkspaceMock,
    listProviders: runtimeClientMocks.listProvidersMock,
    listProviderModels: runtimeClientMocks.listProviderModelsMock,
    listAgents: runtimeClientMocks.listAgentsMock,
    listSessions: runtimeClientMocks.listSessionsMock,
    getSessionReplay: runtimeClientMocks.getSessionReplayMock,
    getStatus: runtimeClientMocks.getStatusMock,
    retryMcpConnections: runtimeClientMocks.retryMcpConnectionsMock,
    getReview: runtimeClientMocks.getReviewMock,
    resolveApproval: runtimeClientMocks.resolveApprovalMock,
    answerQuestion: runtimeClientMocks.answerQuestionMock,
    listNotifications: runtimeClientMocks.listNotificationsMock,
    acknowledgeNotification: runtimeClientMocks.acknowledgeNotificationMock,
    listBackgroundTasks: runtimeClientMocks.listBackgroundTasksMock,
    listSessionBackgroundTasks:
      runtimeClientMocks.listSessionBackgroundTasksMock,
    cancelBackgroundTask: runtimeClientMocks.cancelBackgroundTaskMock,
    getSessionDebug: runtimeClientMocks.getSessionDebugMock,
    getSettings: runtimeClientMocks.getSettingsMock,
    updateSettings: runtimeClientMocks.updateSettingsMock,
    validateProviderCredentials:
      runtimeClientMocks.validateProviderCredentialsMock,
    runStream: runtimeClientMocks.runStreamMock,
  },
}));

describe("useAppStore integration flow", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    localStorage.clear();
    vi.resetModules();
    ({ useAppStore } = await import("./store"));
    useAppStore.setState({
      language: "en",
      agentPreset: "leader",
      providerModel: "opencode-go/glm-5.1",
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
      sessions: [],
      currentSessionId: null,
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
    runtimeClientMocks.openWorkspaceMock.mockResolvedValue({
      current: null,
      recent: [],
      candidates: [],
    });
    runtimeClientMocks.listProvidersMock.mockResolvedValue([]);
    runtimeClientMocks.listProviderModelsMock.mockResolvedValue({
      provider: "opencode-go",
      configured: true,
      models: [],
    });
    runtimeClientMocks.listAgentsMock.mockResolvedValue([]);
    runtimeClientMocks.listSessionsMock.mockResolvedValue([]);
    runtimeClientMocks.getStatusMock.mockResolvedValue(emptyStatusSnapshot);
    runtimeClientMocks.retryMcpConnectionsMock.mockResolvedValue(
      emptyStatusSnapshot,
    );
    runtimeClientMocks.getReviewMock.mockResolvedValue({
      root: "/workspace",
      git: { state: "git_ready" },
      changed_files: [],
      tree: [],
    });
    runtimeClientMocks.getSettingsMock.mockResolvedValue({});
    runtimeClientMocks.updateSettingsMock.mockResolvedValue({});
    runtimeClientMocks.listNotificationsMock.mockResolvedValue([]);
    runtimeClientMocks.listBackgroundTasksMock.mockResolvedValue([]);
    runtimeClientMocks.listSessionBackgroundTasksMock.mockResolvedValue([]);
    runtimeClientMocks.cancelBackgroundTaskMock.mockResolvedValue({});
    runtimeClientMocks.getSessionDebugMock.mockResolvedValue({
      session: makeSessionState("session-1", "completed"),
      prompt: "read README.md",
      persisted_status: "completed",
      current_status: "completed",
      active: false,
      resumable: false,
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
    runtimeClientMocks.validateProviderCredentialsMock.mockResolvedValue({
      provider: "opencode-go",
      configured: true,
      ok: true,
      status: "ok",
      message: "Remote provider validation succeeded.",
    });
  });

  it("handles run -> waiting approval -> allow -> replay through the real store", async () => {
    const sessionId = "session-1";
    const requestId = "approval-1";
    const requestReceived = makeEvent(1, "runtime.request_received", {
      prompt: "write note.txt hello",
    });
    const approvalRequested = makeEvent(
      2,
      "runtime.approval_requested",
      {
        request_id: requestId,
        tool: "write_file",
        target_summary: "note.txt",
        decision: "ask",
      },
      "runtime",
      sessionId,
    );
    const approvalResolved = makeEvent(
      3,
      "runtime.approval_resolved",
      { request_id: requestId, decision: "allow" },
      "runtime",
      sessionId,
    );
    const toolCompleted = makeEvent(
      4,
      "runtime.tool_completed",
      { path: "note.txt" },
      "tool",
      sessionId,
    );
    const responseReady = makeEvent(
      5,
      "graph.response_ready",
      { output_preview: "hello" },
      "graph",
      sessionId,
    );
    const completedResponse = makeRuntimeResponse(
      sessionId,
      "completed",
      [
        requestReceived,
        approvalRequested,
        approvalResolved,
        toolCompleted,
        responseReady,
      ],
      "hello",
    );

    async function* stream() {
      yield makeStreamChunk(sessionId, "running", requestReceived);
      yield makeStreamChunk(sessionId, "waiting", approvalRequested);
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());
    runtimeClientMocks.resolveApprovalMock.mockResolvedValue(completedResponse);
    runtimeClientMocks.getSessionReplayMock.mockResolvedValue(
      completedResponse,
    );
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary(sessionId, "completed", "write note.txt hello"),
    ]);

    const store = useAppStore.getState();
    await store.runTask("write note.txt hello");

    let state = useAppStore.getState();
    expect(state.currentSessionId).toBe(sessionId);
    expect(state.currentSessionState?.status).toBe("waiting");
    expect(state.currentSessionEvents.map((event) => event.event_type)).toEqual(
      ["runtime.request_received", "runtime.approval_requested"],
    );
    expect(state.runStatus).toBe("success");

    await state.resolveApproval("allow");

    state = useAppStore.getState();
    expect(runtimeClientMocks.resolveApprovalMock).toHaveBeenCalledWith(
      sessionId,
      requestId,
      "allow",
    );
    expect(state.currentSessionState?.status).toBe("completed");
    expect(state.currentSessionOutput).toBe("hello");
    expect(state.currentSessionEvents.map((event) => event.event_type)).toEqual(
      [
        "runtime.request_received",
        "runtime.approval_requested",
        "runtime.approval_resolved",
        "runtime.tool_completed",
        "graph.response_ready",
      ],
    );
    expect(state.sessions).toEqual([
      makeStoredSessionSummary(sessionId, "completed", "write note.txt hello"),
    ]);

    await state.selectSession(sessionId);

    state = useAppStore.getState();
    expect(runtimeClientMocks.getSessionReplayMock).toHaveBeenCalledWith(
      sessionId,
    );
    expect(state.currentSessionState?.status).toBe("completed");
    expect(state.currentSessionOutput).toBe("hello");
    expect(state.currentSessionEvents).toEqual(completedResponse.events);
  });

  it("handles run -> waiting question -> answer through the real store", async () => {
    const sessionId = "session-question";
    const requestId = "question-1";
    const requestReceived = makeEvent(
      1,
      "runtime.request_received",
      { prompt: "ask a direction" },
      "runtime",
      sessionId,
    );
    const questionRequested = makeEvent(
      2,
      "runtime.question_requested",
      {
        request_id: requestId,
        tool: "question",
        question_count: 1,
        questions: [
          {
            header: "Direction",
            question: "Which path?",
            multiple: false,
            options: [],
          },
        ],
      },
      "runtime",
      sessionId,
    );
    const questionAnswered = makeEvent(
      3,
      "runtime.question_answered",
      { request_id: requestId },
      "runtime",
      sessionId,
    );
    const responseReady = makeEvent(
      4,
      "graph.response_ready",
      { output: "continued" },
      "graph",
      sessionId,
    );
    const completedResponse = makeRuntimeResponse(
      sessionId,
      "completed",
      [requestReceived, questionRequested, questionAnswered, responseReady],
      "continued",
    );

    async function* stream() {
      yield makeStreamChunk(sessionId, "running", requestReceived);
      yield makeStreamChunk(sessionId, "waiting", questionRequested);
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());
    runtimeClientMocks.answerQuestionMock.mockResolvedValue(completedResponse);
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary(sessionId, "completed", "ask a direction"),
    ]);

    const store = useAppStore.getState();
    await store.runTask("ask a direction");

    let state = useAppStore.getState();
    expect(state.runError).toBeNull();
    expect(state.currentSessionState?.status).toBe("waiting");
    await state.answerQuestion([{ header: "Direction", answers: ["left"] }]);

    state = useAppStore.getState();
    expect(runtimeClientMocks.answerQuestionMock).toHaveBeenCalledWith(
      sessionId,
      requestId,
      [{ header: "Direction", answers: ["left"] }],
    );
    expect(state.currentSessionState?.status).toBe("completed");
    expect(state.currentSessionOutput).toBe("continued");
  });

  it("handles deny and preserves failed replay through the real store", async () => {
    const sessionId = "session-deny";
    const requestId = "approval-deny";
    const requestReceived = makeEvent(
      1,
      "runtime.request_received",
      { prompt: "write nope.txt later" },
      "runtime",
      sessionId,
    );
    const approvalRequested = makeEvent(
      2,
      "runtime.approval_requested",
      {
        request_id: requestId,
        tool: "write_file",
        target_summary: "nope.txt",
        decision: "ask",
      },
      "runtime",
      sessionId,
    );
    const approvalResolved = makeEvent(
      3,
      "runtime.approval_resolved",
      { request_id: requestId, decision: "deny" },
      "runtime",
      sessionId,
    );
    const failedEvent = makeEvent(
      4,
      "runtime.failed",
      { error: "permission denied" },
      "runtime",
      sessionId,
    );
    const failedResponse = makeRuntimeResponse(
      sessionId,
      "failed",
      [requestReceived, approvalRequested, approvalResolved, failedEvent],
      null,
    );

    async function* stream() {
      yield makeStreamChunk(sessionId, "running", requestReceived);
      yield makeStreamChunk(sessionId, "waiting", approvalRequested);
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());
    runtimeClientMocks.resolveApprovalMock.mockResolvedValue(failedResponse);
    runtimeClientMocks.getSessionReplayMock.mockResolvedValue(failedResponse);
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary(sessionId, "failed", "write nope.txt later"),
    ]);

    await useAppStore.getState().runTask("write nope.txt later");
    await useAppStore.getState().resolveApproval("deny");

    const state = useAppStore.getState();
    expect(state.currentSessionState?.status).toBe("failed");
    expect(state.currentSessionOutput).toBeNull();
    expect(state.currentSessionEvents.map((event) => event.event_type)).toEqual(
      [
        "runtime.request_received",
        "runtime.approval_requested",
        "runtime.approval_resolved",
        "runtime.failed",
      ],
    );

    await state.selectSession(sessionId);

    expect(useAppStore.getState().currentSessionEvents).toEqual(
      failedResponse.events,
    );
  });

  it("hydrates currentSessionId and replays the persisted session on load, and preserves configuration state", async () => {
    const sessionId = "persisted-session";
    const replay = makeRuntimeResponse(
      sessionId,
      "completed",
      [
        makeEvent(
          1,
          "runtime.request_received",
          { prompt: "read note.txt" },
          "runtime",
          sessionId,
        ),
      ],
      "note body",
    );

    const persisted: PersistedState = {
      state: {
        language: "zh-CN",
        currentSessionId: sessionId,
        agentPreset: "leader",
        providerModel: "test-model/v1",
      },
      version: 0,
    };
    localStorage.setItem("app-storage", JSON.stringify(persisted));

    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary(sessionId, "completed", "read note.txt"),
    ]);
    runtimeClientMocks.getSessionReplayMock.mockResolvedValue(replay);

    await useAppStore.persist.rehydrate();
    await useAppStore.getState().loadSessions();
    await useAppStore.getState().selectSession(sessionId);

    const state = useAppStore.getState();
    expect(state.language).toBe("zh-CN");
    expect(state.currentSessionId).toBe(sessionId);
    expect(state.agentPreset).toBe("leader");
    expect(state.providerModel).toBe("test-model/v1");
    expect(state.currentSessionState?.status).toBe("completed");
    expect(state.currentSessionOutput).toBe("note body");
    expect(runtimeClientMocks.getSessionReplayMock).toHaveBeenCalledWith(
      sessionId,
    );
  });

  it("falls back to no active session if persisted session is stale", async () => {
    const sessionId = "stale-session";

    const persisted: PersistedState = {
      state: {
        language: "zh-CN",
        currentSessionId: sessionId,
        agentPreset: "leader",
        providerModel: "test-model/v1",
      },
      version: 0,
    };
    localStorage.setItem("app-storage", JSON.stringify(persisted));

    runtimeClientMocks.listSessionsMock.mockResolvedValue([]);
    runtimeClientMocks.getSessionReplayMock.mockRejectedValue(
      new Error("Not Found"),
    );

    await useAppStore.persist.rehydrate();

    let state = useAppStore.getState();
    expect(state.currentSessionId).toBe(sessionId);

    await useAppStore.getState().loadSessions();

    state = useAppStore.getState();
    expect(state.currentSessionId).toBeNull();
    expect(state.replayError).toBeNull();

    await useAppStore.getState().selectSession(sessionId);

    state = useAppStore.getState();
    expect(state.currentSessionId).toBeNull();
    expect(state.replayError).toBeNull();
  });

  it("reloads runtime ops data after switching workspaces", async () => {
    const notification: RuntimeNotification = {
      id: "notification-1",
      session: { id: "session-1" },
      kind: "completion",
      status: "unread",
      summary: "Task completed",
      event_sequence: 1,
      created_at: 1,
      acknowledged_at: null,
      payload: {},
    };
    const task = makeBackgroundTaskSummary("task-1", "inspect workspace");
    runtimeClientMocks.listNotificationsMock.mockResolvedValue([notification]);
    runtimeClientMocks.listBackgroundTasksMock.mockResolvedValue([task]);

    await useAppStore.getState().switchWorkspace("/new-workspace");

    const state = useAppStore.getState();
    expect(runtimeClientMocks.listNotificationsMock).toHaveBeenCalled();
    expect(runtimeClientMocks.listBackgroundTasksMock).toHaveBeenCalled();
    expect(state.notifications).toEqual([notification]);
    expect(state.backgroundTasks).toEqual([task]);
  });

  it("refreshes session-scoped background tasks after selecting a session", async () => {
    const firstTask = makeBackgroundTaskSummary("task-a", "old session task");
    const secondTask = makeBackgroundTaskSummary(
      "task-b",
      "selected session task",
    );
    const replay = makeRuntimeResponse(
      "session-2",
      "completed",
      [
        makeEvent(
          1,
          "runtime.request_received",
          { prompt: "read selected.txt" },
          "runtime",
          "session-2",
        ),
      ],
      "selected",
    );
    useAppStore.setState({
      currentSessionId: "session-1",
      backgroundTasks: [firstTask],
    });
    runtimeClientMocks.getSessionReplayMock.mockResolvedValue(replay);
    runtimeClientMocks.listSessionBackgroundTasksMock.mockResolvedValue([
      secondTask,
    ]);

    await useAppStore.getState().selectSession("session-2");

    const state = useAppStore.getState();
    expect(
      runtimeClientMocks.listSessionBackgroundTasksMock,
    ).toHaveBeenCalledWith("session-2");
    expect(state.backgroundTasks).toEqual([secondTask]);
  });

  it("surfaces approval lookup failure when no pending request exists", async () => {
    const sessionId = "broken-session";
    const requestReceived = makeEvent(
      1,
      "runtime.request_received",
      { prompt: "write later" },
      "runtime",
      sessionId,
    );

    async function* stream() {
      yield makeStreamChunk(sessionId, "running", requestReceived);
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary(sessionId, "running", "write later"),
    ]);

    await useAppStore.getState().runTask("write later");
    await useAppStore.getState().resolveApproval("allow");

    const state = useAppStore.getState();
    expect(runtimeClientMocks.resolveApprovalMock).not.toHaveBeenCalled();
    expect(state.approvalStatus).toBe("error");
    expect(state.approvalError).toBe("No pending approval request found.");
  });

  it("keeps run status running while the stream is still open", async () => {
    const gate = createDeferred<void>();
    const sessionId = "slow-session";
    const requestReceived = makeEvent(
      1,
      "runtime.request_received",
      { prompt: "read slow.txt" },
      "runtime",
      sessionId,
    );

    async function* stream() {
      yield makeStreamChunk(sessionId, "running", requestReceived);
      await gate.promise;
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());

    const runPromise = useAppStore.getState().runTask("read slow.txt");
    await Promise.resolve();
    await Promise.resolve();

    expect(useAppStore.getState().runStatus).toBe("running");

    gate.resolve();
    await runPromise;

    expect(useAppStore.getState().runStatus).toBe("success");
  });

  it("passes runtime metadata through runTask options including store config defaults", async () => {
    const sessionId = "session-meta";
    const requestReceived = makeEvent(
      1,
      "runtime.request_received",
      { prompt: "analyze repo" },
      "runtime",
      sessionId,
    );

    async function* stream() {
      yield makeStreamChunk(sessionId, "completed", requestReceived);
      yield makeStreamChunk(sessionId, "completed", null, "ok");
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary(sessionId, "completed", "analyze repo"),
    ]);

    await useAppStore.getState().runTask("analyze repo", {
      metadata: {
        skills: ["demo"],
        max_steps: 5,
        provider_stream: true,
      },
    });

    expect(runtimeClientMocks.runStreamMock).toHaveBeenCalledWith({
      prompt: "analyze repo",
      session_id: null,
      metadata: {
        skills: ["demo"],
        max_steps: 5,
        provider_stream: true,
        agent: {
          preset: "leader",
          model: "opencode-go/glm-5.1",
          execution_engine: "provider",
        },
      },
    });
    expect(runtimeClientMocks.getStatusMock).toHaveBeenCalled();
    expect(runtimeClientMocks.getReviewMock).toHaveBeenCalled();
  });

  it("uses a 16 step budget for web agent runs when no override is provided", async () => {
    const sessionId = "session-default-steps";
    const requestReceived = makeEvent(
      1,
      "runtime.request_received",
      { prompt: "write hello.c" },
      "runtime",
      sessionId,
    );

    async function* stream() {
      yield makeStreamChunk(sessionId, "completed", requestReceived);
      yield makeStreamChunk(sessionId, "completed", null, "ok");
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary(sessionId, "completed", "write hello.c"),
    ]);

    await useAppStore.getState().runTask("write hello.c");

    expect(runtimeClientMocks.runStreamMock).toHaveBeenCalledWith({
      prompt: "write hello.c",
      session_id: null,
      metadata: {
        max_steps: 16,
        agent: {
          preset: "leader",
          model: "opencode-go/glm-5.1",
          execution_engine: "provider",
        },
      },
    });
  });

  it("normalizes a bare alias only when the current provider catalog owns it", async () => {
    const sessionId = "session-current-provider-match";
    const requestReceived = makeEvent(
      1,
      "runtime.request_received",
      { prompt: "run current provider alias" },
      "runtime",
      sessionId,
    );

    async function* stream() {
      yield makeStreamChunk(sessionId, "completed", requestReceived);
      yield makeStreamChunk(sessionId, "completed", null, "ok");
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary(
        sessionId,
        "completed",
        "run current provider alias",
      ),
    ]);
    useAppStore.setState({
      providerModel: "kimi-k2.6",
      providers: [
        {
          name: "opencode-go",
          label: "OpenCode Go",
          configured: true,
          current: true,
        },
        { name: "kimi", label: "Kimi", configured: true, current: false },
      ],
      providerModels: {
        "opencode-go": {
          provider: "opencode-go",
          configured: true,
          models: ["kimi-k2.6"],
        },
        kimi: {
          provider: "kimi",
          configured: true,
          models: ["kimi-k2.6"],
        },
      },
    });

    await useAppStore.getState().runTask("run current provider alias");

    expect(runtimeClientMocks.runStreamMock).toHaveBeenCalledWith({
      prompt: "run current provider alias",
      session_id: null,
      metadata: {
        max_steps: 16,
        agent: {
          preset: "leader",
          model: "opencode-go/kimi-k2.6",
          execution_engine: "provider",
        },
      },
    });
  });

  it("uses a unique catalog match when the current provider does not own the bare alias", async () => {
    const sessionId = "session-unique-provider-match";
    const requestReceived = makeEvent(
      1,
      "runtime.request_received",
      { prompt: "run unique alias" },
      "runtime",
      sessionId,
    );

    async function* stream() {
      yield makeStreamChunk(sessionId, "completed", requestReceived);
      yield makeStreamChunk(sessionId, "completed", null, "ok");
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary(sessionId, "completed", "run unique alias"),
    ]);
    useAppStore.setState({
      providerModel: "kimi-k2.6",
      providers: [
        {
          name: "opencode-go",
          label: "OpenCode Go",
          configured: true,
          current: true,
        },
        { name: "kimi", label: "Kimi", configured: true, current: false },
      ],
      providerModels: {
        "opencode-go": {
          provider: "opencode-go",
          configured: true,
          models: ["glm-5.1"],
        },
        kimi: {
          provider: "kimi",
          configured: true,
          models: ["kimi-k2.6"],
        },
      },
    });

    await useAppStore.getState().runTask("run unique alias");

    expect(runtimeClientMocks.runStreamMock).toHaveBeenCalledWith({
      prompt: "run unique alias",
      session_id: null,
      metadata: {
        max_steps: 16,
        agent: {
          preset: "leader",
          model: "kimi/kimi-k2.6",
          execution_engine: "provider",
        },
      },
    });
  });

  it("leaves an already-qualified model reference unchanged", async () => {
    const sessionId = "session-qualified-model";
    const requestReceived = makeEvent(
      1,
      "runtime.request_received",
      { prompt: "run qualified alias" },
      "runtime",
      sessionId,
    );

    async function* stream() {
      yield makeStreamChunk(sessionId, "completed", requestReceived);
      yield makeStreamChunk(sessionId, "completed", null, "ok");
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary(sessionId, "completed", "run qualified alias"),
    ]);
    useAppStore.setState({
      providerModel: "kimi/kimi-k2.6",
      providers: [
        {
          name: "opencode-go",
          label: "OpenCode Go",
          configured: true,
          current: true,
        },
        { name: "kimi", label: "Kimi", configured: true, current: false },
      ],
      providerModels: {
        "opencode-go": {
          provider: "opencode-go",
          configured: true,
          models: ["glm-5.1"],
        },
        kimi: {
          provider: "kimi",
          configured: true,
          models: ["kimi-k2.6"],
        },
      },
    });

    await useAppStore.getState().runTask("run qualified alias");

    expect(runtimeClientMocks.runStreamMock).toHaveBeenCalledWith({
      prompt: "run qualified alias",
      session_id: null,
      metadata: {
        max_steps: 16,
        agent: {
          preset: "leader",
          model: "kimi/kimi-k2.6",
          execution_engine: "provider",
        },
      },
    });
  });

  it("leaves a bare alias unchanged when provider ownership is ambiguous or unknown", async () => {
    const sessionId = "session-ambiguous-provider-match";
    const requestReceived = makeEvent(
      1,
      "runtime.request_received",
      { prompt: "run ambiguous alias" },
      "runtime",
      sessionId,
    );

    async function* stream() {
      yield makeStreamChunk(sessionId, "completed", requestReceived);
      yield makeStreamChunk(sessionId, "completed", null, "ok");
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary(sessionId, "completed", "run ambiguous alias"),
    ]);
    useAppStore.setState({
      providerModel: "kimi-k2.6",
      providers: [
        {
          name: "opencode-go",
          label: "OpenCode Go",
          configured: true,
          current: true,
        },
        { name: "kimi", label: "Kimi", configured: true, current: false },
        { name: "glm", label: "GLM", configured: true, current: false },
      ],
      providerModels: {
        "opencode-go": {
          provider: "opencode-go",
          configured: true,
          models: ["glm-5.1"],
        },
        kimi: {
          provider: "kimi",
          configured: true,
          models: ["kimi-k2.6"],
        },
        glm: {
          provider: "glm",
          configured: true,
          models: ["kimi-k2.6"],
        },
      },
    });

    await useAppStore.getState().runTask("run ambiguous alias");

    expect(runtimeClientMocks.runStreamMock).toHaveBeenCalledWith({
      prompt: "run ambiguous alias",
      session_id: null,
      metadata: {
        max_steps: 16,
        agent: {
          preset: "leader",
          model: "kimi-k2.6",
          execution_engine: "provider",
        },
      },
    });
  });

  it("leaves an unknown bare alias unchanged", async () => {
    const sessionId = "session-unknown-provider-match";
    const requestReceived = makeEvent(
      1,
      "runtime.request_received",
      { prompt: "run unknown alias" },
      "runtime",
      sessionId,
    );

    async function* stream() {
      yield makeStreamChunk(sessionId, "completed", requestReceived);
      yield makeStreamChunk(sessionId, "completed", null, "ok");
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary(sessionId, "completed", "run unknown alias"),
    ]);
    useAppStore.setState({
      providerModel: "mystery-model",
      providers: [
        {
          name: "opencode-go",
          label: "OpenCode Go",
          configured: true,
          current: true,
        },
        { name: "kimi", label: "Kimi", configured: true, current: false },
      ],
      providerModels: {
        "opencode-go": {
          provider: "opencode-go",
          configured: true,
          models: ["glm-5.1"],
        },
        kimi: {
          provider: "kimi",
          configured: true,
          models: ["kimi-k2.6"],
        },
      },
    });

    await useAppStore.getState().runTask("run unknown alias");

    expect(runtimeClientMocks.runStreamMock).toHaveBeenCalledWith({
      prompt: "run unknown alias",
      session_id: null,
      metadata: {
        max_steps: 16,
        agent: {
          preset: "leader",
          model: "mystery-model",
          execution_engine: "provider",
        },
      },
    });
  });

  it("retries MCP connections and stores refreshed backend status", async () => {
    const retrySnapshot: RuntimeStatusSnapshot = {
      git: { state: "git_ready", root: "/workspace", error: null },
      lsp: { state: "running", error: null, details: {} },
      mcp: {
        state: "failed",
        error: "MCP[demo]: failed to start server",
        details: {
          retry_available: true,
          servers: [
            {
              server: "demo",
              status: "failed",
              stage: "startup",
              error: "MCP[demo]: failed to start server",
              retry_available: true,
            },
          ],
        },
      },
    };
    runtimeClientMocks.retryMcpConnectionsMock.mockResolvedValue(retrySnapshot);

    await useAppStore.getState().retryMcpConnections();

    const state = useAppStore.getState();
    expect(runtimeClientMocks.retryMcpConnectionsMock).toHaveBeenCalledOnce();
    expect(state.statusSnapshot).toEqual(retrySnapshot);
    expect(state.mcpRetryStatus).toBe("success");
    expect(state.mcpRetryError).toBeNull();
  });

  it("respects explicit null sessionId and starts a fresh run", async () => {
    const sessionId = "current-session";
    useAppStore.setState({ currentSessionId: sessionId });
    const requestReceived = makeEvent(
      1,
      "runtime.request_received",
      { prompt: "new run" },
      "runtime",
      "fresh-session",
    );

    async function* stream() {
      yield makeStreamChunk("fresh-session", "completed", requestReceived);
      yield makeStreamChunk("fresh-session", "completed", null, "ok");
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary("fresh-session", "completed", "new run"),
    ]);

    await useAppStore.getState().runTask("new run", { sessionId: null });

    expect(runtimeClientMocks.runStreamMock).toHaveBeenCalledWith({
      prompt: "new run",
      session_id: null,
      metadata: {
        max_steps: 16,
        agent: {
          preset: "leader",
          model: "opencode-go/glm-5.1",
          execution_engine: "provider",
        },
      },
    });
    expect(useAppStore.getState().currentSessionId).toBe("fresh-session");
  });

  it("uses explicit null sessionId to start a fresh run", async () => {
    const sessionId = "explicit-null-session";
    const requestReceived = makeEvent(
      1,
      "runtime.request_received",
      { prompt: "start new" },
      "runtime",
      sessionId,
    );

    async function* stream() {
      yield makeStreamChunk(sessionId, "completed", requestReceived);
      yield makeStreamChunk(sessionId, "completed", null, "ok");
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary(sessionId, "completed", "start new"),
    ]);

    useAppStore.setState({ currentSessionId: "previous-session" });

    await useAppStore.getState().runTask("start new", {
      sessionId: null,
    });

    expect(runtimeClientMocks.runStreamMock).toHaveBeenCalledWith({
      prompt: "start new",
      session_id: null,
      metadata: {
        max_steps: 16,
        agent: {
          preset: "leader",
          model: "opencode-go/glm-5.1",
          execution_engine: "provider",
        },
      },
    });
  });

  it("loads runtime-owned settings and syncs providerModel from returned model", async () => {
    runtimeClientMocks.getSettingsMock.mockResolvedValue({
      provider: "glm",
      provider_api_key_present: true,
      model: "glm/glm-5",
    });

    await useAppStore.getState().loadSettings();

    const state = useAppStore.getState();
    expect(runtimeClientMocks.getSettingsMock).toHaveBeenCalledOnce();
    expect(state.settings).toEqual({
      provider: "glm",
      provider_api_key_present: true,
      model: "glm/glm-5",
    });
    expect(state.providerModel).toBe("glm/glm-5");
  });

  it("uses the hydrated qualified settings model when providers still expose only a bare alias", async () => {
    const sessionId = "session-hydrated-qualified-model";
    const requestReceived = makeEvent(
      1,
      "runtime.request_received",
      { prompt: "hydrated qualified model" },
      "runtime",
      sessionId,
    );

    async function* stream() {
      yield makeStreamChunk(sessionId, "completed", requestReceived);
      yield makeStreamChunk(sessionId, "completed", null, "ok");
    }

    runtimeClientMocks.getSettingsMock.mockResolvedValue({
      provider: "opencode-go",
      provider_api_key_present: true,
      model: "opencode-go/kimi-k2.6",
    });
    runtimeClientMocks.runStreamMock.mockReturnValue(stream());
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary(
        sessionId,
        "completed",
        "hydrated qualified model",
      ),
    ]);
    useAppStore.setState({
      providerModel: "kimi-k2.6",
      providers: [
        {
          name: "opencode-go",
          label: "OpenCode Go",
          configured: true,
          current: true,
        },
      ],
      providerModels: {
        "opencode-go": {
          provider: "opencode-go",
          configured: true,
          models: [],
        },
      },
    });

    await useAppStore.getState().loadSettings();
    await useAppStore.getState().runTask("hydrated qualified model");

    expect(useAppStore.getState().providerModel).toBe("opencode-go/kimi-k2.6");
    expect(runtimeClientMocks.runStreamMock).toHaveBeenCalledWith({
      prompt: "hydrated qualified model",
      session_id: null,
      metadata: {
        max_steps: 16,
        agent: {
          preset: "leader",
          model: "opencode-go/kimi-k2.6",
          execution_engine: "provider",
        },
      },
    });
  });

  it("runs with the configured settings model even when the provider catalog is empty", async () => {
    const sessionId = "session-settings-model-fallback";
    const requestReceived = makeEvent(
      1,
      "runtime.request_received",
      { prompt: "use configured model" },
      "runtime",
      sessionId,
    );

    async function* stream() {
      yield makeStreamChunk(sessionId, "completed", requestReceived);
      yield makeStreamChunk(sessionId, "completed", null, "ok");
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary(sessionId, "completed", "use configured model"),
    ]);
    useAppStore.setState({
      providerModel: "opencode-go/kimi-k2.6",
      providers: [
        {
          name: "opencode-go",
          label: "OpenCode Go",
          configured: true,
          current: true,
        },
      ],
      providerModels: {
        "opencode-go": {
          provider: "opencode-go",
          configured: true,
          models: [],
        },
      },
    });

    await useAppStore.getState().runTask("use configured model");

    expect(runtimeClientMocks.runStreamMock).toHaveBeenCalledWith({
      prompt: "use configured model",
      session_id: null,
      metadata: {
        max_steps: 16,
        agent: {
          preset: "leader",
          model: "opencode-go/kimi-k2.6",
          execution_engine: "provider",
        },
      },
    });
  });

  it("updates runtime-owned settings without expecting provider_api_key in the response", async () => {
    runtimeClientMocks.updateSettingsMock.mockResolvedValue({
      provider: "opencode-go",
      provider_api_key_present: true,
      model: "opencode-go/glm-5.1",
    });

    await useAppStore.getState().updateSettings({
      provider: "opencode-go",
      provider_api_key: "secret-key",
      model: "opencode-go/glm-5.1",
    });

    const state = useAppStore.getState();
    expect(runtimeClientMocks.updateSettingsMock).toHaveBeenCalledWith({
      provider: "opencode-go",
      provider_api_key: "secret-key",
      model: "opencode-go/glm-5.1",
    });
    expect(state.settings).toEqual({
      provider: "opencode-go",
      provider_api_key_present: true,
      model: "opencode-go/glm-5.1",
    });
    expect(state.providerModel).toBe("opencode-go/glm-5.1");
  });

  it("records provider credential validation results by provider", async () => {
    runtimeClientMocks.validateProviderCredentialsMock.mockResolvedValue({
      provider: "opencode-go",
      configured: true,
      ok: false,
      status: "skipped",
      message:
        "Provider credentials are configured; remote validation is unavailable.",
    });

    await useAppStore.getState().validateProviderCredentials("opencode-go");

    const state = useAppStore.getState();
    expect(
      runtimeClientMocks.validateProviderCredentialsMock,
    ).toHaveBeenCalledWith("opencode-go");
    expect(state.providerValidationStatus["opencode-go"]).toBe("error");
    expect(state.providerValidationResults["opencode-go"]).toMatchObject({
      provider: "opencode-go",
      ok: false,
      status: "skipped",
    });
  });

  it("clears stale provider validation state after settings updates", async () => {
    runtimeClientMocks.updateSettingsMock.mockResolvedValue({
      provider: "opencode-go",
      provider_api_key_present: true,
      model: "opencode-go/glm-5.1",
    });
    useAppStore.setState({
      providerValidationResults: {
        "opencode-go": {
          provider: "opencode-go",
          configured: true,
          ok: true,
          status: "ok",
          message: "Remote provider validation succeeded.",
        },
      },
      providerValidationStatus: { "opencode-go": "success" },
      providerValidationError: { "opencode-go": null },
    });

    await useAppStore.getState().updateSettings({
      provider: "opencode-go",
      provider_api_key: "new-secret-key",
      model: "opencode-go/glm-5.1",
    });

    const state = useAppStore.getState();
    expect(state.providerValidationResults).toEqual({});
    expect(state.providerValidationStatus).toEqual({});
    expect(state.providerValidationError).toEqual({});
  });
});
