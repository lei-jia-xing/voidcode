import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
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
    currentSessionEvents: [],
    currentSessionOutput: null,
    loadSessions: vi.fn(),
    sessionsStatus: 'success',
    sessionsError: null,
    selectSession: vi.fn(),
    runTask: vi.fn(),
    replayStatus: 'idle',
    replayError: null,
    runStatus: 'idle',
    runError: null,
  };

  beforeEach(() => {
    vi.clearAllMocks();
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

  it('renders tasks and events when current session has events', () => {
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      currentSessionEvents: [
        {
          id: 'event-1',
          type: 'tool_call',
          payload: { tool_name: 'test_tool', args: {} },
          metadata: { task_id: 'task-1' }
        },
        {
          id: 'event-2',
          type: 'tool_result',
          payload: { result: 'success' },
          metadata: { task_id: 'task-1' }
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
});
