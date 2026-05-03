import { useState, useEffect } from "react";
import { useTranslation } from "react-i18next";
import {
  X,
  Save,
  Loader2,
  AlertCircle,
  CheckCircle2,
  XCircle,
} from "lucide-react";
import {
  AsyncStatus,
  ProviderModelsResult,
  ProviderSummary,
  ProviderValidationResult,
  RuntimeSettings,
  RuntimeSettingsUpdate,
} from "../lib/runtime/types";
import { ControlButton } from "./ui";

interface SettingsPanelProps {
  isOpen: boolean;
  settings: RuntimeSettings | null;
  settingsStatus: string;
  settingsError: string | null;
  providers?: ProviderSummary[];
  providersStatus?: string;
  providersError?: string | null;
  providerModels?: Record<string, ProviderModelsResult>;
  providerValidationResults?: Record<string, ProviderValidationResult>;
  providerValidationStatus?: Record<string, AsyncStatus>;
  providerValidationError?: Record<string, string | null>;
  language: string;
  onToggleLanguage: () => void;
  onClose: () => void;
  onLoad: () => void;
  onLoadProviders?: () => void;
  onValidateProvider?: (providerName: string) => void;
  onSave: (settings: RuntimeSettingsUpdate) => void;
}

function canonicalModelReference(providerName: string, model: string): string {
  return model.startsWith(`${providerName}/`)
    ? model
    : `${providerName}/${model}`;
}

function displayModelName(model: string, providerName: string): string {
  return model.startsWith(`${providerName}/`)
    ? model.slice(providerName.length + 1)
    : model;
}

function modelBelongsToProvider(model: string, providerName: string): boolean {
  if (!model) return false;
  return model.startsWith(`${providerName}/`);
}

