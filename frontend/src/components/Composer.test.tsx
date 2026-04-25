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

  it("calls onProviderModelChange when model is changed", () => {
    const onProviderModelChange = vi.fn();
    render(
      <Composer {...baseProps} onProviderModelChange={onProviderModelChange} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Model" }));
    fireEvent.click(screen.getByRole("button", { name: "glm-5.2" }));

    expect(onProviderModelChange).toHaveBeenCalledWith("opencode-go/glm-5.2");
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

    expect(screen.getByText(/No providers configured/i)).toBeInTheDocument();
    expect(screen.queryByLabelText("Model")).not.toBeInTheDocument();
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

    expect(screen.getByText("No models available.")).toBeInTheDocument();
    expect(screen.queryByLabelText("Model")).not.toBeInTheDocument();
  });

  it("keeps configured model fallback visible when catalogs are empty", () => {
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

    expect(screen.getByText("OpenCode / kimi-k2.6")).toBeInTheDocument();
    expect(screen.queryByLabelText("Model")).not.toBeInTheDocument();
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
