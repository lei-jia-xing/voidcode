import { useState, useMemo, useEffect } from "react";
import { useTranslation } from "react-i18next";
import {
  X,
  Search,
  Loader2,
  AlertCircle,
  FolderOpen,
  Clock,
  CheckCircle2,
} from "lucide-react";
import { WorkspaceSummary } from "../lib/runtime/types";

interface OpenProjectModalProps {
  isOpen: boolean;
  onClose: () => void;
  recentWorkspaces: WorkspaceSummary[];
  candidateWorkspaces: WorkspaceSummary[];
  workspacesStatus: string;
  workspacesError: string | null;
  workspaceSwitchStatus: string;
  workspaceSwitchError: string | null;
  currentWorkspacePath: string | null;
  onSwitchWorkspace: (path: string) => Promise<void>;
}

export function OpenProjectModal({
  isOpen,
  onClose,
  recentWorkspaces,
  candidateWorkspaces,
  workspacesStatus,
  workspacesError,
  workspaceSwitchStatus,
  workspaceSwitchError,
  currentWorkspacePath,
  onSwitchWorkspace,
}: OpenProjectModalProps) {
  const { t } = useTranslation();
  const [query, setQuery] = useState("");
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [didInitiateSwitch, setDidInitiateSwitch] = useState(false);

  useEffect(() => {
    if (!didInitiateSwitch) return;
    if (workspaceSwitchStatus === "success") {
      setDidInitiateSwitch(false);
      onClose();
    } else if (workspaceSwitchStatus === "error") {
      setDidInitiateSwitch(false);
    }
  }, [didInitiateSwitch, workspaceSwitchStatus, onClose]);

  const normalize = (s: string) => s.toLowerCase().trim();

  const filteredRecent = useMemo(() => {
    if (!query) return recentWorkspaces;
    const q = normalize(query);
    return recentWorkspaces.filter(
      (w) => normalize(w.label).includes(q) || normalize(w.path).includes(q),
    );
  }, [recentWorkspaces, query]);

  const filteredCandidates = useMemo(() => {
    if (!query) return candidateWorkspaces;
    const q = normalize(query);
    return candidateWorkspaces.filter(
      (w) => normalize(w.label).includes(q) || normalize(w.path).includes(q),
    );
  }, [candidateWorkspaces, query]);

  const isLoading = workspacesStatus === "loading";
  const isSwitching = workspaceSwitchStatus === "loading";

  const handleSelect = async (path: string) => {
    if (isSwitching) return;
    if (path === currentWorkspacePath) {
      onClose();
      return;
    }
    setSelectedPath(path);
    setDidInitiateSwitch(true);
    try {
      await onSwitchWorkspace(path);
    } finally {
      setSelectedPath(null);
    }
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <button
        type="button"
        className="absolute inset-0 bg-black/60 backdrop-blur-sm"
        onClick={onClose}
        aria-label={t("common.close")}
      />

      <div className="relative w-full max-w-lg bg-[#0c0c0e] border border-slate-800 rounded-xl shadow-2xl flex flex-col max-h-[80vh]">
        <div className="flex items-center justify-between px-5 h-14 border-b border-slate-800 flex-shrink-0">
          <h2 className="text-base font-semibold text-slate-100">
            {t("project.openTitle")}
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="w-8 h-8 flex items-center justify-center rounded-lg text-slate-400 hover:bg-slate-800 hover:text-slate-200 transition-colors"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="px-5 pt-4 pb-2 flex-shrink-0">
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-500" />
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t("project.searchPlaceholder")}
              className="w-full bg-slate-900 border border-slate-700 rounded-lg pl-9 pr-3 py-2 text-sm text-slate-200 placeholder:text-slate-600 focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 transition-colors"
            />
          </div>
        </div>

        <div className="flex-1 overflow-y-auto px-5 pb-5 min-h-0">
          {workspacesError && (
            <div className="mt-3 flex items-start gap-2 rounded-lg bg-rose-500/10 border border-rose-500/20 p-3 text-sm text-rose-300">
              <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5" />
              {t("project.loadError", { message: workspacesError })}
            </div>
          )}

          {workspaceSwitchError && (
            <div className="mt-3 flex items-start gap-2 rounded-lg bg-rose-500/10 border border-rose-500/20 p-3 text-sm text-rose-300">
              <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5" />
              {t("project.switchError", { message: workspaceSwitchError })}
            </div>
          )}

          {isLoading && (
            <div className="mt-6 flex items-center justify-center gap-2 text-sm text-slate-500">
              <Loader2 className="w-4 h-4 animate-spin" />
              {t("project.loading")}
            </div>
          )}

          {!isLoading && (
            <>
              {filteredRecent.length > 0 && (
                <div className="mt-3">
                  <div className="flex items-center gap-1.5 text-xs font-semibold text-slate-500 uppercase tracking-wider mb-2 px-1">
                    <Clock className="w-3.5 h-3.5" />
                    {t("project.recent")}
                  </div>
                  <div className="space-y-1">
                    {filteredRecent.map((w) => (
                      <WorkspaceItem
                        key={w.path}
                        workspace={w}
                        isCurrent={w.path === currentWorkspacePath}
                        isSwitching={isSwitching && selectedPath === w.path}
                        onSelect={() => handleSelect(w.path)}
                      />
                    ))}
                  </div>
                </div>
              )}

              {filteredCandidates.length > 0 && (
                <div className="mt-4">
                  <div className="flex items-center gap-1.5 text-xs font-semibold text-slate-500 uppercase tracking-wider mb-2 px-1">
                    <FolderOpen className="w-3.5 h-3.5" />
                    {t("project.candidates")}
                  </div>
                  <div className="space-y-1">
                    {filteredCandidates.map((w) => (
                      <WorkspaceItem
                        key={w.path}
                        workspace={w}
                        isCurrent={w.path === currentWorkspacePath}
                        isSwitching={isSwitching && selectedPath === w.path}
                        onSelect={() => handleSelect(w.path)}
                      />
                    ))}
                  </div>
                </div>
              )}

              {filteredRecent.length === 0 &&
                filteredCandidates.length === 0 &&
                !workspacesError && (
                  <div className="mt-8 text-center text-sm text-slate-500">
                    {query
                      ? t("project.noSearchResults")
                      : t("project.noProjects")}
                  </div>
                )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function WorkspaceItem({
  workspace,
  isCurrent,
  isSwitching,
  onSelect,
}: {
  workspace: WorkspaceSummary;
  isCurrent: boolean;
  isSwitching: boolean;
  onSelect: () => void;
}) {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      onClick={onSelect}
      disabled={isSwitching}
      className={`w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-left transition-colors ${
        isCurrent
          ? "bg-emerald-500/10 border border-emerald-500/20 text-emerald-300"
          : "border border-transparent text-slate-300 hover:bg-slate-800/50 hover:text-slate-100"
      } disabled:opacity-60`}
    >
      <div className="flex-shrink-0">
        {isCurrent ? (
          <CheckCircle2 className="w-4 h-4 text-emerald-400" />
        ) : (
          <FolderOpen className="w-4 h-4 text-slate-500" />
        )}
      </div>
      <div className="flex-1 min-w-0">
        <div className="text-sm font-medium truncate">
          {workspace.label || workspace.path}
        </div>
        <div className="text-[11px] text-slate-500 font-mono truncate">
          {workspace.path}
        </div>
      </div>
      {isSwitching && (
        <Loader2 className="w-4 h-4 animate-spin text-indigo-400 flex-shrink-0" />
      )}
      {isCurrent && !isSwitching && (
        <span className="flex-shrink-0 text-[10px] px-1.5 py-0.5 rounded-md bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 font-medium">
          {t("project.currentBadge")}
        </span>
      )}
    </button>
  );
}
