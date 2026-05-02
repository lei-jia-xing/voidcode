import { Loader2, RefreshCw, SplitSquareHorizontal } from "lucide-react";
import { useTranslation } from "react-i18next";
import type {
  AsyncStatus,
  BackgroundTaskOutput,
  BackgroundTaskSummary,
} from "../lib/runtime/types";
import { ControlButton } from "./ui";

interface ChildSessionSidebarProps {
  parentSessionId: string | null;
  tasks: BackgroundTaskSummary[];
  status: AsyncStatus;
  error: string | null;
  selectedTaskId: string | null;
  taskOutput: BackgroundTaskOutput | null;
  taskOutputStatus: AsyncStatus;
  taskOutputError: string | null;
  onSelectParent: () => void;
  onSelectTask: (taskId: string) => void;
  onRefresh: () => void;
}

export function ChildSessionSidebar({
  parentSessionId,
  tasks,
  status,
  error,
  selectedTaskId,
  taskOutput,
  taskOutputStatus,
  taskOutputError,
  onSelectParent,
  onSelectTask,
  onRefresh,
}: ChildSessionSidebarProps) {
  const { t } = useTranslation();
  const childSessionId = taskOutput?.session_result?.session.session.id ?? null;

  if (!parentSessionId) return null;

  return (
    <aside className="hidden w-64 shrink-0 border-l border-[color:var(--vc-border-subtle)] bg-[var(--vc-bg)] lg:flex lg:flex-col">
      <div className="flex h-12 items-center justify-between border-b border-[color:var(--vc-border-subtle)] px-3">
        <div className="min-w-0">
          <div className="text-xs font-semibold uppercase tracking-wide text-[var(--vc-text-muted)]">
            {t("childSessions.title")}
          </div>
          <div className="truncate font-mono text-[10px] text-[var(--vc-text-subtle)]">
            {parentSessionId}
          </div>
        </div>
        <ControlButton
          compact
          icon
          variant="ghost"
          onClick={onRefresh}
          disabled={status === "loading"}
          aria-label={t("childSessions.refresh")}
        >
          {status === "loading" ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <RefreshCw className="h-3.5 w-3.5" />
          )}
        </ControlButton>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        <button
          type="button"
          onClick={onSelectParent}
          className={`mb-3 flex w-full items-start gap-2 rounded-lg border p-2 text-left text-xs transition-colors ${
            selectedTaskId === null
              ? "border-[color:var(--vc-border-strong)] bg-[var(--vc-surface-2)] text-[var(--vc-text-primary)]"
              : "border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] text-[var(--vc-text-muted)] hover:text-[var(--vc-text-primary)]"
          }`}
        >
          <SplitSquareHorizontal className="mt-0.5 h-3.5 w-3.5 shrink-0 text-[var(--vc-text-subtle)]" />
          <span className="min-w-0">
            <span className="block font-medium">
              {t("childSessions.parent")}
            </span>
            <span className="block truncate font-mono text-[10px] text-[var(--vc-text-subtle)]">
              {parentSessionId}
            </span>
          </span>
        </button>

        {error && (
          <div className="mb-3 rounded-md border border-[color:var(--vc-danger-border)] bg-[var(--vc-danger-bg)] p-2 text-xs text-[var(--vc-danger-text)]">
            {error}
          </div>
        )}

        {tasks.length === 0 && status !== "loading" && (
          <div className="rounded-md border border-dashed border-[color:var(--vc-border-subtle)] p-3 text-xs text-[var(--vc-text-subtle)]">
            {t("childSessions.empty")}
          </div>
        )}

        <div className="space-y-2">
          {tasks.map((task) => {
            const active = selectedTaskId === task.task.id;
            return (
              <button
                type="button"
                key={task.task.id}
                onClick={() => onSelectTask(task.task.id)}
                className={`w-full rounded-lg border p-2 text-left text-xs transition-colors ${
                  active
                    ? "border-[color:var(--vc-border-strong)] bg-[var(--vc-surface-2)] text-[var(--vc-text-primary)]"
                    : "border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] text-[var(--vc-text-muted)] hover:text-[var(--vc-text-primary)]"
                }`}
              >
                <span className="block truncate font-medium">
                  {task.prompt}
                </span>
                <span className="mt-1 flex items-center justify-between gap-2 text-[10px] text-[var(--vc-text-subtle)]">
                  <span>{task.status}</span>
                  <span className="truncate font-mono">
                    {task.session_id ?? task.task.id}
                  </span>
                </span>
              </button>
            );
          })}
        </div>

        {selectedTaskId && taskOutputStatus === "loading" && (
          <div className="mt-3 flex items-center gap-2 text-xs text-[var(--vc-text-subtle)]">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            {t("childSessions.loading")}
          </div>
        )}

        {taskOutputError && (
          <div className="mt-3 rounded-md border border-[color:var(--vc-danger-border)] bg-[var(--vc-danger-bg)] p-2 text-xs text-[var(--vc-danger-text)]">
            {taskOutputError}
          </div>
        )}

        {selectedTaskId &&
          taskOutputStatus === "success" &&
          !childSessionId && (
            <div className="mt-3 rounded-md border border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] p-2 text-xs text-[var(--vc-text-subtle)]">
              {t("childSessions.noTranscript")}
            </div>
          )}
      </div>
    </aside>
  );
}
