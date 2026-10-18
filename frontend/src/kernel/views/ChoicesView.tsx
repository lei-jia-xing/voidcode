import React from "react";
import { useTranslation } from "react-i18next";
import type { ChoicesViewData } from "../../lib/kernel/types";

export interface ChoicesViewProps {
  data: Omit<ChoicesViewData, "argument_name"> & { argument_name?: string };
  onSelect: (action: string, args: Record<string, unknown>) => void;
  disabled?: boolean;
}

export const ChoicesView: React.FC<ChoicesViewProps> = ({
  data,
  onSelect,
  disabled = false,
}) => {
  const { t } = useTranslation();
  const options = data.options || [];
  const argumentName = data.argument_name || "choice";

  const handleOptionClick = (optionId: string) => {
    if (disabled) return;
    onSelect(data.action, { [argumentName]: optionId });
  };

  return (
    <div
      data-testid="choices-view"
      className="rounded-lg border border-neutral-700 bg-neutral-900/60 p-4 my-2 text-sm"
    >
      <div className="text-xs font-semibold uppercase text-neutral-400 mb-3 tracking-wider">
        {t("kernel.choicesSelectFor", { action: data.action })}
      </div>
      <div className="flex flex-col gap-2">
        {options.map((opt) => (
          <button
            key={opt.id}
            data-testid={`choice-option-${opt.id}`}
            type="button"
            disabled={disabled}
            onClick={() => handleOptionClick(opt.id)}
            className="flex items-center justify-between px-4 py-2 rounded-md bg-neutral-800 hover:bg-neutral-700 active:bg-neutral-600 disabled:opacity-50 text-neutral-100 text-left transition-colors border border-neutral-700"
          >
            <span>{opt.label}</span>
            <span className="text-xs text-neutral-400 font-mono">
              [{opt.id}]
            </span>
          </button>
        ))}
      </div>
    </div>
  );
};
