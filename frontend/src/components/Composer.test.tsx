import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { Composer } from "./Composer";
import "../i18n";

const baseProps = {
  disabled: false,
  isRunning: false,
  agentPreset: "leader" as const,
  providerModel: "opencode-go/glm-5.1",
  agentPresets: [{ id: "leader", label: "Leader", description: null }],
  providers: [
    {
      name: "opencode-go",
      label: "OpenCode",
      configured: true,
      current: true,
    },
  ],
  providerModels: {
    "opencode-go": {
      provider: "opencode-go",
      configured: true,
      models: ["opencode-go/glm-5.1", "opencode-go/glm-5.2"],
      source: null,
      last_refresh_status: null,
      last_error: null,
      discovery_mode: null,
    },
  },
  onAgentPresetChange: vi.fn(),
  onProviderModelChange: vi.fn(),
  onSubmit: vi.fn(),
};

describe("Composer", () => {
  it("renders agent selector with leader option", () => {
    render(<Composer {...baseProps} />);

    const agentTrigger = screen.getByRole("button", { name: "Agent" });
    expect(agentTrigger).toBeInTheDocument();
    expect(agentTrigger).toHaveTextContent("Leader");
  });

  it("renders model selector grouped by provider", () => {
    render(<Composer {...baseProps} />);

    const modelTrigger = screen.getByRole("button", { name: "Model" });
    expect(modelTrigger).toBeInTheDocument();
    expect(modelTrigger).toHaveTextContent("OpenCode / glm-5.1");
  });

  it("keeps composer selectors accessible while rendering them as flat footer controls", () => {
    render(<Composer {...baseProps} />);

    const agentTrigger = screen.getByRole("button", { name: "Agent" });
    const modelTrigger = screen.getByRole("button", { name: "Model" });

    expect(agentTrigger).toBeEnabled();
    expect(modelTrigger).toBeEnabled();
    expect(agentTrigger.className).not.toContain(
      "border-[color:var(--vc-border-subtle)]",
    );
    expect(modelTrigger.className).not.toContain(
      "border-[color:var(--vc-border-subtle)]",
    );
  });

  it("calls onProviderModelChange when model is changed", () => {
    const onProviderModelChange = vi.fn();
    render(
      <Composer {...baseProps} onProviderModelChange={onProviderModelChange} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Model" }));
    fireEvent.click(screen.getByRole("button", { name: "glm-5.2" }));

    expect(onProviderModelChange).toHaveBeenCalledWith("opencode-go/glm-5.2");
  });

  it("keeps selector menus above the composer shell", () => {
    render(<Composer {...baseProps} />);

    fireEvent.click(screen.getByRole("button", { name: "Model" }));

    expect(screen.getByText("OpenCode").closest(".absolute")).toHaveClass(
      "z-[100]",
    );
  });

  it("shows model context and reasoning effort controls from metadata", () => {
    const onReasoningEffortChange = vi.fn();
    render(
      <Composer
        {...baseProps}
        reasoningEffort="high"
        sessionContextUsage={{
          usedTokens: 12_400,
          contextWindow: 198_000,
          totalTokens: 18_900,
          estimated: false,
        }}
        onReasoningEffortChange={onReasoningEffortChange}
        providerModels={{
          "opencode-go": {
            ...baseProps.providerModels["opencode-go"],
            model_metadata: {
              "opencode-go/glm-5.1": {
                context_window: 198_000,
                max_output_tokens: 128_000,
                supports_reasoning: true,
                supports_reasoning_effort: true,
                default_reasoning_effort: "medium",
              },
            },
          },
        }}
      />,
    );

    expect(
      screen.getByText("12.4K ctx · 6.3% · 18.9K total"),
    ).toBeInTheDocument();

    const effortSelect = screen.getByRole("combobox", {
      name: "Reasoning effort",
    });
    expect(effortSelect).toHaveValue("high");

    fireEvent.change(effortSelect, { target: { value: "low" } });

    expect(onReasoningEffortChange).toHaveBeenCalledWith("low");
  });

  it("prefers session context window metadata over selected model metadata", () => {
    render(
      <Composer
        {...baseProps}
        sessionMetadata={{
          context_window: { model_context_window_tokens: 512_000 },
        }}
        providerModels={{
          "opencode-go": {
            ...baseProps.providerModels["opencode-go"],
            model_metadata: {
              "opencode-go/glm-5.1": {
                context_window: 198_000,
                max_output_tokens: 128_000,
              },
            },
          },
        }}
      />,
    );

    expect(screen.getByText("512K ctx · 128K out")).toBeInTheDocument();
    expect(screen.queryByText("198K ctx · 128K out")).not.toBeInTheDocument();
  });

  it("hides reasoning effort controls for models without effort support", () => {
    render(
      <Composer
        {...baseProps}
        providerModels={{
          "opencode-go": {
            ...baseProps.providerModels["opencode-go"],
            model_metadata: {
              "opencode-go/glm-5.1": {
                context_window: 198_000,
                supports_reasoning: true,
                supports_reasoning_effort: false,
              },
            },
          },
        }}
      />,
    );

    expect(
      screen.getByText("Context unavailable · total unavailable"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("combobox", { name: "Reasoning effort" }),
    ).not.toBeInTheDocument();
  });

  it("falls back when token usage has no context window denominator", () => {
    render(
      <Composer
        {...baseProps}
        sessionContextUsage={{
          usedTokens: 2048,
          contextWindow: null,
          totalTokens: 4096,
          estimated: false,
        }}
      />,
    );

    expect(
      screen.getByText("2K ctx · window unavailable · 4.1K total"),
    ).toBeInTheDocument();
  });

  it("shows known context window when token usage is unavailable", () => {
    render(
      <Composer
        {...baseProps}
        sessionContextUsage={{
          usedTokens: null,
          contextWindow: 1_000_000,
          totalTokens: 6200,
          estimated: false,
        }}
      />,
    );

    expect(
      screen.getByText("Context unavailable · 1M window · 6.2K total"),
    ).toBeInTheDocument();
  });

  it("marks estimated context token usage", () => {
    render(
      <Composer
        {...baseProps}
        sessionContextUsage={{
          usedTokens: 12_400,
          contextWindow: 1_000_000,
          totalTokens: 30_000,
          estimated: true,
        }}
      />,
    );

    expect(
      screen.getByText("≈12.4K ctx · 1.2% · 30K total"),
    ).toBeInTheDocument();
  });

  it("canonicalizes bare grouped model aliases before storing selection", () => {
    const onProviderModelChange = vi.fn();
    render(
      <Composer
        {...baseProps}
        providerModel=""
        providerModels={{
          "opencode-go": {
            provider: "opencode-go",
            configured: true,
            models: ["glm-5.1"],
            source: null,
            last_refresh_status: null,
            last_error: null,
            discovery_mode: null,
          },
        }}
        onProviderModelChange={onProviderModelChange}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Model" }));
    fireEvent.click(screen.getByRole("button", { name: "glm-5.1" }));

    expect(onProviderModelChange).toHaveBeenCalledWith("opencode-go/glm-5.1");
  });

  it("calls onAgentPresetChange when agent is changed", () => {
    const onAgentPresetChange = vi.fn();
    render(
      <Composer {...baseProps} onAgentPresetChange={onAgentPresetChange} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Agent" }));
    fireEvent.click(screen.getByRole("button", { name: "Leader" }));

    expect(onAgentPresetChange).toHaveBeenCalledWith("leader");
  });

  it("filters non-selectable agents from the agent menu", () => {
    render(
      <Composer
        {...baseProps}
        agentPresets={[
          {
            id: "leader",
            label: "Leader",
            description: null,
            mode: "primary",
            selectable: true,
          },
          {
            id: "worker",
            label: "Worker",
            description: null,
            mode: "subagent",
            selectable: false,
          },
        ]}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Agent" }));

    expect(screen.getByRole("button", { name: /Leader/ })).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Worker/ }),
    ).not.toBeInTheDocument();
  });

  it("does not invent a leader label when the backend-selected agent differs", () => {
    render(
      <Composer
        {...baseProps}
        agentPreset="product"
        agentPresets={[
          {
            id: "product",
            label: "Product",
            description: null,
            mode: "primary",
            selectable: true,
          },
        ]}
      />,
    );

    expect(screen.getByRole("button", { name: "Agent" })).toHaveTextContent(
      "Product",
    );
  });

  it("shows empty state when no providers are configured", () => {
    render(
      <Composer
        {...baseProps}
        providers={[
          {
            name: "openai",
            label: "OpenAI",
            configured: false,
            current: false,
          },
        ]}
      />,
    );

    expect(
      screen.queryByText(/No providers configured/i),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Model" }),
    ).not.toBeInTheDocument();
  });

  it("shows empty state when no models are available for configured providers", () => {
    render(
      <Composer
        {...baseProps}
        providerModel=""
        providerModels={{
          "opencode-go": {
            provider: "opencode-go",
            configured: true,
            models: [],
            source: null,
            last_refresh_status: null,
            last_error: null,
            discovery_mode: null,
          },
        }}
      />,
    );

    expect(screen.queryByText("No models available.")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Model" }),
    ).not.toBeInTheDocument();
  });

  it("hides model switcher when catalogs are empty", () => {
    render(
      <Composer
        {...baseProps}
        providerModel="opencode-go/kimi-k2.6"
        providerModels={{
          "opencode-go": {
            provider: "opencode-go",
            configured: true,
            models: [],
            source: null,
            last_refresh_status: null,
            last_error: null,
            discovery_mode: null,
          },
        }}
      />,
    );

    expect(screen.queryByText("OpenCode / kimi-k2.6")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Model" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByPlaceholderText("Ask VoidCode to do something..."),
    ).not.toBeDisabled();
  });

  it("submits message on Enter key", () => {
    const onSubmit = vi.fn();
    render(<Composer {...baseProps} onSubmit={onSubmit} />);

    const textarea = screen.getByPlaceholderText(
      "Ask VoidCode to do something...",
    );
    fireEvent.change(textarea, { target: { value: "hello" } });
    fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });

    expect(onSubmit).toHaveBeenCalledWith("hello");
  });

  it("keeps the send action enabled while treating empty input as a no-op", () => {
    const onSubmit = vi.fn();
    render(<Composer {...baseProps} onSubmit={onSubmit} />);

    const sendButton = screen.getByRole("button", { name: "Send message" });
    expect(sendButton).toBeEnabled();

    fireEvent.click(sendButton);

    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("does not submit on Shift+Enter", () => {
    const onSubmit = vi.fn();
    render(<Composer {...baseProps} onSubmit={onSubmit} />);

    const textarea = screen.getByPlaceholderText(
      "Ask VoidCode to do something...",
    );
    fireEvent.change(textarea, { target: { value: "hello" } });
    fireEvent.keyDown(textarea, { key: "Enter", shiftKey: true });

    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("disables controls when disabled prop is true", () => {
    render(<Composer {...baseProps} disabled />);

    expect(screen.getByRole("button", { name: "Agent" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Model" })).toBeDisabled();
    expect(
      screen.getByPlaceholderText("Ask VoidCode to do something..."),
    ).toBeDisabled();
  });

  it("shows a stop action while running and calls onCancel", () => {
    const onCancel = vi.fn();
    render(<Composer {...baseProps} disabled isRunning onCancel={onCancel} />);

    const stopButton = screen.getByRole("button", { name: "Stop generation" });
    expect(stopButton).toBeEnabled();

    fireEvent.click(stopButton);

    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("groups models from multiple configured providers", () => {
    render(
      <Composer
        {...baseProps}
        providers={[
          {
            name: "opencode-go",
            label: "OpenCode",
            configured: true,
            current: true,
          },
          {
            name: "glm",
            label: "GLM",
            configured: true,
            current: false,
          },
        ]}
        providerModels={{
          "opencode-go": {
            provider: "opencode-go",
            configured: true,
            models: ["opencode-go/glm-5.1"],
            source: null,
            last_refresh_status: null,
            last_error: null,
            discovery_mode: null,
          },
          glm: {
            provider: "glm",
            configured: true,
            models: ["glm/glm-5"],
            source: null,
            last_refresh_status: null,
            last_error: null,
            discovery_mode: null,
          },
        }}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Model" }));

    expect(screen.getByText("glm-5.1")).toBeInTheDocument();
    expect(screen.getByText("glm-5")).toBeInTheDocument();
  });
});
