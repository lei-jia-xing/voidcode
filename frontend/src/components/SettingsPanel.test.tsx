import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { SettingsPanel } from "./SettingsPanel";
import "../i18n";

const baseProps = {
  isOpen: true,
  settings: null,
  settingsStatus: "idle",
  settingsError: null,
  providers: [
    { name: "glm", label: "GLM", configured: true, current: true },
    { name: "openai", label: "OpenAI", configured: false, current: false },
  ],
  providersStatus: "success",
  providersError: null,
  language: "en",
  onToggleLanguage: vi.fn(),
  onClose: vi.fn(),
  onLoad: vi.fn(),
  onLoadProviders: vi.fn(),
  onSave: vi.fn(),
};

describe("SettingsPanel", () => {
  it("calls onLoad and onLoadProviders when opened", () => {
    const onLoad = vi.fn();
    const onLoadProviders = vi.fn();
    render(
      <SettingsPanel
        {...baseProps}
        onLoad={onLoad}
        onLoadProviders={onLoadProviders}
      />,
    );

    expect(onLoad).toHaveBeenCalled();
    expect(onLoadProviders).toHaveBeenCalled();
  });

  it("renders provider list with configured and unconfigured badges", () => {
    render(<SettingsPanel {...baseProps} />);

    expect(screen.getByText("GLM")).toBeInTheDocument();
    expect(screen.getByText("OpenAI")).toBeInTheDocument();
    expect(screen.getByTitle("Configured")).toBeInTheDocument();
    expect(screen.getByTitle("Not configured")).toBeInTheDocument();
  });

  it("groups configured provider models and saves canonical model references", () => {
    const onSave = vi.fn();
    render(
      <SettingsPanel
        {...baseProps}
        settings={{
          provider: "glm",
          model: "",
          provider_api_key_present: true,
        }}
        providerModels={{
          glm: {
            provider: "glm",
            configured: true,
            models: ["glm-5", "nested/model"],
            last_refresh_status: "ok",
            discovery_mode: "configured_endpoint",
          },
          openai: {
            provider: "openai",
            configured: false,
            models: ["gpt-5"],
          },
        }}
        onSave={onSave}
      />,
    );

    expect(screen.getByText("Configured providers")).toBeInTheDocument();
    expect(screen.getByText("Unconfigured providers")).toBeInTheDocument();

    const modelSelect = screen.getByLabelText("Model");
    fireEvent.change(modelSelect, { target: { value: "glm/nested/model" } });
    fireEvent.click(screen.getByRole("button", { name: "Save Settings" }));

    expect(onSave).toHaveBeenCalledWith({
      provider: "glm",
      provider_api_key: undefined,
      model: "glm/nested/model",
    });
  });

  it("tests selected provider credentials and shows validation result", () => {
    const onValidateProvider = vi.fn();
    render(
      <SettingsPanel
        {...baseProps}
        settings={{ provider: "glm", model: "glm/glm-5" }}
        providerModels={{
          glm: {
            provider: "glm",
            configured: true,
            models: ["glm-5"],
            last_refresh_status: "failed",
            last_error: "remote model discovery failed",
            discovery_mode: "configured_endpoint",
          },
        }}
        providerValidationResults={{
          glm: {
            provider: "glm",
            configured: true,
            ok: false,
            status: "failed",
            message: "Provider credential validation failed.",
          },
        }}
        providerValidationStatus={{ glm: "error" }}
        onValidateProvider={onValidateProvider}
      />,
    );

    expect(
      screen.getByText("remote model discovery failed"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Provider credential validation failed."),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Test credentials" }));

    expect(onValidateProvider).toHaveBeenCalledWith("glm");
  });

  it("shows empty state when no providers are available", () => {
    render(<SettingsPanel {...baseProps} providers={[]} />);

    expect(screen.getByText("No providers available.")).toBeInTheDocument();
  });

  it("calls onSave with api key when save is clicked", () => {
    const onSave = vi.fn();
    render(<SettingsPanel {...baseProps} onSave={onSave} />);

    const input = screen.getByPlaceholderText("Enter your API key");
    fireEvent.change(input, { target: { value: "my-secret-key" } });

    const saveButton = screen.getByRole("button", { name: "Save Settings" });
    fireEvent.click(saveButton);

    expect(onSave).toHaveBeenCalledWith({
      provider: undefined,
      provider_api_key: "my-secret-key",
      model: undefined,
    });
  });

  it("preserves existing settings provider and model on save", () => {
    const onSave = vi.fn();
    render(
      <SettingsPanel
        {...baseProps}
        settings={{
          provider: "glm",
          provider_api_key_present: true,
          model: "glm/glm-5",
        }}
        onSave={onSave}
      />,
    );

    const input = screen.getByPlaceholderText("Enter your API key");
    fireEvent.change(input, { target: { value: "new-key" } });

    const saveButton = screen.getByRole("button", { name: "Save Settings" });
    fireEvent.click(saveButton);

    expect(onSave).toHaveBeenCalledWith({
      provider: "glm",
      provider_api_key: "new-key",
      model: "glm/glm-5",
    });
  });

  it("calls onClose when close button is clicked", () => {
    const onClose = vi.fn();
    render(<SettingsPanel {...baseProps} onClose={onClose} />);

    const closeButton = screen.getByRole("button", { name: "Close" });
    fireEvent.click(closeButton);

    expect(onClose).toHaveBeenCalled();
  });

  it("calls onClose when backdrop is clicked", () => {
    const onClose = vi.fn();
    render(<SettingsPanel {...baseProps} onClose={onClose} />);

    const backdrop = screen.getAllByRole("button", { name: "Close" })[0];
    fireEvent.click(backdrop);

    expect(onClose).toHaveBeenCalled();
  });

  it("shows settings error when present", () => {
    render(<SettingsPanel {...baseProps} settingsError="Load failed" />);

    expect(screen.getByText("Load failed")).toBeInTheDocument();
  });

  it("shows providers error when present", () => {
    render(<SettingsPanel {...baseProps} providersError="Provider error" />);

    expect(screen.getByText("Provider error")).toBeInTheDocument();
  });

  it("does not render when isOpen is false", () => {
    const { container } = render(
      <SettingsPanel {...baseProps} isOpen={false} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("disables save button while loading", () => {
    render(<SettingsPanel {...baseProps} settingsStatus="loading" />);

    const saveButton = screen.getByRole("button", { name: "Save Settings" });
    expect(saveButton).toBeDisabled();
  });
});
