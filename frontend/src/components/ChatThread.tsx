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

function ThinkingBlock({ thinking }: { thinking: string[] }) {
  const [expanded, setExpanded] = useState(false);
  const content = useMemo(() => thinking.join(""), [thinking]);

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
        <span className="text-slate-600">
          ({thinking.length} {thinking.length === 1 ? "step" : "steps"})
        </span>
      </button>
      {expanded && (
        <div className="bg-slate-900/60 border border-slate-800 rounded-lg p-3 font-mono text-xs text-slate-400 leading-relaxed overflow-x-auto">
          {content}
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

function ToolIndicators({
  tools,
}: {
  tools: { id?: string; name: string; label?: string; status: string }[];
}) {
  if (tools.length === 0) return null;

  return (
    <div className="flex flex-wrap gap-1.5 mb-3">
      {tools.map((tool, idx) => (
        <span
          key={tool.id ?? `${tool.name}-${idx}`}
          className={`inline-flex items-center gap-1 px-2 py-1 rounded-md text-[11px] font-medium border ${
            tool.status === "running"
              ? "bg-indigo-500/10 text-indigo-400 border-indigo-500/20"
              : tool.status === "completed"
                ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/20"
                : tool.status === "failed"
                  ? "bg-rose-500/10 text-rose-400 border-rose-500/20"
                  : "bg-slate-800 text-slate-400 border-slate-700"
          }`}
        >
          <Wrench className="w-3 h-3" />
          {tool.label ?? tool.name}
          {tool.status === "running" && (
            <Loader2 className="w-3 h-3 animate-spin" />
          )}
          {tool.status === "completed" && <CheckCircle2 className="w-3 h-3" />}
          {tool.status === "failed" && <XCircle className="w-3 h-3" />}
        </span>
      ))}
    </div>
  );
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
                  <ThinkingBlock thinking={message.thinking} />
                  <ToolIndicators tools={message.tools} />
                  {message.content && (
                    <div className="prose prose-invert prose-sm max-w-none prose-pre:bg-slate-900 prose-pre:border prose-pre:border-slate-800">
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>
                        {message.content}
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
