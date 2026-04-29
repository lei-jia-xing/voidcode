import { useState, useMemo } from "react";
import { useTranslation } from "react-i18next";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  ChevronDown,
  ChevronRight,
  CheckCircle2,
  XCircle,
  Loader2,
  Wrench,
  PauseCircle,
  AlertCircle,
} from "lucide-react";
import { ChatMessage } from "../lib/runtime/event-parser";
import type { QuestionAnswer } from "../lib/runtime/types";

type ChatTool = ChatMessage["tools"][number];

interface ChatThreadProps {
  messages: ChatMessage[];
  isRunning: boolean;
  isWaitingApproval: boolean;
  isApprovalSubmitting: boolean;
  approvalError: string | null;
  onResolveApproval: (decision: "allow" | "deny") => void;
  isWaitingQuestion?: boolean;
  isQuestionSubmitting?: boolean;
  questionError?: string | null;
  onAnswerQuestion?: (answers: QuestionAnswer[]) => void;
}

function formatThinkingDuration(startedAt?: number, updatedAt?: number) {
  if (typeof startedAt !== "number" || typeof updatedAt !== "number") {
    return null;
  }

  const elapsedMs = Math.max(0, updatedAt - startedAt);
  if (elapsedMs < 1000) return "<1s";
  if (elapsedMs < 10_000) return `${(elapsedMs / 1000).toFixed(1)}s`;
  return `${Math.round(elapsedMs / 1000)}s`;
}

function ThinkingBlock({
  thinking,
  startedAt,
  updatedAt,
}: {
  thinking: string[];
  startedAt?: number;
  updatedAt?: number;
}) {
  const [expanded, setExpanded] = useState(false);
  const content = useMemo(() => thinking.join(""), [thinking]);
  const duration = formatThinkingDuration(startedAt, updatedAt);

  if (thinking.length === 0) return null;

  return (
    <div className="mb-3">
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1.5 text-xs text-slate-500 hover:text-slate-400 transition-colors mb-1"
      >
        {expanded ? (
          <ChevronDown className="w-3.5 h-3.5" />
        ) : (
          <ChevronRight className="w-3.5 h-3.5" />
        )}
        <span className="font-medium">Thinking</span>
        {duration && <span className="text-slate-600">({duration})</span>}
      </button>
      {expanded && (
        <div className="bg-slate-900/60 border border-slate-800 rounded-lg p-3 font-mono text-xs text-slate-400 leading-relaxed overflow-x-auto">
          {content}
        </div>
      )}
    </div>
  );
}

function toolValue(value: unknown): string | null {
  if (typeof value === "string" && value.length > 0) return value;
  if (typeof value === "number" || typeof value === "boolean")
    return String(value);
  return null;
}

function nestedRecord(
  record: Record<string, unknown> | undefined,
  key: string,
): Record<string, unknown> | undefined {
  const value = record?.[key];
  return value && typeof value === "object"
    ? (value as Record<string, unknown>)
    : undefined;
}

function primaryPath(tool: ChatTool): string | null {
  return (
    toolValue(tool.arguments?.path) ??
    toolValue(tool.arguments?.filePath) ??
    toolValue(tool.result?.path) ??
    toolValue(tool.result?.filePath)
  );
}

function bracketedArgs(
  args: Record<string, unknown> | undefined,
  omit: string[],
) {
  if (!args) return "";
  const primitives = Object.entries(args)
    .filter(([key, value]) => !omit.includes(key) && toolValue(value) !== null)
    .map(([key, value]) => `${key}=${toolValue(value)}`);
  return primitives.length > 0 ? ` [${primitives.join(", ")}]` : "";
}

function resultData(tool: ChatTool) {
  return nestedRecord(tool.result, "data") ?? tool.result;
}

function toolStatusIcon(status: ChatTool["status"]) {
  if (status === "running") return <Loader2 className="w-3 h-3 animate-spin" />;
  if (status === "completed") return <CheckCircle2 className="w-3 h-3" />;
  if (status === "failed") return <XCircle className="w-3 h-3" />;
  return <Wrench className="w-3 h-3" />;
}

