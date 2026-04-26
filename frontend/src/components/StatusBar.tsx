import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { GitBranch, Server, Boxes, Network } from "lucide-react";
import type {
  McpServerStatusDetail,
  RuntimeStatusSnapshot,
} from "../lib/runtime/types";

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
  if (!state) return "bg-slate-600";
  if (kind === "git") {
    if (state === "git_ready") return "bg-emerald-500";
    if (state === "git_error") return "bg-rose-500";
    return "bg-slate-600";
  }
  if (state === "running") return "bg-emerald-500";
  if (state === "failed") return "bg-rose-500";
  if (state === "stopped") return "bg-amber-500";
  return "bg-slate-600";
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
      <button
        type="button"
        onClick={() => setShowDetail(!showDetail)}
        className="inline-flex items-center gap-2 rounded-lg border border-slate-800 bg-slate-900/60 px-2.5 py-1 text-xs text-slate-400 hover:border-slate-700 hover:bg-slate-800/60"
        aria-label={t("status.toggleAria")}
        aria-expanded={showDetail}
      >
        <span className="flex items-center gap-1">
          <GitBranch className="w-3 h-3" />
          <span
            className={`w-1.5 h-1.5 rounded-full ${statusDotClass(snapshot?.git.state, "git")}`}
          />
        </span>
        <span className="w-px h-3 bg-slate-800" />
        <span className="flex items-center gap-1">
          <Server className="w-3 h-3" />
          <span
            className={`w-1.5 h-1.5 rounded-full ${statusDotClass(snapshot?.lsp.state, "capability")}`}
          />
        </span>
        <span className="w-px h-3 bg-slate-800" />
        <span className="flex items-center gap-1">
          <Boxes className="w-3 h-3" />
          <span
            className={`w-1.5 h-1.5 rounded-full ${statusDotClass(snapshot?.mcp.state, "capability")}`}
          />
        </span>
        <span className="w-px h-3 bg-slate-800" />
        <span className="flex items-center gap-1">
          <Network className="w-3 h-3" />
          <span
            className={`w-1.5 h-1.5 rounded-full ${statusDotClass(snapshot?.acp?.state, "capability")}`}
          />
        </span>
      </button>

      {showDetail && (
        <div className="absolute right-0 top-full mt-2 w-64 rounded-lg border border-slate-800 bg-[#0c0c0e] shadow-xl z-50 p-3 space-y-2">
          {status === "error" && (
            <div className="text-xs text-rose-400">
              {t("status.loadError", { message: error })}
            </div>
          )}
          <div className="flex items-center justify-between">
            <span className="text-xs text-slate-400 flex items-center gap-1.5">
              <GitBranch className="w-3 h-3" />
              {t("status.git")}
            </span>
            <span className="text-xs font-mono text-slate-300">
              {snapshot?.git.state ?? t("status.unknown")}
            </span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-xs text-slate-400 flex items-center gap-1.5">
              <Server className="w-3 h-3" />
              {t("status.lsp")}
            </span>
            <span className="text-xs font-mono text-slate-300">
              {snapshot?.lsp.state ?? t("status.unknown")}
            </span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-xs text-slate-400 flex items-center gap-1.5">
              <Boxes className="w-3 h-3" />
              {t("status.mcp")}
            </span>
            <span className="text-xs font-mono text-slate-300">
              {snapshot?.mcp.state ?? t("status.unknown")}
            </span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-xs text-slate-400 flex items-center gap-1.5">
              <Network className="w-3 h-3" />
              {t("status.acp")}
            </span>
            <span className="text-xs font-mono text-slate-300">
              {snapshot?.acp?.state ?? t("status.unknown")}
            </span>
          </div>
          {snapshot?.mcp.error && (
            <div className="text-xs text-rose-400">{snapshot.mcp.error}</div>
          )}
          {snapshot?.acp?.error && (
            <div className="text-xs text-rose-400">{snapshot.acp.error}</div>
          )}
          {acpDetails && Object.keys(acpDetails).length > 0 && (
            <div className="rounded-md bg-slate-900/70 px-2 py-1.5 text-[10px] text-slate-500">
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
            <div className="space-y-1 border-t border-slate-800 pt-2">
              {mcpServers.map((server) => (
                <div
                  key={server.server}
                  className="rounded-md bg-slate-900/70 px-2 py-1.5"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[11px] font-medium text-slate-200">
                      {server.server}
                    </span>
                    <span className="text-[10px] font-mono text-slate-400">
                      {server.status}
                    </span>
                  </div>
                  {server.stage && (
                    <div className="text-[10px] text-slate-500">
                      {server.stage}
                    </div>
                  )}
                  {server.error && (
                    <div className="text-[10px] text-rose-400">
                      {server.error}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
          {mcpRetryError && (
            <div className="text-xs text-rose-400">
              {t("status.retryError", { message: mcpRetryError })}
            </div>
          )}
          {mcpRetryAvailable && onRetryMcp && (
            <button
              type="button"
              onClick={onRetryMcp}
              disabled={mcpRetryStatus === "loading"}
              className="w-full rounded-md border border-slate-700 bg-slate-900 px-2 py-1.5 text-xs text-slate-200 hover:bg-slate-800 disabled:opacity-50"
            >
              {mcpRetryStatus === "loading"
                ? t("status.retrying")
                : t("status.retryMcp")}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
