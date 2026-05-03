import { useState } from "react";
import { useTranslation } from "react-i18next";
import {
  ChevronDown,
  ChevronRight,
  Circle,
  CircleCheck,
  CircleDot,
  CircleX,
  ListTodo,
} from "lucide-react";
import type { TodoPanelSnapshot } from "./todoPanelModel";

function TodoStatusIcon({ status }: { status: string }) {
  if (status === "completed") {
    return (
      <CircleCheck className="h-3.5 w-3.5 text-[var(--vc-confirm-text)]" />
    );
  }
  if (status === "in_progress") {
    return <CircleDot className="h-3.5 w-3.5 text-[var(--vc-text-primary)]" />;
  }
  if (status === "cancelled") {
    return <CircleX className="h-3.5 w-3.5 text-[var(--vc-danger-text)]" />;
  }
  return <Circle className="h-3.5 w-3.5 text-[var(--vc-text-subtle)]" />;
}

function priorityClassName(priority: string) {
  if (priority === "high") {
    return "border-[color:var(--vc-danger-border)] bg-[var(--vc-danger-bg)] text-[var(--vc-danger-text)]";
  }
  if (priority === "low") {
    return "border-[color:var(--vc-border-subtle)] text-[var(--vc-text-subtle)]";
  }
  return "border-[color:var(--vc-border-strong)] text-[var(--vc-text-muted)]";
}

export function TodoPanel({
  snapshot,
}: {
  snapshot: TodoPanelSnapshot | null;
}) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  if (!snapshot || snapshot.items.length === 0) return null;

  const completedCount = snapshot.items.filter(
    (item) => item.status === "completed",
  ).length;

  return (
    <section className="flex-shrink-0 border-t border-[color:var(--vc-border-subtle)] bg-[var(--vc-bg)] px-4 py-2">
      <div className="mx-auto max-w-3xl">
        <button
          type="button"
          aria-expanded={expanded}
          aria-label={t(expanded ? "todo.panel.hide" : "todo.panel.show")}
          onClick={() => setExpanded((open) => !open)}
          className="flex w-full items-center gap-2 py-1 text-left text-xs font-semibold text-[var(--vc-text-muted)] transition-colors hover:text-[var(--vc-text-primary)] focus:outline-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--vc-focus-ring)]"
        >
          {expanded ? (
            <ChevronDown className="h-3 w-3 shrink-0 text-[var(--vc-text-subtle)]" />
          ) : (
            <ChevronRight className="h-3 w-3 shrink-0 text-[var(--vc-text-subtle)]" />
          )}
          <ListTodo className="h-4 w-4 shrink-0 text-[var(--vc-text-subtle)]" />
          <span className="min-w-0 flex-1 truncate">
            {t("todo.panel.summary", {
              completed: completedCount,
              total: snapshot.items.length,
            })}
          </span>
          <span className="shrink-0 text-[11px] font-normal text-[var(--vc-text-subtle)]">
            {t("todo.panel.progress", {
              completed: completedCount,
              total: snapshot.items.length,
            })}
          </span>
        </button>
        {expanded && (
          <div className="max-h-40 space-y-1 overflow-y-auto py-1 pr-1">
            {snapshot.items.map((item, index) => (
              <div
                key={`${item.content}-${index}`}
                className="flex items-center gap-2 px-1.5 py-1 text-xs text-[var(--vc-text-muted)]"
              >
                <TodoStatusIcon status={item.status} />
                <span className="min-w-0 flex-1 truncate text-[var(--vc-text-primary)]">
                  {item.content}
                </span>
                <span className="shrink-0 text-[11px] text-[var(--vc-text-subtle)]">
                  {t(`todo.status.${item.status}`, item.status)}
                </span>
                <span
                  className={`shrink-0 rounded-[var(--vc-radius-control)] border px-1.5 py-0.5 text-[10px] uppercase ${priorityClassName(item.priority)}`}
                >
                  {t(`todo.priority.${item.priority}`, item.priority)}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}
