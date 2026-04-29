import { EventEnvelope, QuestionPrompt } from "./types";

export interface DerivedTask {
  id: string;
  titleKey: string;
  titleValues?: Record<string, string>;
  type: "request" | "tool" | "approval" | "response" | "unknown";
  status: "pending" | "in_progress" | "completed" | "failed" | "waiting";
  sequence: number;
  events: EventEnvelope[];
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  thinking: string[];
  thinkingStartedAt?: number;
  thinkingUpdatedAt?: number;
  tools: {
    id?: string;
    name: string;
    label?: string;
    status: "pending" | "running" | "completed" | "failed";
    arguments?: Record<string, unknown>;
    result?: Record<string, unknown>;
    content?: string | null;
    error?: string | null;
  }[];
  approval: {
    requestId: string;
    tool: string;
    targetSummary: string;
  } | null;
  question?: {
    requestId: string;
    tool: string;
    prompts: QuestionPrompt[];
  } | null;
  status: "in_progress" | "completed" | "failed" | "waiting";
  sequence: number;
}

type ToolStatusPayload = {
  invocation_id?: unknown;
  tool_name?: unknown;
  status?: unknown;
  label?: unknown;
};

type ChatTool = ChatMessage["tools"][number];

function objectPayload(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === "object"
    ? (value as Record<string, unknown>)
    : undefined;
}

function toolStatusFromPayload(status: unknown): ChatTool["status"] {
  return status === "pending" ||
    status === "running" ||
    status === "completed" ||
    status === "failed"
    ? status
    : status === "ok"
      ? "completed"
      : status === "error"
        ? "failed"
        : "completed";
}

function findTool(
  tools: ChatMessage["tools"],
  options: { id?: string; name?: string },
): ChatTool | undefined {
  const { id, name } = options;
  return tools.find((tool) =>
    id ? tool.id === id : tool.name === name && tool.status === "running",
  );
}

function upsertTool(
  currentAssistant: ChatMessage | null,
  tool: ChatTool,
): ChatTool | null {
  if (!currentAssistant) return null;
  const existing = findTool(currentAssistant.tools, {
    id: tool.id,
    name: tool.name,
  });
  if (!existing) {
    currentAssistant.tools.push(tool);
    return tool;
  }
  existing.name = tool.name;
  existing.status = tool.status;
  existing.id = tool.id ?? existing.id;
  existing.label = tool.label ?? existing.label;
  existing.arguments = tool.arguments ?? existing.arguments;
  existing.result = tool.result ?? existing.result;
  if (tool.content !== undefined) existing.content = tool.content;
  if (tool.error !== undefined) existing.error = tool.error;
  return existing;
}

function parseQuestionPrompts(value: unknown): QuestionPrompt[] {
  if (!Array.isArray(value)) return [];

  return value
    .filter(
      (item): item is Record<string, unknown> =>
        Boolean(item) && typeof item === "object",
    )
    .map((item) => {
      const options = Array.isArray(item.options)
        ? item.options
            .filter(
              (option): option is Record<string, unknown> =>
                Boolean(option) && typeof option === "object",
            )
            .map((option) => ({
              label: typeof option.label === "string" ? option.label : "",
              description:
                typeof option.description === "string"
                  ? option.description
                  : null,
            }))
            .filter((option) => option.label.length > 0)
        : [];

      return {
        header: typeof item.header === "string" ? item.header : "Question",
        question: typeof item.question === "string" ? item.question : null,
        multiple: item.multiple === true,
        options,
      };
    });
}

function getToolStatusPayload(event: EventEnvelope): ToolStatusPayload | null {
  const toolStatus = event.payload?.tool_status;
  if (!toolStatus || typeof toolStatus !== "object") return null;
  return toolStatus as ToolStatusPayload;
}

function applyToolStatus(
  currentAssistant: ChatMessage | null,
  toolStatus: ToolStatusPayload,
) {
  if (!currentAssistant) return;

  const name =
    typeof toolStatus.tool_name === "string" ? toolStatus.tool_name : "unknown";
  const id =
    typeof toolStatus.invocation_id === "string"
      ? toolStatus.invocation_id
      : undefined;
  const label =
    typeof toolStatus.label === "string" ? toolStatus.label : undefined;
  upsertTool(currentAssistant, {
    id,
    name,
    label,
    status: toolStatusFromPayload(toolStatus.status),
  });
}

