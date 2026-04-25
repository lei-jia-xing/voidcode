import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  ChevronLeft,
  ChevronRight,
  Code2,
  FolderOpen,
  Globe,
  Plus,
  Settings,
  Server,
  Loader2,
  CheckCircle2,
  XCircle,
  GitCompare,
} from "lucide-react";
import type {
  RuntimeStatusSnapshot,
  StoredSessionSummary,
  WorkspaceRegistrySnapshot,
} from "../lib/runtime/types";
import { StatusBar } from "./StatusBar";

interface SessionSidebarProps {
  workspaces: WorkspaceRegistrySnapshot | null;
  sessions: StoredSessionSummary[];
  currentSessionId: string | null;
  sessionsStatus: string;
  sessionsError: string | null;
  isRunning: boolean;
  isReplayLoading: boolean;
  language: string;
  runtimeTestStatus: "idle" | "testing" | "success" | "error";
  onSelectSession: (sessionId: string) => void;
  onToggleLanguage: () => void;
  onOpenProjects: () => void;
  onToggleReview: () => void;
  onOpenSettings: () => void;
  onTestRuntime: () => void;
  showReview: boolean;
  statusSnapshot: RuntimeStatusSnapshot | null;
  statusStatus: "idle" | "loading" | "success" | "error";
  statusError: string | null;
  mcpRetryStatus?: "idle" | "loading" | "success" | "error";
  mcpRetryError?: string | null;
  onRetryMcp?: () => void;
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
  sessionsStatus,
  sessionsError,
  isRunning,
  isReplayLoading,
  language,
  runtimeTestStatus,
  onSelectSession,
  onToggleLanguage,
  onOpenProjects,
  onToggleReview,
  onOpenSettings,
  onTestRuntime,
  showReview,
  statusSnapshot,
  statusStatus,
  statusError,
  mcpRetryStatus,
  mcpRetryError,
  onRetryMcp,
}: SessionSidebarProps) {
  const { t } = useTranslation();
  const [isExpanded, setIsExpanded] = useState(true);

  const getSessionStatusDotClass = (status: string) => {
    if (status === "running") {
      return "bg-indigo-400 shadow-[0_0_8px_rgba(129,140,248,0.5)] animate-pulse";
    }
    if (status === "completed") return "bg-emerald-400";
    if (status === "failed") return "bg-rose-400";
    if (status === "waiting") return "bg-amber-400";
    return "bg-slate-500";
  };

  const getStatusColorClass = (status: string) => {
    if (status === "in_progress" || status === "running") {
      return "bg-indigo-500/10 text-indigo-400 border-indigo-500/20";
    }
    if (status === "completed") {
      return "bg-emerald-500/10 text-emerald-400 border-emerald-500/20";
    }
    if (status === "failed") {
      return "bg-rose-500/10 text-rose-400 border-rose-500/20";
    }
    if (status === "waiting") {
      return "bg-amber-500/10 text-amber-400 border-amber-500/20";
    }
    return "bg-slate-800 text-slate-400 border-slate-700";
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

  return (
    <aside
      className={`border-r border-slate-800 bg-[#09090b] flex flex-col justify-between flex-shrink-0 transition-[width] duration-200 ${
        isExpanded ? "w-16 md:w-64" : "w-16 md:w-16"
      }`}
    >
      <div className="flex-1 overflow-hidden flex flex-col">
        <div className="h-14 flex items-center justify-center md:justify-start md:px-4 border-b border-slate-800 text-indigo-400 font-bold tracking-tight">
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
                <div className="text-xs font-semibold text-slate-500 uppercase tracking-wider">
                  {t("nav.workspace")}
                </div>
                <button
                  type="button"
                  onClick={() => setIsExpanded((value) => !value)}
                  className="rounded-md p-1 text-slate-500 hover:bg-slate-800/60 hover:text-slate-300"
                  aria-label={t(
                    isExpanded ? "sidebar.collapse" : "sidebar.expand",
                  )}
                >
                  <ChevronLeft className="w-4 h-4" />
                </button>
              </div>

              <div className="rounded-xl border border-slate-800 bg-slate-900/50 p-3 space-y-3">
                <div className="flex items-start gap-2">
                  <div className="mt-0.5 rounded-lg bg-slate-800/80 p-2 text-slate-300">
                    <FolderOpen className="w-4 h-4" />
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="text-xs font-medium text-slate-200 truncate">
                      {currentWorkspace?.label ?? t("project.openTitle")}
                    </div>
                    <div className="text-[11px] font-mono text-slate-500 truncate">
                      {currentWorkspace?.path ?? "—"}
                    </div>
                  </div>
                </div>

                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={onOpenProjects}
                    className="flex-1 inline-flex items-center justify-center gap-1.5 rounded-lg bg-indigo-600 px-3 py-2 text-xs font-medium text-white hover:bg-indigo-500 transition-colors"
                  >
                    <Plus className="w-3.5 h-3.5" />
                    {t("project.openTitle")}
                  </button>
                  <button
                    type="button"
                    onClick={onToggleReview}
                    className={`rounded-lg border px-3 py-2 text-xs font-medium transition-colors ${
                      showReview
                        ? "border-indigo-500/30 bg-indigo-500/10 text-indigo-300"
                        : "border-slate-700 text-slate-400 hover:bg-slate-800/60 hover:text-slate-200"
                    }`}
                  >
                    <GitCompare className="w-3.5 h-3.5" />
                  </button>
                </div>

                <StatusBar
                  snapshot={statusSnapshot}
                  status={statusStatus}
                  error={statusError}
                  mcpRetryStatus={mcpRetryStatus}
                  mcpRetryError={mcpRetryError}
                  onRetryMcp={onRetryMcp}
                />
              </div>
            </div>

            <div>
              <div className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-3 px-2">
                {t("session.listHeader")}
              </div>
              <div className="space-y-1">
                <button
                  type="button"
                  onClick={() => onSelectSession("")}
                  disabled={isRunning || isReplayLoading}
                  className={`w-full flex items-center justify-start px-3 py-2 rounded-lg transition-colors gap-2 ${
                    !currentSessionId
                      ? "bg-emerald-500/10 text-emerald-400"
                      : "text-slate-400 hover:bg-slate-800/50 hover:text-slate-200"
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
                        ? "bg-indigo-500/10 border-indigo-500/30 shadow-[0_0_15px_rgba(99,102,241,0.05)] text-indigo-100"
                        : "border-transparent text-slate-400 hover:bg-slate-800/50 hover:text-slate-200"
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
                    <div className="w-full flex items-center justify-between gap-2 text-[11px] text-slate-500">
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
                <p className="mt-3 px-2 text-xs text-slate-500">
                  {t("session.loadingList")}
                </p>
              )}
              {sessionsError && (
                <p className="mt-3 px-2 text-xs text-rose-400">
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
              className="rounded-md p-2 text-slate-500 hover:bg-slate-800/60 hover:text-slate-300"
              aria-label={t("sidebar.expand")}
            >
              <ChevronRight className="w-4 h-4" />
            </button>

            <div className="w-10 h-10 rounded-xl border border-slate-800 bg-slate-900/50 flex items-center justify-center text-slate-300">
              <FolderOpen className="w-4 h-4" />
            </div>

            <button
              type="button"
              onClick={onOpenProjects}
              className="w-10 h-10 rounded-xl bg-indigo-600 flex items-center justify-center text-white hover:bg-indigo-500 transition-colors"
              aria-label={t("project.openTitle")}
            >
              <Plus className="w-4 h-4" />
            </button>
          </div>
        )}
      </div>

      <div className="p-3 border-t border-slate-800 space-y-1">
        <button
          type="button"
          onClick={onToggleLanguage}
          aria-label={language === "en" ? t("language.zh") : t("language.en")}
          className="w-full flex items-center justify-center md:justify-start md:px-3 py-2.5 rounded-lg text-slate-400 hover:bg-slate-800/50 hover:text-slate-200 transition-colors gap-2"
        >
          <Globe className="w-5 h-5" />
          {isExpanded && (
            <span className="hidden md:block font-medium text-sm">
              {language === "en" ? t("language.zh") : t("language.en")}
            </span>
          )}
        </button>
        <button
          type="button"
          onClick={onTestRuntime}
          disabled={runtimeTestStatus === "testing"}
          aria-label={t("debug.testRuntime")}
          className="w-full flex items-center justify-center md:justify-start md:px-3 py-2.5 rounded-lg text-slate-400 hover:bg-slate-800/50 hover:text-slate-200 transition-colors gap-2"
        >
          {runtimeTestStatus === "testing" ? (
            <Loader2 className="w-5 h-5 animate-spin text-indigo-400" />
          ) : runtimeTestStatus === "success" ? (
            <CheckCircle2 className="w-5 h-5 text-emerald-400" />
          ) : runtimeTestStatus === "error" ? (
            <XCircle className="w-5 h-5 text-rose-400" />
          ) : (
            <Server className="w-5 h-5" />
          )}
          {isExpanded && (
            <span className="hidden md:block font-medium text-sm">
              {runtimeTestStatus === "testing"
                ? t("debug.testing")
                : runtimeTestStatus === "success"
                  ? t("debug.success")
                  : runtimeTestStatus === "error"
                    ? t("debug.error")
                    : t("debug.testRuntime")}
            </span>
          )}
        </button>
        <button
          type="button"
          onClick={onOpenSettings}
          aria-label={t("nav.settings")}
          className="w-full flex items-center justify-center md:justify-start md:px-3 py-2.5 rounded-lg text-slate-400 hover:bg-slate-800/50 hover:text-slate-200 transition-colors gap-2"
        >
          <Settings className="w-5 h-5" />
          {isExpanded && (
            <span className="hidden md:block font-medium text-sm">
              {t("nav.settings")}
            </span>
          )}
        </button>
      </div>
    </aside>
  );
}
