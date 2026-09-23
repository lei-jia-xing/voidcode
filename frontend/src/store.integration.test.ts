import "./test-local-storage";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { deriveChatMessages } from "./lib/runtime/event-parser";

import type {
  ApprovalDecision,
  BackgroundTaskOutput,
  BackgroundTaskResultPayload,
  BackgroundTaskSummary,
  EventEnvelope,
  ProviderModelsResult,
  QuestionAnswer,
  ReviewFileDiff,
  RuntimeNotification,
  RuntimeResponse,
  RuntimeSessionDebugSnapshot,
  RuntimeStatusSnapshot,
  RuntimeStreamChunk,
  RuntimeSettings,
  SessionState,
  StoredSessionSummary,
  TranscriptEvent,
  WorkspaceReviewSnapshot,
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

let useAppStore: typeof import("./store").useAppStore;
let queryClient: typeof import("./lib/queries").queryClient;
let queryKeys: typeof import("./lib/queries").queryKeys;

// The scope every seeded runtime payload belongs to (the workspace the shell has
// open). The store reads its server data out of these same cache entries.
const WORKSPACE_PATH = "/workspace";

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

/** A transcript entry: the ordered event plus its revert-marker state. */
function makeTranscriptEvent(
  sequence: number,
  eventType: string,
  payload: Record<string, unknown>,
  source: EventEnvelope["source"] = "runtime",
  sessionId = "session-1",
): TranscriptEvent {
  return {
    ...makeEvent(sequence, eventType, payload, source, sessionId),
    reverted: false,
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
    keep_alive: false,
    schema_mode: "permissive",
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
    session: {
      ...makeSessionState(sessionId, status),
      metadata: { runtime_state: { run_id: `run-${sessionId}` } },
    },
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
  listProviderModelsMock: vi.fn<() => Promise<ProviderModelsResult>>(),
  listAgentsMock: vi.fn<() => Promise<[]>>(),
  listSkillsMock: vi.fn<() => Promise<[]>>(),
  listCommandsMock: vi.fn<() => Promise<[]>>(),
  listSessionsMock: vi.fn<() => Promise<StoredSessionSummary[]>>(),
  listNotificationsMock: vi.fn<() => Promise<RuntimeNotification[]>>(),
  ackNotificationMock:
    vi.fn<(notificationId: string) => Promise<RuntimeNotification>>(),
  resumeSessionMock: vi.fn<(sessionId: string) => Promise<RuntimeResponse>>(),
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
    listSkills: runtimeClientMocks.listSkillsMock,
    listCommands: runtimeClientMocks.listCommandsMock,
    listSessions: runtimeClientMocks.listSessionsMock,
    listNotifications: runtimeClientMocks.listNotificationsMock,
    ackNotification: runtimeClientMocks.ackNotificationMock,
    resumeSession: runtimeClientMocks.resumeSessionMock,
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

const NO_DELEGATED_CONTEXT = {
  status: 404,
  code: "delegated_context_missing",
  message: "no delegated child context",
};

function seedWorkspaceRegistry() {
  queryClient.setQueryData(queryKeys.workspaceRegistry(), {
    current: {
      path: WORKSPACE_PATH,
      label: "workspace",
      available: true,
      current: true,
      last_opened_at: 1,
    },
    recent: [],
    candidates: [],
  });
}

function seedSessions(sessions: StoredSessionSummary[]) {
  queryClient.setQueryData(queryKeys.sessions(WORKSPACE_PATH), sessions);
}

function seedBackgroundTaskList(
  sessionId: string | null,
  tasks: BackgroundTaskSummary[],
) {
  queryClient.setQueryData(
    queryKeys.backgroundTasks(WORKSPACE_PATH, sessionId),
    tasks,
  );
}

function seedReviewSnapshot(snapshot: WorkspaceReviewSnapshot) {
  queryClient.setQueryData(queryKeys.review(WORKSPACE_PATH), snapshot);
}

function makeDebugSnapshot(sessionId: string): RuntimeSessionDebugSnapshot {
  return {
    session: makeSessionState(sessionId, "completed"),
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
    last_event_sequence: 2,
    suggested_operator_action: "replay",
    operator_guidance: "Replay the session.",
  };
}

function seedStatusSnapshot(snapshot: RuntimeStatusSnapshot) {
  queryClient.setQueryData(queryKeys.status(WORKSPACE_PATH), snapshot);
}

function seedSettings(settings: RuntimeSettings) {
  queryClient.setQueryData(queryKeys.settings(WORKSPACE_PATH), settings);
}

function seedDebugSnapshot(
  sessionId: string,
  snapshot: RuntimeSessionDebugSnapshot,
) {
  queryClient.setQueryData(
    queryKeys.sessionDebug(WORKSPACE_PATH, sessionId),
    snapshot,
  );
}

function cachedBackgroundTasks(sessionId: string | null) {
  return queryClient.getQueryData<BackgroundTaskSummary[]>(
    queryKeys.backgroundTasks(WORKSPACE_PATH, sessionId),
  );
}

// A completed mutation invalidates the surfaces it can have moved; entries with
// no mounted observer are marked stale and reload when they are next rendered.
function isInvalidated(key: readonly unknown[]): boolean {
  return queryClient
    .getQueryCache()
    .findAll({ queryKey: key })
    .some((query) => query.state.isInvalidated);
}

// The shared reset every suite in this file starts from: mocks, the persisted
// blob, and a fresh store module with default state. Registered by the suites
// that need it so a `-t`-filtered run is independent of the other suites.
async function resetStoreForTest() {
  vi.clearAllMocks();
  localStorage.clear();
  vi.resetModules();
  runtimeClientMocks.listNotificationsMock.mockResolvedValue([]);
  runtimeClientMocks.listSessionsMock.mockResolvedValue([]);
  runtimeClientMocks.getChildSessionContextMock.mockRejectedValue(
    NO_DELEGATED_CONTEXT,
  );
  ({ useAppStore } = await import("./store"));
  ({ queryClient, queryKeys } = await import("./lib/queries"));
  // Client state only: server payloads live in the query cache, which every test
  // starts empty and seeds through the helpers above. The workspace registry is
  // the exception — it is the scope every other key is built from, so the shell
  // always has one open here.
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
  seedWorkspaceRegistry();
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
    model_metadata: {},
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
  runtimeClientMocks.getSettingsMock.mockResolvedValue({
    provider_api_key_present: false,
  });
  runtimeClientMocks.updateSettingsMock.mockResolvedValue({
    provider_api_key_present: false,
  });
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
    task: makeTaskResult("task-1", {
      parent_session_id: "session-1",
      child_session_id: "child-session-1",
      approval_request_id: null,
      question_request_id: null,
      summary_output: "summary",
      routing: { mode: "background", subagent_type: "explore" },
    }),
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
    last_event_sequence: 2,
    suggested_operator_action: "replay",
    operator_guidance: "Replay the session.",
  });
  runtimeClientMocks.validateProviderCredentialsMock.mockResolvedValue({
    provider: "deepseek",
    configured: true,
    ok: true,
    status: "ok",
    message: "Remote provider validation succeeded.",
  });
}

describe("useAppStore integration flow", () => {
  beforeEach(resetStoreForTest);

  it("invalidates only the explicitly requested mutation surfaces", async () => {
    const sessionId = "session-refresh";
    useAppStore.setState({ currentSessionId: sessionId });
    seedSessions([]);
    seedStatusSnapshot(emptyStatusSnapshot);
    seedReviewSnapshot({
      root: "/workspace",
      git: { state: "git_ready", root: "/workspace" },
      changed_files: [],
      tree: [],
    });
    seedBackgroundTaskList(null, []);
    seedDebugSnapshot(sessionId, makeDebugSnapshot(sessionId));
    seedSettings({ provider_api_key_present: false });

    const { refreshAfterMutation } = await import("./lib/queries");
    await refreshAfterMutation({ sessions: true, status: true, review: true });

    // A completed mutation invalidates the surfaces it can have moved: an entry a
    // component is watching reloads at once, an inactive one is marked stale and
    // reloads when it is next rendered. Every other surface is left alone, which
    // is what this asserts instead of the old unconditional refetch counts.
    expect(isInvalidated(queryKeys.sessions(WORKSPACE_PATH))).toBe(true);
    expect(isInvalidated(queryKeys.status(WORKSPACE_PATH))).toBe(true);
    expect(isInvalidated(queryKeys.review(WORKSPACE_PATH))).toBe(true);
    expect(isInvalidated(queryKeys.backgroundTasksRoot(WORKSPACE_PATH))).toBe(
      false,
    );
    expect(
      isInvalidated(queryKeys.sessionDebug(WORKSPACE_PATH, sessionId)),
    ).toBe(false);
    expect(isInvalidated(queryKeys.settings(WORKSPACE_PATH))).toBe(false);

    await refreshAfterMutation({
      backgroundTasks: true,
      debug: true,
      sessionId,
    });

    expect(isInvalidated(queryKeys.backgroundTasksRoot(WORKSPACE_PATH))).toBe(
      true,
    );
    expect(
      isInvalidated(queryKeys.sessionDebug(WORKSPACE_PATH, sessionId)),
    ).toBe(true);
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
    seedSessions([]);

    const store = useAppStore.getState();
    await store.runTask("write note.txt hello", WORKSPACE_PATH);

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
    // The settled run refreshes the session list by invalidating it.
    expect(isInvalidated(queryKeys.sessions(WORKSPACE_PATH))).toBe(true);

    await state.selectSession(sessionId, WORKSPACE_PATH);

    state = useAppStore.getState();
    expect(runtimeClientMocks.getSessionReplayMock).toHaveBeenCalledWith(
      sessionId,
      expect.anything(),
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
    await store.runTask("ask a direction", WORKSPACE_PATH);

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

    await useAppStore
      .getState()
      .runTask("write nope.txt later", WORKSPACE_PATH);
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

    await state.selectSession(sessionId, WORKSPACE_PATH);

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

    runtimeClientMocks.getSessionReplayMock.mockResolvedValue(replay);
    // The runtime's flat list is the main-session surface, and the store reads it
    // out of the cache to know a delegated-context probe cannot apply.
    seedSessions([
      makeStoredSessionSummary(sessionId, "completed", "read note.txt"),
    ]);

    await useAppStore.persist.rehydrate();
    await useAppStore.getState().selectSession(sessionId, WORKSPACE_PATH);

    const state = useAppStore.getState();
    expect(state.language).toBe("zh-CN");
    expect(state.currentSessionId).toBe(sessionId);
    expect(state.agentPreset).toBe("leader");
    expect(state.providerModel).toBe("test-model/v1");
    expect(state.currentSessionState?.status).toBe("completed");
    expect(state.currentSessionOutput).toBe("note body");
    expect(runtimeClientMocks.getSessionReplayMock).toHaveBeenCalledWith(
      sessionId,
      expect.anything(),
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

    seedSessions([]);
    runtimeClientMocks.getSessionReplayMock.mockRejectedValue(
      new Error("Not Found"),
    );

    await useAppStore.persist.rehydrate();

    let state = useAppStore.getState();
    expect(state.currentSessionId).toBe(sessionId);

    // Nothing has been replayed behind the selection yet, so the list cannot
    // judge it: the selection survives this reconciliation.
    useAppStore.getState().reconcileSessionList([]);

    state = useAppStore.getState();
    expect(state.replayError).toBeNull();

    await useAppStore.getState().selectSession(sessionId, WORKSPACE_PATH);

    state = useAppStore.getState();
    expect(state.currentSessionId).toBeNull();
    expect(state.replayError).toBeNull();
  });

  it("resumes interrupted sessions with the authoritative transcript, not replay", async () => {
    const sessionId = "resume-session";
    const previousEvent = makeEvent(
      1,
      "runtime.request_received",
      {
        prompt: "continue",
      },
      "runtime",
      sessionId,
    );
    const resumedEvent = makeEvent(
      2,
      "graph.response_ready",
      {
        output: "continued",
      },
      "graph",
      sessionId,
    );
    const response = makeRuntimeResponse(
      sessionId,
      "completed",
      [previousEvent, resumedEvent],
      "continued",
    );
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary(sessionId, "interrupted", "continue"),
    ]);
    useAppStore.setState({
      currentSessionId: sessionId,
      currentSessionState: makeSessionState(sessionId, "interrupted"),
      currentSessionEvents: [previousEvent],
      currentSessionOutput: null,
      replayStatus: "success",
      runStatus: "idle",
    });
    runtimeClientMocks.resumeSessionMock.mockResolvedValue(response);

    await useAppStore.getState().resumeSession();

    const state = useAppStore.getState();
    expect(runtimeClientMocks.resumeSessionMock).toHaveBeenCalledWith(
      sessionId,
    );
    expect(runtimeClientMocks.getSessionReplayMock).not.toHaveBeenCalled();
    expect(state.currentSessionState?.status).toBe("completed");
    expect(state.currentSessionEvents).toEqual(response.events);
    expect(state.currentSessionOutput).toBe("continued");
  });

  it("keeps completed sessions replay-only and preserves events on resume errors", async () => {
    const sessionId = "sealed-session";
    const event = makeEvent(
      1,
      "runtime.request_received",
      { prompt: "sealed" },
      "runtime",
      sessionId,
    );
    useAppStore.setState({
      currentSessionId: sessionId,
      currentSessionState: makeSessionState(sessionId, "completed"),
      currentSessionEvents: [event],
      currentSessionOutput: "sealed output",
      replayStatus: "success",
      runStatus: "idle",
    });

    await useAppStore.getState().resumeSession();
    expect(runtimeClientMocks.resumeSessionMock).not.toHaveBeenCalled();

    useAppStore.setState({
      currentSessionState: makeSessionState(sessionId, "interrupted"),
      currentSessionEvents: [event],
      currentSessionOutput: "before resume",
      resumeStatus: "idle",
    });
    runtimeClientMocks.resumeSessionMock.mockRejectedValueOnce(
      new Error("resume unavailable"),
    );

    await useAppStore.getState().resumeSession();

    const state = useAppStore.getState();
    expect(state.resumeStatus).toBe("error");
    expect(state.currentSessionEvents).toEqual([event]);
    expect(state.currentSessionOutput).toBe("before resume");
  });

  it("judges a finished selection against the session list but never a live one", () => {
    // CONTRACT: the runtime's flat list is authoritative about *finished*
    // sessions; a session whose projection says it is live is judged on the
    // post-run refresh instead. The first half of this test fails if that guard
    // is removed.
    useAppStore.setState({
      currentSessionId: "live-session",
      currentSessionState: makeSessionState("live-session", "running"),
      currentSessionEvents: [
        makeEvent(
          1,
          "runtime.request_received",
          { prompt: "still running" },
          "runtime",
          "live-session",
        ),
      ],
    });

    useAppStore
      .getState()
      .reconcileSessionList([
        makeStoredSessionSummary("other-session", "completed", "other prompt"),
      ]);

    expect(useAppStore.getState().currentSessionId).toBe("live-session");
    expect(useAppStore.getState().currentSessionEvents).toHaveLength(1);

    // The same selection, once its run has settled, *is* judged: the session is
    // gone from the list, so the shell returns to the empty state.
    useAppStore.setState({
      currentSessionState: makeSessionState("live-session", "completed"),
    });

    useAppStore
      .getState()
      .reconcileSessionList([
        makeStoredSessionSummary("other-session", "completed", "other prompt"),
      ]);

    expect(useAppStore.getState().currentSessionId).toBeNull();
    expect(useAppStore.getState().currentSessionEvents).toEqual([]);
  });

  it("refreshes session-scoped background tasks after selecting a session", async () => {
    const firstTask = makeBackgroundTaskSummary("task-a", "prior session task");
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
    seedBackgroundTaskList("session-1", [firstTask]);
    useAppStore.setState({ currentSessionId: "session-1" });
    runtimeClientMocks.getSessionReplayMock.mockResolvedValue(replay);

    await useAppStore.getState().selectSession("session-2", WORKSPACE_PATH);

    // Selecting a session moves the task surface with it: the session scope is
    // part of the task list's key, and the store marks the surface stale so an
    // on-screen panel refetches for the session now selected. The payload itself
    // stays in the cache — the store never takes a copy of it.
    expect(useAppStore.getState().currentSessionId).toBe("session-2");
    expect(isInvalidated(queryKeys.backgroundTasksRoot(WORKSPACE_PATH))).toBe(
      true,
    );
    expect(cachedBackgroundTasks("session-1")).toEqual([firstTask]);
    expect(cachedBackgroundTasks("session-2")).toBeUndefined();
  });

  it("reloads global background tasks when selecting a new session", async () => {
    const sessionTask = makeBackgroundTaskSummary(
      "task-a",
      "prior session task",
    );
    const globalTask = makeBackgroundTaskSummary("task-global", "global task");
    seedBackgroundTaskList("session-1", [sessionTask]);
    seedBackgroundTaskList(null, [globalTask]);
    useAppStore.setState({ currentSessionId: "session-1" });

    await useAppStore.getState().selectSession("", WORKSPACE_PATH);

    // Clearing the selection drops the session scope, so the shell reads the
    // workspace-wide list under its own key. Neither list is the other's answer.
    expect(useAppStore.getState().currentSessionId).toBeNull();
    expect(cachedBackgroundTasks(null)).toEqual([globalTask]);
    expect(cachedBackgroundTasks("session-1")).toEqual([sessionTask]);
  });

  it("selects a background task output as client state, and clears the child view with it", () => {
    // Which task is selected is client state; the payload is the cache entry that
    // selection keys, so a superseded selection cannot show the other task's
    // output (pinned in the query layer's suite, which drives the hooks).
    useAppStore.setState({ childSessionParentId: "session-parent" });

    useAppStore.getState().selectBackgroundTaskOutput("task-fast");

    expect(useAppStore.getState().selectedBackgroundTaskOutputId).toBe(
      "task-fast",
    );
    expect(useAppStore.getState().childSessionParentId).toBe("session-parent");

    useAppStore.getState().selectBackgroundTaskOutput(null);

    expect(useAppStore.getState().selectedBackgroundTaskOutputId).toBeNull();
    // Returning from a delegated child means returning from the task view that
    // stood in for it, so the child link is cleared with the output.
    expect(useAppStore.getState().childSessionParentId).toBeNull();
  });
  it("restores the delegated child parent session on parent return", async () => {
    const parentEvents = [
      makeEvent(1, "runtime.request_received", { prompt: "parent prompt" }),
    ];
    const childOutput: BackgroundTaskOutput = {
      task: makeTaskResult("task-child", {
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
        routing: { mode: "background", subagent_type: "explore" },
      }),
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
          makeTranscriptEvent(
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
    seedBackgroundTaskList(null, []);
    runtimeClientMocks.getSessionReplayMock.mockResolvedValueOnce(
      makeRuntimeResponse(
        "session-parent",
        "completed",
        parentEvents,
        "parent output",
      ),
    );

    await useAppStore.getState().selectSession("child-session", WORKSPACE_PATH);

    expect(useAppStore.getState().currentSessionId).toBe("child-session");
    expect(useAppStore.getState().childSessionParentId).toBe("session-parent");
    // The child view marks the parent's task surface stale; the panel reads that
    // scope's list from its own cache key.
    expect(isInvalidated(queryKeys.backgroundTasksRoot(WORKSPACE_PATH))).toBe(
      true,
    );

    await useAppStore
      .getState()
      .selectSession(
        useAppStore.getState().childSessionParentId ?? "",
        WORKSPACE_PATH,
      );

    const state = useAppStore.getState();
    expect(runtimeClientMocks.getSessionReplayMock).toHaveBeenCalledWith(
      "session-parent",
      expect.anything(),
    );
    expect(state.currentSessionId).toBe("session-parent");
    expect(state.childSessionParentId).toBeNull();
    expect(state.currentSessionOutput).toBe("parent output");
    expect(state.selectedBackgroundTaskOutputId).toBeNull();
  });
  it("replays a listed main session without probing for a delegated context", async () => {
    // The runtime's flat session list is the main-session surface (delegated
    // children are filtered out of it), so a session found there has no parent
    // and the delegated-context lookup could only answer 404. Selecting such a
    // session must go straight to the replay.
    seedSessions([
      makeStoredSessionSummary("session-parent", "completed", "parent prompt"),
    ]);
    runtimeClientMocks.getSessionReplayMock.mockResolvedValueOnce(
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
    runtimeClientMocks.listSessionBackgroundTasksMock.mockResolvedValue([]);

    await useAppStore
      .getState()
      .selectSession("session-parent", WORKSPACE_PATH);

    expect(
      runtimeClientMocks.getChildSessionContextMock,
    ).not.toHaveBeenCalled();
    expect(runtimeClientMocks.getSessionReplayMock).toHaveBeenCalledWith(
      "session-parent",
      expect.anything(),
    );
    const state = useAppStore.getState();
    expect(state.currentSessionId).toBe("session-parent");
    expect(state.currentSessionOutput).toBe("parent output");
    expect(state.childSessionParentId).toBeNull();
    expect(state.replayStatus).toBe("success");
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

    await useAppStore.getState().runTask("write later", WORKSPACE_PATH);
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

    const runPromise = useAppStore
      .getState()
      .runTask("read slow.txt", WORKSPACE_PATH);
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

    const runPromise = useAppStore
      .getState()
      .runTask("read slow.txt", WORKSPACE_PATH);
    await Promise.resolve();
    await Promise.resolve();

    await useAppStore.getState().cancelCurrentRun();

    expect(runtimeClientMocks.cancelSessionMock).toHaveBeenCalledWith(
      sessionId,
      `run-${sessionId}`,
    );
    expect(useAppStore.getState().runStatus).toBe("cancelling");

    await useAppStore.getState().runTask("read second.txt", WORKSPACE_PATH);

    expect(runtimeClientMocks.runStreamMock).toHaveBeenCalledTimes(1);

    gate.resolve();
    await runPromise;

    expect(useAppStore.getState().runStatus).toBe("idle");
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

    const runPromise = useAppStore
      .getState()
      .runTask("read slow.txt", WORKSPACE_PATH);
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

    await useAppStore
      .getState()
      .runTask("read interrupted.txt", WORKSPACE_PATH);

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
          exception_message: "Insufficient balance.",
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

    await useAppStore.getState().runTask("say ok", WORKSPACE_PATH);

    const state = useAppStore.getState();
    expect(state.runStatus).toBe("error");
    expect(state.runError).toBe("Insufficient balance.");
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

    await useAppStore.getState().runTask("say ok", WORKSPACE_PATH);

    const state = useAppStore.getState();
    expect(state.currentSessionState?.status).toBe("failed");
    expect(state.runStatus).toBe("error");
    expect(state.runError).toBe("Provider authentication failed for deepseek.");
  });

  it("drops the in-flight streamed text when the runtime restarts the attempt", async () => {
    const sessionId = "retry-discard-session";
    const requestReceived = makeEvent(
      1,
      "runtime.request_received",
      { prompt: "hello" },
      "runtime",
      sessionId,
    );
    const firstAttemptDelta = makeEvent(
      1,
      "graph.provider_stream",
      { kind: "delta", channel: "text", text: "first attempt" },
      "graph",
      sessionId,
    );
    const retryEvent = makeEvent(
      2,
      "runtime.provider_transient_retry",
      { reason: "transient_failure", discarded_streamed_output: true },
      "runtime",
      sessionId,
    );
    const secondAttemptDelta = makeEvent(
      2,
      "graph.provider_stream",
      { kind: "delta", channel: "text", text: "second attempt" },
      "graph",
      sessionId,
    );
    const responseReady = makeEvent(
      3,
      "graph.response_ready",
      { output_preview: "second attempt" },
      "graph",
      sessionId,
    );

    const renderedAfterChunk: string[] = [];
    const latestAssistantText = () => {
      const messages = deriveChatMessages(
        useAppStore.getState().currentSessionEvents,
        null,
      );
      const assistants = messages.filter(
        (message) => message.role === "assistant",
      );
      return assistants[assistants.length - 1]?.content ?? "";
    };

    async function* stream() {
      yield makeStreamChunk(sessionId, "running", requestReceived);
      yield makeStreamChunk(sessionId, "running", firstAttemptDelta);
      yield makeStreamChunk(sessionId, "running", retryEvent);
      renderedAfterChunk.push(latestAssistantText());
      yield makeStreamChunk(sessionId, "running", secondAttemptDelta);
      renderedAfterChunk.push(latestAssistantText());
      yield makeStreamChunk(sessionId, "completed", responseReady);
      yield makeStreamChunk(sessionId, "completed", null, "second attempt");
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());
    runtimeClientMocks.getSessionReplayMock.mockResolvedValue(
      makeRuntimeResponse(
        sessionId,
        "completed",
        [requestReceived, responseReady],
        "second attempt",
      ),
    );
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary(sessionId, "completed", "hello"),
    ]);

    await useAppStore.getState().runTask("hello", WORKSPACE_PATH);

    // The restart event retracts the first attempt's live text, so the surviving
    // attempt is the only thing rendered (the persisted transcript is untouched).
    expect(renderedAfterChunk).toEqual(["", "second attempt"]);
    const messages = deriveChatMessages(
      useAppStore.getState().currentSessionEvents,
      null,
    );
    const assistants = messages.filter(
      (message) => message.role === "assistant",
    );
    const assistant = assistants[assistants.length - 1];
    expect(assistant?.content).toBe("second attempt");
  });

  it("uses the generic fallback when a failed session has no error event", async () => {
    const sessionId = "generic-failure-session";

    async function* stream() {
      yield makeStreamChunk(sessionId, "failed", null);
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());

    await useAppStore
      .getState()
      .runTask("fail without details", WORKSPACE_PATH);

    const state = useAppStore.getState();
    expect(state.runStatus).toBe("error");
    expect(state.runError).toBe("runtime session failed");
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
    seedSessions([
      makeStoredSessionSummary(
        sessionId,
        "waiting",
        "write approval-recover.txt recover",
      ),
    ]);

    await useAppStore
      .getState()
      .runTask("write approval-recover.txt recover", WORKSPACE_PATH);

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

    // The failed approval still refreshes the session list, by invalidating it.
    expect(isInvalidated(queryKeys.sessions(WORKSPACE_PATH))).toBe(true);
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

    await useAppStore
      .getState()
      .runTask("write rollback.txt retry", WORKSPACE_PATH);

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
  it("keeps the boot selection while the list refresh cannot judge it", async () => {
    // Nothing has been replayed yet, so the flat list cannot tell a delegated
    // child from a session that was deleted; dropping the selection here is
    // what lost a child on reload. This seeds the persisted blob the way the
    // shell writes it (the session id only), so it exercises that guard.
    const { useAppStore: store } = await import("./store");
    localStorage.setItem(
      "app-storage",
      JSON.stringify({
        state: {
          language: "en",
          agentPreset: "leader",
          providerModel: "test-model/v1",
          reasoningEffort: "",
          currentSessionId: "child-session",
          sessionSidebarWidth: 344,
          reviewMode: "changes",
        },
        version: 0,
      }),
    );
    await store.persist.rehydrate();
    expect(store.getState().currentSessionId).toBe("child-session");

    seedSessions([
      makeStoredSessionSummary("session-parent", "completed", "parent prompt"),
    ]);
    store
      .getState()
      .reconcileSessionList([
        makeStoredSessionSummary(
          "session-parent",
          "completed",
          "parent prompt",
        ),
      ]);

    expect(store.getState().currentSessionId).toBe("child-session");
    expect(store.getState().replayStatus).toBe("idle");
  });
  it("clears the selection in memory and in storage when the workspace switches", async () => {
    const { useAppStore: store } = await import("./store");
    store.setState({
      currentSessionId: "open-session",
      childSessionParentId: "session-parent",
      currentSessionState: makeSessionState("open-session", "completed"),
      currentSessionOutput: "open output",
    });

    // The switch action drops the previous workspace's client state before it
    // posts; the scoped payloads are unreachable through the new scope's keys.
    store.getState().prepareWorkspaceSwitch();

    expect(store.getState().currentSessionId).toBeNull();
    expect(store.getState().childSessionParentId).toBeNull();
    expect(
      JSON.parse(localStorage.getItem("app-storage") ?? "{}").state
        .currentSessionId,
    ).toBeNull();
  });
  it("accepts every live-only delta of a burst, not just the first", async () => {
    // Live-only frames share the persisted cursor rather than an identity of
    // their own, so the (session_id, sequence) dedupe that exists for replayed
    // persisted events must not swallow them: a running turn would otherwise
    // stop updating after its first delta.
    const { useAppStore: store } = await import("./store");
    store.setState({
      currentSessionId: "child-session",
      childSessionParentId: "session-parent",
      currentSessionEvents: [
        makeEvent(
          1,
          "runtime.request_received",
          { prompt: "child prompt" },
          "runtime",
          "child-session",
        ),
      ],
      currentSessionState: makeSessionState("child-session", "running"),
    });

    for (const text of ["one ", "two ", "three "]) {
      expect(
        store
          .getState()
          .mergeSessionEvent(
            makeEvent(
              4,
              "graph.provider_stream",
              { kind: "delta", channel: "text", text },
              "graph",
              "child-session",
            ),
          ),
      ).toBe(true);
    }
    const text = deriveChatMessages(
      store.getState().currentSessionEvents,
      null,
      "child-session",
    )
      .filter((message) => message.role === "assistant")
      .map((message) => message.content)
      .join("");
    expect(text).toContain("one two three ");

    // A persisted event still dedupes on its identity.
    const persisted = makeEvent(
      5,
      "runtime.tool_completed",
      { tool: "read", tool_call_id: "call-1", content: "ok" },
      "runtime",
      "child-session",
    );
    expect(store.getState().mergeSessionEvent(persisted)).toBe(true);
    expect(store.getState().mergeSessionEvent(persisted)).toBe(false);
  });

  it("accepts pushes for the session on screen and drops other sessions", async () => {
    const { useAppStore: store } = await import("./store");
    store.setState({
      currentSessionId: "child-session",
      childSessionParentId: "session-parent",
      currentSessionEvents: [],
      currentSessionState: makeSessionState("child-session", "running"),
    });

    const accepted = store
      .getState()
      .mergeSessionEvent(
        makeEvent(
          7,
          "graph.provider_stream",
          { kind: "delta", channel: "text", text: "child delta" },
          "graph",
          "child-session",
        ),
      );
    expect(accepted).toBe(true);
    expect(store.getState().currentSessionEvents).toHaveLength(1);

    const dropped = store
      .getState()
      .mergeSessionEvent(
        makeEvent(
          8,
          "graph.provider_stream",
          { kind: "delta", channel: "text", text: "parent delta" },
          "graph",
          "session-parent",
        ),
      );
    expect(dropped).toBe(false);
    expect(store.getState().currentSessionEvents).toHaveLength(1);

    expect(
      store
        .getState()
        .mergeSessionState(makeSessionState("child-session", "completed")),
    ).toBe(true);
    expect(store.getState().currentSessionState?.status).toBe("completed");
    expect(
      store
        .getState()
        .mergeSessionState(makeSessionState("session-parent", "running")),
    ).toBe(false);
    expect(store.getState().currentSessionState?.status).toBe("completed");
  });

  it("drops a re-pushed live delta whose payload the transcript already holds", async () => {
    const { useAppStore: store } = await import("./store");
    store.setState({
      currentSessionId: "session-1",
      childSessionParentId: null,
      currentSessionEvents: [
        makeEvent(
          1,
          "runtime.request_received",
          { prompt: "hello" },
          "runtime",
          "session-1",
        ),
      ],
      currentSessionState: makeSessionState("session-1", "running"),
    });

    // Live-only frames share the persisted cursor, so identity alone cannot
    // tell a re-pushed frame from the next delta of the burst.
    const delta = (text: string) =>
      makeEvent(
        4,
        "graph.provider_stream",
        { kind: "delta", channel: "text", text },
        "graph",
        "session-1",
      );

    expect(store.getState().mergeSessionEvent(delta("partial "))).toBe(true);
    // The same payload re-pushed is a duplicate: the transcript must not grow.
    expect(store.getState().mergeSessionEvent(delta("partial "))).toBe(false);
    expect(store.getState().currentSessionEvents).toHaveLength(2);
    // New content under the same cursor is still the rest of the burst.
    expect(store.getState().mergeSessionEvent(delta("partial more"))).toBe(
      true,
    );
    expect(store.getState().currentSessionEvents).toHaveLength(3);
  });
});
