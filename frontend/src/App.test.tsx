import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import App from './App';
import { useAppStore } from './store';
import './i18n';

vi.mock('./store', () => ({
  useAppStore: vi.fn(),
}));

vi.mock('./components/RuntimeDebug', () => ({
  RuntimeDebug: () => <div data-testid="runtime-debug-mock" />
}));

describe('App', () => {
  const mockStore = {
    language: 'en',
    setLanguage: vi.fn(),
    sessions: [],
    currentSessionId: null,
    currentSessionState: null,
    currentSessionEvents: [],
    currentSessionOutput: null,
    loadSessions: vi.fn(),
    sessionsStatus: 'success',
    sessionsError: null,
    selectSession: vi.fn(),
    runTask: vi.fn(),
    resolveApproval: vi.fn(),
    replayStatus: 'idle',
    replayError: null,
    runStatus: 'idle',
    runError: null,
    approvalStatus: 'idle',
    approvalError: null,
  };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-04-16T06:00:00Z'));
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(mockStore);
    (useAppStore as unknown as { getState: () => typeof mockStore }).getState = () => mockStore;
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('toggles language when language button is clicked', () => {
    render(<App />);

    const langBtn = screen.getByText('中文');
    expect(langBtn).toBeInTheDocument();

    fireEvent.click(langBtn);

    expect(mockStore.setLanguage).toHaveBeenCalledWith('zh-CN');
  });

  it('renders tasks and events when current session has events', () => {
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      currentSessionEvents: [
        {
          session_id: 'session-1',
          sequence: 1,
          event_type: 'graph.tool_request_created',
          source: 'graph',
          payload: { tool: 'test_tool' }
        },
        {
          session_id: 'session-1',
          sequence: 2,
          event_type: 'runtime.tool_completed',
          source: 'tool',
          payload: { tool: 'test_tool', result: 'success' }
        }
      ]
    });

    render(<App />);

    const emptyStates = screen.queryAllByText('activity.empty');
    expect(emptyStates).toHaveLength(0);
    expect(screen.getByText(/Current Session/i)).toBeInTheDocument();
  });

  it('renders output panel when currentSessionOutput exists', () => {
    const testOutput = 'This is the final output from the agent.';
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      currentSessionOutput: testOutput
    });

    render(<App />);

    expect(screen.getByText('Final Output')).toBeInTheDocument();
    expect(screen.getByText(testOutput)).toBeInTheDocument();
  });

  it('hides stale output when a new run clears the current turn output', () => {
    const { rerender } = render(<App />);

    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      currentSessionOutput: 'previous output',
      runStatus: 'success'
    });
    rerender(<App />);

    expect(screen.getByText('Final Output')).toBeInTheDocument();
    expect(screen.getByText('previous output')).toBeInTheDocument();

    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      currentSessionOutput: null,
      runStatus: 'running'
    });
    rerender(<App />);

    expect(screen.queryByText('Final Output')).not.toBeInTheDocument();
    expect(screen.queryByText('previous output')).not.toBeInTheDocument();
  });

  it('renders the output panel for empty string output', () => {
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      currentSessionOutput: ''
    });

    render(<App />);

    expect(screen.getByText('Final Output')).toBeInTheDocument();
  });

  it('renders approval controls for waiting sessions and triggers allow', () => {
    const resolveApproval = vi.fn();
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      currentSessionId: 'session-1',
      currentSessionState: {
        session: { id: 'session-1' },
        status: 'waiting',
        turn: 1,
        metadata: {}
      },
      currentSessionEvents: [
        {
          session_id: 'session-1',
          sequence: 1,
          event_type: 'runtime.approval_requested',
          source: 'runtime',
          payload: {
            request_id: 'approval-1',
            tool: 'write_file',
            target_summary: 'write README.md'
          }
        }
      ],
      resolveApproval
    });

    render(<App />);

    expect(screen.getByText('Approval Required')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Allow' }));

    expect(resolveApproval).toHaveBeenCalledWith('allow');
  });

  it('triggers deny for waiting sessions', () => {
    const resolveApproval = vi.fn();
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      currentSessionId: 'session-1',
      currentSessionState: {
        session: { id: 'session-1' },
        status: 'waiting',
        turn: 1,
        metadata: {}
      },
      currentSessionEvents: [
        {
          session_id: 'session-1',
          sequence: 1,
          event_type: 'runtime.approval_requested',
          source: 'runtime',
          payload: {
            request_id: 'approval-1',
            tool: 'write_file',
            target_summary: 'write README.md'
          }
        }
      ],
      resolveApproval
    });

    render(<App />);

    fireEvent.click(screen.getByRole('button', { name: 'Deny' }));

    expect(resolveApproval).toHaveBeenCalledWith('deny');
  });

  it('hides approval controls when session is not waiting', () => {
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      currentSessionState: {
        session: { id: 'session-1' },
        status: 'completed',
        turn: 1,
        metadata: {}
      }
    });

    render(<App />);

    expect(screen.queryByText('Approval Required')).not.toBeInTheDocument();
  });

  it('renders approval error and disables controls while submitting', () => {
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      currentSessionId: 'session-1',
      currentSessionState: {
        session: { id: 'session-1' },
        status: 'waiting',
        turn: 1,
        metadata: {}
      },
      currentSessionEvents: [
        {
          session_id: 'session-1',
          sequence: 1,
          event_type: 'runtime.approval_requested',
          source: 'runtime',
          payload: {
            request_id: 'approval-1',
            tool: 'write_file',
            target_summary: 'write README.md'
          }
        }
      ],
      approvalStatus: 'submitting',
      approvalError: 'boom'
    });

    render(<App />);

    expect(screen.getByText('Approval failed: boom')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: 'Submitting...' })).toHaveLength(2);
  });

  it('renders the session list item with prompt-first title, status, and updated time', () => {
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      sessions: [
        {
          session: { id: 'session-123456789' },
          status: 'completed',
          turn: 5,
          prompt: 'test prompt subtitle',
          updated_at: Math.floor(new Date('2026-04-16T05:58:00Z').getTime() / 1000)
        }
      ]
    });

    render(<App />);

    expect(screen.getByText('test prompt subtitle')).toBeInTheDocument();
    expect(screen.getAllByText('session-').length).toBeGreaterThan(0);
    expect(screen.getByText('T5')).toBeInTheDocument();
    expect(screen.getByText('Completed')).toBeInTheDocument();
    expect(screen.getByText('2m ago')).toBeInTheDocument();
  });

  it('renders idle session status labels from contract-valid summaries', () => {
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      sessions: [
        {
          session: { id: 'session-idle-123' },
          status: 'idle',
          turn: 1,
          prompt: 'Resume existing session',
          updated_at: Math.floor(new Date('2026-04-16T05:59:30Z').getTime() / 1000)
        }
      ]
    });

    render(<App />);

    expect(screen.getByText('Pending')).toBeInTheDocument();
  });

  it('renders the header subtitle with current session prompt', () => {
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      currentSessionId: 'session-123456789',
      sessions: [
        {
          session: { id: 'session-123456789' },
          status: 'completed',
          turn: 5,
          prompt: 'test prompt subtitle',
          updated_at: 1000
        }
      ]
    });

    render(<App />);

    const headers = screen.getAllByText('test prompt subtitle');
    expect(headers.length).toBeGreaterThan(0);
    expect(screen.getByText('session-123456789')).toBeInTheDocument();
  });

  it('falls back to the replayed request prompt in the header when summary prompt is unavailable', () => {
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      currentSessionId: 'session-123456789',
      currentSessionEvents: [
        {
          session_id: 'session-123456789',
          sequence: 1,
          event_type: 'runtime.request_received',
          source: 'runtime',
          payload: { prompt: 'prompt from replay' }
        }
      ]
    });

    render(<App />);

    expect(screen.getByText('prompt from replay')).toBeInTheDocument();
  });

  it('prefers the latest replayed request prompt in the header fallback', () => {
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      currentSessionId: 'session-123456789',
      currentSessionEvents: [
        {
          session_id: 'session-123456789',
          sequence: 1,
          event_type: 'runtime.request_received',
          source: 'runtime',
          payload: { prompt: 'old prompt' }
        },
        {
          session_id: 'session-123456789',
          sequence: 4,
          event_type: 'runtime.request_received',
          source: 'runtime',
          payload: { prompt: 'latest prompt' }
        }
      ]
    });

    render(<App />);

    expect(screen.getByText('latest prompt')).toBeInTheDocument();
    expect(screen.queryByText('old prompt')).not.toBeInTheDocument();
  });
});
