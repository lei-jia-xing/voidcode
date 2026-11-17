import React from "react";
import type { JsonViewData } from "../../lib/core/types";
import { componentFg, componentStyle } from "./componentStyle";

export interface JsonViewProps {
  data: JsonViewData;
}

export const JsonView: React.FC<JsonViewProps> = ({ data }) => {
  const fg = componentFg(data);
  return (
    <div
      data-testid="json-view"
      style={componentStyle(data)}
      className="rounded-lg border border-neutral-700 bg-neutral-900/60 p-4 my-2 text-sm"
    >
      <pre
        style={fg}
        className="overflow-x-auto text-xs font-mono text-neutral-200 whitespace-pre-wrap"
      >
        {data.preview}
      </pre>
      {data.reference && (
        <div className="text-xs text-neutral-400 mt-2 font-mono">
          [full JSON: artifact://{data.reference.sha256}]
        </div>
      )}
    </div>
  );
};