function ToolOutputBlock({
  label,
  value,
}: {
  label: string;
  value: string | null;
}) {
  if (!value) return null;
  const preview = value.length > 4000 ? `${value.slice(0, 4000)}\n…` : value;
  return (
    <div className="mt-2">
      <div className="mb-1 text-[11px] uppercase tracking-wide text-slate-500">
        {label}
      </div>
      <pre className="max-h-72 overflow-auto rounded-md border border-slate-800 bg-slate-950/80 p-3 text-xs leading-relaxed text-slate-300 whitespace-pre-wrap">
        {preview}
      </pre>
    </div>
  );
}

function ReadToolActivity({ tool }: { tool: ChatTool }) {
  const path = primaryPath(tool) ?? tool.label ?? tool.name;
  return (
    <div className="flex items-center gap-2 rounded-md border border-slate-800 bg-slate-950/50 px-3 py-2 text-xs text-slate-300">
      <span className="text-sky-300">→</span>
      <span className="font-medium">Read</span>
      <code className="text-slate-200">{path}</code>
      <span className="text-slate-500">
        {bracketedArgs(tool.arguments, ["path", "filePath"])}
      </span>
      <span className="ml-auto text-slate-500">
        {toolStatusIcon(tool.status)}
      </span>
    </div>
  );
}

function WriteToolActivity({ tool }: { tool: ChatTool }) {
  const data = resultData(tool);
  const path = primaryPath(tool) ?? tool.label ?? tool.name;
  const diff = toolValue(data?.diff);
  const bytes = toolValue(data?.byte_count);
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-950/50 p-3 text-xs text-slate-300">
      <div className="flex items-center gap-2">
        <span className="text-emerald-300">←</span>
        <span className="font-medium">Wrote</span>
        <code className="text-slate-200">{path}</code>
        {bytes && <span className="text-slate-500">[{bytes} bytes]</span>}
        <span className="ml-auto text-slate-500">
          {toolStatusIcon(tool.status)}
        </span>
      </div>
      <ToolOutputBlock label="Diff" value={diff} />
      {!diff && tool.content && (
        <ToolOutputBlock label="Result" value={tool.content} />
      )}
    </div>
  );
}

function ShellToolActivity({ tool }: { tool: ChatTool }) {
  const data = resultData(tool);
  const command =
    toolValue(tool.arguments?.command) ??
    toolValue(data?.command) ??
    tool.label ??
    "shell";
  const stdout = toolValue(data?.stdout);
  const stderr = toolValue(data?.stderr);
  const exitCode = toolValue(data?.exit_code);
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-950/50 p-3 text-xs text-slate-300">
      <div className="flex items-center gap-2">
        <span className="text-violet-300">$</span>
        <span className="font-medium">Command</span>
        {exitCode && (
          <span className="ml-auto text-slate-500">exit {exitCode}</span>
        )}
        <span className="text-slate-500">{toolStatusIcon(tool.status)}</span>
      </div>
      <pre className="mt-2 overflow-auto rounded-md border border-slate-800 bg-slate-950/80 p-3 text-xs text-slate-200 whitespace-pre-wrap">
        $ {command}
      </pre>
      <ToolOutputBlock label="stdout" value={stdout ?? tool.content ?? null} />
      <ToolOutputBlock label="stderr" value={stderr} />
      {tool.error && <ToolOutputBlock label="error" value={tool.error} />}
    </div>
  );
}

function SkillToolActivity({ tool }: { tool: ChatTool }) {
  const data = resultData(tool);
  const skill = nestedRecord(data, "skill");
  const name =
    toolValue(tool.arguments?.name) ??
    toolValue(skill?.name) ??
    tool.label ??
    "skill";
  const description = toolValue(skill?.description);
  const sourcePath = toolValue(skill?.source_path);
  const userMessage =
    toolValue(data?.user_message) ?? toolValue(tool.arguments?.user_message);

  return (
    <div className="rounded-lg border border-slate-800 bg-slate-950/50 p-3 text-xs text-slate-300">
      <div className="flex items-center gap-2">
        <span className="text-amber-300">◆</span>
        <span className="font-medium">Loaded skill</span>
        <code className="text-slate-200">{name}</code>
        <span className="ml-auto text-slate-500">
          {toolStatusIcon(tool.status)}
        </span>
      </div>
      {description && <div className="mt-2 text-slate-400">{description}</div>}
      {sourcePath && (
        <div className="mt-2 text-[11px] text-slate-500">
          Source: <code>{sourcePath}</code>
        </div>
      )}
      {userMessage && <ToolOutputBlock label="Context" value={userMessage} />}
      {tool.error && <ToolOutputBlock label="error" value={tool.error} />}
    </div>
  );
}

