import { useTranslation } from "react-i18next";
import { Bell, Loader2, X } from "lucide-react";
import type {
  BackgroundTaskSummary,
  RuntimeNotification,
  RuntimeSessionDebugSnapshot,
} from "../lib/runtime/types";

interface RuntimeOpsPanelProps {
  isOpen: boolean;
  currentSessionId: string | null;
  debugSnapshot: RuntimeSessionDebugSnapshot | null;
  debugStatus: "idle" | "loading" | "success" | "error";
  debugError: string | null;
  notifications: RuntimeNotification[];
  notificationsStatus: "idle" | "loading" | "success" | "error";
  notificationsError: string | null;
  backgroundTasks: BackgroundTaskSummary[];
  backgroundTasksStatus: "idle" | "loading" | "success" | "error";
  backgroundTasksError: string | null;
  onClose: () => void;
  onRefreshNotifications: () => void;
  onAcknowledgeNotification: (notificationId: string) => void;
  onRefreshTasks: () => void;
  onCancelTask: (taskId: string) => void;
  onRefreshDebug: () => void;
}

function ErrorLine({ message }: { message: string | null }) {
  if (!message) return null;
  return <div className="text-xs text-rose-300">{message}</div>;
}

export function RuntimeOpsPanel({
  isOpen,
  currentSessionId,
  debugSnapshot,
  debugStatus,
  debugError,
  notifications,
  notificationsStatus,
  notificationsError,
  backgroundTasks,
  backgroundTasksStatus,
  backgroundTasksError,
  onClose,
  onRefreshNotifications,
  onAcknowledgeNotification,
  onRefreshTasks,
  onCancelTask,
  onRefreshDebug,
}: RuntimeOpsPanelProps) {
  const { t } = useTranslation();

  if (!isOpen) return null;

  return (
    <aside className="w-[22rem] border-l border-slate-800 bg-[#0c0c0e] flex-shrink-0 flex flex-col min-w-0">
      <div className="h-14 border-b border-slate-800 px-4 flex items-center justify-between">
        <div>
          <div className="text-sm font-medium text-slate-200">
            {t("runtimeOps.title")}
          </div>
          <div className="text-[11px] text-slate-500 truncate">
            {currentSessionId ?? t("runtimeOps.noSession")}
          </div>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="rounded-md p-1.5 text-slate-500 hover:bg-slate-800/60 hover:text-slate-300"
          aria-label={t("runtimeOps.close")}
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 space-y-5">
        <section className="space-y-2">
          <div className="flex items-center justify-between">
            <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-400">
              {t("runtimeOps.debug")}
            </h2>
            <button
              type="button"
              onClick={onRefreshDebug}
              disabled={!currentSessionId || debugStatus === "loading"}
              className="text-xs text-slate-500 hover:text-slate-300 disabled:opacity-40"
            >
              {debugStatus === "loading"
                ? t("runtimeOps.loading")
                : t("runtimeOps.refresh")}
            </button>
          </div>
          {debugStatus === "loading" && (
            <div className="flex items-center gap-2 text-xs text-slate-500">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              {t("runtimeOps.loading")}
            </div>
          )}
          <ErrorLine message={debugError} />
          {debugSnapshot && (
            <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-3 text-xs text-slate-400 space-y-1">
              <div>
                {t("runtimeOps.status")}: {debugSnapshot.current_status}
              </div>
              {debugSnapshot.pending_question && (
                <div>
                  {t("runtimeOps.pendingQuestion")}:{" "}
                  {debugSnapshot.pending_question.question_count}
                </div>
              )}
              {debugSnapshot.failure && (
                <div className="text-rose-300">
                  {debugSnapshot.failure.classification}:{" "}
                  {debugSnapshot.failure.message}
                </div>
              )}
              {debugSnapshot.operator_guidance && (
                <div>{debugSnapshot.operator_guidance}</div>
              )}
            </div>
          )}
        </section>

        <section className="space-y-2">
          <div className="flex items-center justify-between">
            <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-400">
              {t("runtimeOps.notifications")}
            </h2>
            <button
              type="button"
              onClick={onRefreshNotifications}
              className="text-xs text-slate-500 hover:text-slate-300"
            >
              {notificationsStatus === "loading"
                ? t("runtimeOps.loading")
                : t("runtimeOps.refresh")}
            </button>
          </div>
          <ErrorLine message={notificationsError} />
          {notifications.length === 0 && notificationsStatus !== "loading" && (
            <div className="text-xs text-slate-500">
              {t("runtimeOps.noNotifications")}
            </div>
          )}
          {notifications.map((notification) => (
            <div
              key={notification.id}
              className="rounded-lg border border-slate-800 bg-slate-950/60 p-3 text-xs text-slate-400"
            >
              <div className="flex items-start gap-2">
                <Bell className="h-3.5 w-3.5 text-sky-300 mt-0.5" />
                <div className="min-w-0 flex-1">
                  <div className="font-medium text-slate-200 truncate">
                    {notification.summary}
                  </div>
                  <div className="mt-1 text-slate-500">
                    {notification.kind} · {notification.status}
                  </div>
                </div>
              </div>
              {notification.status === "unread" && (
                <button
                  type="button"
                  onClick={() => onAcknowledgeNotification(notification.id)}
                  className="mt-2 text-xs text-sky-300 hover:text-sky-200"
                >
                  {t("runtimeOps.acknowledge")}
                </button>
              )}
            </div>
          ))}
        </section>

        <section className="space-y-2">
          <div className="flex items-center justify-between">
            <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-400">
              {t("runtimeOps.backgroundTasks")}
            </h2>
            <button
              type="button"
              onClick={onRefreshTasks}
              className="text-xs text-slate-500 hover:text-slate-300"
            >
              {backgroundTasksStatus === "loading"
                ? t("runtimeOps.loading")
                : t("runtimeOps.refresh")}
            </button>
          </div>
          <ErrorLine message={backgroundTasksError} />
          {backgroundTasks.length === 0 &&
            backgroundTasksStatus !== "loading" && (
              <div className="text-xs text-slate-500">
                {t("runtimeOps.noTasks")}
              </div>
            )}
          {backgroundTasks.map((task) => (
            <div
              key={task.task.id}
              className="rounded-lg border border-slate-800 bg-slate-950/60 p-3 text-xs text-slate-400"
            >
              <div className="font-mono text-[11px] text-slate-500 truncate">
                {task.task.id}
              </div>
              <div className="mt-1 font-medium text-slate-200 truncate">
                {task.prompt}
              </div>
              <div className="mt-1 text-slate-500">{task.status}</div>
              {task.status === "queued" || task.status === "running" ? (
                <button
                  type="button"
                  onClick={() => onCancelTask(task.task.id)}
                  className="mt-2 text-xs text-rose-300 hover:text-rose-200"
                >
                  {t("runtimeOps.cancelTask")}
                </button>
              ) : null}
            </div>
          ))}
        </section>
      </div>
    </aside>
  );
}
