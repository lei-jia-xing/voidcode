import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import App from "./App";
import { useAppStore } from "./store";
import "./i18n";

vi.mock("./store", () => ({
  useAppStore: vi.fn(),
}));

vi.mock("./components/SettingsPanel", () => ({
  SettingsPanel: () => <div data-testid="settings-panel-mock" />,
}));

describe("App", () => {
  const mockStore = {
    language: "en",
    setLanguage: vi.fn(),
    agentPreset: "leader",
    providerModel: "opencode-go/glm-5.1",
    setAgentPreset: vi.fn(),
    setProviderModel: vi.fn(),
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
    loadWorkspaces: vi.fn(),
    switchWorkspace: vi.fn(),
    loadProviders: vi.fn(),
    validateProviderCredentials: vi.fn(),
    loadAgents: vi.fn(),
    statusSnapshot: null,
    statusStatus: "idle",
    statusError: null,
    mcpRetryStatus: "idle",
    mcpRetryError: null,
    loadStatus: vi.fn(),
    retryMcpConnections: vi.fn(),
    reviewSnapshot: null,
    reviewStatus: "idle",
    reviewError: null,
    reviewSelectedPath: null,
    reviewDiff: null,
    reviewDiffStatus: "idle",
    reviewDiffError: null,
    reviewMode: "changes",
    loadReview: vi.fn(),
    selectReviewPath: vi.fn(),
    setReviewMode: vi.fn(),
    sessions: [],
    currentSessionId: null,
    currentSessionState: null,
    currentSessionEvents: [],
    currentSessionOutput: null,
    loadSessions: vi.fn(),
    sessionsStatus: "success",
    sessionsError: null,
    selectSession: vi.fn(),
    runTask: vi.fn(),
    resolveApproval: vi.fn(),
    replayStatus: "idle",
    replayError: null,
    runStatus: "idle",
    runError: null,
    approvalStatus: "idle",
    approvalError: null,
    settings: null,
    settingsStatus: "idle",
    settingsError: null,
    loadSettings: vi.fn(),
    updateSettings: vi.fn(),
  };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-04-16T06:00:00Z"));
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
      mockStore,
    );
    (useAppStore as unknown as { getState: () => typeof mockStore }).getState =
      () => mockStore;
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("does not render a standalone language header button", () => {
    render(<App />);

    expect(screen.queryByTitle("中文")).not.toBeInTheDocument();
  });

  it("renders composer and triggers runTask on submit", () => {
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      workspaces: {
        current: {
          path: "/workspace",
          label: "workspace",
          available: true,
          current: true,
          last_opened_at: 1,
        },
        recent: [],
        candidates: [],
      },
    });

    render(<App />);

    const textarea = screen.getByPlaceholderText(
      "Ask VoidCode to do something...",
    );
    expect(textarea).toBeInTheDocument();

    fireEvent.change(textarea, { target: { value: "read README.md" } });
    fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });

    expect(mockStore.runTask).toHaveBeenCalledWith("read README.md");
  });

  it("loads runtime-owned settings on startup", () => {
    render(<App />);

    expect(mockStore.loadSettings).toHaveBeenCalled();
  });

  it("renders ACP status details in the runtime status popover", () => {
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      workspaces: {
        current: {
          path: "/workspace",
          label: "workspace",
          available: true,
          current: true,
          last_opened_at: 1,
        },
        recent: [],
        candidates: [],
      },
      statusStatus: "success",
      statusSnapshot: {
        git: { state: "git_ready", root: "/workspace", error: null },
        lsp: { state: "running", error: null, details: {} },
        mcp: { state: "stopped", error: null, details: {} },
        acp: {
          state: "running",
          error: null,
          details: { status: "connected", last_request_type: "handshake" },
        },
      },
    });

    render(<App />);
    fireEvent.click(screen.getByLabelText("Toggle runtime status"));

    expect(screen.getByText("ACP")).toBeInTheDocument();
    expect(screen.getByText(/last request: handshake/)).toBeInTheDocument();
  });

  it("renders project-picker-first empty state when no current workspace exists", () => {
    render(<App />);

    expect(
      screen.getByText("Open a project to get started"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Choose a workspace first before using chat, review, or composer.",
      ),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Open Project" }).length).toBe(
      2,
    );
    expect(
      screen.queryByPlaceholderText("Ask VoidCode to do something..."),
    ).not.toBeInTheDocument();
  });

  it("renders chat messages when current session has events", () => {
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      workspaces: {
        current: {
          path: "/workspace",
          label: "workspace",
          available: true,
          current: true,
          last_opened_at: 1,
        },
        recent: [],
        candidates: [],
      },
      currentSessionEvents: [
        {
          session_id: "session-1",
          sequence: 1,
          event_type: "runtime.request_received",
          source: "runtime",
          payload: { prompt: "read README.md" },
        },
        {
          session_id: "session-1",
          sequence: 2,
          event_type: "graph.provider_stream",
          source: "graph",
          payload: { channel: "reasoning", text: "Let me read the file..." },
        },
        {
          session_id: "session-1",
          sequence: 3,
          event_type: "graph.provider_stream",
          source: "graph",
          payload: { channel: "text", text: "Here is the README content." },
        },
        {
          session_id: "session-1",
          sequence: 4,
          event_type: "graph.response_ready",
          source: "graph",
          payload: { output: "Here is the README content." },
        },
      ],
      currentSessionOutput: "Here is the README content.",
    });

    render(<App />);

    expect(screen.getByText("read README.md")).toBeInTheDocument();
    expect(screen.getByText("Here is the README content.")).toBeInTheDocument();
  });

  it("renders thinking block only when reasoning events exist", () => {
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      workspaces: {
        current: {
          path: "/workspace",
          label: "workspace",
          available: true,
          current: true,
          last_opened_at: 1,
        },
        recent: [],
        candidates: [],
      },
      currentSessionEvents: [
        {
          session_id: "session-1",
          sequence: 1,
          event_type: "runtime.request_received",
          source: "runtime",
          payload: { prompt: "analyze code" },
        },
        {
          session_id: "session-1",
          sequence: 2,
          event_type: "graph.provider_stream",
          source: "graph",
          payload: { channel: "reasoning", text: "Analyzing..." },
        },
      ],
      currentSessionOutput: null,
    });

    render(<App />);

    expect(screen.getByText("Thinking")).toBeInTheDocument();
  });

  it("renders streamed assistant text before the final response event", () => {
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      workspaces: {
        current: {
          path: "/workspace",
          label: "workspace",
          available: true,
          current: true,
          last_opened_at: 1,
        },
        recent: [],
        candidates: [],
      },
      currentSessionEvents: [
        {
          session_id: "session-1",
          sequence: 1,
          event_type: "runtime.request_received",
          source: "runtime",
          payload: { prompt: "stream this" },
        },
        {
          session_id: "session-1",
          sequence: 2,
          event_type: "graph.provider_stream",
          source: "graph",
          payload: { channel: "text", text: "streamed " },
        },
        {
          session_id: "session-1",
          sequence: 3,
          event_type: "graph.provider_stream",
          source: "graph",
          payload: { channel: "text", text: "answer" },
        },
      ],
      currentSessionOutput: null,
      runStatus: "running",
    });

    render(<App />);

    expect(screen.getByText("streamed answer")).toBeInTheDocument();
  });

  it("avoids duplicate React keys when replayed events reuse sequence numbers across sessions", () => {
    const consoleErrorSpy = vi
      .spyOn(console, "error")
      .mockImplementation(() => {});
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      workspaces: {
        current: {
          path: "/workspace",
          label: "workspace",
          available: true,
          current: true,
          last_opened_at: 1,
        },
        recent: [],
        candidates: [],
      },
      currentSessionId: "session-2",
      currentSessionEvents: [
        {
          session_id: "session-1",
          sequence: 1,
          event_type: "runtime.request_received",
          source: "runtime",
          payload: { prompt: "first prompt" },
        },
        {
          session_id: "session-2",
          sequence: 1,
          event_type: "runtime.request_received",
          source: "runtime",
          payload: { prompt: "second prompt" },
        },
      ],
    });

    render(<App />);

    expect(screen.getAllByText("first prompt").length).toBeGreaterThanOrEqual(
      1,
    );
    expect(screen.getAllByText("second prompt").length).toBeGreaterThanOrEqual(
      1,
    );
    expect(consoleErrorSpy).not.toHaveBeenCalledWith(
      expect.stringContaining("Encountered two children with the same key"),
    );

    consoleErrorSpy.mockRestore();
  });

  it("avoids duplicate React keys when later turns in the same session reuse sequence numbers", () => {
    const consoleErrorSpy = vi
      .spyOn(console, "error")
      .mockImplementation(() => {});
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      workspaces: {
        current: {
          path: "/workspace",
          label: "workspace",
          available: true,
          current: true,
          last_opened_at: 1,
        },
        recent: [],
        candidates: [],
      },
      currentSessionId: "session-1",
      currentSessionEvents: [
        {
          session_id: "session-1",
          sequence: 1,
          event_type: "runtime.request_received",
          source: "runtime",
          payload: { prompt: "first turn" },
        },
        {
          session_id: "session-1",
          sequence: 2,
          event_type: "graph.response_ready",
          source: "graph",
          payload: { output: "first answer" },
        },
        {
          session_id: "session-1",
          sequence: 1,
          event_type: "runtime.request_received",
          source: "runtime",
          payload: { prompt: "second turn" },
        },
      ],
    });

    render(<App />);

    expect(screen.getAllByText("first turn").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("second turn").length).toBeGreaterThanOrEqual(1);
    expect(consoleErrorSpy).not.toHaveBeenCalledWith(
      expect.stringContaining("Encountered two children with the same key"),
    );

    consoleErrorSpy.mockRestore();
  });

  it("does not render thinking block when no reasoning events exist", () => {
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      workspaces: {
        current: {
          path: "/workspace",
          label: "workspace",
          available: true,
          current: true,
          last_opened_at: 1,
        },
        recent: [],
        candidates: [],
      },
      currentSessionEvents: [
        {
          session_id: "session-1",
          sequence: 1,
          event_type: "runtime.request_received",
          source: "runtime",
          payload: { prompt: "hello" },
        },
      ],
      currentSessionOutput: "Hello!",
    });

    render(<App />);

    expect(screen.queryByText("Thinking")).not.toBeInTheDocument();
  });

  it("renders approval controls for waiting sessions and triggers allow", () => {
    const resolveApproval = vi.fn();
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      workspaces: {
        current: {
          path: "/workspace",
          label: "workspace",
          available: true,
          current: true,
          last_opened_at: 1,
        },
        recent: [],
        candidates: [],
      },
      currentSessionId: "session-1",
      currentSessionState: {
        session: { id: "session-1" },
        status: "waiting",
        turn: 1,
        metadata: {},
      },
      currentSessionEvents: [
        {
          session_id: "session-1",
          sequence: 1,
          event_type: "runtime.request_received",
          source: "runtime",
          payload: { prompt: "write note.txt hello" },
        },
        {
          session_id: "session-1",
          sequence: 2,
          event_type: "runtime.approval_requested",
          source: "runtime",
          payload: {
            request_id: "approval-1",
            tool: "write_file",
            target_summary: "write note.txt",
          },
        },
      ],
      resolveApproval,
    });

    render(<App />);

    expect(screen.getByText("Approval Required")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Allow" }));

    expect(resolveApproval).toHaveBeenCalledWith("allow");
  });

  it("triggers deny for waiting sessions", () => {
    const resolveApproval = vi.fn();
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      workspaces: {
        current: {
          path: "/workspace",
          label: "workspace",
          available: true,
          current: true,
          last_opened_at: 1,
        },
        recent: [],
        candidates: [],
      },
      currentSessionId: "session-1",
      currentSessionState: {
        session: { id: "session-1" },
        status: "waiting",
        turn: 1,
        metadata: {},
      },
      currentSessionEvents: [
        {
          session_id: "session-1",
          sequence: 1,
          event_type: "runtime.request_received",
          source: "runtime",
          payload: { prompt: "write note.txt hello" },
        },
        {
          session_id: "session-1",
          sequence: 2,
          event_type: "runtime.approval_requested",
          source: "runtime",
          payload: {
            request_id: "approval-1",
            tool: "write_file",
            target_summary: "write note.txt",
          },
        },
      ],
      resolveApproval,
    });

    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "Deny" }));

    expect(resolveApproval).toHaveBeenCalledWith("deny");
  });

  it("hides approval controls when session is not waiting", () => {
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      workspaces: {
        current: {
          path: "/workspace",
          label: "workspace",
          available: true,
          current: true,
          last_opened_at: 1,
        },
        recent: [],
        candidates: [],
      },
      currentSessionState: {
        session: { id: "session-1" },
        status: "completed",
        turn: 1,
        metadata: {},
      },
    });

    render(<App />);

    expect(screen.queryByText("Approval Required")).not.toBeInTheDocument();
  });

  it("renders approval error and disables controls while submitting", () => {
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      workspaces: {
        current: {
          path: "/workspace",
          label: "workspace",
          available: true,
          current: true,
          last_opened_at: 1,
        },
        recent: [],
        candidates: [],
      },
      currentSessionId: "session-1",
      currentSessionState: {
        session: { id: "session-1" },
        status: "waiting",
        turn: 1,
        metadata: {},
      },
      currentSessionEvents: [
        {
          session_id: "session-1",
          sequence: 1,
          event_type: "runtime.request_received",
          source: "runtime",
          payload: { prompt: "write note.txt hello" },
        },
        {
          session_id: "session-1",
          sequence: 2,
          event_type: "runtime.approval_requested",
          source: "runtime",
          payload: {
            request_id: "approval-1",
            tool: "write_file",
            target_summary: "write note.txt",
          },
        },
      ],
      approvalStatus: "submitting",
      approvalError: "boom",
    });

    render(<App />);

    expect(screen.getByText("Approval failed: boom")).toBeInTheDocument();
    expect(
      screen.getAllByRole("button", { name: "Submitting..." }),
    ).toHaveLength(2);
  });

  it("renders the session list item with prompt-first title, status, and updated time", () => {
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      workspaces: {
        current: {
          path: "/workspace",
          label: "workspace",
          available: true,
          current: true,
          last_opened_at: 1,
        },
        recent: [],
        candidates: [],
      },
      sessions: [
        {
          session: { id: "session-123456789" },
          status: "completed",
          turn: 5,
          prompt: "test prompt subtitle",
          updated_at: Math.floor(
            new Date("2026-04-16T05:58:00Z").getTime() / 1000,
          ),
        },
      ],
    });

    render(<App />);

    expect(screen.getByText("test prompt subtitle")).toBeInTheDocument();
    expect(screen.getByText("T5")).toBeInTheDocument();
    expect(screen.getByText("Completed")).toBeInTheDocument();
    expect(screen.getByText("2m ago")).toBeInTheDocument();
  });

  it("renders idle session status labels from contract-valid summaries", () => {
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      workspaces: {
        current: {
          path: "/workspace",
          label: "workspace",
          available: true,
          current: true,
          last_opened_at: 1,
        },
        recent: [],
        candidates: [],
      },
      sessions: [
        {
          session: { id: "session-idle-123" },
          status: "idle",
          turn: 1,
          prompt: "Resume existing session",
          updated_at: Math.floor(
            new Date("2026-04-16T05:59:30Z").getTime() / 1000,
          ),
        },
      ],
    });

    render(<App />);

    expect(screen.getByText("Pending")).toBeInTheDocument();
  });

  it("renders the header with current session prompt", () => {
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      workspaces: {
        current: {
          path: "/workspace",
          label: "workspace",
          available: true,
          current: true,
          last_opened_at: 1,
        },
        recent: [],
        candidates: [],
      },
      currentSessionId: "session-123456789",
      sessions: [
        {
          session: { id: "session-123456789" },
          status: "completed",
          turn: 5,
          prompt: "test prompt subtitle",
          updated_at: 1000,
        },
      ],
    });

    render(<App />);

    const promptElements = screen.getAllByText("test prompt subtitle");
    expect(promptElements.length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("session-123456789")).toBeInTheDocument();
  });

  it("falls back to the replayed request prompt in the header when summary prompt is unavailable", () => {
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      workspaces: {
        current: {
          path: "/workspace",
          label: "workspace",
          available: true,
          current: true,
          last_opened_at: 1,
        },
        recent: [],
        candidates: [],
      },
      currentSessionId: "session-123456789",
      currentSessionEvents: [
        {
          session_id: "session-123456789",
          sequence: 1,
          event_type: "runtime.request_received",
          source: "runtime",
          payload: { prompt: "prompt from replay" },
        },
      ],
    });

    render(<App />);

    const promptElements = screen.getAllByText("prompt from replay");
    expect(promptElements.length).toBeGreaterThanOrEqual(1);
  });

  it("prefers the latest replayed request prompt in the header fallback", () => {
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      workspaces: {
        current: {
          path: "/workspace",
          label: "workspace",
          available: true,
          current: true,
          last_opened_at: 1,
        },
        recent: [],
        candidates: [],
      },
      currentSessionId: "session-123456789",
      currentSessionEvents: [
        {
          session_id: "session-123456789",
          sequence: 1,
          event_type: "runtime.request_received",
          source: "runtime",
          payload: { prompt: "old prompt" },
        },
        {
          session_id: "session-123456789",
          sequence: 4,
          event_type: "runtime.request_received",
          source: "runtime",
          payload: { prompt: "latest prompt" },
        },
      ],
    });

    render(<App />);

    const latestElements = screen.getAllByText("latest prompt");
    expect(latestElements.length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("old prompt")).toBeInTheDocument();
  });

  it("renders model controls and updates provider model", () => {
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      workspaces: {
        current: {
          path: "/workspace",
          label: "workspace",
          available: true,
          current: true,
          last_opened_at: 1,
        },
        recent: [],
        candidates: [],
      },
      agentPresets: [{ id: "leader", label: "Leader", description: null }],
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
          models: ["opencode-go/glm-5.1", "new-model/v1"],
          source: null,
          last_refresh_status: null,
          last_error: null,
          discovery_mode: null,
        },
      },
    });

    render(<App />);

    const modelInput = screen.getByRole("button", { name: "Model" });
    expect(modelInput).toHaveTextContent("OpenCode Go / glm-5.1");

    fireEvent.click(modelInput);
    fireEvent.click(screen.getByRole("button", { name: "new-model/v1" }));
    expect(mockStore.setProviderModel).toHaveBeenCalledWith(
      "opencode-go/new-model/v1",
    );
  });

  it("renders settings panel when settings button is clicked", () => {
    render(<App />);

    const settingsButton = screen.getByRole("button", { name: "Settings" });
    fireEvent.click(settingsButton);

    expect(screen.getByTestId("settings-panel-mock")).toBeInTheDocument();
  });

  it("disables composer while running", () => {
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      workspaces: {
        current: {
          path: "/workspace",
          label: "workspace",
          available: true,
          current: true,
          last_opened_at: 1,
        },
        recent: [],
        candidates: [],
      },
      runStatus: "running",
    });

    render(<App />);

    const textarea = screen.getByPlaceholderText(
      "Ask VoidCode to do something...",
    );
    expect(textarea).toBeDisabled();
  });

  it("renders run error banner when run fails", () => {
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      workspaces: {
        current: {
          path: "/workspace",
          label: "workspace",
          available: true,
          current: true,
          last_opened_at: 1,
        },
        recent: [],
        candidates: [],
      },
      runError: "connection timeout",
    });

    render(<App />);

    expect(screen.getByText("Error: connection timeout")).toBeInTheDocument();
  });
});
