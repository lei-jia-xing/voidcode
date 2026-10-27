import React from "react";
import { useTranslation } from "react-i18next";
import type { ConfirmationViewData } from "../../lib/core/types";
import { componentFg, componentStyle } from "./componentStyle";

export interface ConfirmationViewProps {
  data: ConfirmationViewData;
  onDecision: (requestId: string, decision: "allow" | "deny") => void;
  disabled?: boolean;
}

export const ConfirmationView: React.FC<ConfirmationViewProps> = ({
  data,
  onDecision,
  disabled = false,
}) => {
  const { t } = useTranslation();
  const actions = data.actions || ["allow", "deny"];
  const fg = componentFg(data);

  return (
    <div
      data-testid="confirmation-view"
      style={componentStyle(data)}
      className="rounded-lg border border-red-500/80 bg-red-950/30 p-4 my-2 text-sm"
    >
      <div className="flex items-center gap-2 mb-2 text-red-300 font-semibold text-xs tracking-wider uppercase">
        <span className="inline-block w-2 h-2 rounded-full bg-red-400 animate-pulse" />
        {t("core.approvalRequired")}
      </div>
      <div
        style={fg}
        className="text-neutral-100 font-medium mb-3 whitespace-pre-wrap"
      >
        {data.message}
      </div>
      <div
        style={fg}
        className="text-xs text-neutral-400 mb-4 font-mono select-all"
      >
        {t("core.confirmRequestId")}:{" "}
        <span
          data-testid="confirmation-request-id"
          style={fg}
          className="text-neutral-300"
        >
          {data.request_id}
        </span>
      </div>
      <div className="flex items-center gap-3">
        {actions.includes("allow") && (
          <button
            data-testid="confirm-allow-btn"
            type="button"
            disabled={disabled}
            onClick={() => onDecision(data.request_id, "allow")}
            className="px-4 py-1.5 rounded bg-emerald-600 hover:bg-emerald-500 active:bg-emerald-700 disabled:opacity-50 text-white font-medium text-xs tracking-wide uppercase transition-colors shadow-sm"
          >
            {t("core.confirmAllow")}
          </button>
        )}
        {actions.includes("deny") && (
          <button
            data-testid="confirm-deny-btn"
            type="button"
            disabled={disabled}
            onClick={() => onDecision(data.request_id, "deny")}
            className="px-4 py-1.5 rounded bg-neutral-700 hover:bg-neutral-600 active:bg-neutral-800 disabled:opacity-50 text-neutral-200 font-medium text-xs tracking-wide uppercase transition-colors border border-neutral-600"
          >
            {t("core.confirmDeny")}
          </button>
        )}
      </div>
    </div>
  );
};
