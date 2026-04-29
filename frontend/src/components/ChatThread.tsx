import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useTranslation } from "react-i18next";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  ChevronDown,
  ChevronRight,
  Loader2,
  PauseCircle,
  AlertCircle,
  Copy,
  Check,
} from "lucide-react";
import { ChatMessage } from "../lib/runtime/event-parser";
import type { QuestionAnswer } from "../lib/runtime/types";
import {
  estimateStreamedTextHeight,
  STREAM_TEXT_ESTIMATE_WIDTH,
} from "../lib/runtime/text-layout";
import { ControlButton } from "./ui";

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

function StreamingMarkdown({
  content,
  active,
}: {
  content: string;
  active: boolean;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [measuredWidth, setMeasuredWidth] = useState(
    STREAM_TEXT_ESTIMATE_WIDTH,
  );

  useEffect(() => {
    if (!active) return;
    const element = containerRef.current;
    if (!element || typeof ResizeObserver === "undefined") return;

    const updateWidth = () => {
      const nextWidth = Math.round(element.getBoundingClientRect().width);
      if (nextWidth > 0) setMeasuredWidth(nextWidth);
    };
    updateWidth();

    const observer = new ResizeObserver(updateWidth);
    observer.observe(element);
    return () => observer.disconnect();
  }, [active]);

  const estimatedHeight = useMemo(
    () => (active ? estimateStreamedTextHeight(content, measuredWidth) : 0),
    [active, content, measuredWidth],
  );

  return (
    <div
      ref={containerRef}
      className="markdown-body"
      data-pretext-estimated-height={estimatedHeight || undefined}
      style={estimatedHeight > 0 ? { minHeight: estimatedHeight } : undefined}
    >
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
    </div>
  );
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
  const panelId = useId();
  const content = useMemo(() => thinking.join(""), [thinking]);
  const duration = formatThinkingDuration(startedAt, updatedAt);

  if (thinking.length === 0) return null;

  return (
    <div className="mb-3">
      <ControlButton
        compact
        variant="ghost"
        onClick={() => setExpanded(!expanded)}
        className="mb-1 justify-start px-0 text-[var(--vc-text-subtle)]"
        aria-expanded={expanded}
        aria-controls={panelId}
      >
        {expanded ? (
          <ChevronDown className="w-3.5 h-3.5" />
        ) : (
          <ChevronRight className="w-3.5 h-3.5" />
        )}
        <span className="font-medium">Thinking</span>
        {duration && (
          <span className="text-[var(--vc-text-subtle)]">({duration})</span>
        )}
      </ControlButton>
      {expanded && (
        <div
          id={panelId}
          className="bg-[var(--vc-surface-1)] border border-[color:var(--vc-border-subtle)] rounded-lg p-3 font-mono text-xs text-[var(--vc-text-muted)] leading-relaxed overflow-x-auto"
        >
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

function resultData(tool: ChatTool) {
  return nestedRecord(tool.result, "data") ?? tool.result;
}

const CONTEXT_TOOL_NAMES = new Set([
  "read",
  "read_file",
  "list",
  "glob",
  "grep",
  "code_search",
  "ast_grep_search",
]);

const SENSITIVE_ARG_KEYS = new Set([
  "content",
  "oldString",
  "newString",
  "edits",
  "todos",
  "data_uri",
  "patch",
  "internalState",
  "internalData",
]);

function isContextTool(tool: ChatTool) {
  return CONTEXT_TOOL_NAMES.has(tool.name);
}

function toolDisplayTitle(tool: ChatTool, fallback: string) {
  return (
    tool.label ??
    tool.summary ??
    tool.display?.summary ??
    tool.legacy?.summary ??
    tool.display?.title ??
    fallback
  );
}

function toolDisplaySubtitle(tool: ChatTool, fallback?: string) {
  const subtitle = tool.display?.title ?? tool.legacy?.summary ?? fallback;
  const title = toolDisplayTitle(tool, tool.name);
  return subtitle && subtitle !== title ? subtitle : undefined;
}

function primitiveArgs(
  args: Record<string, unknown> | undefined,
  omit: string[] = [],
  limit = 3,
) {
  if (!args) return [];
  return Object.entries(args)
    .filter(
      ([key, value]) =>
        !omit.includes(key) &&
        !SENSITIVE_ARG_KEYS.has(key) &&
        toolValue(value) !== null,
    )
    .slice(0, limit)
    .map(([key, value]) => `${key}=${toolValue(value)}`);
}

function curatedArgs(
  tool: ChatTool,
  options: { omit?: string[]; limit?: number } = {},
) {
  const limit = options.limit ?? 3;
  if (tool.display?.args && tool.display.args.length > 0) {
    return tool.display.args.slice(0, limit);
  }
  return primitiveArgs(tool.arguments, options.omit, limit);
}

function copyableValue(tool: ChatTool, key: string) {
  const copyable = tool.copyable ?? tool.display?.copyable;
  return toolValue(copyable?.[key]);
}

function CopyButton({ value, label }: { value: string; label: string }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const timeoutId = window.setTimeout(() => setCopied(false), 1400);
    return () => window.clearTimeout(timeoutId);
  }, [copied]);

  const handleCopy = async () => {
    if (!navigator.clipboard?.writeText) return;
    await navigator.clipboard.writeText(value);
    setCopied(true);
  };

  return (
    <button
      type="button"
      onClick={handleCopy}
      aria-label={t("tool.copyAria", { label })}
      title={copied ? t("tool.copied") : t("tool.copy")}
      className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-[var(--vc-radius-control)] text-[var(--vc-text-subtle)] transition-colors hover:bg-[var(--vc-surface-2)] hover:text-[var(--vc-text-primary)] focus:outline-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--vc-focus-ring)]"
    >
      {copied ? (
        <Check className="h-3.5 w-3.5" />
      ) : (
        <Copy className="h-3.5 w-3.5" />
      )}
      <span className="sr-only" aria-live="polite">
        {copied ? t("tool.copied") : t("tool.copy")}
      </span>
    </button>
  );
}

function ToolDetailBlock({
  label,
  value,
  copyValue,
}: {
  label: string;
  value: string | null;
  copyValue?: string | null;
}) {
  if (!value) return null;
  const preview = value.length > 4000 ? `${value.slice(0, 4000)}\n…` : value;
  return (
    <div className="mt-2 text-xs text-[var(--vc-text-muted)]">
      <div className="mb-1 flex items-center justify-between gap-2 text-[11px] text-[var(--vc-text-subtle)]">
        <span>{label}</span>
        {copyValue && <CopyButton value={copyValue} label={label} />}
      </div>
      <pre className="max-h-72 overflow-auto rounded-[var(--vc-radius-control)] border border-[color:var(--vc-border-subtle)] bg-[var(--vc-bg)] p-3 text-xs leading-relaxed text-[var(--vc-text-primary)] whitespace-pre-wrap">
        {preview}
      </pre>
    </div>
  );
}

function ToolDisclosureRow({
  tool,
  title,
  subtitle,
  args = [],
  defaultExpanded = false,
  children,
}: {
  tool: ChatTool;
  title: string;
  subtitle?: string;
  args?: string[];
  defaultExpanded?: boolean;
  children?: ReactNode;
}) {
  const { t } = useTranslation();
  const panelId = useId();
  const [expanded, setExpanded] = useState(defaultExpanded);
  const canExpand = Boolean(children);
  const statusLabel =
    tool.status === "completed" ? null : t(`tool.status.${tool.status}`);
  const trailing = [statusLabel, ...args].filter(Boolean).join(" · ");

  const summary = (
    <span className="flex min-w-0 flex-1 items-baseline gap-2 text-left">
      <span className="shrink-0 text-xs font-medium text-[var(--vc-text-primary)]">
        {title}
      </span>
      {subtitle && (
        <span className="min-w-0 truncate text-xs text-[var(--vc-text-muted)]">
          {subtitle}
        </span>
      )}
      {trailing && (
        <span className="hidden shrink-0 text-[11px] text-[var(--vc-text-subtle)] md:inline">
          {trailing}
        </span>
      )}
    </span>
  );

  return (
    <div className="text-xs" data-tool-row={tool.name}>
      {canExpand ? (
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          className="group flex w-full items-center gap-1.5 px-1 py-1 text-left transition-colors hover:text-[var(--vc-text-primary)] focus:outline-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--vc-focus-ring)]"
          aria-expanded={expanded}
          aria-controls={panelId}
          aria-label={t(
            expanded ? "tool.hideDetailsFor" : "tool.showDetailsFor",
            {
              title,
            },
          )}
        >
          {expanded ? (
            <ChevronDown className="h-3 w-3 shrink-0 text-[var(--vc-text-subtle)]" />
          ) : (
            <ChevronRight className="h-3 w-3 shrink-0 text-[var(--vc-text-subtle)]" />
          )}
          {summary}
        </button>
      ) : (
        <div className="flex items-center gap-1.5 px-1 py-1">
          <span className="h-3 w-3 shrink-0" />
          {summary}
        </div>
      )}
      {canExpand && expanded && (
        <div
          id={panelId}
          className="ml-4 pb-2 pt-1 text-[var(--vc-text-muted)]"
        >
          {children}
        </div>
      )}
    </div>
  );
}

function ReadToolActivity({ tool }: { tool: ChatTool }) {
  const path = primaryPath(tool) ?? toolDisplayTitle(tool, tool.name);
  return (
    <ToolDisclosureRow
      tool={tool}
      title={toolDisplayTitle(tool, "Read")}
      subtitle={path}
      args={curatedArgs(tool, { omit: ["path", "filePath"] })}
    />
  );
}

function WriteToolActivity({ tool }: { tool: ChatTool }) {
  const data = resultData(tool);
  const path = primaryPath(tool) ?? toolDisplayTitle(tool, tool.name);
  const diff = toolValue(data?.diff);
  const bytes = toolValue(data?.byte_count);
  return (
    <ToolDisclosureRow
      tool={tool}
      title={toolDisplayTitle(tool, "Write")}
      subtitle={bytes ? `${path} · ${bytes} bytes` : path}
      args={curatedArgs(tool, { omit: ["path", "filePath"] })}
      defaultExpanded
    >
      <ToolDetailBlock label="Diff" value={diff} copyValue={diff} />
      {!diff && tool.content && (
        <ToolDetailBlock
          label="Result"
          value={tool.content}
          copyValue={tool.content}
        />
      )}
      {tool.error && (
        <ToolDetailBlock
          label="Error"
          value={tool.error}
          copyValue={tool.error}
        />
      )}
    </ToolDisclosureRow>
  );
}

function ShellToolActivity({ tool }: { tool: ChatTool }) {
  const { t } = useTranslation();
  const data = resultData(tool);
  const command =
    toolValue(tool.arguments?.command) ??
    toolValue(data?.command) ??
    copyableValue(tool, "command") ??
    "shell";
  const stdout =
    toolValue(data?.stdout) ?? toolValue(data?.output) ?? tool.content;
  const stderr = toolValue(data?.stderr);
  const exitCode =
    toolValue(data?.exit_code) ??
    toolValue(data?.exitCode) ??
    toolValue(data?.code);
  const error = tool.error ?? toolValue(data?.error);
  const title = t("tool.shell.title");
  const summary = toolDisplayTitle(tool, command);
  const subtitle =
    exitCode && exitCode !== "0"
      ? `${summary} · ${t("tool.shell.failedSubtitle", { code: exitCode })}`
      : summary;
  return (
    <ToolDisclosureRow
      tool={tool}
      title={title}
      subtitle={subtitle}
      args={[]}
      defaultExpanded={tool.status === "running"}
    >
      <ShellTerminalBlock
        command={command}
        stdout={stdout ?? null}
        stderr={stderr}
        error={error}
        exitCode={exitCode}
        copyCommand={copyableValue(tool, "command") ?? command}
        copyOutput={
          copyableValue(tool, "output") ?? stdout ?? stderr ?? error ?? null
        }
      />
    </ToolDisclosureRow>
  );
}

function ShellTerminalBlock({
  command,
  stdout,
  stderr,
  error,
  exitCode,
  copyCommand,
  copyOutput,
}: {
  command: string;
  stdout: string | null;
  stderr: string | null;
  error: string | null;
  exitCode: string | null;
  copyCommand: string;
  copyOutput: string | null;
}) {
  const { t } = useTranslation();
  const transcript = [
    `$ ${command}`,
    stdout,
    stderr,
    error,
    exitCode && exitCode !== "0" ? `exit ${exitCode}` : null,
  ]
    .filter((value): value is string => Boolean(value))
    .join("\n");

  return (
    <div
      className="rounded-[var(--vc-radius-control)] border border-[color:var(--vc-border-subtle)] bg-[var(--vc-bg)]"
      data-terminal-block="shell"
    >
      <div className="flex items-center justify-between border-b border-[color:var(--vc-border-subtle)] px-2 py-1">
        <span className="text-[10px] uppercase tracking-wide text-[var(--vc-text-subtle)]">
          {t("tool.shell.terminal")}
        </span>
        <span className="flex items-center gap-1">
          <CopyButton value={copyCommand} label={t("tool.shell.command")} />
          {copyOutput && (
            <CopyButton value={copyOutput} label={t("tool.shell.output")} />
          )}
        </span>
      </div>
      <pre className="max-h-72 overflow-auto p-3 font-mono text-xs leading-relaxed text-[var(--vc-text-primary)] whitespace-pre-wrap">
        {transcript}
      </pre>
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
    <ToolDisclosureRow
      tool={tool}
      title={toolDisplayTitle(tool, "Loaded skill")}
      subtitle={name}
      args={curatedArgs(tool, { omit: ["name", "user_message"] })}
      defaultExpanded
    >
      {description && (
        <div className="mt-2 text-[var(--vc-text-muted)]">{description}</div>
      )}
      {sourcePath && (
        <div className="mt-2 text-[11px] text-[var(--vc-text-subtle)]">
          Source: <code>{sourcePath}</code>
        </div>
      )}
      {userMessage && (
        <ToolDetailBlock
          label="Context"
          value={userMessage}
          copyValue={userMessage}
        />
      )}
      {tool.error && (
        <ToolDetailBlock
          label="Error"
          value={tool.error}
          copyValue={tool.error}
        />
      )}
    </ToolDisclosureRow>
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
    <ToolDisclosureRow
      tool={tool}
      title={toolDisplayTitle(tool, "Started subagent")}
      subtitle={`${route} · ${mode}`}
      args={curatedArgs(tool, {
        omit: ["category", "subagent_type", "description"],
      })}
      defaultExpanded
    >
      {description && (
        <div className="mt-2 text-[var(--vc-text-muted)]">{description}</div>
      )}
      <div className="mt-2 grid gap-1 text-[11px] text-[var(--vc-text-subtle)]">
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
      {tool.content && (
        <ToolDetailBlock
          label="Output"
          value={tool.content}
          copyValue={tool.content}
        />
      )}
      {tool.error && (
        <ToolDetailBlock
          label="Error"
          value={tool.error}
          copyValue={tool.error}
        />
      )}
    </ToolDisclosureRow>
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
  if (tool.display?.hidden) return null;
  const items = todoItems(tool);
  const data = resultData(tool);
  const summary = nestedRecord(data, "summary");
  const summaryText = summary
    ? ["in_progress", "pending", "completed", "cancelled"]
        .map((key) => `${key}=${toolValue(summary[key]) ?? "0"}`)
        .join(", ")
    : `${items.length} todos`;

  return (
    <ToolDisclosureRow
      tool={tool}
      title={toolDisplayTitle(tool, "Updated todos")}
      subtitle={summaryText}
      defaultExpanded={false}
    >
      <div className="mt-2 space-y-1">
        {items.map((item, index) => (
          <div
            key={`${item.content}-${index}`}
            className="flex items-start gap-2 text-[var(--vc-text-muted)]"
          >
            <span className="mt-px text-[var(--vc-text-subtle)]">
              {todoStatusSymbol(item.status)}
            </span>
            <span className="flex-1">{item.content}</span>
            <span className="rounded-[var(--vc-radius-control)] border border-[color:var(--vc-border-subtle)] px-1.5 py-0.5 text-[10px] uppercase text-[var(--vc-text-subtle)]">
              {item.status} · {item.priority}
            </span>
          </div>
        ))}
      </div>
      {tool.error && (
        <ToolDetailBlock
          label="Error"
          value={tool.error}
          copyValue={tool.error}
        />
      )}
    </ToolDisclosureRow>
  );
}

function GenericToolActivity({ tool }: { tool: ChatTool }) {
  return (
    <ToolDisclosureRow
      tool={tool}
      title={toolDisplayTitle(tool, tool.display?.title ?? tool.name)}
      subtitle={toolDisplaySubtitle(tool, tool.summary)}
      args={curatedArgs(tool)}
    />
  );
}

function ContextToolGroup({ tools }: { tools: ChatTool[] }) {
  const { t } = useTranslation();
  const panelId = useId();
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="text-xs" data-tool-row="context-group">
      <button
        type="button"
        onClick={() => setExpanded((value) => !value)}
        className="flex w-full items-baseline gap-1.5 px-1 py-1 text-left transition-colors hover:text-[var(--vc-text-primary)] focus:outline-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--vc-focus-ring)]"
        aria-expanded={expanded}
        aria-controls={panelId}
        aria-label={t(
          expanded ? "tool.hideDetailsFor" : "tool.showDetailsFor",
          {
            title: t("tool.context.title"),
          },
        )}
      >
        {expanded ? (
          <ChevronDown className="h-3 w-3 shrink-0 self-center text-[var(--vc-text-subtle)]" />
        ) : (
          <ChevronRight className="h-3 w-3 shrink-0 self-center text-[var(--vc-text-subtle)]" />
        )}
        <span className="shrink-0 text-xs font-medium text-[var(--vc-text-primary)]">
          {t("tool.context.title")}
        </span>
        <span className="min-w-0 truncate text-xs text-[var(--vc-text-muted)]">
          {t("tool.context.summary", { count: tools.length })}
        </span>
      </button>
      {expanded && (
        <div id={panelId} className="ml-4 space-y-1 py-1">
          {tools.map((tool, index) => {
            const path = primaryPath(tool);
            const title = toolDisplayTitle(
              tool,
              tool.display?.title ?? tool.name,
            );
            const args = curatedArgs(tool, { omit: ["path", "filePath"] });
            return (
              <div
                key={tool.id ?? `${tool.name}-${index}`}
                className="flex items-baseline gap-2 px-1 py-0.5"
              >
                <span className="min-w-0 flex flex-1 items-baseline gap-2">
                  <span className="shrink-0 text-[11px] font-medium text-[var(--vc-text-primary)]">
                    {title}
                  </span>
                  {path && (
                    <span className="truncate font-mono text-[10px] text-[var(--vc-text-subtle)]">
                      {path}
                    </span>
                  )}
                </span>
                {args.slice(0, 2).map((arg) => (
                  <span
                    key={arg}
                    className="hidden max-w-[9rem] truncate font-mono text-[10px] text-[var(--vc-text-muted)] md:inline"
                  >
                    {arg}
                  </span>
                ))}
              </div>
            );
          })}
        </div>
      )}
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
    <div className="mt-3 rounded-[var(--vc-radius-control)] border border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] p-4">
      <div className="flex items-center gap-2 mb-3">
        <PauseCircle className="w-5 h-5 text-[var(--vc-text-subtle)] flex-shrink-0" />
        <div>
          <p className="text-sm font-medium text-[var(--vc-text-primary)]">
            {t("question.heading")}
          </p>
          <p className="mt-0.5 text-sm text-[var(--vc-text-muted)]">
            {t("question.message", {
              tool: question.tool || t("question.unknownTool"),
            })}
          </p>
        </div>
      </div>
      <div className="space-y-3">
        {prompts.map((prompt) => (
          <div key={prompt.header} className="block">
            <span className="text-xs font-medium text-[var(--vc-text-primary)]">
              {prompt.header}
            </span>
            {prompt.question && (
              <span className="mt-1 block text-xs text-[var(--vc-text-muted)]">
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
                    className="flex items-start gap-2 rounded-[var(--vc-radius-control)] border border-[color:var(--vc-border-subtle)] bg-[var(--vc-bg)] px-3 py-2 text-sm text-[var(--vc-text-primary)]"
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
                        <span className="mt-0.5 block text-xs text-[var(--vc-text-subtle)]">
                          {option.description}
                        </span>
                      )}
                    </span>
                  </label>
                );
              })}
            </div>
          </div>
        ))}
      </div>
      <div className="mt-4 flex justify-end">
        <ControlButton
          variant="primary"
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
        >
          {isSubmitting ? t("question.submitting") : t("question.submit")}
        </ControlButton>
      </div>
    </div>
  );
}

