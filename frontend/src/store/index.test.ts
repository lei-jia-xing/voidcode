import { beforeEach, describe, expect, it, vi } from 'vitest';
import { RuntimeClient } from '../lib/runtime/client';
import type {
  EventEnvelope,
  RuntimeResponse,
  RuntimeStreamChunk,
  SessionState,
  StoredSessionSummary,
} from '../lib/runtime/types';
import { useAppStore } from './index';

const { localStorageMock } = vi.hoisted(() => {
  const storage = new Map<string, string>();
  const mock = {
    getItem: vi.fn((key: string) => storage.get(key) ?? null),
    setItem: vi.fn((key: string, value: string) => {
      storage.set(key, value);
    }),
    removeItem: vi.fn((key: string) => {
      storage.delete(key);
    }),
    clear: vi.fn(() => {
      storage.clear();
    }),
  };

  Object.defineProperty(globalThis, 'localStorage', {
    value: mock,
    configurable: true,
    writable: true,
  });

  return { localStorageMock: mock };
});

vi.mock('../lib/runtime/client', () => ({
  RuntimeClient: {
    listSessions: vi.fn(),
    getSessionReplay: vi.fn(),
    runStream: vi.fn(),
  },
}));

const mockedRuntimeClient = vi.mocked(RuntimeClient);

const makeSessionState = (id: string, status: SessionState['status'] = 'completed'): SessionState => ({
  session: { id },
  status,
  turn: 1,
  metadata: { workspace: '/tmp/workspace' },
});

const makeSessionSummary = (id: string): StoredSessionSummary => ({
  session: { id },
  status: 'completed',
  turn: 1,
  prompt: 'read README.md',
  updated_at: 1,
});

const makeEvent = (overrides: Partial<EventEnvelope>): EventEnvelope => ({
  session_id: 'session-123',
  sequence: 1,
  event_type: 'runtime.request_received',
  source: 'runtime',
  payload: { prompt: 'read README.md' },
  ...overrides,
});

const makeReplay = (): RuntimeResponse => ({
  session: makeSessionState('session-123'),
  events: [
    makeEvent({ sequence: 1 }),
    makeEvent({
      sequence: 2,
      event_type: 'graph.response_ready',
      source: 'graph',
      payload: { output_preview: 'done' },
    }),
  ],
  output: 'done',
});

async function* streamChunks(chunks: RuntimeStreamChunk[]): AsyncGenerator<RuntimeStreamChunk, void, unknown> {
  for (const chunk of chunks) {
    yield chunk;
  }
}

describe('useAppStore transport flow', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorageMock.clear();
    useAppStore.setState({
      language: 'en',
      sessions: [],
      currentSessionId: null,
      currentSessionState: null,
      currentSessionEvents: [],
      sessionsStatus: 'idle',
      sessionsError: null,
      replayStatus: 'idle',
      replayError: null,
      runStatus: 'idle',
      runError: null,
      replayRequestId: 0,
    });
  });

  it('loads persisted sessions through the runtime client', async () => {
    mockedRuntimeClient.listSessions.mockResolvedValue([makeSessionSummary('session-123')]);

    await useAppStore.getState().loadSessions();

    expect(mockedRuntimeClient.listSessions).toHaveBeenCalledTimes(1);
    expect(useAppStore.getState().sessions).toEqual([makeSessionSummary('session-123')]);
    expect(useAppStore.getState().sessionsStatus).toBe('success');
  });

  it('replays a selected session through the runtime client', async () => {
    mockedRuntimeClient.getSessionReplay.mockResolvedValue(makeReplay());

    await useAppStore.getState().selectSession('session-123');

    expect(mockedRuntimeClient.getSessionReplay).toHaveBeenCalledWith('session-123');
    expect(useAppStore.getState().currentSessionId).toBe('session-123');
    expect(useAppStore.getState().currentSessionState).toEqual(makeSessionState('session-123'));
    expect(useAppStore.getState().currentSessionEvents).toEqual(makeReplay().events);
    expect(useAppStore.getState().replayStatus).toBe('success');
  });

  it('streams a runtime run, appends ordered events, and refreshes sessions', async () => {
    const loadSessionsSpy = vi.spyOn(useAppStore.getState(), 'loadSessions').mockResolvedValue(undefined);
    const chunks: RuntimeStreamChunk[] = [
      {
        kind: 'event',
        session: makeSessionState('session-123', 'running'),
        event: makeEvent({ sequence: 1, payload: { prompt: 'read README.md' } }),
        output: null,
      },
      {
        kind: 'event',
        session: makeSessionState('session-123', 'completed'),
        event: makeEvent({
          sequence: 2,
          event_type: 'graph.response_ready',
          source: 'graph',
          payload: { output_preview: 'done' },
        }),
        output: null,
      },
      {
        kind: 'output',
        session: makeSessionState('session-123', 'completed'),
        event: null,
        output: 'done',
      },
    ];
    mockedRuntimeClient.runStream.mockReturnValue(streamChunks(chunks));

    await useAppStore.getState().runTask('read README.md');

    expect(mockedRuntimeClient.runStream).toHaveBeenCalledWith({
      prompt: 'read README.md',
      session_id: null,
    });
    expect(useAppStore.getState().currentSessionId).toBe('session-123');
    expect(useAppStore.getState().currentSessionState).toEqual(makeSessionState('session-123', 'completed'));
    expect(useAppStore.getState().currentSessionEvents).toEqual(chunks.flatMap((chunk) => (chunk.event ? [chunk.event] : [])));
    expect(useAppStore.getState().runStatus).toBe('success');
    expect(loadSessionsSpy).toHaveBeenCalledTimes(1);
  });
});