export function SettingsPanel({
  isOpen,
  settings,
  settingsStatus,
  settingsError,
  providers = [],
  providersStatus,
  providersError,
  providerModels = {},
  providerValidationResults = {},
  providerValidationStatus = {},
  providerValidationError = {},
  language,
  onToggleLanguage,
  onClose,
  onLoad,
  onLoadProviders,
  onValidateProvider,
  onSave,
}: SettingsPanelProps) {
  const { t } = useTranslation();
  const [provider, setProvider] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState("");

  useEffect(() => {
    if (isOpen) {
      onLoad();
      onLoadProviders?.();
    }
  }, [isOpen, onLoad, onLoadProviders]);

  useEffect(() => {
    if (settings) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- initialising local form state from external settings
      setProvider(settings.provider || "");
      setModel(settings.model || "");
      setApiKey("");
    }
  }, [settings]);

  const handleSave = () => {
    onSave({
      provider: provider || undefined,
      provider_api_key: apiKey || undefined,
      model: model || undefined,
    });
  };

  if (!isOpen) return null;

  const isLoading = settingsStatus === "loading";
  const configuredProviders = providers.filter((item) => item.configured);
  const unconfiguredProviders = providers.filter((item) => !item.configured);
  const selectedProvider = provider
    ? providers.find((item) => item.name === provider)
    : undefined;
  const providerGroups = [
    {
      label: t("settings.configuredProviders"),
      providers: configuredProviders,
    },
    {
      label: t("settings.unconfiguredProviders"),
      providers: unconfiguredProviders,
    },
  ];
  const selectedProviderModels = provider
    ? providerModels[provider]
    : undefined;
  const selectedModels = selectedProviderModels?.models ?? [];
  const validationStatus = provider
    ? providerValidationStatus[provider]
    : undefined;
  const validationResult = provider
    ? providerValidationResults[provider]
    : undefined;
  const validationError = provider
    ? providerValidationError[provider]
    : undefined;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <button
        type="button"
        className="absolute inset-0 bg-[var(--vc-overlay-bg)] backdrop-blur-sm"
        onClick={onClose}
        aria-label={t("common.close")}
      />
      <form
        className="relative w-full max-w-lg bg-[var(--vc-bg)] border border-[color:var(--vc-border-subtle)] rounded-2xl flex flex-col shadow-2xl max-h-[90vh]"
        onSubmit={(event) => {
          event.preventDefault();
          handleSave();
        }}
      >
        <div className="flex items-center justify-between px-6 h-14 border-b border-[color:var(--vc-border-subtle)]">
          <h2 className="text-base font-semibold text-[var(--vc-text-primary)]">
            {t("settings.title")}
          </h2>
          <ControlButton
            compact
            icon
            variant="ghost"
            onClick={onClose}
            aria-label={t("common.close")}
          >
            <X className="w-5 h-5" />
          </ControlButton>
        </div>

        <div className="flex-1 overflow-y-auto p-6 space-y-6">
          {settingsError && (
            <div className="flex items-start gap-2 rounded-lg bg-[var(--vc-danger-bg)] border border-[color:var(--vc-danger-border)] p-3 text-sm text-[var(--vc-danger-text)]">
              <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5" />
              {settingsError}
            </div>
          )}

          {providersError && (
            <div className="flex items-start gap-2 rounded-lg bg-[var(--vc-danger-bg)] border border-[color:var(--vc-danger-border)] p-3 text-sm text-[var(--vc-danger-text)]">
              <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5" />
              {providersError}
            </div>
          )}

          <div className="space-y-2">
            <div className="text-sm font-medium text-[var(--vc-text-muted)]">
              {t("language.switch")}
            </div>
            <ControlButton variant="secondary" onClick={onToggleLanguage}>
              {language === "en" ? t("language.zh") : t("language.en")}
            </ControlButton>
          </div>

          <div className="space-y-4">
            <div className="text-sm font-medium text-[var(--vc-text-muted)]">
              {t("settings.provider")}
            </div>
            {providers.length === 0 && providersStatus !== "loading" ? (
              <div className="rounded-lg border border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] px-3 py-2 text-sm text-[var(--vc-text-muted)]">
                {t("settings.noProviders")}
              </div>
            ) : (
              <div className="space-y-3">
                {providerGroups.map(({ label, providers: group }) =>
                  group.length > 0 ? (
                    <div key={label} className="space-y-2">
                      <div className="text-xs uppercase tracking-wide text-[var(--vc-text-subtle)]">
                        {label}
                      </div>
                      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                        {group.map((p) => (
                          <button
                            type="button"
                            key={p.name}
                            onClick={() => {
                              setProvider(p.name);
                              setModel((current) =>
                                modelBelongsToProvider(current, p.name)
                                  ? current
                                  : "",
                              );
                            }}
                            aria-pressed={provider === p.name}
                            className={`flex flex-col items-start justify-center rounded-xl border p-3 text-left transition-colors ${
                              provider === p.name
                                ? "border-[color:var(--vc-border-strong)] bg-[var(--vc-surface-2)] ring-1 ring-[color:var(--vc-border-strong)]"
                                : "border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] hover:border-[color:var(--vc-border-strong)] hover:bg-[var(--vc-surface-2)]"
                            }`}
                          >
                            <div className="w-full flex items-center justify-between mb-1">
                              <div className="text-sm font-medium text-[var(--vc-text-primary)]">
                                {p.label}
                              </div>
                              <span className="inline-flex items-center">
                                <span
                                  className={`w-2 h-2 rounded-full ${
                                    p.configured
                                      ? "bg-[var(--vc-confirm-text)]"
                                      : "bg-[var(--vc-text-subtle)]"
                                  }`}
                                  aria-hidden="true"
                                />
                                <span className="sr-only">
                                  {p.configured
                                    ? t("settings.configured")
                                    : t("settings.notConfigured")}
                                </span>
                              </span>
                            </div>
                            <div className="text-[11px] font-mono text-[var(--vc-text-subtle)]">
                              {p.name}
                            </div>
                          </button>
                        ))}
                      </div>
                    </div>
                  ) : null,
                )}
              </div>
            )}
          </div>

          <div className="space-y-2 pt-2 border-t border-[color:var(--vc-border-subtle)]">
            <label
              htmlFor="settings-model"
              className="text-sm font-medium text-[var(--vc-text-muted)]"
            >
              {t("settings.model")}
            </label>
            <select
              id="settings-model"
              value={model}
              onChange={(event) => setModel(event.target.value)}
              disabled={isLoading || !provider || selectedModels.length === 0}
              className="w-full bg-[var(--vc-surface-1)] border border-[color:var(--vc-border-subtle)] rounded-lg px-3 py-2.5 text-sm text-[var(--vc-text-primary)] focus:outline-none focus:border-[color:var(--vc-border-strong)] focus:ring-1 focus:ring-[color:var(--vc-border-strong)] transition-colors disabled:opacity-50"
            >
              <option value="">{t("settings.modelPlaceholder")}</option>
              {selectedModels.map((modelId) => {
                const value = canonicalModelReference(provider, modelId);
                return (
                  <option key={`${provider}:${modelId}`} value={value}>
                    {displayModelName(modelId, provider)}
                  </option>
                );
              })}
            </select>
            <p className="text-xs text-[var(--vc-text-subtle)]">
              {selectedProvider
                ? t("settings.modelHintForProvider", {
                    provider: selectedProvider.label,
                    count: selectedModels.length,
                  })
                : t("settings.modelHint")}
            </p>
          </div>

          {provider && (
            <div className="space-y-2 rounded-xl border border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] p-3 text-xs text-[var(--vc-text-muted)]">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <div className="font-medium text-[var(--vc-text-muted)]">
                    {t("settings.discoveryStatus")}
                  </div>
                  <div className="mt-1 font-mono">
                    {selectedProviderModels?.last_refresh_status ??
                      t("status.unknown")}
                    {selectedProviderModels?.discovery_mode
                      ? ` · ${selectedProviderModels.discovery_mode}`
                      : ""}
                  </div>
                </div>
                <ControlButton
                  compact
                  variant="secondary"
                  onClick={() => onValidateProvider?.(provider)}
                  disabled={isLoading || validationStatus === "loading"}
                >
                  {validationStatus === "loading" ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : validationStatus === "success" ? (
                    <CheckCircle2 className="h-4 w-4 text-[var(--vc-confirm-text)]" />
                  ) : validationStatus === "error" ? (
                    <XCircle className="h-4 w-4 text-[var(--vc-danger-text)]" />
                  ) : null}
                  {t("settings.testCredentials")}
                </ControlButton>
              </div>
              {selectedProviderModels?.last_error && (
                <div className="text-[var(--vc-danger-text)]">
                  {selectedProviderModels.last_error}
                </div>
              )}
              {(validationResult || validationError) && (
                <div
                  className={
                    validationResult?.ok
                      ? "text-[var(--vc-confirm-text)]"
                      : "text-[var(--vc-danger-text)]"
                  }
                >
                  {validationResult?.message ?? validationError}
                </div>
              )}
            </div>
          )}

          <div className="space-y-2 pt-2 border-t border-[color:var(--vc-border-subtle)]">
            <label
              htmlFor="settings-api-key"
              className="text-sm font-medium text-[var(--vc-text-muted)]"
            >
              {t("settings.apiKey")}
            </label>
            <input
              id="settings-api-key"
              type="password"
              autoComplete="off"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder={t("settings.apiKeyPlaceholder")}
              disabled={isLoading}
              spellCheck={false}
              className="w-full bg-[var(--vc-surface-1)] border border-[color:var(--vc-border-subtle)] rounded-lg px-3 py-2.5 text-sm text-[var(--vc-text-primary)] placeholder:text-[var(--vc-text-subtle)] focus:outline-none focus:border-[color:var(--vc-border-strong)] focus:ring-1 focus:ring-[color:var(--vc-border-strong)] transition-colors disabled:opacity-50"
            />
            <p className="text-xs text-[var(--vc-text-subtle)]">
              {t("settings.apiKeyHint")}
            </p>
          </div>
        </div>

        <div className="p-6 border-t border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] rounded-b-2xl">
          <ControlButton
            type="submit"
            disabled={isLoading}
            variant="primary"
            className="w-full"
          >
            {isLoading ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <Save className="w-4 h-4" />
            )}
            {t("settings.save")}
          </ControlButton>
        </div>
      </form>
    </div>
  );
}
