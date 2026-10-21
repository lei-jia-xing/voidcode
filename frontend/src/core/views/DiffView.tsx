import React from "react";
import { useTranslation } from "react-i18next";
import type { DiffViewData } from "../../lib/core/types";

export interface DiffViewProps {
  data: DiffViewData;
}

export const DiffView: React.FC<DiffViewProps> = ({ data }) => {
  const { t } = useTranslation();
  const before = data.before || "";
  const after = data.after || "";

  return (
    <div
      data-testid="diff-view"
      className="rounded-lg border border-neutral-700 bg-neutral-900/60 p-4 my-2 text-sm font-mono"
    >
      {data.path && (
        <div className="text-xs font-semibold text-neutral-400 mb-2 border-b border-neutral-800 pb-1">
          {t("core.diffFile", { path: data.path })}
        </div>
      )}
      <div className="overflow-x-auto text-xs flex flex-col">
        {before.split("\n").map((line, index) => (
          <div
            key={`before-${index}`}
            className="text-red-400 bg-red-950/20 px-2 py-0.5"
          >
            - {line}
          </div>
        ))}
        {after.split("\n").map((line, index) => (
          <div
            key={`after-${index}`}
            className="text-emerald-400 bg-emerald-950/20 px-2 py-0.5"
          >
            + {line}
          </div>
        ))}
      </div>
    </div>
  );
};
