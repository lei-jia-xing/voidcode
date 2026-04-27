import { useState, useRef, useCallback, useEffect, useMemo } from "react";
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
  const [showAgentMenu, setShowAgentMenu] = useState(false);
  const [showModelMenu, setShowModelMenu] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const agentMenuRef = useRef<HTMLDivElement>(null);
  const modelMenuRef = useRef<HTMLDivElement>(null);

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
  const selectableAgentPresets = useMemo(() => {
    return (agentPresets ?? []).filter((agent) => agent.selectable !== false);
  }, [agentPresets]);

  const selectedAgentLabel = useMemo(() => {
    return (
      agentPresets?.find((agent) => agent.id === (agentPreset ?? "leader"))
        ?.label ??
      agentPreset ??
      "leader"
    );
  }, [agentPreset, agentPresets]);

  const selectedModelLabel = useMemo(() => {
    for (const { provider, models } of availableModelGroups) {
      if (models.includes(selectedModel)) {
        return `${provider.label} / ${displayModelName(selectedModel, provider.name)}`;
      }
    }
    return "Select model";
  }, [availableModelGroups, selectedModel]);

  useEffect(() => {
    const handlePointerDown = (event: MouseEvent) => {
      const target = event.target as Node;
      if (agentMenuRef.current && !agentMenuRef.current.contains(target)) {
        setShowAgentMenu(false);
      }
      if (modelMenuRef.current && !modelMenuRef.current.contains(target)) {
        setShowModelMenu(false);
      }
    };

    document.addEventListener("mousedown", handlePointerDown);
    return () => document.removeEventListener("mousedown", handlePointerDown);
  }, []);

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

  const handleAgentSelect = (nextAgent: string) => {
    onAgentPresetChange?.(nextAgent);
    setShowAgentMenu(false);
  };

  const handleModelSelect = (nextModel: string) => {
    onProviderModelChange?.(nextModel);
    setShowModelMenu(false);
  };

  return (
    <div className="border-t border-slate-800 bg-[#0c0c0e] px-4 py-3">
      <div className="max-w-3xl mx-auto">
        <div className="relative flex flex-col bg-slate-900 border border-slate-700 rounded-xl focus-within:border-indigo-500 focus-within:ring-1 focus-within:ring-indigo-500 transition-colors overflow-hidden">
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

          {selectableAgentPresets.length > 0 && (
            <div className="flex flex-wrap items-center gap-2 border-t border-slate-800/60 bg-slate-900/30 px-3 py-1.5 text-xs">
              <div className="relative" ref={agentMenuRef}>
                <button
                  id="composer-agent"
                  aria-label="Agent"
                  type="button"
                  onClick={() => {
                    if (disabled) return;
                    setShowModelMenu(false);
                    setShowAgentMenu((open) => !open);
                  }}
                  disabled={disabled}
                  className="max-w-[180px] truncate rounded border border-slate-700 px-2 py-1 text-left text-slate-300 disabled:opacity-50"
                >
                  {selectedAgentLabel}
                </button>

                {showAgentMenu && (
                  <div className="absolute bottom-full left-0 z-20 mb-2 min-w-[180px] rounded border border-slate-700 bg-[#0c0c0e] py-1 shadow-xl">
                    {selectableAgentPresets.map((agent) => {
                      const active = agent.id === (agentPreset ?? "leader");
                      return (
                        <button
                          key={agent.id}
                          type="button"
                          onClick={() => handleAgentSelect(agent.id)}
                          className={`block w-full px-3 py-1.5 text-left text-sm ${
                            active
                              ? "bg-slate-800 text-slate-100"
                              : "text-slate-300 hover:bg-slate-800/60"
                          }`}
                        >
                          {agent.label}
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>

              {availableModelGroups.length > 0 && (
                <div className="relative min-w-0 flex-1" ref={modelMenuRef}>
                  <button
                    id="composer-model"
                    aria-label="Model"
                    type="button"
                    onClick={() => {
                      if (disabled) return;
                      setShowAgentMenu(false);
                      setShowModelMenu((open) => !open);
                    }}
                    disabled={disabled}
                    className="w-full truncate rounded border border-slate-700 px-2 py-1 text-left text-slate-400 disabled:opacity-50"
                  >
                    {selectedModelAvailable
                      ? selectedModelLabel
                      : "Select model"}
                  </button>

                  {showModelMenu && (
                    <div className="absolute bottom-full left-0 z-20 mb-2 max-h-72 min-w-[260px] max-w-[360px] overflow-y-auto rounded border border-slate-700 bg-[#0c0c0e] py-1 shadow-xl">
                      {availableModelGroups.map(({ provider, models }) => (
                        <div key={provider.name} className="py-1">
                          <div className="px-3 py-1 text-[11px] text-slate-500">
                            {provider.label}
                          </div>
                          {models.map((model) => {
                            const active = model === selectedModel;
                            return (
                              <button
                                key={model}
                                type="button"
                                onClick={() => handleModelSelect(model)}
                                className={`block w-full px-3 py-1.5 text-left text-sm ${
                                  active
                                    ? "bg-slate-800 text-slate-100"
                                    : "text-slate-300 hover:bg-slate-800/60"
                                }`}
                              >
                                {displayModelName(model, provider.name)}
                              </button>
                            );
                          })}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
        <p className="text-[11px] text-slate-600 mt-1.5 text-center">
          {t("chat.hint")}
        </p>
      </div>
    </div>
  );
}

function displayModelName(model: string, providerName: string | null): string {
  if (providerName && model.startsWith(`${providerName}/`)) {
    return model.slice(providerName.length + 1);
  }
  return model;
}
