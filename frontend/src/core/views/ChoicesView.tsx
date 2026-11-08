import React, { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ChoicesViewData } from "../../lib/core/types";
import { componentFg, componentStyle } from "./componentStyle";

export interface ChoicesViewProps {
  title?: string;
  data: Omit<ChoicesViewData, "argument_name"> & { argument_name?: string };
  onSelect: (action: string, args: Record<string, unknown>) => void;
  disabled?: boolean;
}

export const ChoicesView: React.FC<ChoicesViewProps> = ({
  title,
  data,
  onSelect,
  disabled = false,
}) => {
  const { t } = useTranslation();
  const options = useMemo(() => data.options || [], [data.options]);
  const argumentName = data.argument_name || "choice";
  const fg = componentFg(data);
  const [filter, setFilter] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(
    options[0]?.id ?? null,
  );

  const filtered = useMemo(() => {
    if (!filter) return options;
    const needle = filter.toLowerCase();
    return options.filter(
      (opt) =>
        opt.label.toLowerCase().includes(needle) ||
        opt.id.toLowerCase().includes(needle),
    );
  }, [options, filter]);

  // Stable-ID selection survives refresh: keep it when present, else clamp.
  // Derived during render, not an effect setState, so no cascading render.
  const effectiveSelectedId = filtered.some((opt) => opt.id === selectedId)
    ? selectedId
    : (filtered[0]?.id ?? null);

  const selectedRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    selectedRef.current?.scrollIntoView?.({ block: "nearest" });
  }, [effectiveSelectedId, filtered.length]);

  const handleOptionClick = (optionId: string) => {
    if (disabled) return;
    setSelectedId(optionId);
    onSelect(data.action, { [argumentName]: optionId });
  };

  return (
    <div
      data-testid="choices-view"
      style={componentStyle(data)}
      className="rounded-lg border border-neutral-700 bg-neutral-900/60 p-4 my-2 text-sm"
    >
      <div
        style={fg}
        className="text-xs font-semibold uppercase text-neutral-400 mb-3 tracking-wider"
      >
        {title || t("core.choicesSelectFor", { action: data.action })}
      </div>
      <input
        data-testid="choices-filter"
        type="text"
        value={filter}
        disabled={disabled}
        onChange={(e) => setFilter(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown" || e.key === "ArrowUp") {
            e.preventDefault();
            if (!filtered.length) return;
            const index = filtered.findIndex(
              (opt) => opt.id === effectiveSelectedId,
            );
            const next =
              (index + (e.key === "ArrowDown" ? 1 : -1) + filtered.length) %
              filtered.length;
            setSelectedId(filtered[next].id);
          } else if (e.key === "Enter") {
            e.preventDefault();
            if (effectiveSelectedId !== null)
              handleOptionClick(effectiveSelectedId);
          } else if (e.key === "Escape" && filter) {
            e.preventDefault();
            setFilter("");
          }
        }}
        placeholder={t("core.choicesFilter")}
        aria-label={t("core.choicesFilter")}
        className="w-full mb-2 px-3 py-1.5 rounded-md bg-neutral-800 border border-neutral-700 text-neutral-100 text-sm placeholder-neutral-500 focus:outline-none focus:border-cyan-500"
      />
      <div
        role="listbox"
        aria-label={title || data.action}
        className="flex flex-col gap-2 max-h-64 overflow-y-auto"
      >
        {filtered.length === 0 && (
          <div data-testid="choices-empty" className="text-neutral-400 text-sm">
            {options.length === 0
              ? t("core.choicesEmpty")
              : t("core.choicesNoMatch")}
          </div>
        )}
        {filtered.map((opt) => (
          <button
            key={opt.id}
            ref={opt.id === effectiveSelectedId ? selectedRef : undefined}
            data-testid={`choice-option-${opt.id}`}
            type="button"
            disabled={disabled}
            role="option"
            aria-selected={opt.id === effectiveSelectedId}
            onClick={() => handleOptionClick(opt.id)}
            className={`flex items-center justify-between px-4 py-2 rounded-md disabled:opacity-50 text-neutral-100 text-left transition-colors border ${
              opt.id === effectiveSelectedId
                ? "bg-neutral-700 border-cyan-500"
                : "bg-neutral-800 hover:bg-neutral-700 border-neutral-700"
            }`}
          >
            <span>{opt.label}</span>
            <span style={fg} className="text-xs text-neutral-400 font-mono">
              [{opt.id}]
            </span>
          </button>
        ))}
      </div>
    </div>
  );
};
