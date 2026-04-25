import { useState, useRef, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { Send, Loader2 } from "lucide-react";
import type {
  AgentSummary,
  ProviderModelsResult,
  ProviderSummary,
} from "../lib/runtime/types";

interface ComposerProps {
  disabled: boolean;
  isRunning: boolean;
  agentPreset?: string;
  providerModel?: string;
  agentPresets?: AgentSummary[];
  providers?: ProviderSummary[];
  providerModels?: Record<string, ProviderModelsResult>;
  onAgentPresetChange?: (preset: string) => void;
  onProviderModelChange?: (model: string) => void;
  placeholder?: string;
  onSubmit: (message: string) => void;
}

export function Composer({
  disabled,
  isRunning,
  agentPreset,
  providerModel,
  agentPresets,
  providers,
  providerModels,
  onAgentPresetChange,
  onProviderModelChange,
  placeholder,
  onSubmit,
}: ComposerProps) {
  const { t } = useTranslation();
  const [input, setInput] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const configuredProviders = (providers ?? []).filter(
    (provider) => provider.configured,
  );
  const selectedModel = providerModel?.trim() ?? "";
  const availableModelGroups = configuredProviders
    .map((provider) => ({
      provider,
      models: providerModels?.[provider.name]?.models ?? [],
    }))
    .filter((group) => group.models.length > 0);
  const selectedModelAvailable = availableModelGroups.some((group) =>
    group.models.includes(selectedModel),
  );
  const showConfiguredModelFallback =
    selectedModel !== "" &&
    configuredProviders.length > 0 &&
    availableModelGroups.length === 0;

  const resizeTextarea = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, []);

  const handleSubmit = () => {
    const trimmed = input.trim();
    if (!trimmed || disabled) return;
    onSubmit(trimmed);
    setInput("");
    const el = textareaRef.current;
    if (el) el.style.height = "auto";
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  const handleChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInput(e.target.value);
    resizeTextarea();
  };

  return (
    <div className="border-t border-slate-800 bg-[#0c0c0e] px-4 py-3">
      <div className="max-w-3xl mx-auto">
        {agentPresets && agentPresets.length > 0 && (
          <div className="mb-3 grid gap-3 md:grid-cols-2">
            <div className="space-y-1.5">
              <label
                className="text-xs font-medium text-slate-400"
                htmlFor="composer-agent"
              >
                Agent
              </label>
              <select
                id="composer-agent"
                value={agentPreset ?? "leader"}
                onChange={(event) => onAgentPresetChange?.(event.target.value)}
                disabled={disabled}
                className="w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-200 disabled:opacity-50"
              >
                {agentPresets.map((agent) => (
                  <option key={agent.id} value={agent.id}>
                    {agent.label}
                  </option>
                ))}
              </select>
            </div>

            {configuredProviders.length === 0 ? (
              <div className="rounded-lg border border-slate-800 bg-slate-900/60 px-3 py-2 text-xs text-slate-400 md:col-span-1">
                No providers configured. Add an API key in Settings.
              </div>
            ) : availableModelGroups.length === 0 ? (
              <div className="rounded-lg border border-slate-800 bg-slate-900/60 px-3 py-2 text-xs text-slate-400 md:col-span-1">
                {showConfiguredModelFallback ? (
                  <>
                    Using configured model{" "}
                    <span className="font-mono text-slate-300">
                      {selectedModel}
                    </span>
                    , catalog unavailable.
                  </>
                ) : (
                  <>No models available.</>
                )}
              </div>
            ) : (
              <div className="space-y-1.5">
                <label
                  className="text-xs font-medium text-slate-400"
                  htmlFor="composer-model"
                >
                  Model
                </label>
                <select
                  id="composer-model"
                  value={selectedModelAvailable ? selectedModel : ""}
                  onChange={(event) =>
                    onProviderModelChange?.(event.target.value)
                  }
                  disabled={disabled}
                  className="w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-200 disabled:opacity-50"
                >
                  {availableModelGroups.map(({ provider, models }) => (
                    <optgroup key={provider.name} label={provider.label}>
                      {models.map((model) => (
                        <option key={model} value={model}>
                          {model}
                        </option>
                      ))}
                    </optgroup>
                  ))}
                </select>
              </div>
            )}
          </div>
        )}

        <div className="relative flex items-end gap-2 bg-slate-900 border border-slate-700 rounded-xl px-3 py-2 focus-within:border-indigo-500 focus-within:ring-1 focus-within:ring-indigo-500 transition-colors">
          <textarea
            ref={textareaRef}
            value={input}
            onChange={handleChange}
            onKeyDown={handleKeyDown}
            placeholder={placeholder || t("chat.placeholder")}
            disabled={disabled}
            rows={1}
            className="flex-1 bg-transparent text-sm text-slate-200 placeholder:text-slate-500 resize-none outline-none py-1.5 max-h-[200px] disabled:opacity-50"
          />
          <button
            type="button"
            onClick={handleSubmit}
            disabled={disabled || !input.trim()}
            className="flex-shrink-0 w-8 h-8 flex items-center justify-center rounded-lg bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 disabled:cursor-not-allowed text-white transition-colors mb-0.5"
          >
            {isRunning ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <Send className="w-4 h-4" />
            )}
          </button>
        </div>
        <p className="text-[11px] text-slate-600 mt-1.5 text-center">
          {t("chat.hint")}
        </p>
      </div>
    </div>
  );
}
