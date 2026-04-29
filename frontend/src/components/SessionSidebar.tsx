import {
  type CSSProperties,
  type KeyboardEvent,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import { useTranslation } from "react-i18next";
import {
  ChevronLeft,
  ChevronRight,
  Code2,
  FolderOpen,
  Plus,
  Settings,
} from "lucide-react";
import type {
  StoredSessionSummary,
  WorkspaceRegistrySnapshot,
} from "../lib/runtime/types";

export interface SessionSidebarProps {
  workspaces: WorkspaceRegistrySnapshot | null;
  sessions: StoredSessionSummary[];
  currentSessionId: string | null;
  sidebarWidth: number;
  sessionsStatus: string;
  sessionsError: string | null;
  isRunning: boolean;
  isReplayLoading: boolean;
  onSidebarWidthChange: (width: number) => void;
  onSelectSession: (sessionId: string) => void;
  onOpenProjects: () => void;
  onOpenSettings: () => void;
}

const DEFAULT_SESSION_SIDEBAR_WIDTH = 344;
const MIN_SESSION_SIDEBAR_WIDTH = 244;
const MAX_SESSION_SIDEBAR_WIDTH = 520;
const SIDEBAR_KEYBOARD_RESIZE_STEP = 16;

function getViewportWidth(): number {
  return typeof window === "undefined" ? 1024 : window.innerWidth;
}

function getMaxSessionSidebarWidth(viewportWidth = getViewportWidth()): number {
  return Math.min(MAX_SESSION_SIDEBAR_WIDTH, viewportWidth * 0.3 + 64);
}

function clampSessionSidebarWidth(
  width: unknown,
  viewportWidth = getViewportWidth(),
): number {
  const numericWidth = typeof width === "number" ? width : Number(width);
  const safeWidth = Number.isFinite(numericWidth)
    ? numericWidth
    : DEFAULT_SESSION_SIDEBAR_WIDTH;
  const maxWidth = Math.max(
    MIN_SESSION_SIDEBAR_WIDTH,
    getMaxSessionSidebarWidth(viewportWidth),
  );

  return Math.min(maxWidth, Math.max(MIN_SESSION_SIDEBAR_WIDTH, safeWidth));
}

function formatSessionUpdatedAt(updatedAt: number, now = Date.now()): string {
  const timestamp =
    updatedAt < 1_000_000_000_000 ? updatedAt * 1000 : updatedAt;
  const diffMs = Math.max(0, now - timestamp);
  const diffMinutes = Math.floor(diffMs / 60_000);
  const diffHours = Math.floor(diffMs / 3_600_000);
  const diffDays = Math.floor(diffMs / 86_400_000);

  if (diffMinutes < 1) {
    return "just-now";
  }
  if (diffMinutes < 60) {
    return `minutes:${diffMinutes}`;
  }
  if (diffHours < 24) {
    return `hours:${diffHours}`;
  }
  return `days:${Math.max(1, diffDays)}`;
}

export function SessionSidebar({
  workspaces,
  sessions,
  currentSessionId,
  sidebarWidth,
  sessionsStatus,
  sessionsError,
  isRunning,
  isReplayLoading,
  onSidebarWidthChange,
  onSelectSession,
  onOpenProjects,
  onOpenSettings,
}: SessionSidebarProps) {
  const { t } = useTranslation();
  const [isExpanded, setIsExpanded] = useState(true);
  const [isResizing, setIsResizing] = useState(false);
  const [viewportWidth, setViewportWidth] = useState(getViewportWidth);

  const getSessionStatusDotClass = (status: string) => {
    if (status === "running") {
      return "bg-[var(--vc-text-primary)] animate-pulse";
    }
    if (status === "completed") return "bg-[var(--vc-text-muted)]";
    if (status === "failed") return "bg-[var(--vc-text-subtle)]";
    if (status === "waiting") return "bg-[var(--vc-text-subtle)]";
    return "bg-[var(--vc-border-strong)]";
  };

  const getStatusColorClass = (status: string) => {
    if (status === "in_progress" || status === "running") {
      return "bg-[var(--vc-surface-2)] text-[var(--vc-text-primary)] border-[color:var(--vc-border-strong)]";
    }
    if (status === "completed") {
      return "bg-[var(--vc-surface-1)] text-[var(--vc-text-muted)] border-[color:var(--vc-border-subtle)]";
    }
    if (status === "failed") {
      return "bg-[var(--vc-surface-1)] text-[var(--vc-text-muted)] border-[color:var(--vc-border-subtle)]";
    }
    if (status === "waiting") {
      return "bg-[var(--vc-surface-1)] text-[var(--vc-text-muted)] border-[color:var(--vc-border-subtle)]";
    }
    return "bg-[var(--vc-surface-1)] text-[var(--vc-text-subtle)] border-[color:var(--vc-border-subtle)]";
  };

  const formatSessionUpdatedLabel = (updatedAt: number) => {
    const token = formatSessionUpdatedAt(updatedAt);
    if (token === "just-now") {
      return t("session.updatedAtJustNow");
    }
    const [unit, value] = token.split(":");
    if (unit === "minutes") {
      return t("session.updatedAtMinutesAgo", { count: Number(value) });
    }
    if (unit === "hours") {
      return t("session.updatedAtHoursAgo", { count: Number(value) });
    }
    return t("session.updatedAtDaysAgo", { count: Number(value) });
  };

  const sortedSessions = useMemo(
    () => [...sessions].sort((a, b) => b.updated_at - a.updated_at),
    [sessions],
  );

  const currentWorkspace = workspaces?.current ?? null;
  const maxSidebarWidth = getMaxSessionSidebarWidth(viewportWidth);
  const clampedSidebarWidth = clampSessionSidebarWidth(
    sidebarWidth,
    viewportWidth,
  );
  const sidebarStyle = {
    "--session-sidebar-width": `${clampedSidebarWidth}px`,
  } as CSSProperties;

  const resizeToWidth = useCallback(
    (width: number) => {
      onSidebarWidthChange(clampSessionSidebarWidth(width, viewportWidth));
    },
    [onSidebarWidthChange, viewportWidth],
  );

  useEffect(() => {
    const handleResize = () => setViewportWidth(getViewportWidth());
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, []);

  useEffect(() => {
    if (sidebarWidth !== clampedSidebarWidth) {
      onSidebarWidthChange(clampedSidebarWidth);
    }
  }, [clampedSidebarWidth, onSidebarWidthChange, sidebarWidth]);

  useEffect(() => {
    if (!isResizing) return;

    const handlePointerMove = (event: PointerEvent) => {
      event.preventDefault();
      resizeToWidth(event.clientX);
    };
    const stopResizing = () => setIsResizing(false);

    const previousCursor = document.body.style.cursor;
    const previousUserSelect = document.body.style.userSelect;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("pointermove", handlePointerMove, {
      passive: false,
    });
    window.addEventListener("pointerup", stopResizing);
    window.addEventListener("pointercancel", stopResizing);

    return () => {
      document.body.style.cursor = previousCursor;
      document.body.style.userSelect = previousUserSelect;
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", stopResizing);
      window.removeEventListener("pointercancel", stopResizing);
    };
  }, [isResizing, resizeToWidth]);

  const handleResizeKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "ArrowRight") {
      event.preventDefault();
      resizeToWidth(clampedSidebarWidth + SIDEBAR_KEYBOARD_RESIZE_STEP);
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      resizeToWidth(clampedSidebarWidth - SIDEBAR_KEYBOARD_RESIZE_STEP);
    } else if (event.key === "Home") {
      event.preventDefault();
      resizeToWidth(MIN_SESSION_SIDEBAR_WIDTH);
    } else if (event.key === "End") {
      event.preventDefault();
      resizeToWidth(maxSidebarWidth);
    }
  };

  return (
    <aside
      className={`relative border-r border-[var(--vc-border-subtle)] bg-[var(--vc-bg)] flex flex-col justify-between flex-shrink-0 transition-[width] duration-200 ${
        isExpanded ? "w-16 md:w-[var(--session-sidebar-width)]" : "w-16 md:w-16"
      }`}
      style={sidebarStyle}
    >
      {isExpanded && (
        <hr
          tabIndex={0}
          aria-label={t("sidebar.resize")}
          aria-orientation="vertical"
          aria-valuemin={MIN_SESSION_SIDEBAR_WIDTH}
          aria-valuemax={Math.round(maxSidebarWidth)}
          aria-valuenow={Math.round(clampedSidebarWidth)}
          className={`absolute inset-y-0 right-0 z-20 hidden w-[var(--vc-space-2)] translate-x-1/2 cursor-col-resize touch-none transition-colors duration-150 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-[var(--vc-focus-ring)] md:block ${
            isResizing
              ? "bg-[var(--vc-border-strong)]"
              : "bg-transparent hover:bg-[var(--vc-border-strong)]"
          }`}
          onKeyDown={handleResizeKeyDown}
          onPointerDown={(event) => {
            event.preventDefault();
            setIsResizing(true);
            resizeToWidth(event.clientX);
          }}
        />
      )}
      <div className="flex-1 overflow-hidden flex flex-col">
        <div className="h-14 flex items-center justify-center md:justify-start md:px-4 border-b border-[color:var(--vc-border-subtle)] text-[var(--vc-text-primary)] font-bold tracking-tight">
          <Code2 className="w-6 h-6 md:mr-3" />
          {isExpanded && (
            <span className="hidden md:block text-lg">{t("app.title")}</span>
          )}
        </div>

        <div
          className={`p-3 flex-1 overflow-y-auto ${isExpanded ? "hidden md:block" : "hidden"}`}
        >
          <div className="space-y-3">
            <div className="space-y-2">
              <div className="flex items-center justify-between px-2">
                <div className="text-xs font-semibold text-[var(--vc-text-subtle)] uppercase tracking-wider">
                  {t("nav.workspace")}
                </div>
                <button
                  type="button"
                  onClick={() => setIsExpanded((value) => !value)}
                  className="rounded-md p-1 text-[var(--vc-text-subtle)] hover:bg-[var(--vc-surface-1)] hover:text-[var(--vc-text-primary)]"
                  aria-label={t(
                    isExpanded ? "sidebar.collapse" : "sidebar.expand",
                  )}
                >
                  <ChevronLeft className="w-4 h-4" />
                </button>
              </div>

              <div className="rounded-xl border border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] p-3 space-y-3">
                <div className="flex items-start gap-2">
                  <div className="mt-0.5 rounded-lg bg-[var(--vc-surface-2)] p-2 text-[var(--vc-text-muted)]">
                    <FolderOpen className="w-4 h-4" />
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="text-xs font-medium text-[var(--vc-text-primary)] truncate">
                      {currentWorkspace?.label ?? t("project.openTitle")}
                    </div>
                    <div className="text-[11px] font-mono text-[var(--vc-text-subtle)] truncate">
                      {currentWorkspace?.path ?? "—"}
                    </div>
                  </div>
                </div>

                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={onOpenProjects}
                    className="flex-1 inline-flex items-center justify-center gap-1.5 rounded-lg border border-[color:var(--vc-border-strong)] bg-[var(--vc-text-primary)] px-3 py-2 text-xs font-medium text-[var(--vc-bg)] transition-opacity hover:opacity-90"
                  >
                    <Plus className="w-3.5 h-3.5" />
                    {t("project.openTitle")}
                  </button>
                </div>
              </div>
            </div>

            <div>
              <div className="text-xs font-semibold text-[var(--vc-text-subtle)] uppercase tracking-wider mb-3 px-2">
                {t("session.listHeader")}
              </div>
              <div className="space-y-1">
                <button
                  type="button"
                  onClick={() => onSelectSession("")}
                  disabled={isRunning || isReplayLoading}
                  className={`w-full flex items-center justify-start px-3 py-2 rounded-lg border transition-colors gap-2 ${
                    !currentSessionId
                      ? "border-[color:var(--vc-border-strong)] bg-[var(--vc-surface-2)] text-[var(--vc-text-primary)]"
                      : "border-transparent text-[var(--vc-text-muted)] hover:bg-[var(--vc-surface-1)] hover:text-[var(--vc-text-primary)]"
                  }`}
                >
                  <Plus className="w-4 h-4" />
                  <span className="font-medium text-sm">
                    {t("session.newSession")}
                  </span>
                </button>
                {sortedSessions.map((s) => (
                  <button
                    key={s.session.id}
                    type="button"
                    onClick={() => onSelectSession(s.session.id)}
                    disabled={isRunning || isReplayLoading}
                    className={`w-full flex flex-col items-start justify-center px-3 py-2.5 rounded-lg transition-colors overflow-hidden border ${
                      currentSessionId === s.session.id
                        ? "bg-[var(--vc-surface-2)] border-[color:var(--vc-border-strong)] text-[var(--vc-text-primary)]"
                        : "border-transparent text-[var(--vc-text-muted)] hover:bg-[var(--vc-surface-1)] hover:text-[var(--vc-text-primary)]"
                    }`}
                    title={s.prompt || s.session.id}
                  >
                    <div className="w-full flex items-center justify-between gap-2 mb-1">
                      <div className="flex items-center truncate max-w-[75%]">
                        <div
                          className={`w-1.5 h-1.5 rounded-full flex-shrink-0 mr-2 ${getSessionStatusDotClass(s.status)}`}
                        />
                        <span className="font-medium text-sm truncate">
                          {s.prompt || (
                            <span className="font-mono">
                              {s.session.id.substring(0, 8)}
                            </span>
                          )}
                        </span>
                      </div>
                      <span
                        className={`flex-shrink-0 text-[10px] px-1.5 py-0.5 rounded-md font-medium border ${getStatusColorClass(s.status)}`}
                      >
                        {s.status === "running"
                          ? t("session.agentBusy")
                          : t(`task.status.${s.status}`)}
                      </span>
                    </div>
                    <div className="w-full flex items-center justify-between gap-2 text-[11px] text-[var(--vc-text-subtle)]">
                      <div className="min-w-0 flex flex-col items-start">
                        <span
                          className="font-mono truncate max-w-full"
                          title={s.session.id}
                        >
                          {s.session.id.substring(0, 8)}
                        </span>
                        <span className="truncate max-w-full">
                          {formatSessionUpdatedLabel(s.updated_at)}
                        </span>
                      </div>
                      <span className="flex-shrink-0 font-medium">
                        T{s.turn}
                      </span>
                    </div>
                  </button>
                ))}
              </div>
              {sessionsStatus === "loading" && (
                <p className="mt-3 px-2 text-xs text-[var(--vc-text-subtle)]">
                  {t("session.loadingList")}
                </p>
              )}
              {sessionsError && (
                <p className="mt-3 px-2 text-xs text-[var(--vc-danger-text)]">
                  {t("session.loadError", { message: sessionsError })}
                </p>
              )}
            </div>
          </div>
        </div>

        {!isExpanded && (
          <div className="p-3 space-y-3 hidden md:flex md:flex-col md:items-center">
            <button
              type="button"
              onClick={() => setIsExpanded((value) => !value)}
              className="rounded-md p-2 text-[var(--vc-text-subtle)] hover:bg-[var(--vc-surface-1)] hover:text-[var(--vc-text-primary)]"
              aria-label={t("sidebar.expand")}
            >
              <ChevronRight className="w-4 h-4" />
            </button>

            <div className="w-10 h-10 rounded-xl border border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] flex items-center justify-center text-[var(--vc-text-muted)]">
              <FolderOpen className="w-4 h-4" />
            </div>

            <button
              type="button"
              onClick={onOpenProjects}
              className="w-10 h-10 rounded-xl border border-[color:var(--vc-border-strong)] bg-[var(--vc-text-primary)] flex items-center justify-center text-[var(--vc-bg)] transition-opacity hover:opacity-90"
              aria-label={t("project.openTitle")}
            >
              <Plus className="w-4 h-4" />
            </button>

            <button
              type="button"
              onClick={onOpenSettings}
              className="w-10 h-10 rounded-xl border border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] flex items-center justify-center text-[var(--vc-text-muted)] hover:bg-[var(--vc-surface-2)] hover:text-[var(--vc-text-primary)] transition-colors"
              aria-label={t("nav.settings")}
            >
              <Settings className="w-4 h-4" />
            </button>
          </div>
        )}
      </div>

      {isExpanded && (
        <div className="hidden border-t border-[color:var(--vc-border-subtle)] p-3 md:block">
          <button
            type="button"
            onClick={onOpenSettings}
            className="w-full flex items-center gap-2 rounded-lg px-3 py-2.5 text-sm text-[var(--vc-text-muted)] transition-colors hover:bg-[var(--vc-surface-1)] hover:text-[var(--vc-text-primary)]"
          >
            <Settings className="w-4 h-4" />
            <span>{t("nav.settings")}</span>
          </button>
        </div>
      )}
    </aside>
  );
}
