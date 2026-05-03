import type { ChatMessage } from "../lib/runtime/event-parser";

type ChatTool = ChatMessage["tools"][number];

export interface TodoPanelItem {
  content: string;
  status: string;
  priority: string;
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

function todoContent(item: Record<string, unknown>): string | null {
  return (
    stringValue(item.content) ??
    stringValue(item.title) ??
    stringValue(item.task) ??
    stringValue(item.description) ??
    stringValue(item.text) ??
    stringValue(item.name)
  );
}

function normalizeTodoItems(rawTodos: unknown[]): TodoPanelItem[] {
  return rawTodos
    .filter(
      (item): item is Record<string, unknown> =>
        Boolean(item) && typeof item === "object",
    )
    .map((item) => ({
      content: todoContent(item) ?? "Untitled todo",
      status: stringValue(item.status) ?? "pending",
      priority: stringValue(item.priority) ?? "medium",
    }));
}

function parseTodosFromContent(
  content: string | null | undefined,
): TodoPanelItem[] {
  if (!content) return [];
  const itemPattern =
    /^\s*\d+\.\s+\[(pending|in_progress|completed|cancelled)\/(high|medium|low)\]\s+(.+?)\s*$/;
  return content
    .split(/\r?\n/)
    .map((line) => itemPattern.exec(line))
    .filter((match): match is RegExpExecArray => match !== null)
    .map((match) => ({
      status: match[1],
      priority: match[2],
      content: match[3].trim(),
    }));
}

function hasNamedTodo(items: TodoPanelItem[]) {
  return items.some((item) => item.content !== "Untitled todo");
}

function todoItems(tool: ChatTool): TodoPanelItem[] {
  const data = resultData(tool);
  const rawTodos = Array.isArray(data?.todos)
    ? data.todos
    : Array.isArray(tool.arguments?.todos)
      ? tool.arguments.todos
      : [];
  const items = normalizeTodoItems(rawTodos);
  if (hasNamedTodo(items)) return items;

  const parsedItems = parseTodosFromContent(tool.content);
  return parsedItems.length > 0 ? parsedItems : items;
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
      if (tool?.name !== "todo_write") continue;
      if (tool.error) continue;
      return { items: todoItems(tool) };
    }
  }
  return null;
}
