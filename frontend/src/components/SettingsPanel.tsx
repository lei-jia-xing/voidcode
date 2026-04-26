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

  const handleSubmit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    handleSave();
  };

  if (!isOpen) return null;

  const isLoading = settingsStatus === "loading";
  const configuredProviders = providers.filter((item) => item.configured);
  const unconfiguredProviders = providers.filter((item) => !item.configured);
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
        className="absolute inset-0 bg-black/60 backdrop-blur-sm"
        onClick={onClose}
        aria-label={t("common.close")}
      />
      <form
        className="relative w-full max-w-lg bg-[#0c0c0e] border border-slate-800 rounded-2xl flex flex-col shadow-2xl max-h-[90vh]"
        onSubmit={handleSubmit}
      >
        <div className="flex items-center justify-between px-6 h-14 border-b border-slate-800">
          <h2 className="text-base font-semibold text-slate-100">
            {t("settings.title")}
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="w-8 h-8 flex items-center justify-center rounded-lg text-slate-400 hover:bg-slate-800 hover:text-slate-200 transition-colors"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-6 space-y-6">
          {settingsError && (
            <div className="flex items-start gap-2 rounded-lg bg-rose-500/10 border border-rose-500/20 p-3 text-sm text-rose-300">
              <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5" />
              {settingsError}
            </div>
          )}

          {providersError && (
            <div className="flex items-start gap-2 rounded-lg bg-rose-500/10 border border-rose-500/20 p-3 text-sm text-rose-300">
              <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5" />
              {providersError}
            </div>
          )}

          <div className="space-y-2">
            <div className="text-sm font-medium text-slate-300">
              {t("language.switch")}
            </div>
            <button
              type="button"
              onClick={onToggleLanguage}
              className="inline-flex items-center justify-center rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-200 transition-colors hover:border-slate-600 hover:bg-slate-800"
            >
              {language === "en" ? t("language.zh") : t("language.en")}
            </button>
          </div>

          <div className="space-y-4">
            <div className="text-sm font-medium text-slate-300">
              {t("settings.provider")}
            </div>
            {providers.length === 0 && providersStatus !== "loading" ? (
              <div className="rounded-lg border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-400">
                {t("settings.noProviders")}
              </div>
            ) : (
              <div className="space-y-3">
                {providerGroups.map(({ label, providers: group }) =>
                  group.length > 0 ? (
                    <div key={label} className="space-y-2">
                      <div className="text-xs uppercase tracking-wide text-slate-500">
                        {label}
                      </div>
                      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                        {group.map((p) => (
                          <button
                            type="button"
                            key={p.name}
                            onClick={() => setProvider(p.name)}
                            className={`flex flex-col items-start justify-center rounded-xl border p-3 text-left transition-colors ${
                              provider === p.name
                                ? "border-indigo-500 bg-indigo-500/10 ring-1 ring-indigo-500/50"
                                : "border-slate-800 bg-slate-900/40 hover:border-slate-700 hover:bg-slate-800/60"
                            }`}
                          >
                            <div className="w-full flex items-center justify-between mb-1">
                              <div className="text-sm font-medium text-slate-200">
                                {p.label}
                              </div>
                              <div
                                className={`w-2 h-2 rounded-full ${
                                  p.configured
                                    ? "bg-emerald-500"
                                    : "bg-slate-600"
                                }`}
                                title={
                                  p.configured
                                    ? t("settings.configured")
                                    : t("settings.notConfigured")
                                }
                              />
                            </div>
                            <div className="text-[11px] font-mono text-slate-500">
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

          <div className="space-y-2 pt-2 border-t border-slate-800/50">
            <label
              htmlFor="settings-model"
              className="text-sm font-medium text-slate-300"
            >
              {t("settings.model")}
            </label>
            <select
              id="settings-model"
              value={model}
              onChange={(event) => setModel(event.target.value)}
              disabled={isLoading || configuredProviders.length === 0}
              className="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2.5 text-sm text-slate-200 focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 transition-colors disabled:opacity-50"
            >
              <option value="">{t("settings.modelPlaceholder")}</option>
              {configuredProviders.map((item) => {
                const models = providerModels[item.name]?.models ?? [];
                return models.length > 0 ? (
                  <optgroup key={item.name} label={item.label}>
                    {models.map((modelId) => {
                      const value = canonicalModelReference(item.name, modelId);
                      return (
                        <option key={`${item.name}:${modelId}`} value={value}>
                          {displayModelName(modelId, item.name)}
                        </option>
                      );
                    })}
                  </optgroup>
                ) : null;
              })}
            </select>
            <p className="text-xs text-slate-500">{t("settings.modelHint")}</p>
          </div>

          {provider && (
            <div className="space-y-2 rounded-xl border border-slate-800 bg-slate-900/40 p-3 text-xs text-slate-400">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <div className="font-medium text-slate-300">
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
                <button
                  type="button"
                  onClick={() => onValidateProvider?.(provider)}
                  disabled={isLoading || validationStatus === "loading"}
                  className="inline-flex items-center gap-2 rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-slate-200 transition-colors hover:border-slate-600 hover:bg-slate-800 disabled:opacity-50"
                >
                  {validationStatus === "loading" ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : validationStatus === "success" ? (
                    <CheckCircle2 className="h-4 w-4 text-emerald-400" />
                  ) : validationStatus === "error" ? (
                    <XCircle className="h-4 w-4 text-rose-400" />
                  ) : null}
                  {t("settings.testCredentials")}
                </button>
              </div>
              {selectedProviderModels?.last_error && (
                <div className="text-rose-300">
                  {selectedProviderModels.last_error}
                </div>
              )}
              {(validationResult || validationError) && (
                <div
                  className={
                    validationResult?.ok ? "text-emerald-300" : "text-rose-300"
                  }
                >
                  {validationResult?.message ?? validationError}
                </div>
              )}
            </div>
          )}

          <div className="space-y-2 pt-2 border-t border-slate-800/50">
            <label
              htmlFor="settings-api-key"
              className="text-sm font-medium text-slate-300"
            >
              {t("settings.apiKey")}
            </label>
            <input
              id="settings-api-key"
              type="password"
              autoComplete="new-password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder={t("settings.apiKeyPlaceholder")}
              disabled={isLoading}
              className="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2.5 text-sm text-slate-200 placeholder:text-slate-600 focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 transition-colors disabled:opacity-50"
            />
            <p className="text-xs text-slate-500">{t("settings.apiKeyHint")}</p>
          </div>
        </div>

        <div className="p-6 border-t border-slate-800 bg-slate-900/30 rounded-b-2xl">
          <button
            type="submit"
            disabled={isLoading}
            className="w-full flex items-center justify-center gap-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed text-white px-4 py-2.5 rounded-lg text-sm font-medium transition-colors"
          >
            {isLoading ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <Save className="w-4 h-4" />
            )}
            {t("settings.save")}
          </button>
        </div>
      </form>
    </div>
  );
}
