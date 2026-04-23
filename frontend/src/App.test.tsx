import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import App from './App';
import { useAppStore } from './store';
import './i18n';

vi.mock('./store', () => ({
  useAppStore: vi.fn(),
}));

vi.mock('./components/SettingsPanel', () => ({
  SettingsPanel: () => <div data-testid="settings-panel-mock" />
}));

describe('App', () => {
  const mockStore = {
    language: 'en',
    setLanguage: vi.fn(),
    agentPreset: 'leader',
    providerModel: 'opencode-go/glm-5.1',
    setAgentPreset: vi.fn(),
    setProviderModel: vi.fn(),
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
    settings: null,
    settingsStatus: 'idle',
    settingsError: null,
    loadSettings: vi.fn(),
    updateSettings: vi.fn(),
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

  it('renders composer and triggers runTask on submit', () => {
    render(<App />);

    const textarea = screen.getByPlaceholderText('Ask VoidCode to do something...');
    expect(textarea).toBeInTheDocument();

    fireEvent.change(textarea, { target: { value: 'read README.md' } });
    fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });

    expect(mockStore.runTask).toHaveBeenCalledWith('read README.md');
  });

  it('renders chat messages when current session has events', () => {
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      currentSessionEvents: [
        {
          session_id: 'session-1',
          sequence: 1,
          event_type: 'runtime.request_received',
          source: 'runtime',
          payload: { prompt: 'read README.md' }
        },
        {
          session_id: 'session-1',
          sequence: 2,
          event_type: 'graph.provider_stream',
          source: 'graph',
          payload: { channel: 'reasoning', delta: 'Let me read the file...' }
        },
        {
          session_id: 'session-1',
          sequence: 3,
          event_type: 'graph.response_ready',
          source: 'graph',
          payload: { output: 'Here is the README content.' }
        }
      ],
      currentSessionOutput: 'Here is the README content.'
    });

    render(<App />);

    expect(screen.getByText('read README.md')).toBeInTheDocument();
    expect(screen.getByText('Here is the README content.')).toBeInTheDocument();
  });

  it('renders thinking block only when reasoning events exist', () => {
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      currentSessionEvents: [
        {
          session_id: 'session-1',
          sequence: 1,
          event_type: 'runtime.request_received',
          source: 'runtime',
          payload: { prompt: 'analyze code' }
        },
        {
          session_id: 'session-1',
          sequence: 2,
          event_type: 'graph.provider_stream',
          source: 'graph',
          payload: { channel: 'reasoning', delta: 'Analyzing...' }
        }
      ],
      currentSessionOutput: null
    });

    render(<App />);

    expect(screen.getByText('Thinking')).toBeInTheDocument();
  });

  it('does not render thinking block when no reasoning events exist', () => {
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      currentSessionEvents: [
        {
          session_id: 'session-1',
          sequence: 1,
          event_type: 'runtime.request_received',
          source: 'runtime',
          payload: { prompt: 'hello' }
        }
      ],
      currentSessionOutput: 'Hello!'
    });

    render(<App />);

    expect(screen.queryByText('Thinking')).not.toBeInTheDocument();
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
          event_type: 'runtime.request_received',
          source: 'runtime',
          payload: { prompt: 'write note.txt hello' }
        },
        {
          session_id: 'session-1',
          sequence: 2,
          event_type: 'runtime.approval_requested',
          source: 'runtime',
          payload: {
            request_id: 'approval-1',
            tool: 'write_file',
            target_summary: 'write note.txt'
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
          event_type: 'runtime.request_received',
          source: 'runtime',
          payload: { prompt: 'write note.txt hello' }
        },
        {
          session_id: 'session-1',
          sequence: 2,
          event_type: 'runtime.approval_requested',
          source: 'runtime',
          payload: {
            request_id: 'approval-1',
            tool: 'write_file',
            target_summary: 'write note.txt'
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
          event_type: 'runtime.request_received',
          source: 'runtime',
          payload: { prompt: 'write note.txt hello' }
        },
        {
          session_id: 'session-1',
          sequence: 2,
          event_type: 'runtime.approval_requested',
          source: 'runtime',
          payload: {
            request_id: 'approval-1',
            tool: 'write_file',
            target_summary: 'write note.txt'
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

  it('renders the header with current session prompt', () => {
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

    const promptElements = screen.getAllByText('test prompt subtitle');
    expect(promptElements.length).toBeGreaterThanOrEqual(1);
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

    const promptElements = screen.getAllByText('prompt from replay');
    expect(promptElements.length).toBeGreaterThanOrEqual(1);
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

    const latestElements = screen.getAllByText('latest prompt');
    expect(latestElements.length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('old prompt')).toBeInTheDocument();
  });

  it('renders model controls and updates provider model', () => {
    render(<App />);

    const modelButton = screen.getByText('opencode-go/glm-5.1');
    expect(modelButton).toBeInTheDocument();

    fireEvent.click(modelButton);

    const modelInput = screen.getByLabelText('Model');
    expect(modelInput).toBeInTheDocument();

    fireEvent.change(modelInput, { target: { value: 'new-model/v1' } });
    expect(mockStore.setProviderModel).toHaveBeenCalledWith('new-model/v1');
  });

  it('renders settings panel when settings button is clicked', () => {
    render(<App />);

    const settingsButton = screen.getByText('Settings');
    fireEvent.click(settingsButton);

    expect(screen.getByTestId('settings-panel-mock')).toBeInTheDocument();
  });

  it('disables composer while running', () => {
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      runStatus: 'running'
    });

    render(<App />);

    const textarea = screen.getByPlaceholderText('Ask VoidCode to do something...');
    expect(textarea).toBeDisabled();
  });

  it('renders run error banner when run fails', () => {
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      runError: 'connection timeout'
    });

    render(<App />);

    expect(screen.getByText('Error: connection timeout')).toBeInTheDocument();
  });
});
