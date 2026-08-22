import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  ApprovalDecision,
  BackgroundTaskOutput,
  BackgroundTaskSummary,
  EventEnvelope,
  QuestionAnswer,
  ReviewFileDiff,
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
    childSessionParentId?: string | null;
    agentPreset?: "leader";
    providerModel?: string;
    sessionSidebarWidth?: number;
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
  listCommandsMock: vi.fn<() => Promise<[]>>(),
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
  getReviewDiffMock: vi.fn<(path: string) => Promise<ReviewFileDiff>>(),
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
  listBackgroundTasksMock: vi.fn<() => Promise<BackgroundTaskSummary[]>>(),
  listSessionBackgroundTasksMock:
    vi.fn<(sessionId: string) => Promise<BackgroundTaskSummary[]>>(),
  cancelSessionMock: vi.fn<(sessionId: string) => Promise<unknown>>(),
  getBackgroundTaskOutputMock:
    vi.fn<(taskId: string) => Promise<BackgroundTaskOutput>>(),
  getChildSessionContextMock:
    vi.fn<(sessionId: string) => Promise<BackgroundTaskOutput>>(),
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
  runStreamMock: vi.fn<
    (
      request: {
        prompt: string;
        session_id?: string | null;
        metadata?: Record<string, unknown>;
      },
      signal?: AbortSignal,
    ) => AsyncGenerator<RuntimeStreamChunk, void, unknown>
  >(),
}));

