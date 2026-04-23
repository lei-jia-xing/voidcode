import { useState, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { X, Save, Loader2, AlertCircle } from "lucide-react";
import { RuntimeSettings, RuntimeSettingsUpdate } from "../lib/runtime/types";

interface SettingsPanelProps {
  isOpen: boolean;
  settings: RuntimeSettings | null;
  settingsStatus: string;
  settingsError: string | null;
  onClose: () => void;
  onLoad: () => void;
  onSave: (settings: RuntimeSettingsUpdate) => void;
}

export function SettingsPanel({
  isOpen,
  settings,
  settingsStatus,
  settingsError,
  onClose,
  onLoad,
  onSave,
}: SettingsPanelProps) {
  const { t } = useTranslation();
  const [provider, setProvider] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState("");

  useEffect(() => {
    if (isOpen) {
      onLoad();
    }
  }, [isOpen, onLoad]);

  useEffect(() => {
    if (settings) {
      setProvider(settings.provider || "");
      setApiKey("");
      setModel(settings.model || "");
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

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <button
        type="button"
        className="absolute inset-0 bg-black/50 backdrop-blur-sm"
        onClick={onClose}
        aria-label={t("common.close")}
      />
      <div className="relative w-full max-w-md bg-[#0c0c0e] border-l border-slate-800 h-full flex flex-col shadow-2xl">
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

        <div className="flex-1 overflow-y-auto p-6 space-y-5">
          {settingsError && (
            <div className="flex items-start gap-2 rounded-lg bg-rose-500/10 border border-rose-500/20 p-3 text-sm text-rose-300">
              <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5" />
              {settingsError}
            </div>
          )}

          <div className="space-y-2">
            <label
              htmlFor="settings-provider"
              className="text-sm font-medium text-slate-300"
            >
              {t("settings.provider")}
            </label>
            <input
              id="settings-provider"
              type="text"
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
              placeholder={t("settings.providerPlaceholder")}
              disabled={isLoading}
              className="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 placeholder:text-slate-600 focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 transition-colors disabled:opacity-50"
            />
          </div>

          <div className="space-y-2">
            <label
              htmlFor="settings-api-key"
              className="text-sm font-medium text-slate-300"
            >
              {t("settings.apiKey")}
            </label>
            <input
              id="settings-api-key"
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder={t("settings.apiKeyPlaceholder")}
              disabled={isLoading}
              className="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 placeholder:text-slate-600 focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 transition-colors disabled:opacity-50"
            />
            <p className="text-xs text-slate-500">{t("settings.apiKeyHint")}</p>
          </div>

          <div className="space-y-2">
            <label
              htmlFor="settings-model"
              className="text-sm font-medium text-slate-300"
            >
              {t("settings.model")}
            </label>
            <input
              id="settings-model"
              type="text"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder={t("settings.modelPlaceholder")}
              disabled={isLoading}
              className="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 placeholder:text-slate-600 focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 transition-colors disabled:opacity-50"
            />
          </div>
        </div>

        <div className="p-6 border-t border-slate-800">
          <button
            type="button"
            onClick={handleSave}
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
      </div>
    </div>
  );
}
