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
  User,
  Bot,
} from "lucide-react";
import { ChatMessage } from "../lib/runtime/event-parser";

interface ChatThreadProps {
  messages: ChatMessage[];
  isRunning: boolean;
  isWaitingApproval: boolean;
  isApprovalSubmitting: boolean;
  approvalError: string | null;
  onResolveApproval: (decision: "allow" | "deny") => void;
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

function ToolIndicators({
  tools,
}: {
  tools: { name: string; status: string }[];
}) {
  if (tools.length === 0) return null;

  return (
    <div className="flex flex-wrap gap-1.5 mb-3">
      {tools.map((tool, idx) => (
        <span
          key={`${tool.name}-${idx}`}
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
          {tool.name}
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
        Waiting for approval
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
}: ChatThreadProps) {
  const { t } = useTranslation();

  const hasMessages = messages.length > 0;

  return (
    <div className="flex-1 overflow-y-auto px-4 py-6">
      <div className="max-w-3xl mx-auto space-y-6">
        {!hasMessages && (
          <div className="flex flex-col items-center justify-center py-20 text-slate-500">
            <Bot className="w-12 h-12 mb-4 text-slate-600" />
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
                  <div className="bg-indigo-600 text-indigo-50 rounded-2xl rounded-tr-sm px-4 py-3 max-w-[85%]">
                    <p className="text-sm leading-relaxed whitespace-pre-wrap">
                      {message.content}
                    </p>
                  </div>
                </div>
                <div className="w-7 h-7 rounded-full bg-indigo-500/20 flex items-center justify-center flex-shrink-0 mt-0.5">
                  <User className="w-4 h-4 text-indigo-400" />
                </div>
              </div>
            );
          }

          return (
            <div key={message.id} className="flex items-start gap-3">
              <div className="w-7 h-7 rounded-full bg-emerald-500/20 flex items-center justify-center flex-shrink-0 mt-0.5">
                <Bot className="w-4 h-4 text-emerald-400" />
              </div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-xs font-medium text-slate-300">
                    {t("chat.assistantName")}
                  </span>
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
                </div>
              </div>
            </div>
          );
        })}

        {isRunning &&
          messages.length > 0 &&
          messages[messages.length - 1].role === "user" && (
            <div className="flex items-start gap-3">
              <div className="w-7 h-7 rounded-full bg-emerald-500/20 flex items-center justify-center flex-shrink-0 mt-0.5">
                <Bot className="w-4 h-4 text-emerald-400" />
              </div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-xs font-medium text-slate-300">
                    {t("chat.assistantName")}
                  </span>
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
            <div className="w-7 h-7 rounded-full bg-rose-500/20 flex items-center justify-center flex-shrink-0 mt-0.5">
              <AlertCircle className="w-4 h-4 text-rose-400" />
            </div>
            <div className="flex-1 min-w-0">
              <div className="bg-rose-500/10 border border-rose-500/20 rounded-2xl rounded-tl-sm px-4 py-3 text-sm text-rose-300">
                {t("approval.error", { message: approvalError })}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