export function deriveTasksFromEvents(events: EventEnvelope[]): DerivedTask[] {
  const tasks: DerivedTask[] = [];
  let currentToolTask: DerivedTask | null = null;
  let currentRequest: DerivedTask | null = null;

  for (const event of events) {
    if (event.event_type === "runtime.request_received") {
      if (currentToolTask && currentToolTask.status === "in_progress") {
        currentToolTask.status = "completed";
      }
      currentToolTask = null;

      if (currentRequest && currentRequest.status === "in_progress") {
        currentRequest.status = "completed";
      }

      const prompt =
        typeof event.payload?.prompt === "string"
          ? event.payload.prompt
          : "Unknown Request";
      currentRequest = {
        id: `req-${event.sequence}`,
        titleKey: "task.request",
        titleValues: { prompt },
        type: "request",
        status: "in_progress",
        sequence: event.sequence,
        events: [event],
      };
      tasks.push(currentRequest);
    } else if (event.event_type === "graph.tool_request_created") {
      if (currentToolTask && currentToolTask.status === "in_progress") {
        currentToolTask.status = "completed";
      }
      const toolName =
        typeof event.payload?.tool === "string"
          ? event.payload.tool
          : "unknown";
      currentToolTask = {
        id: `tool-${event.sequence}`,
        titleKey: "task.tool",
        titleValues: { tool: toolName },
        type: "tool",
        status: "in_progress",
        sequence: event.sequence,
        events: [event],
      };
      tasks.push(currentToolTask);
    } else if (event.event_type === "runtime.tool_completed") {
      if (currentToolTask) {
        currentToolTask.status = "completed";
        currentToolTask.events.push(event);
        currentToolTask = null;
      } else {
        tasks.push({
          id: `tool-done-${event.sequence}`,
          titleKey: "task.unknown",
          titleValues: { type: "Tool Completed (Orphaned)" },
          type: "tool",
          status: "completed",
          sequence: event.sequence,
          events: [event],
        });
      }
    } else if (event.event_type === "graph.response_ready") {
      if (currentToolTask && currentToolTask.status === "in_progress") {
        currentToolTask.status = "completed";
      }
      currentToolTask = null;
      if (currentRequest && currentRequest.status === "in_progress") {
        currentRequest.status = "completed";
      }
      tasks.push({
        id: `resp-${event.sequence}`,
        titleKey: "task.response",
        type: "response",
        status: "completed",
        sequence: event.sequence,
        events: [event],
      });
    } else if (
      event.event_type === "runtime.permission_resolved" ||
      event.event_type === "runtime.approval_requested" ||
      event.event_type === "runtime.approval_resolved"
    ) {
      if (currentToolTask) {
        currentToolTask.events.push(event);
        const decision = event.payload?.decision;
        if (decision === "deny") {
          currentToolTask.status = "failed";
        } else if (decision === "ask") {
          currentToolTask.status = "waiting";
        }
      } else {
        const toolName =
          typeof event.payload?.tool === "string"
            ? event.payload.tool
            : "unknown";
        tasks.push({
          id: `perm-${event.sequence}`,
          titleKey: "task.permission",
          titleValues: { tool: toolName },
          type: "approval",
          status: "completed",
          sequence: event.sequence,
          events: [event],
        });
      }
    } else if (
      event.event_type === "runtime.question_requested" ||
      event.event_type === "runtime.question_answered"
    ) {
      const toolName =
        typeof event.payload?.tool === "string"
          ? event.payload.tool
          : "unknown";
      tasks.push({
        id: `question-${event.sequence}`,
        titleKey: "task.question",
        titleValues: { tool: toolName },
        type: "approval",
        status:
          event.event_type === "runtime.question_requested"
            ? "waiting"
            : "completed",
        sequence: event.sequence,
        events: [event],
      });
    } else {
      if (currentToolTask) {
        currentToolTask.events.push(event);
      } else {
        tasks.push({
          id: `evt-${event.sequence}`,
          titleKey: "task.unknown",
          titleValues: { type: event.event_type },
          type: "unknown",
          status: "completed",
          sequence: event.sequence,
          events: [event],
        });
      }
    }
  }

  return tasks;
}

export function deriveActivitiesFromEvents(events: EventEnvelope[]) {
  return events.map((event) => {
    let payloadStr = "";
    try {
      payloadStr = event.payload ? JSON.stringify(event.payload) : "";
    } catch {
      payloadStr = "{...}";
    }

    return {
      id: `act-${event.sequence}`,
      type: "log" as const,
      message: event.event_type,
      source: event.source,
      timestamp: "",
      sequence: event.sequence,
      payloadStr,
    };
  });
}

