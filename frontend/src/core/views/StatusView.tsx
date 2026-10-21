import React from "react";
import type { StatusViewData } from "../../lib/core/types";

export interface StatusViewProps {
  data: StatusViewData;
}

export const StatusView: React.FC<StatusViewProps> = ({ data }) => {
  return (
    <div
      data-testid="status-view"
      className="inline-flex items-center gap-2 px-3 py-1.5 rounded-full border border-emerald-500/50 bg-emerald-950/30 text-xs font-medium text-emerald-200 my-1"
    >
      <span className="w-2 h-2 rounded-full bg-emerald-400" />
      <span className="font-semibold text-emerald-300">{data.label}:</span>
      <span>{data.value}</span>
    </div>
  );
};
