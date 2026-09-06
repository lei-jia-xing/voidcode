import type { ChatMessage } from "../lib/runtime/event-parser";

type ChatTool = ChatMessage["tools"][number];

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

function resultData(tool: ChatTool) {
  return recordValue(tool.result?.data) ?? tool.result;
}

function todoItems(tool: ChatTool): TodoPanelItem[] {
  const data = resultData(tool);
  const phases = Array.isArray(data?.phases) ? data.phases : [];
  return phases.flatMap((rawPhase) => {
    if (!rawPhase || typeof rawPhase !== "object") return [];
    const phase = rawPhase as Record<string, unknown>;
    const tasks = Array.isArray(phase.tasks) ? phase.tasks : [];
    return normalizeTodoItems(tasks, stringValue(phase.name) ?? undefined);
  });
}

export function deriveLatestTodoSnapshot(
  messages: ChatMessage[],
): TodoPanelSnapshot | null {
  for (
    let messageIndex = messages.length - 1;
    messageIndex >= 0;
    messageIndex -= 1
  ) {
    const tools = messages[messageIndex]?.tools ?? [];
    for (let toolIndex = tools.length - 1; toolIndex >= 0; toolIndex -= 1) {
      const tool = tools[toolIndex];
      if (tool?.name !== "todo") continue;
      // A running/failed call can be an incomplete or redacted view of the
      // latest state. Keep the last successful snapshot until completion.
      if (tool.status !== "completed" || tool.error) continue;
      const data = resultData(tool);
      if (Array.isArray(data?.phases)) {
        const phases = data.phases;
        const items = todoItems(tool);
        const explicitlyEmpty = phases.every((phase) => {
          const record = recordValue(phase);
          return (
            record !== undefined &&
            Array.isArray(record.tasks) &&
            record.tasks.length === 0
          );
        });
        // Preserve an explicitly successful empty phase/task state as a clear;
        // only malformed or redacted non-empty phases use rendered content.
        if (items.length > 0 || explicitlyEmpty) return { items };
      }
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