function formatList(value: unknown): string | null {
  if (!Array.isArray(value)) return null;
  if (value.length === 0) return "none";
  return value.map((item) => String(item)).join(", ");
}

function TaskToolActivity({ tool }: { tool: ChatTool }) {
  const data = resultData(tool);
  const route =
    toolValue(tool.arguments?.category) ??
    toolValue(data?.requested_category) ??
    toolValue(tool.arguments?.subagent_type) ??
    toolValue(data?.requested_subagent_type) ??
    "subagent";
  const mode =
    tool.arguments?.run_in_background === false ? "sync" : "background";
  const taskId = toolValue(data?.task_id);
  const sessionId =
    toolValue(data?.child_session_id) ?? toolValue(data?.session_id);
  const skills =
    formatList(tool.arguments?.load_skills) ?? formatList(data?.load_skills);
  const description = toolValue(tool.arguments?.description);

  return (
    <div className="rounded-lg border border-slate-800 bg-slate-950/50 p-3 text-xs text-slate-300">
      <div className="flex items-center gap-2">
        <span className="text-cyan-300">↳</span>
        <span className="font-medium">Started subagent</span>
        <code className="text-slate-200">{route}</code>
        <span className="text-slate-500">[{mode}]</span>
        <span className="ml-auto text-slate-500">
          {toolStatusIcon(tool.status)}
        </span>
      </div>
      {description && <div className="mt-2 text-slate-400">{description}</div>}
      <div className="mt-2 grid gap-1 text-[11px] text-slate-500">
        {taskId && (
          <div>
            Task ID: <code>{taskId}</code>
          </div>
        )}
        {sessionId && (
          <div>
            Session: <code>{sessionId}</code>
          </div>
        )}
        {skills && (
          <div>
            Skills: <code>{skills}</code>
          </div>
        )}
      </div>
      {tool.content && <ToolOutputBlock label="Output" value={tool.content} />}
      {tool.error && <ToolOutputBlock label="error" value={tool.error} />}
    </div>
  );
}

function todoItems(
  tool: ChatTool,
): { content: string; status: string; priority: string }[] {
  const data = resultData(tool);
  const rawTodos = Array.isArray(data?.todos)
    ? data.todos
    : Array.isArray(tool.arguments?.todos)
      ? tool.arguments.todos
      : [];
  return rawTodos
    .filter(
      (item): item is Record<string, unknown> =>
        Boolean(item) && typeof item === "object",
    )
    .map((item) => ({
      content: toolValue(item.content) ?? "Untitled todo",
      status: toolValue(item.status) ?? "pending",
      priority: toolValue(item.priority) ?? "medium",
    }));
}

function todoStatusSymbol(status: string) {
  if (status === "completed") return "✓";
  if (status === "in_progress") return "●";
  if (status === "cancelled") return "×";
  return "○";
}

function TodoToolActivity({ tool }: { tool: ChatTool }) {
  const items = todoItems(tool);
  const data = resultData(tool);
  const summary = nestedRecord(data, "summary");
  const summaryText = summary
    ? ["in_progress", "pending", "completed", "cancelled"]
        .map((key) => `${key}=${toolValue(summary[key]) ?? "0"}`)
        .join(", ")
    : `${items.length} todos`;

  return (
    <div className="rounded-lg border border-slate-800 bg-slate-950/50 p-3 text-xs text-slate-300">
      <div className="flex items-center gap-2">
        <span className="text-lime-300">☑</span>
        <span className="font-medium">Updated todos</span>
        <span className="text-slate-500">[{summaryText}]</span>
        <span className="ml-auto text-slate-500">
          {toolStatusIcon(tool.status)}
        </span>
      </div>
      <div className="mt-2 space-y-1">
        {items.map((item, index) => (
          <div
            key={`${item.content}-${index}`}
            className="flex items-start gap-2 text-slate-300"
          >
            <span className="mt-px text-slate-500">
              {todoStatusSymbol(item.status)}
            </span>
            <span className="flex-1">{item.content}</span>
            <span className="rounded border border-slate-800 px-1.5 py-0.5 text-[10px] uppercase text-slate-500">
              {item.status} · {item.priority}
            </span>
          </div>
        ))}
      </div>
      {tool.error && <ToolOutputBlock label="error" value={tool.error} />}
    </div>
  );
}

