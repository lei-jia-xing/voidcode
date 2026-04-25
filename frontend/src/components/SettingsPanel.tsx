import { useState, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { X, Save, Loader2, AlertCircle } from "lucide-react";
import {
  ProviderSummary,
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
  language: string;
  onToggleLanguage: () => void;
  onClose: () => void;
  onLoad: () => void;
  onLoadProviders?: () => void;
  onSave: (settings: RuntimeSettingsUpdate) => void;
}

export function SettingsPanel({
  isOpen,
  settings,
  settingsStatus,
  settingsError,
  providers = [],
  providersStatus,
  providersError,
  language,
  onToggleLanguage,
  onClose,
  onLoad,
  onLoadProviders,
  onSave,
}: SettingsPanelProps) {
  const { t } = useTranslation();
  const [provider, setProvider] = useState("");
  const [apiKey, setApiKey] = useState("");

  useEffect(() => {
    if (isOpen) {
      onLoad();
      onLoadProviders?.();
    }
  }, [isOpen, onLoad, onLoadProviders]);

  useEffect(() => {
    if (settings) {
      setProvider(settings.provider || "");
      setApiKey("");
    }
  }, [settings]);

  const handleSave = () => {
    onSave({
      provider: provider || undefined,
      provider_api_key: apiKey || undefined,
      model: settings?.model || undefined,
    });
  };

  const handleSubmit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    handleSave();
  };

  if (!isOpen) return null;

  const isLoading = settingsStatus === "loading";

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

          <div className="space-y-3">
            <div className="text-sm font-medium text-slate-300">
              {t("settings.provider")}
            </div>
            {providers.length === 0 && providersStatus !== "loading" ? (
              <div className="rounded-lg border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-400">
                No providers available.
              </div>
            ) : (
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                {providers.map((p) => (
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
                          p.configured ? "bg-emerald-500" : "bg-slate-600"
                        }`}
                        title={p.configured ? "Configured" : "Not configured"}
                      />
                    </div>
                    <div className="text-[11px] font-mono text-slate-500">
                      {p.name}
                    </div>
                  </button>
                ))}
              </div>
            )}
          </div>

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
