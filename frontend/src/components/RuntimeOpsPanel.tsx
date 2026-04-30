import { useTranslation } from "react-i18next";
import { Bell, Loader2, X } from "lucide-react";
import type {
  BackgroundTaskSummary,
  RuntimeNotification,
  RuntimeSessionDebugSnapshot,
} from "../lib/runtime/types";
import { ControlButton } from "./ui";

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
  return <div className="text-xs text-[var(--vc-danger-text)]">{message}</div>;
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
    <aside className="w-[22rem] border-l border-[color:var(--vc-border-subtle)] bg-[var(--vc-bg)] flex-shrink-0 flex flex-col min-w-0">
      <div className="h-14 border-b border-[color:var(--vc-border-subtle)] px-4 flex items-center justify-between">
        <div>
          <div className="text-sm font-medium text-[var(--vc-text-primary)]">
            {t("runtimeOps.title")}
          </div>
          <div className="text-[11px] text-[var(--vc-text-subtle)] truncate">
            {currentSessionId ?? t("runtimeOps.noSession")}
          </div>
        </div>
        <ControlButton
          compact
          icon
          variant="ghost"
          onClick={onClose}
          aria-label={t("runtimeOps.close")}
        >
          <X className="h-4 w-4" />
        </ControlButton>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 space-y-5">
        <section className="space-y-2">
          <div className="flex items-center justify-between">
            <h2 className="text-xs font-semibold uppercase tracking-wide text-[var(--vc-text-muted)]">
              {t("runtimeOps.debug")}
            </h2>
            <ControlButton
              compact
              variant="ghost"
              onClick={onRefreshDebug}
              disabled={!currentSessionId || debugStatus === "loading"}
            >
              {debugStatus === "loading"
                ? t("runtimeOps.loading")
                : t("runtimeOps.refresh")}
            </ControlButton>
          </div>
          {debugStatus === "loading" && (
            <div className="flex items-center gap-2 text-xs text-[var(--vc-text-subtle)]">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              {t("runtimeOps.loading")}
            </div>
          )}
          <ErrorLine message={debugError} />
          {debugSnapshot && (
            <div className="rounded-lg border border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] p-3 text-xs text-[var(--vc-text-muted)] space-y-1">
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
                <div className="text-[var(--vc-danger-text)]">
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
            <h2 className="text-xs font-semibold uppercase tracking-wide text-[var(--vc-text-muted)]">
              {t("runtimeOps.notifications")}
            </h2>
            <ControlButton
              compact
              variant="ghost"
              onClick={onRefreshNotifications}
            >
              {notificationsStatus === "loading"
                ? t("runtimeOps.loading")
                : t("runtimeOps.refresh")}
            </ControlButton>
          </div>
          <ErrorLine message={notificationsError} />
          {notifications.length === 0 && notificationsStatus !== "loading" && (
            <div className="text-xs text-[var(--vc-text-subtle)]">
              {t("runtimeOps.noNotifications")}
            </div>
          )}
          {notifications.map((notification) => (
            <div
              key={notification.id}
              className="rounded-lg border border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] p-3 text-xs text-[var(--vc-text-muted)]"
            >
              <div className="flex items-start gap-2">
                <Bell className="h-3.5 w-3.5 text-[var(--vc-text-subtle)] mt-0.5" />
                <div className="min-w-0 flex-1">
                  <div className="font-medium text-[var(--vc-text-primary)] truncate">
                    {notification.summary}
                  </div>
                  <div className="mt-1 text-[var(--vc-text-subtle)]">
                    {notification.kind} · {notification.status}
                  </div>
                </div>
              </div>
              {notification.status === "unread" && (
                <ControlButton
                  compact
                  variant="ghost"
                  onClick={() => onAcknowledgeNotification(notification.id)}
                  className="mt-2"
                >
                  {t("runtimeOps.acknowledge")}
                </ControlButton>
              )}
            </div>
          ))}
        </section>

        <section className="space-y-2">
          <div className="flex items-center justify-between">
            <h2 className="text-xs font-semibold uppercase tracking-wide text-[var(--vc-text-muted)]">
              {t("runtimeOps.backgroundTasks")}
            </h2>
            <ControlButton compact variant="ghost" onClick={onRefreshTasks}>
              {backgroundTasksStatus === "loading"
                ? t("runtimeOps.loading")
                : t("runtimeOps.refresh")}
            </ControlButton>
          </div>
          <ErrorLine message={backgroundTasksError} />
          {backgroundTasks.length === 0 &&
            backgroundTasksStatus !== "loading" && (
              <div className="text-xs text-[var(--vc-text-subtle)]">
                {t("runtimeOps.noTasks")}
              </div>
            )}
          {backgroundTasks.map((task) => (
            <div
              key={task.task.id}
              className="rounded-lg border border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] p-3 text-xs text-[var(--vc-text-muted)]"
            >
              <div className="font-mono text-[11px] text-[var(--vc-text-subtle)] truncate">
                {task.task.id}
              </div>
              <div className="mt-1 font-medium text-[var(--vc-text-primary)] truncate">
                {task.prompt}
              </div>
              <div className="mt-1 text-[var(--vc-text-subtle)]">
                {task.status}
              </div>
              {task.status === "queued" || task.status === "running" ? (
                <ControlButton
                  compact
                  variant="danger"
                  onClick={() => onCancelTask(task.task.id)}
                  className="mt-2"
                >
                  {t("runtimeOps.cancelTask")}
                </ControlButton>
              ) : null}
            </div>
          ))}
        </section>
      </div>
    </aside>
  );
}
