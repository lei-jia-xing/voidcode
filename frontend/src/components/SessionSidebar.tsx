import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import {
  Code2,
  Globe,
  Settings,
  Server,
  Loader2,
  CheckCircle2,
  XCircle,
  Plus,
} from "lucide-react";
import { StoredSessionSummary } from "../lib/runtime/types";

interface SessionSidebarProps {
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
  onOpenSettings: () => void;
  onTestRuntime: () => void;
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
  onOpenSettings,
  onTestRuntime,
}: SessionSidebarProps) {
  const { t } = useTranslation();

  const getSessionStatusDotClass = (status: string) => {
    if (status === "running")
      return "bg-indigo-400 shadow-[0_0_8px_rgba(129,140,248,0.5)] animate-pulse";
    if (status === "completed") return "bg-emerald-400";
    if (status === "failed") return "bg-rose-400";
    if (status === "waiting") return "bg-amber-400";
    return "bg-slate-500";
  };

  const getStatusColorClass = (status: string) => {
    if (status === "in_progress" || status === "running")
      return "bg-indigo-500/10 text-indigo-400 border-indigo-500/20";
    if (status === "completed")
      return "bg-emerald-500/10 text-emerald-400 border-emerald-500/20";
    if (status === "failed")
      return "bg-rose-500/10 text-rose-400 border-rose-500/20";
    if (status === "waiting")
      return "bg-amber-500/10 text-amber-400 border-amber-500/20";
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

  const sortedSessions = useMemo(() => {
    return [...sessions].sort((a, b) => b.updated_at - a.updated_at);
  }, [sessions]);

  return (
    <aside className="w-16 md:w-64 border-r border-slate-800 bg-[#09090b] flex flex-col justify-between flex-shrink-0">
      <div className="flex-1 overflow-hidden flex flex-col">
        <div className="h-14 flex items-center justify-center md:justify-start md:px-4 border-b border-slate-800 text-indigo-400 font-bold tracking-tight">
          <Code2 className="w-6 h-6 md:mr-3" />
          <span className="hidden md:block text-lg">{t("app.title")}</span>
        </div>

        <div className="p-3 flex-1 overflow-y-auto hidden md:block">
          <div className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-3 px-2">
            {t("session.listHeader")}
          </div>
          <div className="space-y-1">
            <button
              type="button"
              onClick={() => onSelectSession("")}
              disabled={isRunning || isReplayLoading}
              className={`w-full flex items-center justify-start px-3 py-2 rounded-lg transition-colors gap-2 ${!currentSessionId ? "bg-emerald-500/10 text-emerald-400" : "text-slate-400 hover:bg-slate-800/50 hover:text-slate-200"}`}
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
                  <span className="flex-shrink-0 font-medium">T{s.turn}</span>
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

      <div className="p-3 border-t border-slate-800 space-y-1">
        <button
          type="button"
          onClick={onToggleLanguage}
          className="w-full flex items-center justify-center md:justify-start md:px-3 py-2.5 rounded-lg text-slate-400 hover:bg-slate-800/50 hover:text-slate-200 transition-colors gap-2"
        >
          <Globe className="w-5 h-5" />
          <span className="hidden md:block font-medium text-sm">
            {language === "en" ? t("language.zh") : t("language.en")}
          </span>
        </button>
        <button
          type="button"
          onClick={onTestRuntime}
          disabled={runtimeTestStatus === "testing"}
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
          <span className="hidden md:block font-medium text-sm">
            {runtimeTestStatus === "testing"
              ? t("debug.testing")
              : runtimeTestStatus === "success"
                ? t("debug.success")
                : runtimeTestStatus === "error"
                  ? t("debug.error")
                  : t("debug.testRuntime")}
          </span>
        </button>
        <button
          type="button"
          onClick={onOpenSettings}
          className="w-full flex items-center justify-center md:justify-start md:px-3 py-2.5 rounded-lg text-slate-400 hover:bg-slate-800/50 hover:text-slate-200 transition-colors gap-2"
        >
          <Settings className="w-5 h-5" />
          <span className="hidden md:block font-medium text-sm">
            {t("nav.settings")}
          </span>
        </button>
      </div>
    </aside>
  );
}
