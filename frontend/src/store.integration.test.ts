import { beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  ApprovalDecision,
  EventEnvelope,
  RuntimeResponse,
  RuntimeStreamChunk,
  SessionState,
  StoredSessionSummary,
} from './lib/runtime/types';

type PersistedState = {
  state: {
    language: 'en' | 'zh-CN';
    currentSessionId: string | null;
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

Object.defineProperty(globalThis, 'localStorage', {
  value: testStorage,
  configurable: true,
});

let useAppStore: typeof import('./store').useAppStore;

function makeSessionState(sessionId: string, status: SessionState['status']): SessionState {
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
  source: EventEnvelope['source'] = 'runtime',
  sessionId = 'session-1',
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
  status: StoredSessionSummary['status'],
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

function makeRuntimeResponse(
  sessionId: string,
  status: SessionState['status'],
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
  status: SessionState['status'],
  event: EventEnvelope | null,
  output: string | null = null,
): RuntimeStreamChunk {
  return {
    kind: output === null ? 'event' : 'output',
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
  listSessionsMock: vi.fn<() => Promise<StoredSessionSummary[]>>(),
  getSessionReplayMock: vi.fn<(sessionId: string) => Promise<RuntimeResponse>>(),
  resolveApprovalMock: vi.fn<
    (sessionId: string, requestId: string, decision: ApprovalDecision) => Promise<RuntimeResponse>
  >(),
  runStreamMock: vi.fn<
    (request: { prompt: string; session_id?: string | null; metadata?: Record<string, unknown> }) => AsyncGenerator<RuntimeStreamChunk, void, unknown>
  >(),
}));

vi.mock('./lib/runtime/client', () => ({
  RuntimeClient: {
    listSessions: runtimeClientMocks.listSessionsMock,
    getSessionReplay: runtimeClientMocks.getSessionReplayMock,
    resolveApproval: runtimeClientMocks.resolveApprovalMock,
    runStream: runtimeClientMocks.runStreamMock,
  },
}));

describe('useAppStore integration flow', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    localStorage.clear();
    vi.resetModules();
    ({ useAppStore } = await import('./store'));
    useAppStore.setState({
      language: 'en',
      sessions: [],
      currentSessionId: null,
      currentSessionState: null,
      currentSessionEvents: [],
      currentSessionOutput: null,
      sessionsStatus: 'idle',
      sessionsError: null,
      replayStatus: 'idle',
      replayError: null,
      runStatus: 'idle',
      runError: null,
      approvalStatus: 'idle',
      approvalError: null,
      replayRequestId: 0,
    });
    runtimeClientMocks.listSessionsMock.mockResolvedValue([]);
  });

  it('handles run -> waiting approval -> allow -> replay through the real store', async () => {
    const sessionId = 'session-1';
    const requestId = 'approval-1';
    const requestReceived = makeEvent(1, 'runtime.request_received', { prompt: 'write note.txt hello' });
    const approvalRequested = makeEvent(
      2,
      'runtime.approval_requested',
      { request_id: requestId, tool: 'write_file', target_summary: 'note.txt', decision: 'ask' },
      'runtime',
      sessionId,
    );
    const approvalResolved = makeEvent(
      3,
      'runtime.approval_resolved',
      { request_id: requestId, decision: 'allow' },
      'runtime',
      sessionId,
    );
    const toolCompleted = makeEvent(4, 'runtime.tool_completed', { path: 'note.txt' }, 'tool', sessionId);
    const responseReady = makeEvent(5, 'graph.response_ready', { output_preview: 'hello' }, 'graph', sessionId);
    const completedResponse = makeRuntimeResponse(
      sessionId,
      'completed',
      [requestReceived, approvalRequested, approvalResolved, toolCompleted, responseReady],
      'hello',
    );

    async function* stream() {
      yield makeStreamChunk(sessionId, 'running', requestReceived);
      yield makeStreamChunk(sessionId, 'waiting', approvalRequested);
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());
    runtimeClientMocks.resolveApprovalMock.mockResolvedValue(completedResponse);
    runtimeClientMocks.getSessionReplayMock.mockResolvedValue(completedResponse);
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary(sessionId, 'completed', 'write note.txt hello'),
    ]);

    const store = useAppStore.getState();
    await store.runTask('write note.txt hello');

    let state = useAppStore.getState();
    expect(state.currentSessionId).toBe(sessionId);
    expect(state.currentSessionState?.status).toBe('waiting');
    expect(state.currentSessionEvents.map((event) => event.event_type)).toEqual([
      'runtime.request_received',
      'runtime.approval_requested',
    ]);
    expect(state.runStatus).toBe('success');

    await state.resolveApproval('allow');

    state = useAppStore.getState();
    expect(runtimeClientMocks.resolveApprovalMock).toHaveBeenCalledWith(sessionId, requestId, 'allow');
    expect(state.currentSessionState?.status).toBe('completed');
    expect(state.currentSessionOutput).toBe('hello');
    expect(state.currentSessionEvents.map((event) => event.event_type)).toEqual([
      'runtime.request_received',
      'runtime.approval_requested',
      'runtime.approval_resolved',
      'runtime.tool_completed',
      'graph.response_ready',
    ]);
    expect(state.sessions).toEqual([
      makeStoredSessionSummary(sessionId, 'completed', 'write note.txt hello'),
    ]);

    await state.selectSession(sessionId);

    state = useAppStore.getState();
    expect(runtimeClientMocks.getSessionReplayMock).toHaveBeenCalledWith(sessionId);
    expect(state.currentSessionState?.status).toBe('completed');
    expect(state.currentSessionOutput).toBe('hello');
    expect(state.currentSessionEvents).toEqual(completedResponse.events);
  });

  it('handles deny and preserves failed replay through the real store', async () => {
    const sessionId = 'session-deny';
    const requestId = 'approval-deny';
    const requestReceived = makeEvent(1, 'runtime.request_received', { prompt: 'write nope.txt later' }, 'runtime', sessionId);
    const approvalRequested = makeEvent(
      2,
      'runtime.approval_requested',
      { request_id: requestId, tool: 'write_file', target_summary: 'nope.txt', decision: 'ask' },
      'runtime',
      sessionId,
    );
    const approvalResolved = makeEvent(
      3,
      'runtime.approval_resolved',
      { request_id: requestId, decision: 'deny' },
      'runtime',
      sessionId,
    );
    const failedEvent = makeEvent(4, 'runtime.failed', { error: 'permission denied' }, 'runtime', sessionId);
    const failedResponse = makeRuntimeResponse(
      sessionId,
      'failed',
      [requestReceived, approvalRequested, approvalResolved, failedEvent],
      null,
    );

    async function* stream() {
      yield makeStreamChunk(sessionId, 'running', requestReceived);
      yield makeStreamChunk(sessionId, 'waiting', approvalRequested);
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());
    runtimeClientMocks.resolveApprovalMock.mockResolvedValue(failedResponse);
    runtimeClientMocks.getSessionReplayMock.mockResolvedValue(failedResponse);
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary(sessionId, 'failed', 'write nope.txt later'),
    ]);

    await useAppStore.getState().runTask('write nope.txt later');
    await useAppStore.getState().resolveApproval('deny');

    const state = useAppStore.getState();
    expect(state.currentSessionState?.status).toBe('failed');
    expect(state.currentSessionOutput).toBeNull();
    expect(state.currentSessionEvents.map((event) => event.event_type)).toEqual([
      'runtime.request_received',
      'runtime.approval_requested',
      'runtime.approval_resolved',
      'runtime.failed',
    ]);

    await state.selectSession(sessionId);

    expect(useAppStore.getState().currentSessionEvents).toEqual(failedResponse.events);
  });

  it('hydrates currentSessionId and replays the persisted session on load', async () => {
    const sessionId = 'persisted-session';
    const replay = makeRuntimeResponse(
      sessionId,
      'completed',
      [makeEvent(1, 'runtime.request_received', { prompt: 'read note.txt' }, 'runtime', sessionId)],
      'note body',
    );

    const persisted: PersistedState = {
      state: { language: 'zh-CN', currentSessionId: sessionId },
      version: 0,
    };
    localStorage.setItem('app-storage', JSON.stringify(persisted));

    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary(sessionId, 'completed', 'read note.txt'),
    ]);
    runtimeClientMocks.getSessionReplayMock.mockResolvedValue(replay);

    await useAppStore.persist.rehydrate();
    await useAppStore.getState().loadSessions();
    await useAppStore.getState().selectSession(sessionId);

    const state = useAppStore.getState();
    expect(state.language).toBe('zh-CN');
    expect(state.currentSessionId).toBe(sessionId);
    expect(state.currentSessionState?.status).toBe('completed');
    expect(state.currentSessionOutput).toBe('note body');
    expect(runtimeClientMocks.getSessionReplayMock).toHaveBeenCalledWith(sessionId);
  });

  it('surfaces approval lookup failure when no pending request exists', async () => {
    const sessionId = 'broken-session';
    const requestReceived = makeEvent(1, 'runtime.request_received', { prompt: 'write later' }, 'runtime', sessionId);

    async function* stream() {
      yield makeStreamChunk(sessionId, 'running', requestReceived);
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());

    await useAppStore.getState().runTask('write later');
    await useAppStore.getState().resolveApproval('allow');

    const state = useAppStore.getState();
    expect(runtimeClientMocks.resolveApprovalMock).not.toHaveBeenCalled();
    expect(state.approvalStatus).toBe('error');
    expect(state.approvalError).toBe('No pending approval request found.');
  });

  it('keeps run status running while the stream is still open', async () => {
    const gate = createDeferred<void>();
    const sessionId = 'slow-session';
    const requestReceived = makeEvent(1, 'runtime.request_received', { prompt: 'read slow.txt' }, 'runtime', sessionId);

    async function* stream() {
      yield makeStreamChunk(sessionId, 'running', requestReceived);
      await gate.promise;
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());

    const runPromise = useAppStore.getState().runTask('read slow.txt');
    await Promise.resolve();
    await Promise.resolve();

    expect(useAppStore.getState().runStatus).toBe('running');

    gate.resolve();
    await runPromise;

    expect(useAppStore.getState().runStatus).toBe('success');
  });

  it('passes runtime metadata through runTask options', async () => {
    const sessionId = 'session-meta';
    const requestReceived = makeEvent(1, 'runtime.request_received', { prompt: 'analyze repo' }, 'runtime', sessionId);

    async function* stream() {
      yield makeStreamChunk(sessionId, 'completed', requestReceived);
      yield makeStreamChunk(sessionId, 'completed', null, 'ok');
    }

    runtimeClientMocks.runStreamMock.mockReturnValue(stream());
    runtimeClientMocks.listSessionsMock.mockResolvedValue([
      makeStoredSessionSummary(sessionId, 'completed', 'analyze repo'),
    ]);

    await useAppStore.getState().runTask('analyze repo', {
      metadata: {
        skills: ['demo'],
        max_steps: 5,
        provider_stream: true,
      },
    });

    expect(runtimeClientMocks.runStreamMock).toHaveBeenCalledWith({
      prompt: 'analyze repo',
      session_id: null,
      metadata: {
        skills: ['demo'],
        max_steps: 5,
        provider_stream: true,
      },
    });
  });
});
