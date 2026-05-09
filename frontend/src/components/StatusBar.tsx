import { useId, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Activity,
  Boxes,
  ChevronDown,
  Loader2,
  Network,
  RefreshCw,
  Server,
} from "lucide-react";
import type {
  CapabilityStatusSnapshot,
  LspServerStatusDetail,
  McpServerStatusDetail,
  RuntimeStatusSnapshot,
} from "../lib/runtime/types";
import { ControlButton } from "./ui";

interface StatusBarProps {
  snapshot: RuntimeStatusSnapshot | null;
  status: "idle" | "loading" | "success" | "error";
  error: string | null;
  mcpRetryStatus?: "idle" | "loading" | "success" | "error";
  mcpRetryError?: string | null;
  onRetryMcp?: () => void;
}

type CapabilityStateKey =
  | "running"
  | "stopped"
  | "failed"
  | "unconfigured"
  | "starting"
  | "disabled"
  | "unknown";

type Tone = "ok" | "warn" | "error" | "muted";

type CategoryKey = "server" | "lsp" | "mcp";

function toneFromState(state: string | undefined): Tone {
  if (!state) return "muted";
  if (state === "running") return "ok";
  if (state === "starting") return "warn";
  if (state === "failed") return "error";
  if (state === "stopped" || state === "disabled" || state === "unconfigured") {
    return "muted";
  }
  return "muted";
}

function dotClassFromTone(tone: Tone): string {
  switch (tone) {
    case "ok":
      return "bg-[var(--vc-confirm-text)]";
    case "warn":
      return "bg-amber-400";
    case "error":
      return "bg-[var(--vc-danger-text)]";
    default:
      return "bg-[var(--vc-text-subtle)]";
  }
}

function badgeClassFromTone(tone: Tone): string {
  switch (tone) {
    case "ok":
      return "border-[color:var(--vc-confirm-text)]/40 text-[var(--vc-confirm-text)] bg-[color:var(--vc-confirm-text)]/10";
    case "warn":
      return "border-amber-400/40 text-amber-300 bg-amber-400/10";
    case "error":
      return "border-[color:var(--vc-danger-border)] text-[var(--vc-danger-text)] bg-[var(--vc-danger-bg)]";
    default:
      return "border-[color:var(--vc-border-subtle)] text-[var(--vc-text-subtle)] bg-[var(--vc-surface-1)]";
  }
}

function severityRank(tone: Tone): number {
  if (tone === "error") return 3;
  if (tone === "warn") return 2;
  if (tone === "ok") return 1;
  return 0;
}

function aggregateTone(...tones: Tone[]): Tone {
  return tones.reduce(
    (worst, candidate) =>
      severityRank(candidate) > severityRank(worst) ? candidate : worst,
    "muted" as Tone,
  );
}

function lspServers(details: Record<string, unknown> | undefined) {
  if (!details) return [];
  const servers = details.servers;
  if (!Array.isArray(servers)) return [];
  return servers as LspServerStatusDetail[];
}

function mcpServers(details: Record<string, unknown> | undefined) {
  if (!details) return [];
  const servers = details.servers;
  if (!Array.isArray(servers)) return [];
  return servers as McpServerStatusDetail[];
}

function CategoryTab({
  active,
  onSelect,
  label,
  tone,
  count,
  icon,
  ariaControls,
}: {
  active: boolean;
  onSelect: () => void;
  label: string;
  tone: Tone;
  count?: number;
  icon: React.ReactNode;
  ariaControls: string;
}) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      aria-controls={ariaControls}
      onClick={onSelect}
      className={`group flex items-center gap-1.5 rounded-md px-2 py-1.5 text-[11px] font-medium transition-colors ${
        active
          ? "bg-[var(--vc-surface-2)] text-[var(--vc-text-primary)]"
          : "text-[var(--vc-text-muted)] hover:bg-[var(--vc-surface-1)] hover:text-[var(--vc-text-primary)]"
      }`}
    >
      <span className="flex h-3.5 w-3.5 items-center justify-center text-[var(--vc-text-subtle)]">
        {icon}
      </span>
      <span>{label}</span>
      <span
        aria-hidden="true"
        className={`h-1.5 w-1.5 rounded-full ${dotClassFromTone(tone)}`}
      />
      {typeof count === "number" && count > 0 && (
        <span className="ml-0.5 rounded-full bg-[var(--vc-surface-1)] px-1.5 py-px text-[9px] font-mono text-[var(--vc-text-subtle)]">
          {count}
        </span>
      )}
    </button>
  );
}