function GenericToolActivity({ tool }: { tool: ChatTool }) {
  const argumentsText = tool.arguments
    ? JSON.stringify(tool.arguments, null, 2)
    : null;
  const resultText = tool.result
    ? JSON.stringify(tool.result, null, 2)
    : tool.content;
  return (
    <div className="rounded-md border border-slate-800 bg-slate-950/50 px-3 py-2 text-xs text-slate-300">
      <div className="flex items-center gap-2">
        <Wrench className="w-3 h-3 text-slate-500" />
        <span className="font-medium">{tool.label ?? tool.name}</span>
        <span className="ml-auto text-slate-500">
          {toolStatusIcon(tool.status)}
        </span>
      </div>
      <ToolOutputBlock label="Arguments" value={argumentsText} />
      <ToolOutputBlock label="Result" value={resultText ?? null} />
      {tool.error && <ToolOutputBlock label="error" value={tool.error} />}
    </div>
  );
}

function QuestionCard({
  question,
  isSubmitting,
  onAnswer,
}: {
  question: NonNullable<ChatMessage["question"]>;
  isSubmitting: boolean;
  onAnswer: (answers: QuestionAnswer[]) => void;
}) {
  const { t } = useTranslation();
  const [values, setValues] = useState<Record<string, string[]>>({});

  const prompts = question.prompts.length
    ? question.prompts
    : [{ header: "Question", question: null, multiple: false, options: [] }];

  const canSubmit = prompts.every((prompt) =>
    (values[prompt.header] ?? []).some((value) => value.trim()),
  );

  const toggleOption = (
    header: string,
    label: string,
    multiple: boolean,
    checked: boolean,
  ) => {
    setValues((current) => {
      if (!multiple) {
        return { ...current, [header]: [label] };
      }

      const selected = current[header] ?? [];
      return {
        ...current,
        [header]: checked
          ? [...selected, label]
          : selected.filter((item) => item !== label),
      };
    });
  };

  return (
    <div className="mt-3 rounded-lg border border-sky-500/20 bg-sky-500/10 p-4">
      <div className="flex items-center gap-2 mb-3">
        <PauseCircle className="w-5 h-5 text-sky-300 flex-shrink-0" />
        <div>
          <p className="text-sm font-medium text-sky-200">
            {t("question.heading")}
          </p>
          <p className="mt-0.5 text-sm text-slate-300">
            {t("question.message", {
              tool: question.tool || t("question.unknownTool"),
            })}
          </p>
        </div>
      </div>
      <div className="space-y-3">
        {prompts.map((prompt) => (
          <label key={prompt.header} className="block">
            <span className="text-xs font-medium text-slate-300">
              {prompt.header}
            </span>
            {prompt.question && (
              <span className="mt-1 block text-xs text-slate-400">
                {prompt.question}
              </span>
            )}
            <div className="mt-2 space-y-2">
              {prompt.options.map((option) => {
                const selected = values[prompt.header] ?? [];
                const checked = selected.includes(option.label);
                return (
                  <label
                    key={option.label}
                    className="flex items-start gap-2 rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-200"
                  >
                    <input
                      type={prompt.multiple ? "checkbox" : "radio"}
                      name={`question-${question.requestId}-${prompt.header}`}
                      checked={checked}
                      onChange={(event) =>
                        toggleOption(
                          prompt.header,
                          option.label,
                          prompt.multiple,
                          event.target.checked,
                        )
                      }
                      disabled={isSubmitting}
                      className="mt-0.5"
                    />
                    <span>
                      <span className="block font-medium">{option.label}</span>
                      {option.description && (
                        <span className="mt-0.5 block text-xs text-slate-500">
                          {option.description}
                        </span>
                      )}
                    </span>
                  </label>
                );
              })}
            </div>
          </label>
        ))}
      </div>
      <div className="mt-4 flex justify-end">
        <button
          type="button"
          onClick={() =>
            onAnswer(
              prompts.map((prompt) => ({
                header: prompt.header,
                answers: (values[prompt.header] ?? []).filter((value) =>
                  value.trim(),
                ),
              })),
            )
          }
          disabled={isSubmitting || !canSubmit}
          className="rounded-lg border border-sky-500/20 bg-sky-500/10 px-4 py-2 text-sm font-medium text-sky-200 transition-colors hover:bg-sky-500/20 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {isSubmitting ? t("question.submitting") : t("question.submit")}
        </button>
      </div>
    </div>
  );
}