export function deriveChatMessages(
  events: EventEnvelope[],
  currentOutput: string | null,
  fallbackSessionId: string | null = null,
): ChatMessage[] {
  const messages: ChatMessage[] = [];
  let currentAssistant: ChatMessage | null = null;
  let requestOrdinal = 0;

  for (const event of events) {
    const messageSessionId = event.session_id || fallbackSessionId || "session";
    const toolStatus = getToolStatusPayload(event);

    if (event.event_type === "runtime.request_received") {
      requestOrdinal += 1;

      if (currentAssistant) {
        if (currentAssistant.status === "in_progress") {
          currentAssistant.status = "completed";
        }
        currentAssistant = null;
      }

      const prompt =
        typeof event.payload?.prompt === "string" ? event.payload.prompt : "";
      messages.push({
        id: `user-${messageSessionId}-${requestOrdinal}-${event.sequence}`,
        role: "user",
        content: prompt,
        thinking: [],
        tools: [],
        approval: null,
        question: null,
        status: "completed",
        sequence: event.sequence,
      });

      currentAssistant = {
        id: `assistant-${messageSessionId}-${requestOrdinal}-${event.sequence}`,
        role: "assistant",
        content: "",
        thinking: [],
        tools: [],
        approval: null,
        question: null,
        status: "in_progress",
        sequence: event.sequence,
      };
      messages.push(currentAssistant);
    } else if (
      event.event_type === "graph.provider_stream" &&
      event.payload?.channel === "reasoning"
    ) {
      if (currentAssistant) {
        const delta =
          typeof event.payload?.delta === "string"
            ? event.payload.delta
            : typeof event.payload?.content === "string"
              ? event.payload.content
              : typeof event.payload?.text === "string"
                ? event.payload.text
                : "";
        if (delta) {
          currentAssistant.thinking.push(delta);
          if (typeof event.received_at === "number") {
            currentAssistant.thinkingStartedAt ??= event.received_at;
            currentAssistant.thinkingUpdatedAt = event.received_at;
          }
        }
      }
    } else if (
      event.event_type === "graph.provider_stream" &&
      event.payload?.channel === "text"
    ) {
      if (currentAssistant) {
        const delta =
          typeof event.payload?.text === "string"
            ? event.payload.text
            : typeof event.payload?.delta === "string"
              ? event.payload.delta
              : typeof event.payload?.content === "string"
                ? event.payload.content
                : "";
        if (delta) {
          currentAssistant.content += delta;
        }
      }
    } else if (event.event_type === "graph.tool_request_created") {
      if (currentAssistant) {
        if (toolStatus) {
          applyToolStatus(currentAssistant, toolStatus);
          continue;
        }
        const toolName =
          typeof event.payload?.tool === "string"
            ? event.payload.tool
            : "unknown";
        const toolCallId =
          typeof event.payload?.tool_call_id === "string"
            ? event.payload.tool_call_id
            : undefined;
        upsertTool(currentAssistant, {
          id: toolCallId,
          name: toolName,
          status: "running",
          arguments: objectPayload(event.payload?.arguments),
        });
      }
    } else if (event.event_type === "runtime.tool_completed") {
      if (currentAssistant) {
        if (toolStatus) {
          applyToolStatus(currentAssistant, toolStatus);
          continue;
        }
        const toolName =
          typeof event.payload?.tool === "string" ? event.payload.tool : null;
        if (toolName) {
          const toolCallId =
            typeof event.payload?.tool_call_id === "string"
              ? event.payload.tool_call_id
              : undefined;
          upsertTool(currentAssistant, {
            id: toolCallId,
            name: toolName,
            status: toolStatusFromPayload(event.payload?.status),
            arguments: objectPayload(event.payload?.arguments),
            result: event.payload,
            content:
              typeof event.payload?.content === "string"
                ? event.payload.content
                : null,
            error:
              typeof event.payload?.error === "string"
                ? event.payload.error
                : null,
          });
        }
      }
    } else if (event.event_type === "runtime.approval_requested") {
      if (currentAssistant) {
        currentAssistant.status = "waiting";
        currentAssistant.approval = {
          requestId: String(event.payload?.request_id || ""),
          tool: String(event.payload?.tool || ""),
          targetSummary: String(event.payload?.target_summary || ""),
        };
      }
    } else if (event.event_type === "runtime.question_requested") {
      if (currentAssistant) {
        currentAssistant.status = "waiting";
        currentAssistant.question = {
          requestId: String(event.payload?.request_id || ""),
          tool: String(event.payload?.tool || ""),
          prompts: parseQuestionPrompts(event.payload?.questions),
        };
      }
    } else if (event.event_type === "runtime.question_answered") {
      if (currentAssistant && currentAssistant.status === "waiting") {
        currentAssistant.status = "in_progress";
        currentAssistant.question = null;
      }
    } else if (event.event_type === "runtime.approval_resolved") {
      if (currentAssistant) {
        const decision = event.payload?.decision;
        if (decision === "deny") {
          currentAssistant.status = "failed";
        } else if (currentAssistant.status === "waiting") {
          currentAssistant.status = "in_progress";
        }
        currentAssistant.approval = null;
      }
    } else if (event.event_type === "graph.response_ready") {
      if (currentAssistant) {
        const output =
          typeof event.payload?.output === "string"
            ? event.payload.output
            : typeof event.payload?.output_preview === "string"
              ? event.payload.output_preview
              : "";
        if (output) currentAssistant.content = output;
        currentAssistant.status = "completed";
      }
    } else if (event.event_type === "runtime.failed") {
      if (currentAssistant) {
        currentAssistant.status = "failed";
      }
    } else if (toolStatus) {
      applyToolStatus(currentAssistant, toolStatus);
    }
  }

  if (
    currentAssistant &&
    currentAssistant.status === "in_progress" &&
    currentOutput !== null
  ) {
    currentAssistant.content = currentOutput;
  }

  return messages;
}
