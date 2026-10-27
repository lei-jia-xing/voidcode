import React from "react";
import type { StatusViewData } from "../../lib/core/types";
import { componentFg, componentStyle } from "./componentStyle";

export interface StatusViewProps {
  data: StatusViewData;
}

export const StatusView: React.FC<StatusViewProps> = ({ data }) => {
  const fg = componentFg(data);
  return (
    <div
      data-testid="status-view"
      style={componentStyle(data)}
      className="inline-flex items-center gap-2 px-3 py-1.5 rounded-full border border-emerald-500/50 bg-emerald-950/30 text-xs font-medium text-emerald-200 my-1"
    >
      <span
        style={fg ? { backgroundColor: fg.color } : undefined}
        className="w-2 h-2 rounded-full bg-emerald-400"
      />
      <span style={fg} className="font-semibold text-emerald-300">
        {data.label}:
      </span>
      <span style={fg}>{data.value}</span>
    </div>
  );
};