function ToolActivities({ tools }: { tools: ChatTool[] }) {
  if (tools.length === 0) return null;

  const contextTools = tools.filter(isContextTool);
  const shouldGroupContext = contextTools.length > 1;
  const renderedItems: ReactNode[] = [];
  let didRenderContextGroup = false;

  tools.forEach((tool, idx) => {
    if (shouldGroupContext && isContextTool(tool)) {
      if (!didRenderContextGroup) {
        renderedItems.push(
          <ContextToolGroup key="context-tool-group" tools={contextTools} />,
        );
        didRenderContextGroup = true;
      }
      return;
    }

    renderedItems.push(
      <ToolActivity key={tool.id ?? `${tool.name}-${idx}`} tool={tool} />,
    );
  });

  return <div className="mb-3 space-y-2">{renderedItems}</div>;
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
    <div className="mt-3 rounded-[var(--vc-radius-control)] border border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] p-4">
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div className="flex items-center gap-2">
          <PauseCircle className="w-5 h-5 text-[var(--vc-text-subtle)] flex-shrink-0" />
          <div>
            <p className="text-sm font-medium text-[var(--vc-text-primary)]">
              {t("approval.heading")}
            </p>
            <p className="mt-0.5 text-sm text-[var(--vc-text-muted)]">
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
          <ControlButton
            variant="danger"
            onClick={() => onResolve("deny")}
            disabled={isSubmitting}
          >
            {isSubmitting ? t("approval.submitting") : t("approval.deny")}
          </ControlButton>
          <ControlButton
            variant="confirm"
            onClick={() => onResolve("allow")}
            disabled={isSubmitting}
          >
            {isSubmitting ? t("approval.submitting") : t("approval.allow")}
          </ControlButton>
        </div>
      </div>
    </div>
  );
}

