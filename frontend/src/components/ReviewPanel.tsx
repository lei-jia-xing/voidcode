import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  ChevronRight,
  FileText,
  FolderTree,
  GitBranch,
  Loader2,
  X,
} from "lucide-react";
import type {
  ReviewChangedFile,
  ReviewTreeNode,
  ReviewFileDiff,
  WorkspaceReviewSnapshot,
} from "../lib/runtime/types";

const DEFAULT_PANEL_WIDTH = 384;
const MIN_PANEL_WIDTH = 384;
const MAX_PANEL_WIDTH = 960;
const MIN_MAIN_CONTENT_WIDTH = 280;

interface ReviewPanelProps {
  isOpen: boolean;
  mode: "changes" | "files";
  snapshot: WorkspaceReviewSnapshot | null;
  status: "idle" | "loading" | "success" | "error";
  error: string | null;
  selectedPath: string | null;
  diff: ReviewFileDiff | null;
  diffStatus: "idle" | "loading" | "success" | "error";
  diffError: string | null;
  onClose: () => void;
  onModeChange: (mode: "changes" | "files") => void;
  onRefresh: () => void;
  onSelectPath: (path: string) => void;
}

function changeLabel(changeType: ReviewChangedFile["change_type"]): string {
  switch (changeType) {
    case "added":
      return "A";
    case "modified":
      return "M";
    case "deleted":
      return "D";
    case "renamed":
      return "R";
    case "untracked":
      return "U";
    case "copied":
      return "C";
    case "type_changed":
      return "T";
    default:
      return "?";
  }
}

