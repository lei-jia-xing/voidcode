import { useId, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { GitBranch, Server, Boxes, Network } from "lucide-react";
import type {
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

function statusDotClass(
  state: string | undefined,
  kind: "git" | "capability",
): string {
  if (!state) return "bg-[var(--vc-text-subtle)]";
  if (kind === "git") {
    if (state === "git_ready") return "bg-[var(--vc-confirm-text)]";
    if (state === "git_error") return "bg-[var(--vc-danger-text)]";
    return "bg-[var(--vc-text-subtle)]";
  }
  if (state === "running") return "bg-[var(--vc-confirm-text)]";
  if (state === "failed") return "bg-[var(--vc-danger-text)]";
  if (state === "stopped") return "bg-[var(--vc-text-subtle)]";
  return "bg-[var(--vc-text-subtle)]";
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
  const [showDetail, setShowDetail] = useState(false);
  const detailId = useId();
  const mcpDetails = snapshot?.mcp.details;
  const acpDetails = snapshot?.acp?.details;
  const mcpServers = useMemo(
    () =>
      Array.isArray(mcpDetails?.servers)
        ? (mcpDetails.servers as McpServerStatusDetail[])
        : [],
    [mcpDetails],
  );
  const mcpRetryAvailable = Boolean(mcpDetails?.retry_available);

  if (status === "idle" && !snapshot) return null;

  return (
    <div className="relative">
      <ControlButton
        compact
        variant="secondary"
        onClick={() => setShowDetail(!showDetail)}
        className="text-[var(--vc-text-muted)]"
        aria-label={t("status.toggleAria")}
        aria-expanded={showDetail}
        aria-controls={detailId}
      >
        <span className="flex items-center gap-1">
          <GitBranch className="w-3 h-3" />
          <span
            className={`w-1.5 h-1.5 rounded-full ${statusDotClass(snapshot?.git.state, "git")}`}
          />
        </span>
        <span className="w-px h-3 bg-[var(--vc-border-subtle)]" />
        <span className="flex items-center gap-1">
          <Server className="w-3 h-3" />
          <span
            className={`w-1.5 h-1.5 rounded-full ${statusDotClass(snapshot?.lsp.state, "capability")}`}
          />
        </span>
        <span className="w-px h-3 bg-[var(--vc-border-subtle)]" />
        <span className="flex items-center gap-1">
          <Boxes className="w-3 h-3" />
          <span
            className={`w-1.5 h-1.5 rounded-full ${statusDotClass(snapshot?.mcp.state, "capability")}`}
          />
        </span>
        <span className="w-px h-3 bg-[var(--vc-border-subtle)]" />
        <span className="flex items-center gap-1">
          <Network className="w-3 h-3" />
          <span
            className={`w-1.5 h-1.5 rounded-full ${statusDotClass(snapshot?.acp?.state, "capability")}`}
          />
        </span>
      </ControlButton>

      {showDetail && (
        <div
          id={detailId}
          className="absolute right-0 top-full mt-2 w-64 rounded-lg border border-[color:var(--vc-border-subtle)] bg-[var(--vc-bg)] shadow-xl z-50 p-3 space-y-2"
        >
          {status === "error" && (
            <div className="text-xs text-[var(--vc-danger-text)]">
              {t("status.loadError", { message: error })}
            </div>
          )}
          <div className="flex items-center justify-between">
            <span className="text-xs text-[var(--vc-text-muted)] flex items-center gap-1.5">
              <GitBranch className="w-3 h-3" />
              {t("status.git")}
            </span>
            <span className="text-xs font-mono text-[var(--vc-text-primary)]">
              {snapshot?.git.state ?? t("status.unknown")}
            </span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-xs text-[var(--vc-text-muted)] flex items-center gap-1.5">
              <Server className="w-3 h-3" />
              {t("status.lsp")}
            </span>
            <span className="text-xs font-mono text-[var(--vc-text-primary)]">
              {snapshot?.lsp.state ?? t("status.unknown")}
            </span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-xs text-[var(--vc-text-muted)] flex items-center gap-1.5">
              <Boxes className="w-3 h-3" />
              {t("status.mcp")}
            </span>
            <span className="text-xs font-mono text-[var(--vc-text-primary)]">
              {snapshot?.mcp.state ?? t("status.unknown")}
            </span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-xs text-[var(--vc-text-muted)] flex items-center gap-1.5">
              <Network className="w-3 h-3" />
              {t("status.acp")}
            </span>
            <span className="text-xs font-mono text-[var(--vc-text-primary)]">
              {snapshot?.acp?.state ?? t("status.unknown")}
            </span>
          </div>
          {snapshot?.mcp.error && (
            <div className="text-xs text-[var(--vc-danger-text)]">
              {snapshot.mcp.error}
            </div>
          )}
          {snapshot?.acp?.error && (
            <div className="text-xs text-[var(--vc-danger-text)]">
              {snapshot.acp.error}
            </div>
          )}
          {acpDetails && Object.keys(acpDetails).length > 0 && (
            <div className="rounded-md bg-[var(--vc-surface-1)] px-2 py-1.5 text-[10px] text-[var(--vc-text-subtle)]">
              {typeof acpDetails.status === "string" && (
                <div>
                  {t("status.acpTransport")}: {acpDetails.status}
                </div>
              )}
              {typeof acpDetails.last_request_type === "string" && (
                <div>
                  {t("status.acpLastRequest")}: {acpDetails.last_request_type}
                </div>
              )}
            </div>
          )}
          {mcpServers.length > 0 && (
            <div className="space-y-1 border-t border-[color:var(--vc-border-subtle)] pt-2">
              {mcpServers.map((server) => (
                <div
                  key={server.server}
                  className="rounded-md bg-[var(--vc-surface-1)] px-2 py-1.5"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[11px] font-medium text-[var(--vc-text-primary)]">
                      {server.server}
                    </span>
                    <span className="text-[10px] font-mono text-[var(--vc-text-muted)]">
                      {server.status}
                    </span>
                  </div>
                  {server.stage && (
                    <div className="text-[10px] text-[var(--vc-text-subtle)]">
                      {server.stage}
                    </div>
                  )}
                  {server.error && (
                    <div className="text-[10px] text-[var(--vc-danger-text)]">
                      {server.error}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
          {mcpRetryError && (
            <div className="text-xs text-[var(--vc-danger-text)]">
              {t("status.retryError", { message: mcpRetryError })}
            </div>
          )}
          {mcpRetryAvailable && onRetryMcp && (
            <ControlButton
              compact
              variant="secondary"
              onClick={onRetryMcp}
              disabled={mcpRetryStatus === "loading"}
              className="w-full"
            >
              {mcpRetryStatus === "loading"
                ? t("status.retrying")
                : t("status.retryMcp")}
            </ControlButton>
          )}
        </div>
      )}
    </div>
  );
}