function StatusBadge({ tone, label }: { tone: Tone; label: string }) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px] font-medium ${badgeClassFromTone(tone)}`}
    >
      <span
        aria-hidden="true"
        className={`h-1.5 w-1.5 rounded-full ${dotClassFromTone(tone)}`}
      />
      {label}
    </span>
  );
}

function CapabilityHeader({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-xs font-medium uppercase tracking-wide text-[var(--vc-text-muted)]">
        {label}
      </span>
    </div>
  );
}

function ServerRow({
  name,
  tone,
  state,
  detail,
  errorText,
  command,
}: {
  name: string;
  tone: Tone;
  state: string;
  detail?: string | null;
  errorText?: string | null;
  command?: string[];
}) {
  const commandPreview =
    command && command.length > 0 ? command.join(" ") : null;
  return (
    <div className="rounded-md border border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] px-2 py-1.5">
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-[11px] font-medium text-[var(--vc-text-primary)]">
          {name}
        </span>
        <StatusBadge tone={tone} label={state} />
      </div>
      {detail && (
        <div className="mt-0.5 text-[10px] text-[var(--vc-text-subtle)]">
          {detail}
        </div>
      )}
      {commandPreview && (
        <div
          className="mt-1 truncate font-mono text-[10px] text-[var(--vc-text-subtle)]"
          title={commandPreview}
        >
          $ {commandPreview}
        </div>
      )}
      {errorText && (
        <div className="mt-1 text-[10px] text-[var(--vc-danger-text)]">
          {errorText}
        </div>
      )}
    </div>
  );
}

function ServerSection({
  capability,
  emptyLabel,
  noConfigLabel,
}: {
  capability: CapabilityStatusSnapshot | undefined;
  emptyLabel: string;
  noConfigLabel: string;
}) {
  const { t } = useTranslation();
  if (!capability) {
    return (
      <div className="text-xs text-[var(--vc-text-subtle)]">{emptyLabel}</div>
    );
  }
  const details = capability.details ?? {};
  return (
    <div className="space-y-2">
      <CapabilityHeader label={t("status.server")} />
      {capability.error && (
        <div className="rounded-md border border-[color:var(--vc-danger-border)] bg-[var(--vc-danger-bg)] px-2 py-1.5 text-[11px] text-[var(--vc-danger-text)]">
          {capability.error}
        </div>
      )}
      {capability.state === "unconfigured" && (
        <div className="rounded-md border border-dashed border-[color:var(--vc-border-subtle)] px-2 py-1.5 text-[11px] text-[var(--vc-text-subtle)]">
          {noConfigLabel}
        </div>
      )}
      {typeof details.url === "string" && details.url.length > 0 && (
        <div className="rounded-md border border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] px-2 py-1.5">
          <div className="text-[10px] uppercase tracking-wide text-[var(--vc-text-subtle)]">
            URL
          </div>
          <div className="mt-0.5 break-all font-mono text-[11px] text-[var(--vc-text-primary)]">
            {details.url}
          </div>
        </div>
      )}
    </div>
  );
}

function LspSection({
  capability,
  emptyLabel,
  noConfigLabel,
}: {
  capability: CapabilityStatusSnapshot | undefined;
  emptyLabel: string;
  noConfigLabel: string;
}) {
  const { t } = useTranslation();
  const servers = useMemo(
    () => lspServers(capability?.details),
    [capability?.details],
  );
  if (!capability) {
    return (
      <div className="text-xs text-[var(--vc-text-subtle)]">{emptyLabel}</div>
    );
  }
  return (
    <div className="space-y-2">
      <CapabilityHeader label={t("status.lsp")} />
      {capability.error && (
        <div className="rounded-md border border-[color:var(--vc-danger-border)] bg-[var(--vc-danger-bg)] px-2 py-1.5 text-[11px] text-[var(--vc-danger-text)]">
          {capability.error}
        </div>
      )}
      {capability.state === "unconfigured" && (
        <div className="rounded-md border border-dashed border-[color:var(--vc-border-subtle)] px-2 py-1.5 text-[11px] text-[var(--vc-text-subtle)]">
          {noConfigLabel}
        </div>
      )}
      <div className="space-y-1.5">
        {servers.length === 0 ? (
          <div className="text-[11px] text-[var(--vc-text-subtle)]">
            {t("status.noServers")}
          </div>
        ) : (
          servers.map((server) => (
            <ServerRow
              key={server.server}
              name={server.server}
              tone={toneFromState(server.status)}
              state={server.status}
              errorText={server.error ?? null}
              command={server.command}
            />
          ))
        )}
      </div>
    </div>
  );
}

function McpSection({
  capability,
  emptyLabel,
  noConfigLabel,
  retry,
}: {
  capability: CapabilityStatusSnapshot | undefined;
  emptyLabel: string;
  noConfigLabel: string;
  retry?: {
    available: boolean;
    status: "idle" | "loading" | "success" | "error";
    error: string | null;
    onRetry?: () => void;
  };
}) {
  const { t } = useTranslation();
  const servers = useMemo(
    () => mcpServers(capability?.details),
    [capability?.details],
  );
  if (!capability) {
    return (
      <div className="text-xs text-[var(--vc-text-subtle)]">{emptyLabel}</div>
    );
  }
  return (
    <div className="space-y-2">
      <CapabilityHeader label={t("status.mcp")} />
      {capability.error && (
        <div className="rounded-md border border-[color:var(--vc-danger-border)] bg-[var(--vc-danger-bg)] px-2 py-1.5 text-[11px] text-[var(--vc-danger-text)]">
          {capability.error}
        </div>
      )}
      {capability.state === "unconfigured" && (
        <div className="rounded-md border border-dashed border-[color:var(--vc-border-subtle)] px-2 py-1.5 text-[11px] text-[var(--vc-text-subtle)]">
          {noConfigLabel}
        </div>
      )}
      <div className="space-y-1.5">
        {servers.length === 0 ? (
          <div className="text-[11px] text-[var(--vc-text-subtle)]">
            {t("status.noServers")}
          </div>
        ) : (
          servers.map((server) => {
            const tone = toneFromState(server.status);
            const meta = [
              server.transport ? `transport: ${server.transport}` : null,
              server.scope ? `scope: ${server.scope}` : null,
              server.stage ? `stage: ${server.stage}` : null,
            ]
              .filter(Boolean)
              .join(" · ");
            return (
              <ServerRow
                key={server.server}
                name={server.server}
                tone={tone}
                state={server.status}
                detail={meta || null}
                errorText={server.error ?? null}
                command={server.command}
              />
            );
          })
        )}
      </div>
      {retry?.error && (
        <div className="rounded-md border border-[color:var(--vc-danger-border)] bg-[var(--vc-danger-bg)] px-2 py-1.5 text-[11px] text-[var(--vc-danger-text)]">
          {t("status.retryError", { message: retry.error })}
        </div>
      )}
      {retry?.available && retry.onRetry && (
        <ControlButton
          compact
          variant="secondary"
          onClick={retry.onRetry}
          disabled={retry.status === "loading"}
          className="w-full"
        >
          {retry.status === "loading" ? (
            <>
              <Loader2 className="h-3 w-3 animate-spin" />
              {t("status.retrying")}
            </>
          ) : (
            <>
              <RefreshCw className="h-3 w-3" />
              {t("status.retryMcp")}
            </>
          )}
        </ControlButton>
      )}
    </div>
  );
}

export function StatusBar({
  snapshot,
  status,
  error,
  mcpRetryStatus = "idle",
  mcpRetryError = null,
  onRetryMcp,
}: StatusBarProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [activeTab, setActiveTab] = useState<CategoryKey>("server");
  const popoverId = useId();

  const serverTone = toneFromState(snapshot?.acp?.state);
  const lspTone = toneFromState(snapshot?.lsp.state);
  const mcpTone = toneFromState(snapshot?.mcp.state);
  const overallTone = aggregateTone(serverTone, lspTone, mcpTone);
  const mcpDetails = snapshot?.mcp.details;
  const mcpRetryAvailable = Boolean(mcpDetails?.retry_available);

  const counts = useMemo(() => {
    return {
      server: snapshot?.acp?.state === "failed" ? 1 : 0,
      lsp: lspServers(snapshot?.lsp.details).filter(
        (server) => server.status === "failed",
      ).length,
      mcp: mcpServers(snapshot?.mcp.details).filter(
        (server) => server.status === "failed",
      ).length,
    };
  }, [snapshot]);

  const overallLabel =
    overallTone === "ok"
      ? t("status.overallOk")
      : overallTone === "warn"
        ? t("status.overallWarn")
        : overallTone === "error"
          ? t("status.overallError")
          : t("status.overallIdle");

  if (status === "idle" && !snapshot) return null;

  return (
    <div className="relative">
      <ControlButton
        compact
        variant="secondary"
        onClick={() => setOpen((value) => !value)}
        className="text-[var(--vc-text-muted)]"
        aria-label={t("status.toggleAria")}
        aria-expanded={open}
        aria-controls={popoverId}
      >
        <span className="flex items-center gap-1.5">
          <Activity className="h-3.5 w-3.5" />
          <span
            aria-hidden="true"
            className={`h-1.5 w-1.5 rounded-full ${dotClassFromTone(overallTone)}`}
          />
          <span className="hidden sm:inline">{overallLabel}</span>
          <ChevronDown
            className={`h-3 w-3 transition-transform ${open ? "rotate-180" : ""}`}
          />
        </span>
      </ControlButton>

      {open && (
        <div
          id={popoverId}
          className="absolute right-0 top-full z-50 mt-2 w-80 rounded-xl border border-[color:var(--vc-border-subtle)] bg-[var(--vc-bg)] p-3 shadow-2xl"
        >
          {status === "error" && (
            <div className="mb-2 rounded-md border border-[color:var(--vc-danger-border)] bg-[var(--vc-danger-bg)] px-2 py-1.5 text-[11px] text-[var(--vc-danger-text)]">
              {t("status.loadError", { message: error })}
            </div>
          )}

          <div
            role="tablist"
            aria-label={t("status.toggleAria")}
            className="mb-3 grid grid-cols-3 gap-1 rounded-lg border border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] p-1"
          >
            <CategoryTab
              active={activeTab === "server"}
              onSelect={() => setActiveTab("server")}
              label={t("status.server")}
              tone={serverTone}
              count={counts.server}
              icon={<Network className="h-3 w-3" />}
              ariaControls={`${popoverId}-server`}
            />
            <CategoryTab
              active={activeTab === "lsp"}
              onSelect={() => setActiveTab("lsp")}
              label={t("status.lsp")}
              tone={lspTone}
              count={counts.lsp}
              icon={<Server className="h-3 w-3" />}
              ariaControls={`${popoverId}-lsp`}
            />
            <CategoryTab
              active={activeTab === "mcp"}
              onSelect={() => setActiveTab("mcp")}
              label={t("status.mcp")}
              tone={mcpTone}
              count={counts.mcp}
              icon={<Boxes className="h-3 w-3" />}
              ariaControls={`${popoverId}-mcp`}
            />
          </div>

          <div role="tabpanel" id={`${popoverId}-${activeTab}`}>
            {activeTab === "server" && (
              <ServerSection
                capability={snapshot?.acp}
                emptyLabel={t("status.serverNoData")}
                noConfigLabel={t("status.serverUnconfigured")}
              />
            )}
            {activeTab === "lsp" && (
              <LspSection
                capability={snapshot?.lsp}
                emptyLabel={t("status.lspNoData")}
                noConfigLabel={t("status.lspUnconfigured")}
              />
            )}
            {activeTab === "mcp" && (
              <McpSection
                capability={snapshot?.mcp}
                emptyLabel={t("status.mcpNoData")}
                noConfigLabel={t("status.mcpUnconfigured")}
                retry={{
                  available: mcpRetryAvailable,
                  status: mcpRetryStatus,
                  error: mcpRetryError,
                  onRetry: onRetryMcp,
                }}
              />
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export type { CapabilityStateKey };
