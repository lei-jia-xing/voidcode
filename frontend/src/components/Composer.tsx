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
  reasoningEffort?: string;
  agentPresets?: AgentSummary[];
  providers?: ProviderSummary[];
  providerModels?: Record<string, ProviderModelsResult>;
  onAgentPresetChange?: (preset: string) => void;
  onProviderModelChange?: (model: string) => void;
  onReasoningEffortChange?: (effort: string) => void;
  placeholder?: string;
  onSubmit: (message: string) => void;
}

type ProviderModelMetadata = NonNullable<
  ProviderModelsResult["model_metadata"]
>[string];

export function Composer({
  disabled,
  isRunning,
  agentPreset,
  providerModel,
  reasoningEffort = "",
  agentPresets,
  providers,
  providerModels,
  onAgentPresetChange,
  onProviderModelChange,
  onReasoningEffortChange,
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
    group.models.some(
      (model) =>
        canonicalModelReference(group.provider.name, model) === selectedModel,
    ),
  );
  const selectedModelMetadata = useMemo(() => {
    for (const { provider, models } of availableModelGroups) {
      for (const model of models) {
        const canonical = canonicalModelReference(provider.name, model);
        if (canonical !== selectedModel) continue;
        const metadata = providerModels?.[provider.name]?.model_metadata ?? {};
        return metadata[model] ?? metadata[canonical];
      }
    }
    return undefined;
  }, [availableModelGroups, providerModels, selectedModel]);
  const supportsReasoningEffort =
    selectedModelMetadata?.supports_reasoning_effort === true;
  const effectiveReasoningEffort =
    reasoningEffort ||
    selectedModelMetadata?.default_reasoning_effort ||
    "medium";
  const contextLabel = formatModelContext(selectedModelMetadata);
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
      const matchedModel = models.find(
        (model) =>
          canonicalModelReference(provider.name, model) === selectedModel,
      );
      if (matchedModel) {
        return `${provider.label} / ${displayModelName(matchedModel, provider.name)}`;
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

  const handleEffortSelect = (event: React.ChangeEvent<HTMLSelectElement>) => {
    onReasoningEffortChange?.(event.target.value);
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
                            const value = canonicalModelReference(
                              provider.name,
                              model,
                            );
                            const active = value === selectedModel;
                            return (
                              <button
                                key={`${provider.name}:${model}`}
                                type="button"
                                onClick={() => handleModelSelect(value)}
                                className={`block w-full px-3 py-1.5 text-left text-sm ${
                                  active
                                    ? "bg-slate-800 text-slate-100"
                                    : "text-slate-300 hover:bg-slate-800/60"
                                }`}
                              >
                                <span className="block">
                                  {displayModelName(model, provider.name)}
                                </span>
                                <span className="mt-0.5 block text-[10px] text-slate-500">
                                  {formatModelContext(
                                    providerModels?.[provider.name]
                                      ?.model_metadata?.[model] ??
                                      providerModels?.[provider.name]
                                        ?.model_metadata?.[value],
                                  )}
                                </span>
                              </button>
                            );
                          })}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
              {selectedModelAvailable && contextLabel && (
                <span className="rounded border border-slate-800 px-2 py-1 text-slate-500">
                  {contextLabel}
                </span>
              )}
              {selectedModelAvailable && supportsReasoningEffort && (
                <label className="inline-flex items-center gap-1 text-slate-500">
                  <span>Effort</span>
                  <select
                    aria-label="Reasoning effort"
                    value={effectiveReasoningEffort}
                    onChange={handleEffortSelect}
                    disabled={disabled}
                    className="rounded border border-slate-700 bg-slate-950 px-1.5 py-1 text-slate-300 disabled:opacity-50"
                  >
                    <option value="minimal">minimal</option>
                    <option value="low">low</option>
                    <option value="medium">medium</option>
                    <option value="high">high</option>
                    <option value="xhigh">xhigh</option>
                  </select>
                </label>
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

function canonicalModelReference(providerName: string, model: string): string {
  return model.startsWith(`${providerName}/`)
    ? model
    : `${providerName}/${model}`;
}

function formatModelContext(
  metadata: ProviderModelMetadata | undefined,
): string {
  if (!metadata) return "";
  const parts: string[] = [];
  if (typeof metadata.context_window === "number") {
    parts.push(`${formatTokenCount(metadata.context_window)} ctx`);
  }
  if (typeof metadata.max_output_tokens === "number") {
    parts.push(`${formatTokenCount(metadata.max_output_tokens)} out`);
  }
  if (metadata.supports_reasoning_effort === true) {
    parts.push(`effort ${metadata.default_reasoning_effort ?? "available"}`);
  } else if (metadata.supports_reasoning === true) {
    parts.push("reasoning");
  }
  return parts.join(" · ");
}

function formatTokenCount(value: number): string {
  if (value >= 1_000_000) return `${Math.round(value / 100_000) / 10}M`;
  if (value >= 1_000) return `${Math.round(value / 100) / 10}K`;
  return String(value);
}
