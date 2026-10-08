import type { ChatMessage } from "../lib/runtime/event-parser";

export type TodoStatus =
  "pending" | "in_progress" | "completed" | "abandoned" | "blocked";

export interface TodoPanelItem {
  content: string;
  status: TodoStatus;
  phase?: string;
  blocker?: string;
}

export interface TodoPanelSnapshot {
  items: TodoPanelItem[];
}

function recordValue(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === "object"
    ? (value as Record<string, unknown>)
    : undefined;
}

function stringValue(value: unknown): string | null {
  if (typeof value === "string" && value.trim().length > 0) return value;
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  const record = recordValue(value);
  if (record) {
    return stringValue(record.preview);
  }
  return null;
}

function todoItemsFromData(data: Record<string, unknown>): TodoPanelItem[] {
  const phases = Array.isArray(data.phases) ? data.phases : [];
  return phases.flatMap((rawPhase) => {
    if (!rawPhase || typeof rawPhase !== "object") return [];
    const phase = rawPhase as Record<string, unknown>;
    const tasks = Array.isArray(phase.tasks) ? phase.tasks : [];
    return normalizeTodoItems(tasks, stringValue(phase.name) ?? undefined);
  });
}

function todoSnapshot(data: Record<string, unknown>): TodoPanelSnapshot | null {
  if (!Array.isArray(data.phases)) return null;
  const phases = data.phases;
  const items = todoItemsFromData(data);
  const explicitlyEmpty = phases.every((phase) => {
    const record = recordValue(phase);
    return (
      record !== undefined &&
      Array.isArray(record.tasks) &&
      record.tasks.length === 0
    );
  });
  return items.length > 0 || explicitlyEmpty ? { items } : null;
}

export function deriveLatestTodoSnapshot(
  messages: ChatMessage[],
): TodoPanelSnapshot | null {
  for (
    let messageIndex = messages.length - 1;
    messageIndex >= 0;
    messageIndex -= 1
  ) {
    const message = messages[messageIndex];
    const state = message?.todoState ? todoSnapshot(message.todoState) : null;
    if (state) return state;
    const tools = message?.tools ?? [];
    for (let toolIndex = tools.length - 1; toolIndex >= 0; toolIndex -= 1) {
      const tool = tools[toolIndex];
      if (tool?.name !== "todo") continue;
      // A running/failed call can be an incomplete or redacted view of the
      // latest state. Keep the last successful snapshot until completion.
      if (tool.status !== "completed" || tool.error) continue;
      const data = tool.result;
      const snapshot = data ? todoSnapshot(data) : null;
      if (snapshot) return snapshot;
      const content = stringValue(tool.content);
      if (content) return { items: [{ content, status: "completed" }] };
    }
  }
  return null;
}

function normalizeTodoItems(
  rawTasks: unknown[],
  phase?: string,
): TodoPanelItem[] {
  return rawTasks.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const record = item as Record<string, unknown>;
    const content = stringValue(record.content);
    if (!content) return [];
    const rawStatus = stringValue(record.status);
    const status: TodoStatus =
      rawStatus === "in_progress" ||
      rawStatus === "completed" ||
      rawStatus === "abandoned" ||
      rawStatus === "blocked"
        ? rawStatus
        : "pending";
    const normalized: TodoPanelItem = { content, status };
    if (phase) normalized.phase = phase;
    const blocker = stringValue(record.blocker);
    if (blocker) normalized.blocker = blocker;
    return [normalized];
  });
}
