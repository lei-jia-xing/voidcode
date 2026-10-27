import React from "react";
import { useTranslation } from "react-i18next";
import type { NoticeViewData } from "../../lib/core/types";
import { componentFg, componentStyle } from "./componentStyle";

export interface NoticeViewProps {
  data: NoticeViewData;
}

export const NoticeView: React.FC<NoticeViewProps> = ({ data }) => {
  const { t } = useTranslation();
  const level = data.level || "info";
  const fg = componentFg(data);
  const borderColors = {
    info: "border-blue-500 bg-blue-950/40 text-blue-200",
    warning: "border-yellow-500 bg-yellow-950/40 text-yellow-200",
    error: "border-red-500 bg-red-950/40 text-red-200",
  };
  const badgeKeys: Record<string, string> = {
    info: "core.noticeInfo",
    warning: "core.noticeWarning",
    error: "core.noticeError",
  };
  return (
    <div
      data-testid="notice-view"
      data-level={level}
      style={componentStyle(data)}
      className={`rounded-lg border p-4 my-2 text-sm ${borderColors[level] || borderColors.info}`}
    >
      <div
        style={fg}
        className="font-semibold text-xs tracking-wider uppercase mb-1"
      >
        {t(badgeKeys[level] || "core.noticeInfo")}
      </div>
      <div style={fg} className="whitespace-pre-wrap">
        {data.message}
      </div>
    </div>
  );
};
