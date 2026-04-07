import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import App from './App';
import { useAppStore } from './store';
import type { EventEnvelope, StoredSessionSummary } from './lib/runtime/types';
import './i18n';

vi.mock('./store', () => ({
  useAppStore: vi.fn(),
}));

vi.mock('./components/RuntimeDebug', () => ({
  RuntimeDebug: () => <div data-testid="runtime-debug-mock" />
}));

describe('App', () => {
  const makeEvent = (overrides: Partial<EventEnvelope>): EventEnvelope => ({
    session_id: 'session-123',
    sequence: 1,
    event_type: 'runtime.request_received',
    source: 'runtime',
    payload: { prompt: 'read README.md' },
    ...overrides,
  });

  const makeSession = (id: string): StoredSessionSummary => ({
    session: { id },
    status: 'completed',
    turn: 1,
    prompt: 'read README.md',
    updated_at: 1,
  });

  const createMockStore = () => ({
    language: 'en' as const,
    setLanguage: vi.fn(),
    sessions: [] as StoredSessionSummary[],
    currentSessionId: null as string | null,
    currentSessionEvents: [] as EventEnvelope[],
    loadSessions: vi.fn().mockResolvedValue(undefined),
    sessionsStatus: 'success' as const,
    sessionsError: null as string | null,
    selectSession: vi.fn().mockResolvedValue(undefined),
    runTask: vi.fn().mockResolvedValue(undefined),
    replayStatus: 'idle' as const,
    replayError: null as string | null,
    runStatus: 'idle' as const,
    runError: null as string | null,
  });

  let mockStore = createMockStore();

  beforeEach(() => {
    vi.clearAllMocks();
    mockStore = createMockStore();
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(mockStore);
    (useAppStore as unknown as { getState: () => typeof mockStore }).getState = () => mockStore;
  });

  it('toggles language when language button is clicked', () => {
    render(<App />);

    const langBtn = screen.getByText('中文');
    expect(langBtn).toBeInTheDocument();

    fireEvent.click(langBtn);

    expect(mockStore.setLanguage).toHaveBeenCalledWith('zh-CN');
  });

  it('renders runtime-backed sessions, tasks, and activities', async () => {
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      sessions: [makeSession('session-123')],
      currentSessionId: 'session-123',
      currentSessionEvents: [
        makeEvent({ sequence: 1, payload: { prompt: 'read README.md' } }),
        makeEvent({
          sequence: 2,
          event_type: 'graph.tool_request_created',
          source: 'graph',
          payload: { tool: 'read_file', arguments: { path: 'README.md' }, path: 'README.md' },
        }),
        makeEvent({
          sequence: 3,
          event_type: 'runtime.tool_completed',
          source: 'tool',
          payload: { content: 'VoidCode' },
        }),
        makeEvent({
          sequence: 4,
          event_type: 'graph.response_ready',
          source: 'graph',
          payload: { output_preview: 'VoidCode' },
        }),
      ],
    });

    render(<App />);

    expect(screen.getByText(/Current Session/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /session-123/i })).toBeInTheDocument();
    expect(screen.getByText('Request: read README.md')).toBeInTheDocument();
    expect(screen.getByText('Tool: read_file')).toBeInTheDocument();
    expect(screen.getByText('Response Ready')).toBeInTheDocument();
    expect(screen.getByText('runtime.request_received')).toBeInTheDocument();
    expect(screen.getByText('graph.response_ready')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /session-123/i }));

    await waitFor(() => {
      expect(mockStore.selectSession).toHaveBeenCalledWith('session-123');
    });
  });

  it('submits a new prompt through the runtime-backed run action', async () => {
    render(<App />);

    fireEvent.change(screen.getByPlaceholderText('Enter a task to run...'), {
      target: { value: 'read README.md' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'New Task' }));

    await waitFor(() => {
      expect(mockStore.runTask).toHaveBeenCalledWith('read README.md');
    });
  });
});