function ToolActivities({ tools }: { tools: ChatTool[] }) {
  if (tools.length === 0) return null;

  return (
    <div className="mb-3 space-y-2">
      {tools.map((tool, idx) => (
        <ToolActivity key={tool.id ?? `${tool.name}-${idx}`} tool={tool} />
      ))}
    </div>
  );
}

function ToolActivity({ tool }: { tool: ChatTool }) {
  if (tool.name === "read_file" || tool.name === "read")
    return <ReadToolActivity tool={tool} />;
  if (
    tool.name === "write_file" ||
    tool.name === "write" ||
    tool.name === "edit"
  ) {
    return <WriteToolActivity tool={tool} />;
  }
  if (tool.name === "shell_exec" || tool.name === "bash")
    return <ShellToolActivity tool={tool} />;
  if (tool.name === "skill") return <SkillToolActivity tool={tool} />;
  if (tool.name === "task") return <TaskToolActivity tool={tool} />;
  if (tool.name === "todo_write") return <TodoToolActivity tool={tool} />;
  return <GenericToolActivity tool={tool} />;
}

function visibleAssistantContent(content: string) {
  const lines = content.split("\n");
  const visibleLines: string[] = [];
  let insideFence = false;
  let insideToolBlock = false;

  for (const line of lines) {
    const trimmed = line.trim();
    if (trimmed.startsWith("```")) {
      insideFence = !insideFence;
    }

    if (!insideFence && /^<tool\b[^>]*>\s*$/i.test(trimmed)) {
      insideToolBlock = true;
      continue;
    }

    if (insideToolBlock) {
      if (/^<\/tool>\s*$/i.test(trimmed)) {
        insideToolBlock = false;
      }
      continue;
    }

    visibleLines.push(line);
  }

  return visibleLines.join("\n").trim();
}

function ApprovalCard({
  approval,
  isSubmitting,
  onResolve,
}: {
  approval: NonNullable<ChatMessage["approval"]>;
  isSubmitting: boolean;
  onResolve: (decision: "allow" | "deny") => void;
}) {
  const { t } = useTranslation();

  return (
    <div className="mt-3 rounded-lg border border-amber-500/20 bg-amber-500/10 p-4">
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div className="flex items-center gap-2">
          <PauseCircle className="w-5 h-5 text-amber-400 flex-shrink-0" />
          <div>
            <p className="text-sm font-medium text-amber-300">
              {t("approval.heading")}
            </p>
            <p className="mt-0.5 text-sm text-slate-300">
              {t("approval.message", {
                target:
                  approval.targetSummary ||
                  approval.tool ||
                  t("approval.unknownTarget"),
              })}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => onResolve("deny")}
            disabled={isSubmitting}
            className="rounded-lg border border-rose-500/20 bg-rose-500/10 px-4 py-2 text-sm font-medium text-rose-300 transition-colors hover:bg-rose-500/20 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {isSubmitting ? t("approval.submitting") : t("approval.deny")}
          </button>
          <button
            type="button"
            onClick={() => onResolve("allow")}
            disabled={isSubmitting}
            className="rounded-lg border border-emerald-500/20 bg-emerald-500/10 px-4 py-2 text-sm font-medium text-emerald-300 transition-colors hover:bg-emerald-500/20 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {isSubmitting ? t("approval.submitting") : t("approval.allow")}
          </button>
        </div>
      </div>
    </div>
  );
}