function StatusIndicator({ status }: { status: ChatMessage["status"] }) {
  if (status === "in_progress") {
    return (
      <span className="inline-flex items-center gap-1 text-[11px] text-[var(--vc-text-muted)]">
        <Loader2 className="w-3 h-3 animate-spin" />
        Responding...
      </span>
    );
  }
  if (status === "waiting") {
    return (
      <span className="inline-flex items-center gap-1 text-[11px] text-[var(--vc-text-muted)]">
        <PauseCircle className="w-3 h-3" />
        Waiting for input
      </span>
    );
  }
  if (status === "failed") {
    return (
      <span className="inline-flex items-center gap-1 text-[11px] text-[var(--vc-danger-text)]">
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
          <div className="flex flex-col items-center justify-center py-20 text-[var(--vc-text-subtle)]">
            <p className="text-lg font-medium text-[var(--vc-text-muted)] mb-1">
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
                    <div className="rounded-2xl rounded-tr-sm border border-[color:var(--vc-border-strong)] bg-[var(--vc-surface-2)] px-4 py-3 text-[var(--vc-text-primary)]">
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
                <div className="space-y-3">
                  <ThinkingBlock
                    thinking={message.thinking}
                    startedAt={message.thinkingStartedAt}
                    updatedAt={message.thinkingUpdatedAt}
                  />
                  <ToolActivities tools={message.tools} />
                  {assistantContent && (
                    <StreamingMarkdown
                      content={assistantContent}
                      active={message.status === "in_progress"}
                    />
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
                <div className="flex items-center gap-2 text-sm text-[var(--vc-text-muted)]">
                  <Loader2 className="w-4 h-4 animate-spin" />
                  {t("chat.thinking")}
                </div>
              </div>
            </div>
          )}

        {approvalError && (
          <div className="flex items-start gap-3">
            <div className="flex-1 min-w-0">
              <div className="rounded-2xl rounded-tl-sm border border-[color:var(--vc-danger-border)] bg-[var(--vc-danger-bg)] px-4 py-3 text-sm text-[var(--vc-danger-text)]">
                {t("approval.error", { message: approvalError })}
              </div>
            </div>
          </div>
        )}
        {questionError && (
          <div className="flex items-start gap-3">
            <div className="flex-1 min-w-0">
              <div className="rounded-2xl rounded-tl-sm border border-[color:var(--vc-danger-border)] bg-[var(--vc-danger-bg)] px-4 py-3 text-sm text-[var(--vc-danger-text)]">
                {t("question.error", { message: questionError })}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
