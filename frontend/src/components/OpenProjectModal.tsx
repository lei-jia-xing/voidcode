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
import { ControlButton } from "./ui";

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

function normalizeWorkspaceQuery(value: string) {
  return value.toLowerCase().trim();
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
      // eslint-disable-next-line react-hooks/set-state-in-effect -- clearing own trigger signal
      setDidInitiateSwitch(false);
      onClose();
    } else if (workspaceSwitchStatus === "error") {
      setDidInitiateSwitch(false);
    }
  }, [didInitiateSwitch, workspaceSwitchStatus, onClose]);

  const filteredRecent = useMemo(() => {
    if (!query) return recentWorkspaces;
    const q = normalizeWorkspaceQuery(query);
    return recentWorkspaces.filter(
      (w) =>
        normalizeWorkspaceQuery(w.label).includes(q) ||
        normalizeWorkspaceQuery(w.path).includes(q),
    );
  }, [recentWorkspaces, query]);

  const filteredCandidates = useMemo(() => {
    if (!query) return candidateWorkspaces;
    const q = normalizeWorkspaceQuery(query);
    return candidateWorkspaces.filter(
      (w) =>
        normalizeWorkspaceQuery(w.label).includes(q) ||
        normalizeWorkspaceQuery(w.path).includes(q),
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
        className="absolute inset-0 bg-[var(--vc-overlay-bg)] backdrop-blur-sm"
        onClick={onClose}
        aria-label={t("common.close")}
      />

      <div className="relative w-full max-w-lg bg-[var(--vc-bg)] border border-[color:var(--vc-border-subtle)] rounded-xl shadow-2xl flex flex-col max-h-[80vh]">
        <div className="flex items-center justify-between px-5 h-14 border-b border-[color:var(--vc-border-subtle)] flex-shrink-0">
          <h2 className="text-base font-semibold text-[var(--vc-text-primary)]">
            {t("project.openTitle")}
          </h2>
          <ControlButton
            compact
            icon
            variant="ghost"
            onClick={onClose}
            aria-label={t("common.close")}
          >
            <X className="w-5 h-5" />
          </ControlButton>
        </div>

        <div className="px-5 pt-4 pb-2 flex-shrink-0">
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-[var(--vc-text-subtle)]" />
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t("project.searchPlaceholder")}
              className="w-full bg-[var(--vc-surface-1)] border border-[color:var(--vc-border-subtle)] rounded-lg pl-9 pr-3 py-2 text-sm text-[var(--vc-text-primary)] placeholder:text-[var(--vc-text-subtle)] focus:outline-none focus:border-[color:var(--vc-border-strong)] focus:ring-1 focus:ring-[color:var(--vc-border-strong)] transition-colors"
            />
          </div>
        </div>

        <div className="flex-1 overflow-y-auto px-5 pb-5 min-h-0">
          {workspacesError && (
            <div className="mt-3 flex items-start gap-2 rounded-lg bg-[var(--vc-danger-bg)] border border-[color:var(--vc-danger-border)] p-3 text-sm text-[var(--vc-danger-text)]">
              <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5" />
              {t("project.loadError", { message: workspacesError })}
            </div>
          )}

          {workspaceSwitchError && (
            <div className="mt-3 flex items-start gap-2 rounded-lg bg-[var(--vc-danger-bg)] border border-[color:var(--vc-danger-border)] p-3 text-sm text-[var(--vc-danger-text)]">
              <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5" />
              {t("project.switchError", { message: workspaceSwitchError })}
            </div>
          )}

          {isLoading && (
            <div className="mt-6 flex items-center justify-center gap-2 text-sm text-[var(--vc-text-subtle)]">
              <Loader2 className="w-4 h-4 animate-spin" />
              {t("project.loading")}
            </div>
          )}

          {!isLoading && (
            <>
              {filteredRecent.length > 0 && (
                <div className="mt-3">
                  <div className="flex items-center gap-1.5 text-xs font-semibold text-[var(--vc-text-subtle)] uppercase tracking-wider mb-2 px-1">
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
                  <div className="flex items-center gap-1.5 text-xs font-semibold text-[var(--vc-text-subtle)] uppercase tracking-wider mb-2 px-1">
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
                  <div className="mt-8 text-center text-sm text-[var(--vc-text-subtle)]">
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
          ? "border border-[color:var(--vc-border-strong)] bg-[var(--vc-surface-2)] text-[var(--vc-text-primary)]"
          : "border border-transparent text-[var(--vc-text-muted)] hover:bg-[var(--vc-surface-1)] hover:text-[var(--vc-text-primary)]"
      } disabled:opacity-60`}
    >
      <div className="flex-shrink-0">
        {isCurrent ? (
          <CheckCircle2 className="w-4 h-4 text-[var(--vc-confirm-text)]" />
        ) : (
          <FolderOpen className="w-4 h-4 text-[var(--vc-text-subtle)]" />
        )}
      </div>
      <div className="flex-1 min-w-0">
        <div className="text-sm font-medium truncate">
          {workspace.label || workspace.path}
        </div>
        <div className="text-[11px] text-[var(--vc-text-subtle)] font-mono truncate">
          {workspace.path}
        </div>
      </div>
      {isSwitching && (
        <Loader2 className="w-4 h-4 animate-spin text-[var(--vc-text-muted)] flex-shrink-0" />
      )}
      {isCurrent && !isSwitching && (
        <span className="flex-shrink-0 text-[10px] px-1.5 py-0.5 rounded-md bg-[var(--vc-confirm-bg)] text-[var(--vc-confirm-text)] border border-[color:var(--vc-confirm-border)] font-medium">
          {t("project.currentBadge")}
        </span>
      )}
    </button>
  );
}