function StatusIndicator({ status }: { status: ChatMessage["status"] }) {
  if (status === "in_progress") {
    return (
      <span className="inline-flex items-center gap-1 text-[11px] text-indigo-400">
        <Loader2 className="w-3 h-3 animate-spin" />
        Responding...
      </span>
    );
  }
  if (status === "waiting") {
    return (
      <span className="inline-flex items-center gap-1 text-[11px] text-amber-400">
        <PauseCircle className="w-3 h-3" />
        Waiting for input
      </span>
    );
  }
  if (status === "failed") {
    return (
      <span className="inline-flex items-center gap-1 text-[11px] text-rose-400">
        <AlertCircle className="w-3 h-3" />
        Failed
      </span>
    );
  }
  return null;
}

export function ChatThread({
  messages,
  isRunning,
  isWaitingApproval,
  isApprovalSubmitting,
  approvalError,
  onResolveApproval,
  isWaitingQuestion = false,
  isQuestionSubmitting = false,
  questionError = null,
  onAnswerQuestion = () => undefined,
}: ChatThreadProps) {
  const { t } = useTranslation();

  const hasMessages = messages.length > 0;

  return (
    <div className="flex-1 overflow-y-auto px-4 py-6">
      <div className="max-w-3xl mx-auto space-y-6">
        {!hasMessages && (
          <div className="flex flex-col items-center justify-center py-20 text-slate-500">
            <p className="text-lg font-medium text-slate-400 mb-1">
              {t("chat.welcomeTitle")}
            </p>
            <p className="text-sm">{t("chat.welcomeSubtitle")}</p>
          </div>
        )}

        {messages.map((message) => {
          const assistantContent =
            message.role === "assistant"
              ? visibleAssistantContent(message.content)
              : "";
          if (message.role === "user") {
            return (
              <div
                key={message.id}
                className="flex items-start gap-3 justify-end"
              >
                <div className="flex-1 flex justify-end">
                  <div className="max-w-[85%]">
                    <div className="bg-indigo-600 text-indigo-50 rounded-2xl rounded-tr-sm px-4 py-3">
                      <p className="text-sm leading-relaxed whitespace-pre-wrap">
                        {message.content}
                      </p>
                    </div>
                  </div>
                </div>
              </div>
            );
          }

          return (
            <div key={message.id} className="flex items-start gap-3">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-1 min-h-4">
                  <StatusIndicator status={message.status} />
                </div>
                <div className="bg-slate-800/40 border border-slate-800 rounded-2xl rounded-tl-sm px-4 py-3">
                  <ThinkingBlock
                    thinking={message.thinking}
                    startedAt={message.thinkingStartedAt}
                    updatedAt={message.thinkingUpdatedAt}
                  />
                  <ToolActivities tools={message.tools} />
                  {assistantContent && (
                    <div className="markdown-body">
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>
                        {assistantContent}
                      </ReactMarkdown>
                    </div>
                  )}
                  {message.approval && isWaitingApproval && (
                    <ApprovalCard
                      approval={message.approval}
                      isSubmitting={isApprovalSubmitting}
                      onResolve={onResolveApproval}
                    />
                  )}
                  {message.question && isWaitingQuestion && (
                    <QuestionCard
                      question={message.question}
                      isSubmitting={isQuestionSubmitting}
                      onAnswer={onAnswerQuestion}
                    />
                  )}
                </div>
              </div>
            </div>
          );
        })}

        {isRunning &&
          messages.length > 0 &&
          messages[messages.length - 1].role === "user" && (
            <div className="flex items-start gap-3">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-1">
                  <StatusIndicator status="in_progress" />
                </div>
                <div className="bg-slate-800/40 border border-slate-800 rounded-2xl rounded-tl-sm px-4 py-3">
                  <div className="flex items-center gap-2 text-sm text-slate-400">
                    <Loader2 className="w-4 h-4 animate-spin" />
                    {t("chat.thinking")}
                  </div>
                </div>
              </div>
            </div>
          )}

        {approvalError && (
          <div className="flex items-start gap-3">
            <div className="flex-1 min-w-0">
              <div className="bg-rose-500/10 border border-rose-500/20 rounded-2xl rounded-tl-sm px-4 py-3 text-sm text-rose-300">
                {t("approval.error", { message: approvalError })}
              </div>
            </div>
          </div>
        )}
        {questionError && (
          <div className="flex items-start gap-3">
            <div className="flex-1 min-w-0">
              <div className="bg-rose-500/10 border border-rose-500/20 rounded-2xl rounded-tl-sm px-4 py-3 text-sm text-rose-300">
                {t("question.error", { message: questionError })}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