function TreeList({
  nodes,
  depth,
  selectedPath,
  onSelectPath,
}: {
  nodes: ReviewTreeNode[];
  depth: number;
  selectedPath: string | null;
  onSelectPath: (path: string) => void;
}) {
  return (
    <div className="space-y-0.5">
      {nodes.map((node) => {
        const isSelected = selectedPath === node.path;
        if (node.kind === "directory") {
          return (
            <div key={node.path}>
              <div
                className="flex items-center gap-2 px-2 py-1 text-xs text-slate-400"
                style={{ paddingLeft: `${depth * 12 + 8}px` }}
              >
                <ChevronRight className="h-3.5 w-3.5 text-slate-600" />
                <FolderTree className="h-3.5 w-3.5 text-slate-500" />
                <span className="truncate">{node.name}</span>
              </div>
              <TreeList
                nodes={node.children}
                depth={depth + 1}
                selectedPath={selectedPath}
                onSelectPath={onSelectPath}
              />
            </div>
          );
        }
        return (
          <button
            key={node.path}
            type="button"
            onClick={() => onSelectPath(node.path)}
            className={`flex w-full items-center gap-2 rounded-md px-2 py-1 text-left text-xs transition-colors ${
              isSelected
                ? "bg-indigo-500/10 text-indigo-200"
                : "text-slate-400 hover:bg-slate-800/60 hover:text-slate-200"
            }`}
            style={{ paddingLeft: `${depth * 12 + 8}px` }}
          >
            <FileText className="h-3.5 w-3.5 flex-shrink-0 text-slate-500" />
            <span className="truncate">{node.name}</span>
            {node.changed && (
              <span className="ml-auto text-[10px] font-semibold text-amber-400">
                •
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

export function ReviewPanel({
  isOpen,
  mode,
  snapshot,
  status,
  error,
  selectedPath,
  diff,
  diffStatus,
  diffError,
  onClose,
  onModeChange,
  onRefresh,
  onSelectPath,
}: ReviewPanelProps) {
  const { t } = useTranslation();
  const [panelWidth, setPanelWidth] = useState(DEFAULT_PANEL_WIDTH);
  const [isResizing, setIsResizing] = useState(false);

  const selectedChangedFile = useMemo(
    () =>
      snapshot?.changed_files.find((item) => item.path === selectedPath) ??
      null,
    [snapshot, selectedPath],
  );

  const resizeToClientX = useCallback((clientX: number) => {
    const availableWidth = window.innerWidth;
    const maxWidth = Math.max(
      MIN_PANEL_WIDTH,
      Math.min(MAX_PANEL_WIDTH, availableWidth - MIN_MAIN_CONTENT_WIDTH),
    );
    const nextWidth = Math.min(
      maxWidth,
      Math.max(MIN_PANEL_WIDTH, availableWidth - clientX),
    );
    setPanelWidth(nextWidth);
  }, []);

  useEffect(() => {
    if (!isResizing) return;

    const handlePointerMove = (event: PointerEvent) => {
      event.preventDefault();
      resizeToClientX(event.clientX);
    };
    const handlePointerUp = () => setIsResizing(false);

    const previousCursor = document.body.style.cursor;
    const previousUserSelect = document.body.style.userSelect;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", handlePointerUp);

    return () => {
      document.body.style.cursor = previousCursor;
      document.body.style.userSelect = previousUserSelect;
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", handlePointerUp);
    };
  }, [isResizing, resizeToClientX]);

  if (!isOpen) {
    return null;
  }

  const isNotGitRepo = snapshot?.git.state === "not_git_repo";
  const showEmptyChanges = snapshot && snapshot.changed_files.length === 0;

  return (
    <aside
      className="relative border-l border-slate-800 bg-[#0c0c0e] flex-shrink-0 flex flex-col min-w-0"
      style={{ width: `${panelWidth}px` }}
    >
      <button
        type="button"
        aria-label="Resize review panel"
        aria-orientation="vertical"
        className={`absolute inset-y-0 left-0 z-10 w-2 -translate-x-1 cursor-col-resize transition-colors hover:bg-indigo-500/30 ${
          isResizing ? "bg-indigo-500/40" : "bg-transparent"
        }`}
        onPointerDown={(event) => {
          event.preventDefault();
          setIsResizing(true);
          resizeToClientX(event.clientX);
        }}
      />
      <div className="h-14 border-b border-slate-800 px-4 flex items-center justify-between">
        <div>
          <div className="text-sm font-medium text-slate-200">
            {t("review.title")}
          </div>
          <div className="text-[11px] text-slate-500">
            {snapshot?.root ?? t("review.loading")}
          </div>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="rounded-md p-1.5 text-slate-500 hover:bg-slate-800/60 hover:text-slate-300"
          aria-label={t("review.close")}
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="border-b border-slate-800 px-4 py-3 space-y-3">
        <div className="flex rounded-lg border border-slate-800 bg-slate-950/60 p-1">
          <button
            type="button"
            onClick={() => onModeChange("changes")}
            className={`flex-1 rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
              mode === "changes"
                ? "bg-slate-800 text-slate-100"
                : "text-slate-500 hover:text-slate-300"
            }`}
          >
            {t("review.modeChanges")}
          </button>
          <button
            type="button"
            onClick={() => onModeChange("files")}
            className={`flex-1 rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
              mode === "files"
                ? "bg-slate-800 text-slate-100"
                : "text-slate-500 hover:text-slate-300"
            }`}
          >
            {t("review.modeFiles")}
          </button>
        </div>
        <button
          type="button"
          onClick={onRefresh}
          className="text-xs text-slate-500 hover:text-slate-300"
        >
          {t("review.refresh")}
        </button>
      </div>

      <div className="flex min-h-0 flex-1">
        <div className="w-[13rem] flex-shrink-0 border-r border-slate-800 overflow-y-auto px-2 py-3">
          {status === "loading" && (
            <div className="flex items-center gap-2 px-2 text-xs text-slate-500">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              {t("review.loading")}
            </div>
          )}

          {status === "error" && (
            <div className="px-2 text-xs text-rose-400">
              {t("review.loadError", { message: error ?? "unknown" })}
            </div>
          )}

          {status === "success" && snapshot && isNotGitRepo && (
            <div className="rounded-lg border border-slate-800 bg-slate-900/60 p-3 text-xs text-slate-400">
              <div className="mb-1 font-medium text-slate-200">
                {t("review.noRepoTitle")}
              </div>
              <div>{t("review.noRepoBody")}</div>
            </div>
          )}

          {status === "success" &&
            snapshot &&
            !isNotGitRepo &&
            mode === "changes" &&
            showEmptyChanges && (
              <div className="rounded-lg border border-slate-800 bg-slate-900/60 p-3 text-xs text-slate-400">
                <div className="mb-1 font-medium text-slate-200">
                  {t("review.noChangesTitle")}
                </div>
                <div>{t("review.noChangesBody")}</div>
              </div>
            )}

          {status === "success" &&
            snapshot &&
            !isNotGitRepo &&
            mode === "changes" &&
            !showEmptyChanges && (
              <div className="space-y-1">
                {snapshot.changed_files.map((item) => {
                  const isSelected = selectedPath === item.path;
                  return (
                    <button
                      key={item.path}
                      type="button"
                      onClick={() => onSelectPath(item.path)}
                      className={`w-full rounded-md px-2 py-2 text-left transition-colors ${
                        isSelected
                          ? "bg-indigo-500/10 text-indigo-200"
                          : "hover:bg-slate-800/60 text-slate-400 hover:text-slate-200"
                      }`}
                    >
                      <div className="flex items-center gap-2 text-xs">
                        <span className="w-4 font-semibold text-amber-400">
                          {changeLabel(item.change_type)}
                        </span>
                        <GitBranch className="h-3.5 w-3.5 text-slate-500" />
                        <span className="truncate">{item.path}</span>
                      </div>
                      {item.old_path && (
                        <div className="mt-1 truncate pl-6 text-[10px] text-slate-500">
                          {item.old_path}
                        </div>
                      )}
                    </button>
                  );
                })}
              </div>
            )}

          {status === "success" &&
            snapshot &&
            mode === "files" &&
            (snapshot.tree.length > 0 ? (
              <TreeList
                nodes={snapshot.tree}
                depth={0}
                selectedPath={selectedPath}
                onSelectPath={onSelectPath}
              />
            ) : (
              <div className="rounded-lg border border-slate-800 bg-slate-900/60 p-3 text-xs text-slate-400">
                {t("review.treeEmpty")}
              </div>
            ))}
        </div>

        <div className="min-w-0 flex-1 overflow-y-auto px-4 py-3">
          {!selectedPath && (
            <div className="text-xs text-slate-500">
              {t("review.selectFile")}
            </div>
          )}

          {selectedPath && (
            <>
              <div className="mb-3 border-b border-slate-800 pb-3">
                <div className="text-sm font-medium text-slate-200">
                  {selectedPath}
                </div>
                {selectedChangedFile && (
                  <div className="mt-1 text-[11px] uppercase tracking-wide text-amber-400">
                    {selectedChangedFile.change_type}
                  </div>
                )}
              </div>

              {diffStatus === "loading" && (
                <div className="flex items-center gap-2 text-xs text-slate-500">
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  {t("review.diffLoading")}
                </div>
              )}

              {diffStatus === "error" && (
                <div className="text-xs text-rose-400">
                  {t("review.diffError", { message: diffError ?? "unknown" })}
                </div>
              )}

              {diffStatus === "success" && diff?.state === "not_git_repo" && (
                <div className="text-xs text-slate-500">
                  {t("review.noRepoDiff")}
                </div>
              )}

              {diffStatus === "success" && diff?.state === "clean" && (
                <div className="text-xs text-slate-500">
                  {t("review.cleanFile")}
                </div>
              )}

              {diffStatus === "success" && diff?.diff && (
                <pre className="overflow-x-hidden rounded-xl border border-slate-800 bg-slate-950/80 p-3 text-[11px] leading-relaxed text-slate-300 whitespace-pre-wrap break-words font-mono">
                  {diff.diff}
                </pre>
              )}
            </>
          )}
        </div>
      </div>
    </aside>
  );
}
