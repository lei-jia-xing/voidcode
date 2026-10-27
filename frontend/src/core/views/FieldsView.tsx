import React from "react";
import type { FieldsViewData } from "../../lib/core/types";
import { componentFg, componentStyle } from "./componentStyle";

export interface FieldsViewProps {
  data: FieldsViewData;
}

export const FieldsView: React.FC<FieldsViewProps> = ({ data }) => {
  const rows = data.rows || [];
  const fg = componentFg(data);

  return (
    <div
      data-testid="fields-view"
      style={componentStyle(data)}
      className="rounded-lg border border-neutral-700 bg-neutral-900/60 p-4 my-2 text-sm"
    >
      <div className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2">
        {rows.map((row, idx) => (
          <React.Fragment key={idx}>
            <span
              style={fg}
              className="font-semibold text-neutral-400 select-none"
            >
              {row.label}:
            </span>
            <span style={fg} className="text-neutral-100 break-words">
              {row.value}
            </span>
          </React.Fragment>
        ))}
      </div>
    </div>
  );
};
