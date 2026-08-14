import { useTranslation } from "react-i18next";
import { ChevronRight, FileCode2, Loader2, X } from "lucide-react";
import { useState } from "react";
import type {
  ProviderContextSegmentSnapshot,
  RuntimeSessionDebugSnapshot,
} from "../lib/runtime/types";
import { ControlButton } from "./ui";

interface ContextPanelProps {
  isOpen: boolean;
  debug: RuntimeSessionDebugSnapshot | null;
  status: "idle" | "loading" | "success" | "error";
  error: string | null;
  onClose: () => void;
  onRefresh: () => void;
}

export function ContextPanel({
  isOpen,
  debug,
  status,
  error,
  onClose,
  onRefresh,
}: ContextPanelProps) {
  const { t } = useTranslation();
  if (!isOpen) return null;

  const providerContext = debug?.provider_context ?? null;

  return (
    <aside className="relative flex h-full w-[420px] flex-shrink-0 flex-col border-l border-[color:var(--vc-border-subtle)] bg-[var(--vc-bg)]">
      <header className="flex flex-shrink-0 items-center justify-between border-b border-[color:var(--vc-border-subtle)] px-4 py-2.5">
        <span className="flex items-center gap-2 text-sm font-medium text-[var(--vc-text-primary)]">
          <FileCode2 className="h-4 w-4" />
          {t("context.title")}
        </span>
        <div className="flex items-center gap-1">
          <ControlButton
            compact
            variant="ghost"
            onClick={onRefresh}
            aria-label={t("context.refresh")}
          >
            <Loader2
              className={`h-4 w-4 ${status === "loading" ? "animate-spin" : ""}`}
            />
          </ControlButton>
          <ControlButton
            compact
            variant="ghost"
            onClick={onClose}
            aria-label={t("context.close")}
          >
            <X className="h-4 w-4" />
          </ControlButton>
        </div>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
        {status === "loading" && !debug ? (
          <p className="text-sm text-[var(--vc-text-muted)]">
            {t("context.loading")}
          </p>
        ) : error ? (
          <p className="text-sm text-[var(--vc-danger-text)]">{error}</p>
        ) : !providerContext ? (
          <p className="text-sm text-[var(--vc-text-muted)]">
            {t("context.unavailable")}
          </p>
        ) : (
          <div className="space-y-4">
            <ContextMeta
              provider={providerContext.provider}
              model={providerContext.model}
              segmentCount={providerContext.segment_count}
              messageCount={providerContext.message_count}
            />
            <ContextWindowPayload payload={providerContext.context_window} />
            <SegmentList segments={providerContext.segments} />
            <DiagnosticList diagnostics={providerContext.diagnostics} />
          </div>
        )}
      </div>
    </aside>
  );
}

function ContextMeta({
  provider,
  model,
  segmentCount,
  messageCount,
}: {
  provider: string;
  model: string;
  segmentCount: number;
  messageCount: number;
}) {
  return (
    <div className="rounded-lg border border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] p-3 text-xs">
      <div className="font-mono text-[var(--vc-text-primary)]">
        {provider}/{model}
      </div>
      <div className="mt-1 text-[var(--vc-text-muted)]">
        {segmentCount} segments · {messageCount} messages
      </div>
    </div>
  );
}

function ContextWindowPayload({
  payload,
}: {
  payload: Record<string, unknown>;
}) {
  const entries = Object.entries(payload).filter(
    ([, value]) => value !== null && value !== undefined,
  );
  if (entries.length === 0) return null;
  return (
    <Section title="context.contextWindow">
      <dl className="space-y-1">
        {entries.map(([key, value]) => (
          <div key={key} className="flex justify-between gap-3">
            <dt className="text-[var(--vc-text-muted)]">{key}</dt>
            <dd className="truncate font-mono text-[var(--vc-text-primary)]">
              {typeof value === "object"
                ? JSON.stringify(value)
                : String(value)}
            </dd>
          </div>
        ))}
      </dl>
    </Section>
  );
}

function SegmentList({
  segments,
}: {
  segments: ProviderContextSegmentSnapshot[];
}) {
  if (segments.length === 0) return null;
  return (
    <Section title="context.segments" count={segments.length}>
      <ul className="space-y-2">
        {segments.map((segment) => (
          <SegmentRow key={segment.index} segment={segment} />
        ))}
      </ul>
    </Section>
  );
}

function SegmentRow({ segment }: { segment: ProviderContextSegmentSnapshot }) {
  const [expanded, setExpanded] = useState(false);
  const content = segment.content ?? "";
  const preview = content.length > 120 ? `${content.slice(0, 120)}…` : content;
  return (
    <li className="rounded-lg border border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] p-2">
      <button
        type="button"
        onClick={() => setExpanded((value) => !value)}
        className="flex w-full items-center gap-2 text-left"
      >
        <ChevronRight
          className={`h-3.5 w-3.5 shrink-0 text-[var(--vc-text-muted)] transition-transform ${expanded ? "rotate-90" : ""}`}
        />
        <span className="shrink-0 rounded bg-[var(--vc-surface-2)] px-1.5 py-0.5 font-mono text-[10px] uppercase text-[var(--vc-text-subtle)]">
          {segment.role}
        </span>
        <span className="shrink-0 text-[10px] text-[var(--vc-text-muted)]">
          {segment.source}
        </span>
        {segment.tool_name ? (
          <span className="shrink-0 font-mono text-[10px] text-[var(--vc-text-subtle)]">
            {segment.tool_name}
          </span>
        ) : null}
        <span className="min-w-0 flex-1 truncate text-xs text-[var(--vc-text-muted)]">
          {preview}
        </span>
        {segment.content_truncated ? (
          <span className="shrink-0 text-[10px] text-[var(--vc-text-subtle)]">
            …
          </span>
        ) : null}
      </button>
      {expanded ? (
        <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-[var(--vc-surface-2)] p-2 font-mono text-[11px] leading-relaxed text-[var(--vc-text-primary)]">
          {content || "(empty)"}
        </pre>
      ) : null}
    </li>
  );
}

function DiagnosticList({
  diagnostics,
}: {
  diagnostics: Record<string, unknown>[];
}) {
  if (diagnostics.length === 0) return null;
  return (
    <Section title="context.diagnostics" count={diagnostics.length}>
      <ul className="space-y-1.5">
        {diagnostics.map((diagnostic, index) => {
          const severity = String(diagnostic.severity ?? "info");
          const code = String(diagnostic.code ?? "");
          const message = String(diagnostic.message ?? "");
          return (
            <li
              key={index}
              className="rounded border border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] p-2 text-xs"
            >
              <span className="font-mono text-[var(--vc-text-subtle)]">
                [{severity}] {code}
              </span>
              <span className="ml-2 text-[var(--vc-text-muted)]">
                {message}
              </span>
            </li>
          );
        })}
      </ul>
    </Section>
  );
}

function Section({
  title,
  count,
  children,
}: {
  title: string;
  count?: number;
  children: React.ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <section>
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-[var(--vc-text-muted)]">
        {t(title)}
        {typeof count === "number" ? ` (${count})` : ""}
      </h3>
      {children}
    </section>
  );
}