vi.mock("./lib/runtime/client", () => ({
  RuntimeClient: {
    openWorkspace: runtimeClientMocks.openWorkspaceMock,
    listProviders: runtimeClientMocks.listProvidersMock,
    listProviderModels: runtimeClientMocks.listProviderModelsMock,
    listAgents: runtimeClientMocks.listAgentsMock,
    listCommands: runtimeClientMocks.listCommandsMock,
    listSessions: runtimeClientMocks.listSessionsMock,
    getSessionReplay: runtimeClientMocks.getSessionReplayMock,
    getStatus: runtimeClientMocks.getStatusMock,
    retryMcpConnections: runtimeClientMocks.retryMcpConnectionsMock,
    getReview: runtimeClientMocks.getReviewMock,
    getReviewDiff: runtimeClientMocks.getReviewDiffMock,
    resolveApproval: runtimeClientMocks.resolveApprovalMock,
    answerQuestion: runtimeClientMocks.answerQuestionMock,
    listBackgroundTasks: runtimeClientMocks.listBackgroundTasksMock,
    listSessionBackgroundTasks:
      runtimeClientMocks.listSessionBackgroundTasksMock,
    cancelSession: runtimeClientMocks.cancelSessionMock,
    getBackgroundTaskOutput: runtimeClientMocks.getBackgroundTaskOutputMock,
    getChildSessionContext: runtimeClientMocks.getChildSessionContextMock,
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
      providerModel: "deepseek/deepseek-v4-pro",
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
      childSessionParentId: null,
      sessionSidebarWidth: 344,
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
    runtimeClientMocks.listCommandsMock.mockResolvedValue([]);
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
    runtimeClientMocks.getReviewDiffMock.mockResolvedValue({
      root: "/workspace",
      path: "README.md",
      state: "clean",
      diff: null,
    });
    runtimeClientMocks.getSettingsMock.mockResolvedValue({});
    runtimeClientMocks.updateSettingsMock.mockResolvedValue({});
    runtimeClientMocks.listBackgroundTasksMock.mockResolvedValue([]);
    runtimeClientMocks.listSessionBackgroundTasksMock.mockResolvedValue([]);
    runtimeClientMocks.cancelSessionMock.mockResolvedValue({
      session_id: "session-1",
      status: "interrupted",
      interrupted: true,
      cancelled: true,
      run_id: "run-1",
      reason: "web user interrupt",
    });
    runtimeClientMocks.getBackgroundTaskOutputMock.mockResolvedValue({
      task: {
        task_id: "task-1",
        status: "completed",
        parent_session_id: "session-1",
        requested_child_session_id: null,
        child_session_id: "child-session-1",
        approval_request_id: null,
        question_request_id: null,
        approval_blocked: false,
        summary_output: "summary",
        error: null,
        result_available: true,
        cancellation_cause: null,
        routing: { mode: "subagent", subagent_type: "explore" },
      },
      session_result: null,
      output: "output",
    });
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
      provider: "deepseek",
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
        tool: "write",
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

  it("acknowledges approval immediately while a resumed run is still resolving", async () => {
    const sessionId = "approval-slow-resume";
    const requestId = "approval-slow-1";
    const requestReceived = makeEvent(
      1,
      "runtime.request_received",
      { prompt: "write slow.txt hello" },
      "runtime",
      sessionId,
    );
    const approvalRequested = makeEvent(
      2,
      "runtime.approval_requested",
      {
        request_id: requestId,
        tool: "write",
        target_summary: "slow.txt",
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
    const toolStarted = makeEvent(
      4,
      "runtime.tool_started",
      { tool: "write", tool_call_id: "write-1" },
      "runtime",
      sessionId,
    );
    const responseReady = makeEvent(
      5,
      "graph.response_ready",
      { output_preview: "done" },
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
        toolStarted,
        responseReady,
      ],
      "done",
    );
    const slowApproval = createDeferred<RuntimeResponse>();

    async function* stream() {
      yield makeStreamChunk(sessionId, "running", requestReceived);
      yield makeStreamChunk(sessionId, "waiting", approvalRequested);
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());
    runtimeClientMocks.resolveApprovalMock.mockReturnValue(
      slowApproval.promise,
    );
    runtimeClientMocks.getSessionReplayMock.mockResolvedValue(
      completedResponse,
    );
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary(sessionId, "completed", "write slow.txt hello"),
    ]);

    await useAppStore.getState().runTask("write slow.txt hello");

    const approvalPromise = useAppStore.getState().resolveApproval("allow");
    await Promise.resolve();

    let state = useAppStore.getState();
    expect(runtimeClientMocks.resolveApprovalMock).toHaveBeenCalledWith(
      sessionId,
      requestId,
      "allow",
    );
    expect(state.approvalStatus).toBe("submitting");
    expect(state.approvalError).toBeNull();
    expect(state.runStatus).toBe("success");
    expect(state.currentSessionState?.status).toBe("waiting");
    expect(state.currentSessionEvents.map((event) => event.event_type)).toEqual(
      ["runtime.request_received", "runtime.approval_requested"],
    );

    slowApproval.resolve(completedResponse);
    await approvalPromise;

    state = useAppStore.getState();
    expect(state.approvalStatus).toBe("idle");
    expect(state.runStatus).toBe("idle");
    expect(state.currentSessionState?.status).toBe("completed");
    expect(state.currentSessionOutput).toBe("done");
    expect(state.currentSessionEvents).toEqual(completedResponse.events);
  });

  it("keeps deny approval submitting while the resolution POST is in flight", async () => {
    const sessionId = "approval-slow-deny";
    const requestId = "approval-deny-slow-1";
    const requestReceived = makeEvent(
      1,
      "runtime.request_received",
      { prompt: "write denied.txt hello" },
      "runtime",
      sessionId,
    );
    const approvalRequested = makeEvent(
      2,
      "runtime.approval_requested",
      {
        request_id: requestId,
        tool: "write",
        target_summary: "denied.txt",
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
    const slowApproval = createDeferred<RuntimeResponse>();

    async function* stream() {
      yield makeStreamChunk(sessionId, "running", requestReceived);
      yield makeStreamChunk(sessionId, "waiting", approvalRequested);
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());
    runtimeClientMocks.resolveApprovalMock.mockReturnValue(
      slowApproval.promise,
    );
    runtimeClientMocks.getSessionReplayMock.mockResolvedValue(failedResponse);
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary(sessionId, "failed", "write denied.txt hello"),
    ]);

    await useAppStore.getState().runTask("write denied.txt hello");

    const approvalPromise = useAppStore.getState().resolveApproval("deny");
    await Promise.resolve();

    let state = useAppStore.getState();
    expect(runtimeClientMocks.resolveApprovalMock).toHaveBeenCalledWith(
      sessionId,
      requestId,
      "deny",
    );
    expect(state.approvalStatus).toBe("submitting");
    expect(state.approvalError).toBeNull();
    expect(state.runStatus).toBe("success");
    expect(state.currentSessionState?.status).toBe("waiting");
    expect(state.currentSessionEvents.map((event) => event.event_type)).toEqual(
      ["runtime.request_received", "runtime.approval_requested"],
    );

    slowApproval.resolve(failedResponse);
    await approvalPromise;

    state = useAppStore.getState();
    expect(state.approvalStatus).toBe("idle");
    expect(state.runStatus).toBe("idle");
    expect(state.currentSessionState?.status).toBe("failed");
    expect(state.currentSessionEvents).toEqual(failedResponse.events);
  });

  it("preserves backend tool display metadata while streaming and replaying", async () => {
    const sessionId = "session-tool-display";
    const requestReceived = makeEvent(
      1,
      "runtime.request_received",
      { prompt: "run npm test" },
      "runtime",
      sessionId,
    );
    const shellStarted = makeEvent(
      2,
      "runtime.tool_started",
      {
        tool: "shell_exec",
        tool_call_id: "shell-1",
        display: {
          kind: "shell",
          title: "Shell",
          summary: "Run test suite",
          args: ["npm test"],
          copyable: { command: "npm test" },
        },
        tool_status: {
          invocation_id: "shell-1",
          tool_name: "shell_exec",
          phase: "running",
          status: "running",
          display: {
            kind: "shell",
            title: "Shell",
            summary: "Run test suite",
            args: ["npm test"],
            copyable: { command: "npm test" },
          },
        },
      },
      "runtime",
      sessionId,
    );
    const shellCompleted = makeEvent(
      3,
      "runtime.tool_completed",
      {
        tool: "shell_exec",
        tool_call_id: "shell-1",
        status: "ok",
        arguments: { command: "npm test" },
        data: { command: "npm test", exit_code: 0, stdout: "2 passed" },
        display: {
          kind: "shell",
          title: "Shell",
          summary: "Run test suite",
          args: ["npm test"],
          copyable: { command: "npm test", output: "2 passed" },
        },
        tool_status: {
          invocation_id: "shell-1",
          tool_name: "shell_exec",
          phase: "completed",
          status: "completed",
          display: {
            kind: "shell",
            title: "Shell",
            summary: "Run test suite",
            args: ["npm test"],
            copyable: { command: "npm test", output: "2 passed" },
          },
        },
      },
      "runtime",
      sessionId,
    );
    const responseReady = makeEvent(
      4,
      "graph.response_ready",
      { output: "Tests passed" },
      "graph",
      sessionId,
    );
    const completedResponse = makeRuntimeResponse(
      sessionId,
      "completed",
      [requestReceived, shellStarted, shellCompleted, responseReady],
      "Tests passed",
    );

    async function* stream() {
      yield makeStreamChunk(sessionId, "running", requestReceived);
      yield makeStreamChunk(sessionId, "running", shellStarted);
      yield makeStreamChunk(sessionId, "completed", shellCompleted);
      yield makeStreamChunk(sessionId, "completed", responseReady);
      yield makeStreamChunk(sessionId, "completed", null, "Tests passed");
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());
    runtimeClientMocks.getSessionReplayMock.mockResolvedValue(
      completedResponse,
    );
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary(sessionId, "completed", "run npm test"),
    ]);

    await useAppStore.getState().runTask("run npm test");

    let state = useAppStore.getState();
    expect(state.currentSessionEvents[1]?.payload.tool_status).toMatchObject({
      invocation_id: "shell-1",
      display: { summary: "Run test suite" },
    });
    expect(state.currentSessionEvents[2]?.payload.display).toEqual({
      kind: "shell",
      title: "Shell",
      summary: "Run test suite",
      args: ["npm test"],
      copyable: { command: "npm test", output: "2 passed" },
    });

    await state.selectSession(sessionId);

    state = useAppStore.getState();
    expect(state.currentSessionEvents).toEqual(completedResponse.events);
    expect(state.currentSessionEvents[2]?.payload.tool_status).toMatchObject({
      invocation_id: "shell-1",
      status: "completed",
      display: { copyable: { command: "npm test", output: "2 passed" } },
    });
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
    expect(state.questionStatus).toBe("idle");
    expect(state.currentSessionState?.status).toBe("completed");
    expect(state.currentSessionOutput).toBe("continued");
  });

  it("answers a pending question even when runStatus still reads running", async () => {
    // The backend emits runtime.question_requested and then closes the run
    // stream. Between that event and the frontend observing the stream close,
    // runStatus can still read "running" (the streamed run's post-loop set has
    // not run yet). In that window the composer is disabled (session status is
    // "waiting"), so the question card is the only input path; a run-lock guard
    // on answerQuestion would silently drop the answer and strand the user.
    const sessionId = "session-question-race";
    const requestId = "question-race-1";
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
            options: [{ label: "left" }],
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

    runtimeClientMocks.answerQuestionMock.mockResolvedValue(completedResponse);
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary(sessionId, "completed", "ask a direction"),
    ]);

    // Simulate the exact window: the question event has arrived (session is
    // "waiting" and a pending request is present) but the stream has not yet
    // closed, so runStatus is still "running".
    useAppStore.setState({
      currentSessionId: sessionId,
      currentSessionState: makeSessionState(sessionId, "waiting"),
      currentSessionEvents: [requestReceived, questionRequested],
      replayStatus: "idle",
      runStatus: "running",
      questionStatus: "idle",
    });

    const store = useAppStore.getState();
    await store.answerQuestion([{ header: "Direction", answers: ["left"] }]);

    expect(runtimeClientMocks.answerQuestionMock).toHaveBeenCalledWith(
      sessionId,
      requestId,
      [{ header: "Direction", answers: ["left"] }],
    );
    const state = useAppStore.getState();
    expect(state.questionStatus).toBe("idle");
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
        tool: "write",
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

  it("persists the expanded session sidebar width", async () => {
    useAppStore.getState().setSessionSidebarWidth(380);

    const persisted = JSON.parse(localStorage.getItem("app-storage") ?? "{}");

    expect(useAppStore.getState().sessionSidebarWidth).toBe(380);
    expect(persisted.state.sessionSidebarWidth).toBe(380);
  });

  it("loads clean review diff state for selected nested file tree paths", async () => {
    const reviewDiff: ReviewFileDiff = {
      root: "/workspace",
      path: "src/app file #1.ts",
      state: "clean",
      diff: null,
    };
    runtimeClientMocks.getReviewDiffMock.mockResolvedValue(reviewDiff);

    await useAppStore.getState().selectReviewPath("src/app file #1.ts");

    const state = useAppStore.getState();
    expect(runtimeClientMocks.getReviewDiffMock).toHaveBeenCalledWith(
      "src/app file #1.ts",
    );
    expect(state.reviewSelectedPath).toBe("src/app file #1.ts");
    expect(state.reviewDiffStatus).toBe("success");
    expect(state.reviewDiff).toEqual(reviewDiff);
    expect(state.reviewDiffError).toBeNull();
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
    const task = makeBackgroundTaskSummary("task-1", "inspect workspace");
    runtimeClientMocks.listBackgroundTasksMock.mockResolvedValue([task]);

    await useAppStore.getState().switchWorkspace("/new-workspace");

    const state = useAppStore.getState();
    expect(runtimeClientMocks.listBackgroundTasksMock).toHaveBeenCalled();
    expect(state.backgroundTasks).toEqual([task]);
  });

  it("refreshes session-scoped background tasks after selecting a session", async () => {
    const firstTask = makeBackgroundTaskSummary("task-a", "prior session task");
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

  it("reloads global background tasks when selecting a new session", async () => {
    const sessionTask = makeBackgroundTaskSummary(
      "task-a",
      "prior session task",
    );
    const globalTask = makeBackgroundTaskSummary("task-global", "global task");
    useAppStore.setState({
      currentSessionId: "session-1",
      backgroundTasks: [sessionTask],
    });
    runtimeClientMocks.listBackgroundTasksMock.mockResolvedValue([globalTask]);

    await useAppStore.getState().selectSession("");

    const state = useAppStore.getState();
    expect(runtimeClientMocks.listBackgroundTasksMock).toHaveBeenCalled();
    expect(
      runtimeClientMocks.listSessionBackgroundTasksMock,
    ).not.toHaveBeenCalled();
    expect(state.currentSessionId).toBeNull();
    expect(state.backgroundTasks).toEqual([globalTask]);
  });

  it("ignores stale background task responses after session scope changes", async () => {
    const staleTask = makeBackgroundTaskSummary("task-stale", "stale task");
    const currentTask = makeBackgroundTaskSummary(
      "task-current",
      "current task",
    );
    const firstRequest = createDeferred<BackgroundTaskSummary[]>();
    useAppStore.setState({ currentSessionId: "session-1" });
    runtimeClientMocks.listSessionBackgroundTasksMock.mockReturnValueOnce(
      firstRequest.promise,
    );

    const staleLoad = useAppStore.getState().loadBackgroundTasks();
    useAppStore.setState({ currentSessionId: "session-2" });
    runtimeClientMocks.listSessionBackgroundTasksMock.mockResolvedValueOnce([
      currentTask,
    ]);
    await useAppStore.getState().loadBackgroundTasks();

    firstRequest.resolve([staleTask]);
    await staleLoad;

    const state = useAppStore.getState();
    expect(
      runtimeClientMocks.listSessionBackgroundTasksMock,
    ).toHaveBeenNthCalledWith(1, "session-1");
    expect(
      runtimeClientMocks.listSessionBackgroundTasksMock,
    ).toHaveBeenNthCalledWith(2, "session-2");
    expect(state.backgroundTasks).toEqual([currentTask]);
  });

  it("loads and guards selected background task output", async () => {
    const slowOutput = createDeferred<BackgroundTaskOutput>();
    const fastOutput: BackgroundTaskOutput = {
      task: {
        task_id: "task-fast",
        status: "completed",
        parent_session_id: "session-1",
        requested_child_session_id: "requested-child",
        child_session_id: "child-session",
        approval_request_id: null,
        question_request_id: null,
        approval_blocked: false,
        summary_output: "fast summary",
        error: null,
        result_available: true,
        cancellation_cause: null,
        routing: { mode: "subagent", subagent_type: "explore" },
      },
      session_result: {
        session: makeSessionState("child-session", "completed"),
        prompt: "inspect output",
        status: "completed",
        summary: "session summary",
        output: "session output",
        error: null,
        last_event_sequence: 2,
        transcript: [],
      },
      output: "fast output",
    };
    runtimeClientMocks.getBackgroundTaskOutputMock.mockReturnValueOnce(
      slowOutput.promise,
    );

    const slowLoad = useAppStore
      .getState()
      .loadBackgroundTaskOutput("task-slow");
    expect(useAppStore.getState().selectedBackgroundTaskOutputId).toBe(
      "task-slow",
    );
    expect(useAppStore.getState().backgroundTaskOutputStatus).toBe("loading");

    runtimeClientMocks.getBackgroundTaskOutputMock.mockResolvedValueOnce(
      fastOutput,
    );
    await useAppStore.getState().loadBackgroundTaskOutput("task-fast");

    slowOutput.resolve({
      ...fastOutput,
      task: { ...fastOutput.task, task_id: "task-slow" },
      output: "stale output",
    });
    await slowLoad;

    const state = useAppStore.getState();
    expect(runtimeClientMocks.getBackgroundTaskOutputMock).toHaveBeenCalledWith(
      "task-slow",
    );
    expect(runtimeClientMocks.getBackgroundTaskOutputMock).toHaveBeenCalledWith(
      "task-fast",
    );
    expect(state.selectedBackgroundTaskOutputId).toBe("task-fast");
    expect(state.backgroundTaskOutputStatus).toBe("success");
    expect(state.backgroundTaskOutput).toEqual(fastOutput);
    expect(state.backgroundTaskOutputError).toBeNull();
  });

  it("keeps delegated child output selected while refreshing session-scoped task lists", async () => {
    const childTask = makeBackgroundTaskSummary("task-child", "child task");
    const childOutput: BackgroundTaskOutput = {
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
        session: makeSessionState("child-session", "completed"),
        prompt: "child prompt",
        status: "completed",
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

    useAppStore.setState({
      currentSessionId: "session-parent",
      selectedBackgroundTaskOutputId: "task-child",
      backgroundTaskOutput: childOutput,
      backgroundTaskOutputStatus: "success",
    });
    runtimeClientMocks.listSessionBackgroundTasksMock.mockResolvedValue([
      childTask,
    ]);

    await useAppStore.getState().loadBackgroundTasks();

    const state = useAppStore.getState();
    expect(state.backgroundTasks).toEqual([childTask]);
    expect(state.selectedBackgroundTaskOutputId).toBe("task-child");
    expect(state.backgroundTaskOutput).toEqual(childOutput);
    expect(
      runtimeClientMocks.listSessionBackgroundTasksMock,
    ).toHaveBeenCalledWith("session-parent");
  });

  it("restores the delegated child parent session on parent return", async () => {
    const parentEvents = [
      makeEvent(1, "runtime.request_received", { prompt: "parent prompt" }),
    ];
    const childOutput: BackgroundTaskOutput = {
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
          ...makeSessionState("child-session", "completed"),
          session: { id: "child-session", parent_id: "session-parent" },
        },
        prompt: "child prompt",
        status: "completed",
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
        ],
      },
      output: "child output",
    };
    runtimeClientMocks.getChildSessionContextMock.mockResolvedValueOnce(
      childOutput,
    );
    runtimeClientMocks.listSessionBackgroundTasksMock.mockResolvedValue([]);
    runtimeClientMocks.getSessionReplayMock.mockResolvedValueOnce(
      makeRuntimeResponse(
        "session-parent",
        "completed",
        parentEvents,
        "parent output",
      ),
    );

    await useAppStore.getState().selectSession("child-session");

    expect(useAppStore.getState().currentSessionId).toBe("child-session");
    expect(useAppStore.getState().childSessionParentId).toBe("session-parent");
    expect(
      runtimeClientMocks.listSessionBackgroundTasksMock,
    ).toHaveBeenCalledWith("session-parent");

    await useAppStore
      .getState()
      .selectSession(useAppStore.getState().childSessionParentId ?? "");

    const state = useAppStore.getState();
    expect(runtimeClientMocks.getSessionReplayMock).toHaveBeenCalledWith(
      "session-parent",
    );
    expect(state.currentSessionId).toBe("session-parent");
    expect(state.childSessionParentId).toBeNull();
    expect(state.currentSessionOutput).toBe("parent output");
    expect(state.selectedBackgroundTaskOutputId).toBeNull();
  });

  it("keeps delegated child replay run status in sync while the child is still running", async () => {
    runtimeClientMocks.getChildSessionContextMock.mockResolvedValueOnce({
      task: {
        task_id: "task-child-running",
        status: "running",
        parent_session_id: "session-parent",
        requested_child_session_id: "child-session",
        delegated_prompt: "inspect child",
        child_session_id: "child-session",
        approval_request_id: null,
        question_request_id: null,
        approval_blocked: false,
        summary_output: null,
        error: null,
        result_available: false,
        cancellation_cause: null,
      },
      session_result: {
        session: {
          session: { id: "child-session", parent_id: "session-parent" },
          status: "running",
          turn: 1,
          metadata: {},
        },
        prompt: "inspect child",
        status: "running",
        summary: null,
        output: null,
        error: null,
        last_event_sequence: 1,
        transcript: [
          makeEvent(
            1,
            "runtime.request_received",
            { prompt: "inspect child" },
            "runtime",
            "child-session",
          ),
        ],
      },
      output: null,
    });
    runtimeClientMocks.listSessionBackgroundTasksMock.mockResolvedValue([]);

    await useAppStore.getState().selectSession("child-session");

    const state = useAppStore.getState();
    expect(state.currentSessionId).toBe("child-session");
    expect(state.childSessionParentId).toBe("session-parent");
    expect(state.replayStatus).toBe("success");
    expect(state.runStatus).toBe("running");
  });

  it("allows returning to the parent session while a delegated child replay is still running", async () => {
    const parentEvents = [
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
    ];

    runtimeClientMocks.getChildSessionContextMock.mockResolvedValueOnce({
      task: {
        task_id: "task-child-running",
        status: "running",
        parent_session_id: "session-parent",
        requested_child_session_id: "child-session",
        delegated_prompt: "inspect child",
        child_session_id: "child-session",
        approval_request_id: null,
        question_request_id: null,
        approval_blocked: false,
        summary_output: null,
        error: null,
        result_available: false,
        cancellation_cause: null,
      },
      session_result: {
        session: {
          session: { id: "child-session", parent_id: "session-parent" },
          status: "running",
          turn: 1,
          metadata: {},
        },
        prompt: "inspect child",
        status: "running",
        summary: null,
        output: "child output",
        error: null,
        last_event_sequence: 1,
        transcript: [
          makeEvent(
            1,
            "runtime.request_received",
            { prompt: "inspect child" },
            "runtime",
            "child-session",
          ),
        ],
      },
      output: "child output",
    });
    runtimeClientMocks.listSessionBackgroundTasksMock.mockResolvedValue([]);
    runtimeClientMocks.getSessionReplayMock.mockResolvedValueOnce(
      makeRuntimeResponse(
        "session-parent",
        "completed",
        parentEvents,
        "parent output",
      ),
    );

    await useAppStore.getState().selectSession("child-session");
    await useAppStore.getState().selectSession("session-parent");

    const state = useAppStore.getState();
    expect(runtimeClientMocks.getSessionReplayMock).toHaveBeenCalledWith(
      "session-parent",
    );
    expect(state.currentSessionId).toBe("session-parent");
    expect(state.childSessionParentId).toBeNull();
    expect(state.runStatus).toBe("idle");
    expect(state.currentSessionOutput).toBe("parent output");
  });

  it("refreshes an already-browsed delegated child session in place without clearing the child view", async () => {
    const childOutput: BackgroundTaskOutput = {
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
          ...makeSessionState("child-session", "completed"),
          session: { id: "child-session", parent_id: "session-parent" },
        },
        prompt: "child prompt",
        status: "completed",
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
        ],
      },
      output: "child output",
    };
    runtimeClientMocks.getChildSessionContextMock.mockResolvedValueOnce(
      childOutput,
    );
    runtimeClientMocks.getBackgroundTaskOutputMock.mockResolvedValueOnce(
      childOutput,
    );
    runtimeClientMocks.listSessionBackgroundTasksMock.mockResolvedValue([
      {
        task: { id: "task-child" },
        status: "completed",
        prompt: "child prompt",
        session_id: "child-session",
        error: null,
        created_at: 1,
        updated_at: 1,
      },
    ]);

    await useAppStore.getState().selectSession("child-session");

    const before = useAppStore.getState();
    expect(before.currentSessionId).toBe("child-session");
    expect(before.childSessionParentId).toBe("session-parent");
    expect(before.selectedBackgroundTaskOutputId).toBe("task-child");

    // Re-selecting the already-browsed child must refresh in place instead of
    // clearing the child view (which made the transcript flip on its own).
    await useAppStore.getState().selectSession("child-session");

    const after = useAppStore.getState();
    expect(runtimeClientMocks.getBackgroundTaskOutputMock).toHaveBeenCalledWith(
      "task-child",
    );
    expect(runtimeClientMocks.getChildSessionContextMock).toHaveBeenCalledTimes(
      1,
    );
    expect(after.currentSessionId).toBe("child-session");
    expect(after.childSessionParentId).toBe("session-parent");
    expect(after.selectedBackgroundTaskOutputId).toBe("task-child");
    expect(after.replayStatus).toBe("success");
    expect(after.backgroundTaskOutputStatus).toBe("success");
  });

  it("keeps the delegated child view when the flat session list omits child sessions", async () => {
    const childOutput: BackgroundTaskOutput = {
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
          ...makeSessionState("child-session", "completed"),
          session: { id: "child-session", parent_id: "session-parent" },
        },
        prompt: "child prompt",
        status: "completed",
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
        ],
      },
      output: "child output",
    };
    runtimeClientMocks.getChildSessionContextMock.mockResolvedValueOnce(
      childOutput,
    );
    runtimeClientMocks.listSessionBackgroundTasksMock.mockResolvedValue([
      {
        task: { id: "task-child" },
        status: "completed",
        prompt: "child prompt",
        session_id: "child-session",
        error: null,
        created_at: 1,
        updated_at: 1,
      },
    ]);
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary("session-parent", "completed", "parent prompt"),
    ]);

    await useAppStore.getState().selectSession("child-session");
    await useAppStore.getState().loadSessions();

    const state = useAppStore.getState();
    expect(state.currentSessionId).toBe("child-session");
    expect(state.childSessionParentId).toBe("session-parent");
    expect(state.selectedBackgroundTaskOutputId).toBe("task-child");
  });

  it("shows an interrupted delegated child session once and refreshes it in place", async () => {
    const interruptedChildOutput: BackgroundTaskOutput = {
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
          ...makeSessionState("child-session", "interrupted"),
          session: { id: "child-session", parent_id: "session-parent" },
        },
        prompt: "child prompt",
        status: "interrupted",
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
    runtimeClientMocks.getChildSessionContextMock.mockResolvedValueOnce(
      interruptedChildOutput,
    );
    runtimeClientMocks.getBackgroundTaskOutputMock.mockResolvedValueOnce(
      interruptedChildOutput,
    );
    runtimeClientMocks.listSessionBackgroundTasksMock.mockResolvedValue([
      {
        task: { id: "task-child" },
        status: "completed",
        prompt: "child prompt",
        session_id: "child-session",
        error: null,
        created_at: 1,
        updated_at: 1,
      },
    ]);

    await useAppStore.getState().selectSession("child-session");

    const state = useAppStore.getState();
    expect(state.currentSessionId).toBe("child-session");
    expect(state.childSessionParentId).toBe("session-parent");
    expect(state.selectedBackgroundTaskOutputId).toBe("task-child");
    expect(state.backgroundTaskOutput).toBe(interruptedChildOutput);
    expect(state.replayStatus).toBe("success");
    // An interrupted (unsealed) child is terminal for display: it must not be
    // treated as a live run.
    expect(state.runStatus).toBe("idle");
    expect(state.currentSessionEvents).toHaveLength(2);
    expect(state.currentSessionEvents[1].event_type).toBe(
      "graph.response_ready",
    );

    // Re-selecting the already-browsed interrupted child refreshes in place
    // instead of clearing the child view and re-fetching the context.
    await useAppStore.getState().selectSession("child-session");

    expect(runtimeClientMocks.getChildSessionContextMock).toHaveBeenCalledTimes(
      1,
    );
    expect(runtimeClientMocks.getBackgroundTaskOutputMock).toHaveBeenCalledWith(
      "task-child",
    );
    const after = useAppStore.getState();
    expect(after.currentSessionId).toBe("child-session");
    expect(after.childSessionParentId).toBe("session-parent");
    expect(after.selectedBackgroundTaskOutputId).toBe("task-child");
    expect(after.backgroundTaskOutputStatus).toBe("success");
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

  it("interrupts the active current session run", async () => {
    const gate = createDeferred<void>();
    const sessionId = "interrupt-session";
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

    await useAppStore.getState().cancelCurrentRun();

    expect(runtimeClientMocks.cancelSessionMock).toHaveBeenCalledWith(
      sessionId,
    );
    expect(useAppStore.getState().runStatus).toBe("cancelling");

    await useAppStore.getState().runTask("read second.txt");

    expect(runtimeClientMocks.runStreamMock).toHaveBeenCalledTimes(1);

    gate.resolve();
    await runPromise;

    expect(useAppStore.getState().runStatus).toBe("idle");
  });

  it("keeps the run locked when interrupting before a session id is available", async () => {
    const gate = createDeferred<void>();
    const pendingSessionId = "pending-session-id";
    const requestReceived = makeEvent(
      1,
      "runtime.request_received",
      { prompt: "read before session id" },
      "runtime",
      pendingSessionId,
    );

    async function* stream() {
      yield makeStreamChunk(pendingSessionId, "running", requestReceived);
      await gate.promise;
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());

    const runPromise = useAppStore.getState().runTask("read before session id");
    await Promise.resolve();
    useAppStore.setState({ currentSessionId: null, currentSessionState: null });

    await useAppStore.getState().cancelCurrentRun();

    expect(runtimeClientMocks.cancelSessionMock).not.toHaveBeenCalled();
    expect(useAppStore.getState().runStatus).toBe("cancelling");

    await useAppStore.getState().runTask("read second.txt");

    expect(runtimeClientMocks.runStreamMock).toHaveBeenCalledTimes(1);

    gate.resolve();
    await runPromise;

    expect(useAppStore.getState().runStatus).toBe("idle");
  });

  it("keeps the run locked until the stream settles when runtime says the run is no longer active", async () => {
    const gate = createDeferred<void>();
    const sessionId = "stale-session";
    const requestReceived = makeEvent(
      1,
      "runtime.request_received",
      { prompt: "read stale.txt" },
      "runtime",
      sessionId,
    );

    async function* stream() {
      yield makeStreamChunk(sessionId, "running", requestReceived);
      await gate.promise;
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());
    runtimeClientMocks.cancelSessionMock.mockResolvedValueOnce({
      session_id: sessionId,
      status: "not_active",
      interrupted: false,
      cancelled: false,
      run_id: null,
      reason: null,
    });

    const runPromise = useAppStore.getState().runTask("read stale.txt");
    await Promise.resolve();
    await Promise.resolve();

    await useAppStore.getState().cancelCurrentRun();

    expect(runtimeClientMocks.cancelSessionMock).toHaveBeenCalledWith(
      sessionId,
    );
    expect(useAppStore.getState().runStatus).toBe("cancelling");

    await useAppStore.getState().runTask("read second.txt");

    expect(runtimeClientMocks.runStreamMock).toHaveBeenCalledTimes(1);

    gate.resolve();
    await runPromise;

    expect(useAppStore.getState().runStatus).toBe("idle");
  });

  it("preserves idle state when cancel request fails after the stream already exits", async () => {
    const gate = createDeferred<void>();
    const cancelGate = createDeferred<void>();
    const sessionId = "cancel-race-session";
    const requestReceived = makeEvent(
      1,
      "runtime.request_received",
      { prompt: "read race.txt" },
      "runtime",
      sessionId,
    );

    async function* stream() {
      yield makeStreamChunk(sessionId, "running", requestReceived);
      await gate.promise;
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());
    runtimeClientMocks.cancelSessionMock.mockImplementationOnce(
      () => cancelGate.promise,
    );

    const runPromise = useAppStore.getState().runTask("read race.txt");
    await Promise.resolve();
    await Promise.resolve();

    const cancelPromise = useAppStore.getState().cancelCurrentRun();
    await Promise.resolve();

    expect(useAppStore.getState().runStatus).toBe("cancelling");

    gate.resolve();
    await runPromise;

    expect(useAppStore.getState().runStatus).toBe("idle");

    cancelGate.reject(new Error("cancel timed out"));
    await cancelPromise;

    expect(useAppStore.getState().runStatus).toBe("idle");
    expect(useAppStore.getState().runError).toBeNull();
  });

  it("settles to idle without an error when cancelling aborts the stream mid-run", async () => {
    const sessionId = "abort-session";
    const requestReceived = makeEvent(
      1,
      "runtime.request_received",
      { prompt: "read slow.txt" },
      "runtime",
      sessionId,
    );

    // The mock mirrors a real fetch aborted mid-read: the generator rejects
    // with AbortError the moment the store's controller aborts.
    runtimeClientMocks.runStreamMock.mockImplementation(async function* (
      _request,
      signal?: AbortSignal,
    ) {
      yield makeStreamChunk(sessionId, "running", requestReceived);
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

    const runPromise = useAppStore.getState().runTask("read slow.txt");
    await Promise.resolve();
    await Promise.resolve();

    expect(useAppStore.getState().runStatus).toBe("running");
    // The run's AbortSignal is handed to the stream, not just a plain request.
    expect(runtimeClientMocks.runStreamMock.mock.calls[0][1]).toBeInstanceOf(
      AbortSignal,
    );

    const cancelPromise = useAppStore.getState().cancelCurrentRun();
    await cancelPromise;
    await runPromise;

    // A torn-down stream during a user interrupt is not a failure: the run
    // settles to idle with no error banner, even though no SSE cancellation
    // event was ever emitted.
    expect(useAppStore.getState().runStatus).toBe("idle");
    expect(useAppStore.getState().runError).toBeNull();
  });

  it("keeps the run idle without an error banner when the cancel POST fails and the stream aborts", async () => {
    const sessionId = "cancel-post-fail-session";
    const requestReceived = makeEvent(
      1,
      "runtime.request_received",
      { prompt: "read slow.txt" },
      "runtime",
      sessionId,
    );

    runtimeClientMocks.runStreamMock.mockImplementation(async function* (
      _request,
      signal?: AbortSignal,
    ) {
      yield makeStreamChunk(sessionId, "running", requestReceived);
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
    runtimeClientMocks.cancelSessionMock.mockRejectedValue(
      new Error("cancel post failed"),
    );

    const runPromise = useAppStore.getState().runTask("read slow.txt");
    await Promise.resolve();
    await Promise.resolve();

    const cancelPromise = useAppStore.getState().cancelCurrentRun();
    await cancelPromise;
    await runPromise;

    expect(runtimeClientMocks.cancelSessionMock).toHaveBeenCalledWith(
      sessionId,
    );
    // The failed cancel POST must not flip the run back to "running"; the
    // abort tears the stream down and the run settles to idle.
    expect(useAppStore.getState().runStatus).toBe("idle");
    expect(useAppStore.getState().runError).toBeNull();
  });

  it("settles a run to idle when the stream ends with an interrupted session status and no cancellation event", async () => {
    const sessionId = "interrupted-final-session";

    async function* stream(): AsyncGenerator<
      RuntimeStreamChunk,
      void,
      unknown
    > {
      // The backend session row is the authoritative fact: it landed as
      // "interrupted", but no runtime.failed{cancelled:true} event and no
      // user cancel accompanied the stream close.
      yield {
        kind: "session",
        session: makeSessionState(sessionId, "interrupted"),
        event: null,
        output: null,
      };
    }
    runtimeClientMocks.runStreamMock.mockReturnValue(stream());
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary(
        sessionId,
        "interrupted",
        "read interrupted.txt",
      ),
    ]);

    await useAppStore.getState().runTask("read interrupted.txt");

    expect(useAppStore.getState().runStatus).toBe("idle");
    expect(useAppStore.getState().runError).toBeNull();
    expect(useAppStore.getState().currentSessionState?.status).toBe(
      "interrupted",
    );
  });

  it("surfaces runtime failed stream details as run errors", async () => {
    const sessionId = "failed-provider-session";
    const requestReceived = makeEvent(
      1,
      "runtime.request_received",
      { prompt: "say ok" },
      "runtime",
      sessionId,
    );
    const failedEvent = makeEvent(
      2,
      "runtime.failed",
      {
        error: "provider retry exhausted",
        provider_error_details: {
          exception_message:
            "litellm.AuthenticationError: Insufficient balance.",
          exception_type: "AuthenticationError",
        },
      },
      "runtime",
      sessionId,
    );

    async function* stream() {
      yield makeStreamChunk(sessionId, "running", requestReceived);
      yield makeStreamChunk(sessionId, "failed", failedEvent);
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());

    await useAppStore.getState().runTask("say ok");

    const state = useAppStore.getState();
    expect(state.runStatus).toBe("error");
    expect(state.runError).toBe(
      "litellm.AuthenticationError: Insufficient balance.",
    );
  });
  it("keeps a transient provider error over a generic terminal failure", async () => {
    const sessionId = "transient-provider-error-session";
    const requestReceived = makeEvent(
      1,
      "runtime.request_received",
      { prompt: "say ok" },
      "runtime",
      sessionId,
    );
    const providerError = makeEvent(
      2,
      "graph.provider_stream",
      {
        channel: "error",
        kind: "error",
        error: "Provider authentication failed for deepseek.",
        error_kind: "missing_auth",
      },
      "graph",
      sessionId,
    );

    async function* stream() {
      yield makeStreamChunk(sessionId, "running", requestReceived);
      yield makeStreamChunk(sessionId, "running", providerError);
      yield makeStreamChunk(sessionId, "failed", null);
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary(sessionId, "failed", "say ok"),
    ]);

    await useAppStore.getState().runTask("say ok");

    const state = useAppStore.getState();
    expect(state.currentSessionState?.status).toBe("failed");
    expect(state.runStatus).toBe("error");
    expect(state.runError).toBe("Provider authentication failed for deepseek.");
  });

  it("uses the generic fallback when a failed session has no error event", async () => {
    const sessionId = "generic-failure-session";

    async function* stream() {
      yield makeStreamChunk(sessionId, "failed", null);
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());

    await useAppStore.getState().runTask("fail without details");

    const state = useAppStore.getState();
    expect(state.runStatus).toBe("error");
    expect(state.runError).toBe("runtime session failed");
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
        agent: {
          custom_flag: "kept",
        },
      },
    });

    expect(runtimeClientMocks.runStreamMock.mock.calls[0][0]).toEqual({
      prompt: "analyze repo",
      session_id: null,
      metadata: {
        skills: ["demo"],
        max_steps: 5,
        provider_stream: true,
        agent: {
          preset: "leader",
          model: "deepseek/deepseek-v4-pro",
          custom_flag: "kept",
        },
      },
    });
    expect(runtimeClientMocks.getStatusMock).toHaveBeenCalled();
    expect(runtimeClientMocks.getReviewMock).toHaveBeenCalled();
  });

  it("omits max_steps for web agent runs when no override is provided", async () => {
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

    expect(runtimeClientMocks.runStreamMock.mock.calls[0][0]).toEqual({
      prompt: "write hello.c",
      session_id: null,
      metadata: {
        agent: {
          preset: "leader",
          model: "deepseek/deepseek-v4-pro",
        },
      },
    });
  });

  it("sends reasoning_effort only when the selected model supports it", async () => {
    const sessionId = "session-reasoning-effort";
    const requestReceived = makeEvent(
      1,
      "runtime.request_received",
      { prompt: "think carefully" },
      "runtime",
      sessionId,
    );

    async function* stream() {
      yield makeStreamChunk(sessionId, "completed", requestReceived);
      yield makeStreamChunk(sessionId, "completed", null, "ok");
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary(sessionId, "completed", "think carefully"),
    ]);
    useAppStore.setState({
      reasoningEffort: "high",
      providerModel: "glm/glm-5",
      providers: [
        { name: "glm", label: "GLM", configured: true, current: true },
      ],
      providerModels: {
        glm: {
          provider: "glm",
          configured: true,
          models: ["glm-5"],
          model_metadata: {
            "glm-5": {
              supports_reasoning_effort: true,
              default_reasoning_effort: "medium",
            },
          },
        },
      },
    });

    await useAppStore.getState().runTask("think carefully");

    expect(runtimeClientMocks.runStreamMock.mock.calls[0][0]).toEqual({
      prompt: "think carefully",
      session_id: null,
      metadata: {
        reasoning_effort: "high",
        agent: {
          preset: "leader",
          model: "glm/glm-5",
        },
      },
    });
  });

  it("omits reasoning_effort when the selected model does not support it", async () => {
    const sessionId = "session-no-reasoning-effort";
    const requestReceived = makeEvent(
      1,
      "runtime.request_received",
      { prompt: "plain run" },
      "runtime",
      sessionId,
    );

    async function* stream() {
      yield makeStreamChunk(sessionId, "completed", requestReceived);
      yield makeStreamChunk(sessionId, "completed", null, "ok");
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary(sessionId, "completed", "plain run"),
    ]);
    useAppStore.setState({
      reasoningEffort: "high",
      providerModel: "deepseek/deepseek-v4-pro",
      providers: [
        {
          name: "deepseek",
          label: "DeepSeek",
          configured: true,
          current: true,
        },
      ],
      providerModels: {
        deepseek: {
          provider: "deepseek",
          configured: true,
          models: ["deepseek-v4-pro"],
          model_metadata: {
            "deepseek-v4-pro": {
              supports_reasoning_effort: false,
              default_reasoning_effort: null,
            },
          },
        },
      },
    });

    await useAppStore.getState().runTask("plain run", {
      metadata: { reasoning_effort: "xhigh" },
    });

    expect(runtimeClientMocks.runStreamMock.mock.calls[0][0]).toEqual({
      prompt: "plain run",
      session_id: null,
      metadata: {
        agent: {
          preset: "leader",
          model: "deepseek/deepseek-v4-pro",
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

    expect(runtimeClientMocks.runStreamMock.mock.calls[0][0]).toEqual({
      prompt: "run current provider alias",
      session_id: null,
      metadata: {
        agent: {
          preset: "leader",
          model: "opencode-go/kimi-k2.6",
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

    expect(runtimeClientMocks.runStreamMock.mock.calls[0][0]).toEqual({
      prompt: "run unique alias",
      session_id: null,
      metadata: {
        agent: {
          preset: "leader",
          model: "kimi/kimi-k2.6",
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

    expect(runtimeClientMocks.runStreamMock.mock.calls[0][0]).toEqual({
      prompt: "run qualified alias",
      session_id: null,
      metadata: {
        agent: {
          preset: "leader",
          model: "kimi/kimi-k2.6",
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

    expect(runtimeClientMocks.runStreamMock.mock.calls[0][0]).toEqual({
      prompt: "run ambiguous alias",
      session_id: null,
      metadata: {
        agent: {
          preset: "leader",
          model: "kimi-k2.6",
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

    expect(runtimeClientMocks.runStreamMock.mock.calls[0][0]).toEqual({
      prompt: "run unknown alias",
      session_id: null,
      metadata: {
        agent: {
          preset: "leader",
          model: "mystery-model",
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
      background_tasks: {
        active_worker_slots: 1,
        queued_count: 2,
        running_count: 1,
        terminal_count: 4,
        default_concurrency: 3,
        provider_concurrency: { "opencode-go": 2 },
        model_concurrency: { "opencode-go/glm-5.1": 1 },
        status_counts: { queued: 2, running: 1, completed: 4 },
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

    expect(runtimeClientMocks.runStreamMock.mock.calls[0][0]).toEqual({
      prompt: "new run",
      session_id: null,
      metadata: {
        agent: {
          preset: "leader",
          model: "deepseek/deepseek-v4-pro",
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

    expect(runtimeClientMocks.runStreamMock.mock.calls[0][0]).toEqual({
      prompt: "start new",
      session_id: null,
      metadata: {
        agent: {
          preset: "leader",
          model: "deepseek/deepseek-v4-pro",
        },
      },
    });
  });

  it("loads runtime-owned settings without overriding an existing live providerModel", async () => {
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
    expect(state.providerModel).toBe("deepseek/deepseek-v4-pro");
  });

  it("keeps an explicit live providerModel when loading runtime-owned settings", async () => {
    useAppStore.setState({ providerModel: "opencode-go/kimi-k2.6" });
    runtimeClientMocks.getSettingsMock.mockResolvedValue({
      provider: "glm",
      provider_api_key_present: true,
      model: "glm/glm-5",
    });

    await useAppStore.getState().loadSettings();

    const state = useAppStore.getState();
    expect(state.settings).toEqual({
      provider: "glm",
      provider_api_key_present: true,
      model: "glm/glm-5",
    });
    expect(state.providerModel).toBe("opencode-go/kimi-k2.6");
  });

  it("keeps the live providerModel when settings load after hydration", async () => {
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

    expect(useAppStore.getState().providerModel).toBe("kimi-k2.6");
    expect(runtimeClientMocks.runStreamMock.mock.calls[0][0]).toEqual({
      prompt: "hydrated qualified model",
      session_id: null,
      metadata: {
        agent: {
          preset: "leader",
          model: "kimi-k2.6",
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

    expect(runtimeClientMocks.runStreamMock.mock.calls[0][0]).toEqual({
      prompt: "use configured model",
      session_id: null,
      metadata: {
        agent: {
          preset: "leader",
          model: "opencode-go/kimi-k2.6",
        },
      },
    });
  });

  it("updates runtime-owned settings without expecting provider_api_key in the response", async () => {
    runtimeClientMocks.updateSettingsMock.mockResolvedValue({
      provider: "deepseek",
      provider_api_key_present: true,
      model: "deepseek/deepseek-v4-pro",
    });

    await useAppStore.getState().updateSettings({
      provider: "deepseek",
      provider_api_key: "secret-key",
      model: "deepseek/deepseek-v4-pro",
    });

    const state = useAppStore.getState();
    expect(runtimeClientMocks.updateSettingsMock).toHaveBeenCalledWith({
      provider: "deepseek",
      provider_api_key: "secret-key",
      model: "deepseek/deepseek-v4-pro",
    });
    expect(state.settings).toEqual({
      provider: "deepseek",
      provider_api_key_present: true,
      model: "deepseek/deepseek-v4-pro",
    });
    expect(state.providerModel).toBe("deepseek/deepseek-v4-pro");
  });

  it("keeps an explicit live providerModel when saving runtime-owned settings", async () => {
    useAppStore.setState({ providerModel: "opencode-go/kimi-k2.6" });
    runtimeClientMocks.updateSettingsMock.mockResolvedValue({
      provider: "deepseek",
      provider_api_key_present: true,
      model: "deepseek/deepseek-v4-pro",
    });

    await useAppStore.getState().updateSettings({
      provider: "deepseek",
      model: "deepseek/deepseek-v4-pro",
    });

    const state = useAppStore.getState();
    expect(state.settings).toEqual({
      provider: "deepseek",
      provider_api_key_present: true,
      model: "deepseek/deepseek-v4-pro",
    });
    expect(state.providerModel).toBe("opencode-go/kimi-k2.6");
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

  it("recovers composer state after approval resolution failure", async () => {
    const sessionId = "approval-recover";
    const requestReceived = makeEvent(
      1,
      "runtime.request_received",
      { prompt: "write approval-recover.txt recover" },
      "runtime",
      sessionId,
    );
    const requestId = "approval-def456";
    const approvalRequested = makeEvent(
      2,
      "runtime.approval_requested",
      { request_id: requestId, tool: "write", decision: "ask" },
      "runtime",
      sessionId,
    );

    // Recovery payload: backend may return a fresh waiting state
    // (e.g. re-emitted approval) or any terminal state after the
    // approval error.  The important thing is that the store uses
    // this data to replace the stale waiting session.
    const recoveryResponse = makeRuntimeResponse(
      sessionId,
      "waiting",
      [requestReceived, approvalRequested],
      null,
    );

    async function* stream() {
      yield makeStreamChunk(sessionId, "running", requestReceived);
      yield makeStreamChunk(sessionId, "waiting", approvalRequested);
    }

    const approvalFailureMessage = "Failed to resolve approval";

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());
    runtimeClientMocks.resolveApprovalMock.mockRejectedValue(
      new Error(approvalFailureMessage),
    );
    runtimeClientMocks.getSessionReplayMock.mockResolvedValue(recoveryResponse);
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary(
        sessionId,
        "waiting",
        "write approval-recover.txt recover",
      ),
    ]);

    await useAppStore.getState().runTask("write approval-recover.txt recover");

    let state = useAppStore.getState();
    expect(state.currentSessionId).toBe(sessionId);
    expect(state.currentSessionState?.status).toBe("waiting");

    // Trigger approval — expect it to fail and then recover.
    await state.resolveApproval("allow");

    state = useAppStore.getState();

    // Approval failure recorded.
    expect(runtimeClientMocks.resolveApprovalMock).toHaveBeenCalledWith(
      sessionId,
      requestId,
      "allow",
    );
    expect(state.approvalStatus).toBe("error");
    expect(state.approvalError).toBe(approvalFailureMessage);

    // Composer must recover — runStatus goes back to idle so the
    // composer-disabled guard no longer blocks user input.
    expect(state.runStatus).toBe("idle");

    // Session replay was fetched after the error so the UI reflects
    // the latest backend state rather than stale waiting data.
    expect(runtimeClientMocks.getSessionReplayMock).toHaveBeenCalledWith(
      sessionId,
    );
    expect(state.currentSessionState).toEqual(recoveryResponse.session);
    expect(state.currentSessionEvents).toEqual(recoveryResponse.events);
    expect(state.replayStatus).toBe("success");
    expect(state.replayError).toBeNull();

    // Sessions list was refreshed.
    expect(runtimeClientMocks.listSessionsMock).toHaveBeenCalled();
  });

  it("rolls back optimistic approval when resolution and recovery replay both fail", async () => {
    const sessionId = "approval-rollback";
    const requestId = "approval-retry-123";
    const requestReceived = makeEvent(
      1,
      "runtime.request_received",
      { prompt: "write rollback.txt retry" },
      "runtime",
      sessionId,
    );
    const approvalRequested = makeEvent(
      2,
      "runtime.approval_requested",
      { request_id: requestId, tool: "write", decision: "ask" },
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
      { path: "rollback.txt" },
      "tool",
      sessionId,
    );
    const completedResponse = makeRuntimeResponse(
      sessionId,
      "completed",
      [requestReceived, approvalRequested, approvalResolved, toolCompleted],
      "retry ok",
    );

    async function* stream() {
      yield makeStreamChunk(sessionId, "running", requestReceived);
      yield makeStreamChunk(sessionId, "waiting", approvalRequested);
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());
    runtimeClientMocks.resolveApprovalMock
      .mockRejectedValueOnce(new Error("approval post failed"))
      .mockResolvedValueOnce(completedResponse);
    runtimeClientMocks.getSessionReplayMock.mockRejectedValue(
      new Error("replay failed"),
    );
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary(
        sessionId,
        "waiting",
        "write rollback.txt retry",
      ),
    ]);

    await useAppStore.getState().runTask("write rollback.txt retry");

    await useAppStore.getState().resolveApproval("allow");

    let state = useAppStore.getState();
    expect(runtimeClientMocks.resolveApprovalMock).toHaveBeenCalledTimes(1);
    expect(state.approvalStatus).toBe("error");
    expect(state.approvalError).toBe("approval post failed");
    expect(state.currentSessionState?.status).toBe("waiting");
    expect(state.currentSessionOutput).toBeNull();
    expect(state.currentSessionEvents).toEqual([
      requestReceived,
      approvalRequested,
    ]);
    expect(
      state.currentSessionEvents.some(
        (event) => event.event_type === "runtime.approval_resolved",
      ),
    ).toBe(false);

    await useAppStore.getState().resolveApproval("allow");

    state = useAppStore.getState();
    expect(runtimeClientMocks.resolveApprovalMock).toHaveBeenNthCalledWith(
      2,
      sessionId,
      requestId,
      "allow",
    );
    expect(state.currentSessionState?.status).toBe("completed");
    expect(state.currentSessionOutput).toBe("retry ok");
  });

  it("keeps composer disabled when approval failure replay is still running", async () => {
    const sessionId = "approval-running-replay";
    const requestReceived = makeEvent(
      1,
      "runtime.request_received",
      { prompt: "write approval-running.txt recover" },
      "runtime",
      sessionId,
    );
    const requestId = "approval-running-123";
    const approvalRequested = makeEvent(
      2,
      "runtime.approval_requested",
      { request_id: requestId, tool: "write", decision: "ask" },
      "runtime",
      sessionId,
    );
    const replayProgress = makeEvent(
      3,
      "runtime.tool_started",
      { tool: "write" },
      "runtime",
      sessionId,
    );
    const runningReplayResponse = makeRuntimeResponse(
      sessionId,
      "running",
      [requestReceived, approvalRequested, replayProgress],
      null,
    );

    async function* stream() {
      yield makeStreamChunk(sessionId, "running", requestReceived);
      yield makeStreamChunk(sessionId, "waiting", approvalRequested);
    }

    const approvalFailureMessage = "Failed to resolve approval";

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());
    runtimeClientMocks.resolveApprovalMock.mockRejectedValue(
      new Error(approvalFailureMessage),
    );
    runtimeClientMocks.getSessionReplayMock.mockResolvedValue(
      runningReplayResponse,
    );
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary(
        sessionId,
        "running",
        "write approval-running.txt recover",
      ),
    ]);

    await useAppStore.getState().runTask("write approval-running.txt recover");

    const stateBeforeApproval = useAppStore.getState();
    expect(stateBeforeApproval.currentSessionState?.status).toBe("waiting");

    await stateBeforeApproval.resolveApproval("allow");

    const state = useAppStore.getState();
    expect(runtimeClientMocks.resolveApprovalMock).toHaveBeenCalledWith(
      sessionId,
      requestId,
      "allow",
    );
    expect(state.approvalStatus).toBe("error");
    expect(state.approvalError).toBe(approvalFailureMessage);
    expect(state.currentSessionState?.status).toBe("running");
    expect(state.currentSessionState).toEqual(runningReplayResponse.session);
    expect(state.currentSessionEvents).toEqual(runningReplayResponse.events);
    expect(state.runStatus).toBe("running");
    expect(state.replayStatus).toBe("success");
    expect(state.replayError).toBeNull();
  });
});
