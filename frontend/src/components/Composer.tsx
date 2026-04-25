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
        <div className="relative flex flex-col bg-slate-900 border border-slate-700 rounded-xl focus-within:border-indigo-500 focus-within:ring-1 focus-within:ring-indigo-500 transition-colors overflow-hidden">
          {agentPresets && agentPresets.length > 0 && (
            <div className="flex items-center gap-2 px-3 py-1.5 border-b border-slate-800/60 bg-slate-900/30 text-xs">
              <select
                id="composer-agent"
                aria-label="Agent"
                value={agentPreset ?? "leader"}
                onChange={(event) => onAgentPresetChange?.(event.target.value)}
                disabled={disabled}
                className="bg-transparent border-none text-slate-300 font-medium outline-none max-w-[150px] cursor-pointer appearance-none hover:text-white disabled:opacity-50"
              >
                {agentPresets.map((agent) => (
                  <option key={agent.id} value={agent.id}>
                    {agent.label}
                  </option>
                ))}
              </select>

              <div className="w-px h-3 bg-slate-700" />

              {configuredProviders.length === 0 ? (
                <div className="text-slate-500 truncate flex-1">
                  No providers configured.
                </div>
              ) : availableModelGroups.length === 0 ? (
                <div className="text-slate-500 truncate flex-1">
                  {showConfiguredModelFallback ? (
                    <>
                      Configured:{" "}
                      <span className="font-mono">{selectedModel}</span>
                    </>
                  ) : (
                    <>No models available.</>
                  )}
                </div>
              ) : (
                <select
                  id="composer-model"
                  aria-label="Model"
                  value={selectedModelAvailable ? selectedModel : ""}
                  onChange={(event) =>
                    onProviderModelChange?.(event.target.value)
                  }
                  disabled={disabled}
                  className="bg-transparent border-none text-slate-400 outline-none flex-1 cursor-pointer appearance-none hover:text-slate-200 disabled:opacity-50 truncate"
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
              )}
            </div>
          )}

          <div className="flex items-end gap-2 px-3 py-2 bg-transparent">
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
        </div>
        <p className="text-[11px] text-slate-600 mt-1.5 text-center">
          {t("chat.hint")}
        </p>
      </div>
    </div>
  );
}
