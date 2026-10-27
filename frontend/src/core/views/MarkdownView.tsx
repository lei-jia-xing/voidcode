import React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { MarkdownViewData } from "../../lib/core/types";
import { componentStyle } from "./componentStyle";

export interface MarkdownViewProps {
  data: MarkdownViewData;
}

// Same renderer and plugin set the transcript already uses: headings, lists,
// tables and code render as markdown. react-markdown v10 does not eval raw
// HTML or script, so plugin content stays inert.
const REMARK_PLUGINS = [remarkGfm];

export const MarkdownView: React.FC<MarkdownViewProps> = ({ data }) => {
  return (
    <div
      data-testid="markdown-view"
      style={componentStyle(data)}
      className="markdown-body rounded-lg border border-neutral-700 bg-neutral-900/60 p-4 my-2 text-sm text-neutral-200"
    >
      <ReactMarkdown remarkPlugins={REMARK_PLUGINS}>
        {data.content}
      </ReactMarkdown>
    </div>
  );
};
