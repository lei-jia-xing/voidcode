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
    sessionSidebarWidth: 344,
    setSessionSidebarWidth: vi.fn(),
    currentSessionState: null,
    currentSessionEvents: [],
    currentSessionOutput: null,
    loadSessions: vi.fn(),
    sessionsStatus: "success",
    sessionsError: null,
    selectSession: vi.fn(),
    runTask: vi.fn(),
    cancelCurrentRun: vi.fn(),
    resolveApproval: vi.fn(),
    replayStatus: "idle",
    replayError: null,
    runStatus: "idle",
    runError: null,
    approvalStatus: "idle",
    approvalError: null,
    questionStatus: "idle",
    questionError: null,
    answerQuestion: vi.fn(),
    notifications: [],
    notificationsStatus: "idle",
    notificationsError: null,
    loadNotifications: vi.fn(),
    acknowledgeNotification: vi.fn(),
    backgroundTasks: [],
    backgroundTasksStatus: "idle",
    backgroundTasksError: null,
    selectedBackgroundTaskOutputId: null,
    backgroundTaskOutput: null,
    backgroundTaskOutputStatus: "idle",
    backgroundTaskOutputError: null,
    loadBackgroundTasks: vi.fn(),
    loadBackgroundTaskOutput: vi.fn(),
    cancelBackgroundTask: vi.fn(),
    sessionDebug: null,
    sessionDebugStatus: "idle",
    sessionDebugError: null,
    loadSessionDebug: vi.fn(),
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

  it("renders Server LSP and MCP status details without Git in the runtime status popover", () => {
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

    expect(screen.getAllByText("Server").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("LSP").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("MCP").length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText("Git")).not.toBeInTheDocument();
    expect(screen.getByText(/last request: handshake/)).toBeInTheDocument();
  });

  it("uses a working bar instead of the old agent idle header badge", () => {
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

    expect(
      screen.getByRole("status", { name: "Model working" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Agent Idle")).not.toBeInTheDocument();
    expect(screen.queryByText("Agent Busy")).not.toBeInTheDocument();
  });

  it("wires the running composer stop button to cancel the current run", () => {
    const cancelCurrentRun = vi.fn();
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...mockStore,
      cancelCurrentRun,
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
    fireEvent.click(screen.getByRole("button", { name: "Stop generation" }));

    expect(cancelCurrentRun).toHaveBeenCalledTimes(1);
  });

  it("renders independent sessions, file tree, and code review toggles in the workspace header", () => {
    const workspaceStore = {
      ...mockStore,
      reviewSnapshot: {
        root: "/workspace",
        git: { state: "git_ready" as const, root: "/workspace" },
        changed_files: [
          { path: "src/app.ts", change_type: "modified" as const },
        ],
        tree: [
          {
            kind: "directory" as const,
            name: "src",
            path: "src",
            changed: true,
            children: [
              {
                kind: "file" as const,
                name: "app.ts",
                path: "src/app.ts",
                changed: true,
                children: [],
              },
            ],
          },
        ],
      },
      reviewSelectedPath: "src/app.ts",
      reviewDiff: {
        root: "/workspace",
        path: "src/app.ts",
        state: "changed" as const,
        diff: "--- a/src/app.ts\n+++ b/src/app.ts\n@@ -1 +1 @@\n-old\n+new",
      },
      reviewStatus: "success" as const,
      reviewDiffStatus: "success" as const,
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
    };
    (useAppStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
      workspaceStore,
    );

    render(<App />);

    const sessionsToggle = screen.getByRole("button", {
      name: "Toggle sessions",
    });
    const fileTreeToggle = screen.getByRole("button", {
      name: "Toggle file tree",
    });
    const codeReviewToggle = screen.getByRole("button", {
      name: "Toggle code review",
    });

    expect(sessionsToggle).toHaveTextContent("Sessions");
    expect(fileTreeToggle).toHaveTextContent("File Tree");
    expect(codeReviewToggle).toHaveTextContent("Code Review");
    expect(
      screen.queryByRole("button", { name: "Toggle review" }),
    ).not.toBeInTheDocument();
    expect(sessionsToggle).toHaveAttribute("aria-expanded", "true");
    expect(fileTreeToggle).toHaveAttribute("aria-expanded", "false");
    expect(codeReviewToggle).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(sessionsToggle);
    expect(sessionsToggle).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(fileTreeToggle);
    expect(
      screen.getByRole("button", { name: "Toggle file tree" }),
    ).toHaveAttribute("aria-expanded", "true");
    expect(
      screen.getByRole("button", { name: "Toggle code review" }),
    ).toHaveAttribute("aria-expanded", "false");
    expect(
      screen.getByRole("complementary", { name: "File Tree" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Files" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Changes" }),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Toggle code review" }));
    expect(
      screen.getByRole("button", { name: "Toggle file tree" }),
    ).toHaveAttribute("aria-expanded", "true");
    expect(
      screen.getByRole("button", { name: "Toggle code review" }),
    ).toHaveAttribute("aria-expanded", "true");
    expect(
      screen.getByRole("complementary", { name: "Code Review" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/--- a\/src\/app\.ts/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Toggle code review" }));
    expect(
      screen.getByRole("button", { name: "Toggle code review" }),
    ).toHaveAttribute("aria-expanded", "false");
    expect(
      screen.getByRole("button", { name: "Toggle file tree" }),
    ).toHaveAttribute("aria-expanded", "true");

    fireEvent.click(screen.getByRole("button", { name: "app.ts" }));
    expect(workspaceStore.selectReviewPath).toHaveBeenCalledWith("src/app.ts");
    expect(
      screen.getByRole("button", { name: "Toggle code review" }),
    ).toHaveAttribute("aria-expanded", "true");
  });

  it("opens runtime ops and loads background task output from task selection", () => {
    const loadBackgroundTaskOutput = vi.fn();
    const selectSession = vi.fn();
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
      backgroundTasks: [
        {
          task: { id: "task-output-1" },
          status: "completed",
          prompt: "inspect repo",
          session_id: "session-1",
          error: null,
          created_at: 1,
          updated_at: 1,
        },
      ],
      selectedBackgroundTaskOutputId: "task-output-1",
      backgroundTaskOutputStatus: "success",
      backgroundTaskOutput: {
        task: {
          task_id: "task-output-1",
          status: "completed",
          parent_session_id: "session-1",
          requested_child_session_id: "requested-child-1",
          child_session_id: "child-session-1",
          approval_request_id: null,
          question_request_id: null,
          approval_blocked: false,
          summary_output: "Task summary text",
          error: null,
          result_available: true,
          cancellation_cause: null,
          duration_seconds: 12.5,
          tool_call_count: 3,
          routing: { mode: "subagent", subagent_type: "explore" },
        },
        session_result: {
          session: {
            session: { id: "child-session-1" },
            status: "completed" as const,
            turn: 1,
            metadata: {},
          },
          prompt: "inspect repo",
          status: "completed",
          summary: "Session summary text",
          output: "Session output text",
          error: null,
          last_event_sequence: 1,
          transcript: [],
        },
        output: "Raw output text",
      },
      loadBackgroundTaskOutput,
      selectSession,
    });

    render(<App />);
    fireEvent.click(screen.getByLabelText("Runtime Ops"));
    fireEvent.click(screen.getByRole("button", { name: "View output" }));

    expect(loadBackgroundTaskOutput).toHaveBeenCalledWith("task-output-1");
    expect(screen.getByText("Task summary text")).toBeInTheDocument();
    expect(screen.getByText("Raw output text")).toBeInTheDocument();
    expect(screen.getByText("Session output text")).toBeInTheDocument();
    expect(screen.getByText("child-session-1")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("12.5s")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "child-session-1" }));
    expect(selectSession).toHaveBeenCalledWith("child-session-1");
  });

  it("does not show the agent status badge in the workspace header", () => {
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
      runStatus: "idle",
    });

    render(<App />);

    expect(screen.queryByText("Agent Busy")).not.toBeInTheDocument();
    expect(screen.queryByText("Agent Idle")).not.toBeInTheDocument();
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

  it("does not render thinking block for reasoning events", () => {
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

    expect(screen.queryByText("Thinking")).not.toBeInTheDocument();
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
    expect(
      screen.getAllByText("session-123456789").length,
    ).toBeGreaterThanOrEqual(1);
  });

  it("renders concise deterministic titles for long session prompts in header and sidebar", () => {
    const longPrompt =
      "Implement the remaining opencode-style frontend chrome fixes from user feedback by editing the top chrome controls and composer footer selectors.";
    const conciseTitle =
      "Implement the remaining opencode-style frontend chrome…";
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
          prompt: longPrompt,
          updated_at: 1000,
        },
      ],
    });

    render(<App />);

    expect(screen.getAllByText(conciseTitle).length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText(longPrompt)).not.toBeInTheDocument();
  });

  it("shortens the live Vulkan Chinese prompt without keeping request boilerplate", () => {
    const vulkanPrompt =
      "请你作为 leader agent，在当前仓库中实现一个最小 Vulkan 三角形示例。要求：先检查项目结构和可用构建方式，再创建必要的源文件/构建配置/README说明；尽量保持最小可运行，不要做无关功能。";
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
      currentSessionId: "session-vulkan-123",
      sessions: [
        {
          session: { id: "session-vulkan-123" },
          status: "completed",
          turn: 2,
          prompt: vulkanPrompt,
          updated_at: 1000,
        },
      ],
    });

    render(<App />);

    const titleElements = screen.getAllByText("最小 Vulkan 三角形示例");
    expect(titleElements.length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText(vulkanPrompt)).not.toBeInTheDocument();
    expect(
      screen.queryByText(/请你作为 leader agent，在当前仓库中/),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/要求：先检查项目结构/)).not.toBeInTheDocument();
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
