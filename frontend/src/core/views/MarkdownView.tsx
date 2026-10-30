import React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { MarkdownViewData } from "../../lib/core/types";
import { componentStyle } from "./componentStyle";

export interface MarkdownViewProps {
  data: MarkdownViewData;
  onAction?: (
    action: string,
    args: Record<string, unknown>,
    prepareInput: boolean,
  ) => void;
  disabled?: boolean;
}

// Same renderer and plugin set the transcript already uses: headings, lists,
// tables and code render as markdown. react-markdown v10 does not eval raw
// HTML or script, so plugin content stays inert.
const REMARK_PLUGINS = [remarkGfm];

export const MarkdownView: React.FC<MarkdownViewProps> = ({
  data,
  onAction,
  disabled = false,
}) => {
  return (
    <div
      data-testid="markdown-view"
      style={componentStyle(data)}
      className="markdown-body rounded-lg border border-neutral-700 bg-neutral-900/60 p-4 my-2 text-sm text-neutral-200"
    >
      <ReactMarkdown remarkPlugins={REMARK_PLUGINS}>
        {data.content}
      </ReactMarkdown>
      {data.actions && data.actions.length > 0 && onAction && (
        <div className="flex flex-wrap gap-2 border-t border-neutral-700 pt-3 mt-3">
          {data.actions.map((action, index) => (
            <button
              key={index}
              type="button"
              disabled={disabled}
              onClick={() =>
                onAction(
                  action.action,
                  action.arguments || {},
                  action.prepare_input === true,
                )
              }
              className="px-3 py-1.5 rounded bg-neutral-800 hover:bg-neutral-700 disabled:opacity-50 text-neutral-100"
            >
              {action.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
};
