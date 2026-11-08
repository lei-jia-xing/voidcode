import React, {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useTranslation } from "react-i18next";
import { CoreClient, CoreApiError } from "../lib/core/client";
import type { CommandCatalog } from "../lib/core/client";
import type {
  HealthStatus,
  FormField,
  Icon,
  PluginViewDescriptor,
  SessionRow,
  SessionFrame,
  StreamEvent,
  ToolExecution,
  ToolOutput,
  TranscriptEntry,
  TranscriptTool,
} from "../lib/core/types";
import { PluginViewRenderer } from "./views/PluginViewRenderer";

export interface CoreAppProps {
  baseUrl?: string;
  token?: string;
}

/** Tool birth icon from the recorded transcript value; absent means generic. */
function toolIconText(icon: Icon | null | undefined, generic: string): string {
  const pick = icon?.glyph;
  return typeof pick === "string" ? pick : generic;
}

/** Canonical text for one typed tool output; the union replaces type guessing. */
function toolOutputText(output: ToolOutput): string {
  if (output.type === "text") {
    return output.presentation || output.text;
  }
  if (output.type === "attachment") {
    return `[${output.mime}]`;
  }
  return output.presentation ?? "";
}

/** Public slash paths use spaces; machine command IDs stay unchanged. */
function slashPathOf(descriptor: {
  name: string;
  client_syntax?: string | null;
}): string {
  const syntax = descriptor.client_syntax;
  const prefix =
    typeof syntax === "string" && syntax.startsWith("/")
      ? syntax.slice(1).trim()
      : "";
  return (prefix || descriptor.name).replace(/\./g, " ");
}

/** Human argument hints derived from the admitted schema. */
function argumentHints(schema: Record<string, unknown> | undefined): string {
  const properties = schema?.properties;
  if (!properties || typeof properties !== "object") return "";
  const required = Array.isArray(schema?.required) ? schema.required : [];
  return Object.keys(properties)
    .map((name) => (required.includes(name) ? `<${name}>` : `[${name}]`))
    .join(" ");
}

/** Public slash path for display: the catalog's preferred spelling when the
name is admitted, else the canonical name with dots as spaces. */
function displaySlashPath(
  commands: Array<{ name: string; client_syntax?: string | null }>,
  canonicalName: string,
): string {
  const descriptor = commands.find((c) => c.name === canonicalName);
  return descriptor
    ? slashPathOf(descriptor)
    : canonicalName.replace(/\./g, " ");
}

/** Longest registered public path that matches the input tokens, or null on
no match or on an equally-longest ambiguous collision (never silent first). */
function matchSlashPath<
  T extends { name: string; client_syntax?: string | null },
>(
  commands: T[],
  tokens: string[],
):
  { descriptor: T; path: string; length: number } | { ambiguous: true } | null {
  let best: { descriptor: T; path: string; length: number } | null = null;
  let ambiguous = false;
  for (const descriptor of commands) {
    const path = slashPathOf(descriptor);
    const parts = path.split(" ").filter((part) => part !== "");
    if (parts.length === 0 || parts.length > tokens.length) continue;
    if (!parts.every((part, index) => tokens[index] === part)) continue;
    if (best === null || parts.length > best.length) {
      best = { descriptor, path, length: parts.length };
      ambiguous = false;
    } else if (parts.length === best.length) {
      ambiguous = true;
    }
  }
  if (best === null) return null;
  return ambiguous ? { ambiguous: true } : best;
}

/** Execution cue key for stopped/unknown-effects tools, else null. */
function toolExecutionCueKey(
  execution: ToolExecution | null | undefined,
): string | null {
  if (!execution) return null;
  if (!execution.execution_stopped) return "core.toolStoppedUnconfirmed";
  if (execution.side_effect_state === "unknown")
    return "core.toolEffectsUnknown";
  return null;
}

function ToolResultCard({
  evSequence,
  tool,
  t,
}: {
  evSequence: number;
  tool: TranscriptTool;
  t: (key: string) => string;
}) {
  const [expanded, setExpanded] = useState(false);
  const result = tool.result;
  const toolName =
    tool.call.tool_name ?? result.tool_name ?? t("core.unknownLabel");
  const text = toolOutputText(result.output);
  const failed = tool.interrupted === true || result.status === "error";
  const errorMsg = result.error ?? null;
  const cueKey = toolExecutionCueKey(result.execution);

  const bounds = result.output?.bounds;
  const isTruncated = Boolean(bounds?.truncated || bounds?.reference);
  const fullText = result.output?.type === "text" ? result.output.text : text;
  const refId =
    typeof bounds?.reference === "string"
      ? bounds.reference
      : bounds?.reference?.sha256;

  return (
    <div
      key={evSequence}
      data-testid={`event-${evSequence}`}
      className={`text-xs font-mono px-3 py-1.5 rounded border max-w-2xl ${
        failed
          ? "text-red-400 bg-red-950/20 border-red-900/50"
          : "text-emerald-400 bg-emerald-950/20 border-emerald-900/50"
      }`}
    >
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div>
          {failed ? "✖" : "✔"} {toolIconText(tool.icon, "▷")}{" "}
          {t("core.toolLabel")} [{toolName}]:{" "}
          {failed ? (errorMsg ?? text) : text}
          {cueKey && <span className="text-neutral-400"> ({t(cueKey)})</span>}
          {isTruncated && (
            <span
              data-testid={`event-${evSequence}-truncated-tag`}
              className="ml-1.5 px-1 py-0.5 rounded text-[10px] bg-amber-950/50 text-amber-400 border border-amber-800/50"
            >
              [truncated
              {refId
                ? `: ${refId.startsWith("artifact://") ? refId.slice(11, 19) : refId.slice(0, 8)}…`
                : ""}
              ]
            </span>
          )}
        </div>
        {isTruncated && (
          <button
            type="button"
            data-testid={`event-${evSequence}-toggle-output`}
            onClick={() => setExpanded((prev) => !prev)}
            className="text-[11px] underline text-cyan-400 hover:text-cyan-300 ml-auto cursor-pointer focus:outline-none"
          >
            {expanded ? "Hide full output" : "View full output"}
          </button>
        )}
      </div>
      {isTruncated && expanded && (
        <div
          data-testid={`event-${evSequence}-full-output`}
          className="mt-2 pt-2 border-t border-neutral-700/50 text-neutral-200 whitespace-pre-wrap break-words max-h-96 overflow-y-auto font-mono text-[11px] bg-neutral-900/60 p-2 rounded"
        >
          {refId && (
            <div className="text-[10px] text-neutral-400 mb-1 select-all font-mono">
              Artifact: {refId}
            </div>
          )}
          {fullText}
        </div>
      )}
    </div>
  );
}

/** Narrow one dynamic pending_approval object to its display fields. */
function parkedApprovalOf(value: unknown): {
  requestId: string;
  toolName: string | null;
  toolArguments: unknown;
} | null {
  if (value === null || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  if (typeof record.request_id !== "string" || !record.request_id) return null;
  const call =
    record.call !== null && typeof record.call === "object"
      ? (record.call as Record<string, unknown>)
      : null;
  return {
    requestId: record.request_id,
    toolName: typeof call?.tool_name === "string" ? call.tool_name : null,
    toolArguments: call?.arguments ?? {},
  };
}

/** One authentic pending confirmation: a typed UI envelope bound to the revision it was displayed under. */
interface PendingConfirmationEntry {
  owner: string;
  name: string;
  title: string;
  view_type: string;
  data: Record<string, unknown>;
  revisionId: string;
}

export const CoreApp: React.FC<CoreAppProps> = ({ baseUrl = "", token }) => {
  const searchParams =
    typeof window !== "undefined"
      ? new URLSearchParams(window.location.search)
      : null;
  const effectiveBaseUrl = baseUrl || searchParams?.get("server") || "";
  const effectiveToken = token || searchParams?.get("token") || undefined;

  const client = useMemo(
    () => new CoreClient(effectiveBaseUrl, effectiveToken),
    [effectiveBaseUrl, effectiveToken],
  );
  useEffect(() => () => client.close(), [client]);
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string>("");
  // The catalog travels with the revision it was captured under: every command
  // it advertises is dispatched with exactly that revision.
  const [catalog, setCatalog] = useState<CommandCatalog>({
    revisionId: "",
    commands: [],
  });
  const [events, setEvents] = useState<
    Array<{
      sequence: number;
      type: string;
      revision_id: string;
      payload: Record<string, unknown>;
      transcript?: TranscriptEntry | null;
    }>
  >([]);
  const [viewScope, setViewScope] = useState<{
    sessionId: string;
    epoch: number;
    clientKey: string;
    revisionId: string;
    views: PluginViewDescriptor[];
  }>({ sessionId: "", epoch: -1, clientKey: "", revisionId: "", views: [] });
  const [commandView, setCommandView] = useState<{
    sessionId: string;
    epoch: number;
    clientKey: string;
    revisionId: string;
    descriptor: PluginViewDescriptor;
    liveSource?: { action: string; args: Record<string, unknown> };
    refreshAfterMs?: number | null;
  } | null>(null);
  const commandViewLiveRef = useRef<{
    action: string;
    args: Record<string, unknown>;
    revisionId: string;
  } | null>(null);
  const commandViewRequestRef = useRef(0);
  const [suggestionIndex, setSuggestionIndex] = useState(0);
  const [completionDismissed, setCompletionDismissed] = useState(false);
  // Pending confirmations are a typed UI envelope per request (ui_type +
  // validated ui_data), never parsed product data: the client only renders the
  // declared view kind. They are held by request id in request order, so a
  // second request never displaces the first, and each keeps the revision it
  // was displayed under. A request id already decided can never come back.
  const [pendingConfirmations, setPendingConfirmations] = useState<
    Record<string, PendingConfirmationEntry>
  >({});
  const resolvedConfirmationsRef = useRef<Set<string>>(new Set());
  const pendingConfirmation = Object.values(pendingConfirmations)[0] ?? null;

  const retainPendingConfirmation = useCallback(
    (requestId: string, entry: PendingConfirmationEntry) => {
      if (!requestId || resolvedConfirmationsRef.current.has(requestId)) {
        return; // a decided request never resurrects, even from a late reply
      }
      setPendingConfirmations((prev) =>
        // The same request is never duplicated or reset by its own duplicate fact
        // or by the live pending_confirmation result that carries it.
        requestId in prev ? prev : { ...prev, [requestId]: entry },
      );
    },
    [],
  );
  const [promptInput, setPromptInput] = useState<string>("");
  // Every async result is bound to the (session, epoch) it was issued for: a
  // late A reply is invalid even after the user returns to A, because the epoch
  // advanced in between.
  const sessionRef = useRef<string>("");
  const epochRef = useRef<number>(0);
  // A client identity change (different server/token) invalidates every result
  // from the previous transport, on top of the session and epoch.
  const clientKeyRef = useRef<string>("");
  const selectionRef = useRef<string>("");
  // Monotonic request serials: only the newest catalog/view snapshot may render,
  // so a reply issued before an activation commit can never overwrite the one
  // issued after it.
  const catalogRequestRef = useRef<number>(0);
  const viewsRequestRef = useRef<number>(0);
  const healthRequestRef = useRef<number>(0);
  // Render-time scope is state: refs stay exclusively for async guards.
  const [scope, setScope] = useState<{
    sessionId: string;
    epoch: number;
    clientKey: string;
  }>({
    sessionId: "",
    epoch: 0,
    clientKey: "",
  });
  // The rendered client identity comes from the props/state that produced this
  // render, so a stale scope can never pass the gate while the effect catches up.
  const renderedClientKey = JSON.stringify([
    effectiveBaseUrl,
    effectiveToken ?? null,
  ]);
  // Views are only actionable when the catalog they are validated against was
  // captured under the SAME revision: after an activation commit both are
  // cleared and refetched, and no form/choice can be rendered from a v1 views
  // envelope while the v2 catalog (or the reverse) is what the client holds.
  const pluginViews =
    scope.sessionId === activeSessionId &&
    scope.clientKey === renderedClientKey &&
    viewScope.sessionId === activeSessionId &&
    viewScope.clientKey === renderedClientKey &&
    viewScope.epoch === scope.epoch &&
    viewScope.revisionId !== "" &&
    viewScope.revisionId === catalog.revisionId
      ? viewScope.views
      : [];
  const isCurrent = useCallback(
    (session: string, epoch: number, clientKey: string) =>
      epochRef.current === epoch &&
      sessionRef.current === session &&
      clientKeyRef.current === clientKey,
    [],
  );
  const [isRunning, setIsRunning] = useState<boolean>(false);
  // Command invocations in flight: separate from the authoritative root-run
  // state above, so a monitor action can never reset the main run indicator.
  const [commandInFlight, setCommandInFlight] = useState<boolean>(false);
  // The last health answer this scope accepted, mirrored in a ref so the
  // identity compare reads a stable snapshot instead of calling setState
  // inside another state updater (StrictMode may replay updaters).
  const upgradeRef = useRef<{
    fenced: boolean;
    stage: string;
    coreVersion: string;
    generation: number;
    activeUpgrade: { upgrade_id: string; stage: string } | null;
  } | null>(null);
  // Controlled-upgrade state: the last known server identity plus whether the
  // core is fenced for an upgrade. Dispatch pauses while fenced; the banner
  // renders from this, and identity compares detect a changed server.
  const [upgrade, setUpgrade] = useState<{
    fenced: boolean;
    stage: string;
    coreVersion: string;
    generation: number;
    activeUpgrade: { upgrade_id: string; stage: string } | null;
  } | null>(null);
  // Monotonic marker of the newest user-issued run activity: a stale `show`
  // from an older reconcile round must never overwrite it.
  const runActivityRef = useRef<number>(0);
  // Request ids with a decision worker in flight: a held click while one is
  // outstanding is ignored, never a second request. Cleared on response,
  // decided fact, or error — never resurrecting a resolved id.
  const inFlightRef = useRef<Set<string>>(new Set());
  // Action confirmation: it resumes with the approval request id, never /confirm.
  const [pendingApproval, setPendingApproval] = useState<{
    sessionId: string;
    epoch: number;
    clientKey: string;
    requestId: string;
    toolName: string;
    toolArguments: string;
  } | null>(null);
  const { t } = useTranslation();
  const [statusMessage, setStatusMessage] = useState<string>(() =>
    t("core.statusReady"),
  );

  /**
   * Project one persisted event into the client's interaction state. Called once
   * per authenticated event, in sequence order, outside any state updater.
   *
   * The event carries `revision_id`: the generation that produced it, so a
   * request-level fact (a confirmation request) binds to it, never to the
   * current catalog. `core.confirmation_requested` opens the modal and
   * `core.confirmation_decided` ({request_id, decision}) reduces it, so a
   * still-pending request survives a reload and a decided one never resurrects.
   */
  const projectEvent = useCallback(
    (
      ev: {
        sequence: number;
        type: string;
        revision_id: string;
        payload: Record<string, unknown>;
        transcript?: TranscriptEntry | null;
      },
      session: string,
      epoch: number,
      clientKey: string,
    ) => {
      if (ev.type === "core.approval_requested") {
        const parked = parkedApprovalOf(ev.payload);
        if (parked) {
          setPendingApproval({
            sessionId: session,
            epoch,
            clientKey,
            requestId: parked.requestId,
            toolName: parked.toolName ?? t("core.unknownLabel"),
            toolArguments: JSON.stringify(parked.toolArguments),
          });
          setStatusMessage(t("core.statusWaitingApproval"));
        }
        return;
      }
      if (ev.type === "core.approval_decided") {
        setPendingApproval(null);
        return;
      }
      if (
        ev.type === "core.run_interrupted" ||
        ev.type === "core.run_failed" ||
        ev.type === "core.run_completed"
      ) {
        // An authentic terminal fact means nothing is parked any more; a stale
        // approval modal must never outlive the run that asked for it.
        setPendingApproval(null);
        return;
      }
      if (ev.type === "core.confirmation_requested") {
        const p = ev.payload;
        const requestId = typeof p.request_id === "string" ? p.request_id : "";
        const uiType = typeof p.ui_type === "string" ? p.ui_type : "";
        const uiData = p.ui_data;
        if (
          requestId &&
          uiType &&
          uiData !== null &&
          typeof uiData === "object"
        ) {
          retainPendingConfirmation(requestId, {
            owner:
              typeof p.owner === "string" ? p.owner : t("core.unknownLabel"),
            name: "pending_confirmation",
            title: t("core.approvalRequired"),
            view_type: uiType,
            data: {
              ...uiData,
              request_id: requestId,
            },
            // The event's own revision created the request: no current-catalog
            // fallback and no relabelling.
            revisionId: ev.revision_id,
          });
        }
        return;
      }
      if (ev.type === "core.confirmation_decided") {
        const decided = ev.payload;
        const requestId =
          typeof decided.request_id === "string" ? decided.request_id : "";
        if (requestId) {
          // Terminal reduction: the request is closed whether or not it is the
          // one on screen, and a late reply can never resurrect it.
          resolvedConfirmationsRef.current.add(requestId);
          setPendingConfirmations((prev) => {
            if (!(requestId in prev)) return prev;
            const next = { ...prev };
            delete next[requestId];
            return next;
          });
        }
        return;
      }
    },
    [retainPendingConfirmation, t],
  );

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const lastSequenceRef = useRef<number>(0);
  // Latched terminal stream error for this session scope: once set, no
  // reconnect reconcile may overwrite the visible failure message.
  const terminalErrorRef = useRef<string | null>(null);
  const refreshViews = useCallback(
    (sessionId: string, epoch: number, clientKey: string) => {
      const serial = ++viewsRequestRef.current;
      client
        .views(sessionId)
        .then((snapshot) => {
          // A newer snapshot (e.g. the one right after an activation commit) has
          // already won: an older in-flight reply never overwrites it.
          if (viewsRequestRef.current !== serial) return;
          if (!isCurrent(sessionId, epoch, clientKey)) {
            return; // stale session/epoch/transport: never render these views
          }
          setViewScope({
            sessionId,
            epoch,
            clientKey,
            revisionId: snapshot.revisionId,
            views: snapshot.views,
          });
        })
        .catch((err) => {
          if (!isCurrent(sessionId, epoch, clientKey)) {
            return; // a superseded read's failure must not touch this view
          }
          console.error("Failed to fetch views", err);
        });
    },
    [client, isCurrent],
  );

  // Refetch the catalog after an activation commit: descriptors and revision are
  // replaced together, so no displayed command keeps the old generation.
  const refreshCatalog = useCallback(() => {
    const serial = ++catalogRequestRef.current;
    client
      .commandCatalog()
      .then((snapshot) => {
        if (catalogRequestRef.current !== serial) return; // a newer catalog won
        setCatalog(snapshot);
      })
      .catch((err) => {
        if (catalogRequestRef.current !== serial) return;
        setStatusMessage(
          t("core.statusCatalogFailed", { message: String(err) }),
        );
      });
  }, [client, t]);
  // Append-once application for one head backlog or live event batch: filter
  // to fresh sequences, commit rows, project gates, and invalidate the
  // displayed generation on activation commits. Called outside any updater.
  const applyEvents = useCallback(
    (
      batch: StreamEvent[],
      session: string,
      epoch: number,
      clientKey: string,
    ) => {
      // Authenticated, ordered events are processed exactly once, outside any
      // state updater: a StrictMode replay of the updater can never re-run a
      // projection or overwrite a newer interaction state.
      const fresh = batch
        .filter((e) => e.sequence > lastSequenceRef.current)
        .sort((a, b) => a.sequence - b.sequence);
      if (fresh.length > 0) {
        lastSequenceRef.current = fresh[fresh.length - 1].sequence;
      }
      setEvents((prev) => {
        const seen = new Set(prev.map((e) => e.sequence));
        const incoming = fresh.filter((e) => !seen.has(e.sequence));
        if (incoming.length === 0) {
          return prev;
        }
        return [
          ...prev,
          ...incoming.map((e) => ({
            sequence: e.sequence,
            type: e.type,
            revision_id: e.revision_id,
            payload: e.payload,
            transcript: e.transcript,
          })),
        ].sort((a, b) => a.sequence - b.sequence);
      });
      for (const ev of fresh) {
        projectEvent(ev, session, epoch, clientKey);
      }

      // An activation commit or an upgrade adoption invalidates the displayed
      // generation: catalog and views are cleared immediately (nothing old
      // stays actionable), then refetched together and anything captured
      // under another revision is dropped — never relabelled, never re-sent.
      const commits = fresh.filter(
        (e) =>
          e.type === "core.activation_committed" ||
          e.type === "core.revision_adopted",
      );
      if (commits.length > 0) {
        const committed = commits[commits.length - 1];
        const committedPayload = committed.payload;
        const toRevisionId =
          typeof committedPayload.to_revision_id === "string"
            ? committedPayload.to_revision_id
            : "";
        // The row's revision column is the committed target (same fact the
        // payload carries); a blank target is surfaced, never stored as a
        // revision the client pretends to know.
        const targetRevision = toRevisionId || committed.revision_id;
        setStatusMessage(
          toRevisionId
            ? t("core.statusActivationCommitted", {
                revision: toRevisionId,
              })
            : t("core.statusActivationMalformed"),
        );
        setPendingConfirmations((prev) =>
          // Only a request that belongs to the committed generation survives;
          // the others could never be resolved against it.
          Object.fromEntries(
            Object.entries(prev).filter(
              ([, entry]) => entry.revisionId === targetRevision,
            ),
          ),
        );
        setCatalog({ revisionId: "", commands: [] });
        setViewScope((prev) =>
          prev.sessionId === session &&
          prev.epoch === epoch &&
          prev.clientKey === clientKey
            ? { ...prev, revisionId: "", views: [] }
            : prev,
        );
        refreshCatalog();
        refreshViews(session, epoch, clientKey);
      }
    },
    [projectEvent, refreshCatalog, refreshViews, t],
  );
  // Read server identity/liveness. Only the newest reply for the current scope
  // may render: a stale health answer must never overwrite a newer one or
  // clear a fence the supervisor already raised.
  const refreshHealth = useCallback(
    (sessionId: string, epoch: number, clientKey: string) => {
      const serial = ++healthRequestRef.current;
      client
        .health()
        .then((snapshot: HealthStatus) => {
          if (healthRequestRef.current !== serial) return;
          if (!isCurrent(sessionId, epoch, clientKey)) return;
          setUpgrade({
            fenced: "fenced" in snapshot && snapshot.fenced === true,
            stage: String(snapshot.stage ?? ""),
            coreVersion: String(snapshot.core_version ?? ""),
            generation:
              typeof snapshot.generation === "number"
                ? snapshot.generation
                : -1,
            activeUpgrade:
              snapshot.active_upgrade &&
              typeof snapshot.active_upgrade === "object"
                ? {
                    upgrade_id: String(
                      snapshot.active_upgrade.upgrade_id ?? "",
                    ),
                    stage: String(snapshot.active_upgrade.stage ?? ""),
                  }
                : null,
          });
        })
        .catch((err) => {
          // Health is best-effort corroboration: unreachable during a handoff
          // gap is expected and must not clobber the last known identity.
          if (!isCurrent(sessionId, epoch, clientKey)) return;
          console.error("Failed to fetch core health", err);
        });
    },
    [client, isCurrent],
  );
  /**
   * ONE connection-established reconciliation path for the initial stream and
   * every reconnect — announced restart or silent drop. Fetches identity,
   * compares it against the last known server, refetches catalog+views, and
   * reconciles the running indicator from durable `show`. Never resends the
   * prompt and never touches the applied cursor. Scope-, serial-, and
   * activity-guarded: a stale reply from a superseded scope, an older
   * reconcile round, or an older run activity never renders.
   */
  const reconcileConnection = useCallback(
    (sessionId: string, epoch: number, clientKey: string) => {
      const round = ++healthRequestRef.current;
      client
        .health()
        .then((snapshot: HealthStatus) => {
          if (healthRequestRef.current !== round) return;
          if (!isCurrent(sessionId, epoch, clientKey)) return;
          const next = {
            fenced: "fenced" in snapshot && snapshot.fenced === true,
            stage: String(snapshot.stage ?? ""),
            coreVersion: String(snapshot.core_version ?? ""),
            generation:
              typeof snapshot.generation === "number"
                ? snapshot.generation
                : -1,
            activeUpgrade:
              snapshot.active_upgrade &&
              typeof snapshot.active_upgrade === "object"
                ? {
                    upgrade_id: String(
                      snapshot.active_upgrade.upgrade_id ?? "",
                    ),
                    stage: String(snapshot.active_upgrade.stage ?? ""),
                  }
                : null,
          };
          // Pure compare against the ref snapshot: no setState inside another
          // updater, so a StrictMode replay cannot double-clear.
          const prev = upgradeRef.current;
          if (
            prev &&
            prev.generation !== -1 &&
            next.generation !== -1 &&
            (prev.generation !== next.generation ||
              prev.coreVersion !== next.coreVersion)
          ) {
            // The server changed under us: nothing displayed stays
            // actionable until catalog+views are refetched below.
            setCatalog({ revisionId: "", commands: [] });
            setViewScope((vs) =>
              vs.sessionId === sessionId &&
              vs.epoch === epoch &&
              vs.clientKey === clientKey
                ? { ...vs, revisionId: "", views: [] }
                : vs,
            );
          }
          upgradeRef.current = next;
          setUpgrade(next);
          if (terminalErrorRef.current === null) {
            setStatusMessage(
              next.fenced
                ? t("core.upgradeFenced", {
                    stage: next.activeUpgrade?.stage ?? next.stage,
                  })
                : t("core.statusConnected", { sessionId }),
            );
          }
        })
        .catch((err) => {
          // Unreachable during a handoff gap is expected and must not clobber
          // the last known identity.
          if (!isCurrent(sessionId, epoch, clientKey)) return;
          console.error("Failed to fetch core health", err);
        });
      refreshCatalog();
      refreshViews(sessionId, epoch, clientKey);
      const activityAtIssue = runActivityRef.current;
      const cursorAtIssue = lastSequenceRef.current;
      client
        .show(sessionId)
        .then((snapshot) => {
          if (healthRequestRef.current !== round) return;
          if (!isCurrent(sessionId, epoch, clientKey)) return;
          // A newer user run started after this round was issued: its own
          // lifecycle owns isRunning now, not this stale read.
          if (runActivityRef.current !== activityAtIssue) return;
          // Status only: the applied cursor stays owned by event
          // application, never replaced by show.last_event_sequence. And any
          // event applied after this round was issued (e.g. a terminal fact
          // the stream just delivered) wins over this read.
          if (lastSequenceRef.current !== cursorAtIssue) return;
          setIsRunning(snapshot.status === "running");
        })
        .catch((err) => {
          if (!isCurrent(sessionId, epoch, clientKey)) return;
          console.error("Failed to reconcile session status", err);
        });
    },
    [client, isCurrent, refreshCatalog, refreshViews, t],
  );

  const handleCreateSession = useCallback(async () => {
    const epoch = epochRef.current;
    const clientKey = renderedClientKey;
    const issueSession = sessionRef.current;
    try {
      const newSession = await client.createSession();
      if (!isCurrent(issueSession, epoch, clientKey)) {
        return; // another switch or transport happened while it was created
      }
      setSessions((prev) => [newSession, ...prev]);
      setActiveSessionId(newSession.session_id);
    } catch (err: unknown) {
      if (!isCurrent(issueSession, epoch, clientKey)) return;
      const msg = err instanceof Error ? err.message : String(err);
      setStatusMessage(t("core.statusCreateFailed", { message: msg }));
    }
  }, [client, isCurrent, renderedClientKey, t]);
  const handleCancelRun = async () => {
    if (!activeSessionId) return;
    const issueSession = activeSessionId;
    const epoch = epochRef.current;
    const clientKey = renderedClientKey;
    try {
      const res = await client.cancel(issueSession);
      if (!isCurrent(issueSession, epoch, clientKey)) return;
      setStatusMessage(
        res.cancellation_requested
          ? t("core.cancellationRequested")
          : t("core.noActiveRun"),
      );
    } catch (err: unknown) {
      if (!isCurrent(issueSession, epoch, clientKey)) return;
      const msg = err instanceof Error ? err.message : String(err);
      setStatusMessage(t("core.cancelFailed", { message: msg }));
    }
  };

  // A transport change invalidates every in-flight result before paint, and does
  // so even when no session is selected yet (the layout session effect returns
  // early on an empty id, so client refs must be cleared here).
  const bootstrapRef = useRef<CoreClient | null>(null);
  useLayoutEffect(() => {
    if (clientKeyRef.current === renderedClientKey) {
      return;
    }
    // A new transport owns every later fetch: the previous client's callbacks are
    // refused by identity even if they resolve after the switch.
    bootstrapRef.current = null;
    clientKeyRef.current = renderedClientKey;
    epochRef.current += 1;
    sessionRef.current = "";
    selectionRef.current = "";
    setActiveSessionId((current) => (current === "" ? current : ""));
    setScope({
      sessionId: "",
      epoch: epochRef.current,
      clientKey: renderedClientKey,
    });
    setViewScope({
      sessionId: "",
      epoch: -1,
      clientKey: renderedClientKey,
      revisionId: "",
      views: [],
    });
    // The previous transport's catalog and session rows must not stay actionable
    // under the new one, and every read it issued is refused by its own serial.
    catalogRequestRef.current += 1;
    viewsRequestRef.current += 1;
    healthRequestRef.current += 1;
    commandViewRequestRef.current += 1;
    commandViewLiveRef.current = null;
    setCommandView(null);
    setCatalog({ revisionId: "", commands: [] });
    setSessions([]);
    setEvents([]);
    setPendingApproval(null);
    resolvedConfirmationsRef.current.clear();
    setPendingConfirmations({});
    setIsRunning(false);
    setCommandInFlight(false);
    upgradeRef.current = null;
    setUpgrade(null);
  }, [renderedClientKey]);

  // Bootstrap fetches the catalog and the session list once per client object;
  // invalidation itself happened in the layout effect above.
  useEffect(() => {
    const issued = client;
    if (bootstrapRef.current === issued) {
      return; // StrictMode remount must not re-issue these reads
    }
    bootstrapRef.current = issued;

    refreshCatalog();

    issued
      .listSessions()
      .then((list) => {
        if (bootstrapRef.current !== issued) return;
        setSessions(list);
        // A session the user already picked (or created) always wins.
        if (selectionRef.current !== "") {
          return;
        }
        if (list.length > 0) {
          setActiveSessionId(list[0].session_id);
        } else {
          void handleCreateSession();
        }
      })
      .catch((err) => {
        if (bootstrapRef.current !== issued) return;
        setStatusMessage(t("core.statusListFailed", { message: String(err) }));
      });
  }, [client, handleCreateSession, refreshCatalog, t]);

  // Invalidate the previous scope BEFORE paint: layout effect, not passive.
  // It must not act while the rendered scope still belongs to the previous
  // transport: the client-change layout above runs first in this same commit and
  // clears the selection, so acting here would re-query the old session under
  // the new client and strand the UI.
  useLayoutEffect(() => {
    if (!activeSessionId) return;
    if (scope.clientKey !== renderedClientKey) return;
    // Bump the epoch: anything still in flight from the previous session (or a
    // return to a previously visited session id) is stale from here on.
    epochRef.current += 1;
    const epoch = epochRef.current;
    const clientKey = renderedClientKey;
    clientKeyRef.current = clientKey;
    sessionRef.current = activeSessionId;
    selectionRef.current = activeSessionId;
    setScope({ sessionId: activeSessionId, epoch, clientKey });
    setViewScope({
      sessionId: activeSessionId,
      epoch,
      clientKey,
      revisionId: "",
      views: [],
    });
    setEvents([]);
    lastSequenceRef.current = 0;
    terminalErrorRef.current = null;
    resolvedConfirmationsRef.current.clear();
    commandViewRequestRef.current += 1;
    commandViewLiveRef.current = null;
    setCommandView(null);
    setPendingConfirmations({});
    setPendingApproval(null);
    setIsRunning(false);
    setCommandInFlight(false);
    setStatusMessage(
      t("core.statusLoadingSession", { sessionId: activeSessionId }),
    );
    refreshViews(activeSessionId, epoch, clientKey);
  }, [activeSessionId, renderedClientKey, refreshViews, scope.clientKey, t]);

  // Handle session change: subscribe to the event stream of the bound scope.
  useEffect(() => {
    if (!activeSessionId) return;

    if (
      scope.sessionId !== activeSessionId ||
      scope.clientKey !== renderedClientKey
    ) {
      return; // the rendered scope is not this session/transport yet
    }
    const boundSession = scope.sessionId;
    const boundEpoch = scope.epoch;
    const boundClientKey = scope.clientKey;
    const cleanup = client.streamEvents(
      boundSession,
      0,
      (frame: SessionFrame) => {
        if (!isCurrent(boundSession, boundEpoch, boundClientKey)) {
          return; // a follower from a superseded session/epoch: never render it
        }
        if ("events" in frame) {
          if (frame.session.session_id !== boundSession) {
            return;
          }
          applyEvents(frame.events, boundSession, boundEpoch, boundClientKey);
          return;
        }
        if (frame.kind === "server_restart") {
          // The server replaced itself. The frame cursor is a watermark of
          // what THAT stream delivered: the reconnect resumes from this
          // client's own applied cursor (lastSequenceRef), so nothing applied
          // is skipped and nothing unapplied is jumped over. Reconciliation
          // itself runs on the reconnected stream (onReconnect below), so an
          // announced restart and a silent drop share one code path.
          setStatusMessage(t("core.statusReconnecting"));
          return;
        }
        if (frame.kind === "error") {
          // Terminal stream failure: the transport already stopped this
          // stream, so show the safe error once and stay stopped. The
          // applied cursor never advances past the unapplied bad record,
          // and no later reconcile may overwrite the terminal message.
          terminalErrorRef.current = frame.error;
          setStatusMessage(
            t("core.streamError", { message: terminalErrorRef.current }),
          );
          return;
        }
        applyEvents([frame.event], boundSession, boundEpoch, boundClientKey);
      },
      (err) => {
        if (!isCurrent(boundSession, boundEpoch, boundClientKey)) {
          return; // a superseded follower's failure must not touch this view
        }
        if (terminalErrorRef.current === null) {
          setStatusMessage(t("core.streamError", { message: err.message }));
        }
      },
      () => {
        // ONE establishment source: the transport fires this once per
        // connection that yields its first frame — initial, announced
        // restart, or silent-drop reconnect — so reconcile unconditionally
        // after the scope guard. No head/ready-flag second path.
        if (!isCurrent(boundSession, boundEpoch, boundClientKey)) {
          return; // a superseded follower's reconnect must not touch this view
        }
        reconcileConnection(boundSession, boundEpoch, boundClientKey);
      },
    );
    return () => {
      cleanup();
    };
  }, [
    activeSessionId,
    client,
    renderedClientKey,
    scope.sessionId,
    scope.clientKey,
    scope.epoch,
    refreshCatalog,
    refreshHealth,
    refreshViews,
    reconcileConnection,
    isCurrent,
    applyEvents,
    t,
  ]);

  // Scroll to bottom on new events
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [events]);

  const handleApprovalDecision = async (decision: "allow" | "deny") => {
    const approval = pendingApproval;
    if (!approval) return;
    if (!isCurrent(approval.sessionId, approval.epoch, approval.clientKey)) {
      setPendingApproval(null);
      return;
    }
    // The gate stays mounted until the server accepts or a durable decided
    // fact closes it: clearing early would strand the approval, because
    // applied-cursor dedup never replays the old request fact.
    if (inFlightRef.current.has(approval.requestId)) return;
    inFlightRef.current.add(approval.requestId);
    runActivityRef.current += 1;
    setIsRunning(true);
    try {
      // Native approvals resume the parked run with the exact request identity;
      // /confirm is only for plugin Action confirmations.
      const res = await client.resume(
        approval.sessionId,
        approval.requestId,
        decision,
      );
      if (!isCurrent(approval.sessionId, approval.epoch, approval.clientKey))
        return;
      setPendingApproval(null);
      const reparked = parkedApprovalOf(res.pending_approval);
      if (reparked) {
        setPendingApproval({
          sessionId: approval.sessionId,
          epoch: approval.epoch,
          clientKey: approval.clientKey,
          requestId: reparked.requestId,
          toolName: reparked.toolName ?? t("core.unknownLabel"),
          toolArguments: JSON.stringify(reparked.toolArguments),
        });
      }
      setStatusMessage(
        t("core.statusApproved", { decision, status: res.status }),
      );
      refreshViews(approval.sessionId, approval.epoch, approval.clientKey);
    } catch (err: unknown) {
      if (!isCurrent(approval.sessionId, approval.epoch, approval.clientKey))
        return;
      if (err instanceof CoreApiError && err.code === "upgrade_pending") {
        // The gate stays armed with the same request id: no auto-resend, the
        // user re-decides after the restart. Refresh health for the banner.
        refreshHealth(approval.sessionId, approval.epoch, approval.clientKey);
        setStatusMessage(t("core.statusUpgradePending"));
        return;
      }
      const msg = err instanceof Error ? err.message : String(err);
      setStatusMessage(t("core.statusApprovalFailed", { message: msg }));
    } finally {
      inFlightRef.current.delete(approval.requestId);
      if (isCurrent(approval.sessionId, approval.epoch, approval.clientKey)) {
        setIsRunning(false);
      }
    }
  };

  const handleConfirmDecision = async (
    requestId: string,
    decision: "allow" | "deny",
    revisionId: string,
  ) => {
    if (!activeSessionId) return;
    const issueSession = activeSessionId;
    const epoch = epochRef.current;
    const clientKey = renderedClientKey;
    // The entry stays mounted until the server accepts or the durable decided
    // fact closes it: removing early would strand the request, because
    // applied-cursor dedup never replays the old requested fact.
    if (inFlightRef.current.has(requestId)) return;
    inFlightRef.current.add(requestId);
    try {
      const res = await client.confirm(
        issueSession,
        requestId,
        decision,
        revisionId,
      );
      if (!isCurrent(issueSession, epoch, clientKey)) return;
      // The server accepted the decision: the entry closes now, and the
      // durable decided fact (or its replay) confirms it permanently.
      setPendingConfirmations((prev) => {
        if (!(requestId in prev)) return prev;
        const next = { ...prev };
        delete next[requestId];
        return next;
      });
      setStatusMessage(
        t("core.statusConfirm", { decision, status: res.status }),
      );
    } catch (err: unknown) {
      if (!isCurrent(issueSession, epoch, clientKey)) return;
      if (err instanceof CoreApiError && err.code === "upgrade_pending") {
        refreshHealth(issueSession, epoch, clientKey);
        setStatusMessage(t("core.statusUpgradePending"));
        return;
      }
      const msg = err instanceof Error ? err.message : String(err);
      setStatusMessage(t("core.statusConfirmFailed", { message: msg }));
    } finally {
      inFlightRef.current.delete(requestId);
    }
  };

  const prepareCommandInput = async (
    cmd: string,
    revisionId: string,
    args: Record<string, unknown> = {},
  ) => {
    if (!activeSessionId) return;
    const descriptor = catalog.commands.find((entry) => entry.name === cmd);
    if (!descriptor?.has_input || catalog.revisionId !== revisionId) {
      setStatusMessage(t("core.commandInputRevisionChanged"));
      return;
    }
    const serial = ++commandViewRequestRef.current;
    const issueSession = activeSessionId;
    const epoch = epochRef.current;
    const clientKey = renderedClientKey;
    try {
      const snapshot = await client.commandInput(
        issueSession,
        cmd,
        revisionId,
        args,
      );
      if (
        !isCurrent(issueSession, epoch, clientKey) ||
        serial !== commandViewRequestRef.current
      )
        return;
      if (snapshot.revision_id !== revisionId)
        throw new Error(t("core.commandInputRevisionChanged"));
      const presentation = snapshot.presentation as {
        title: string;
        view_type: PluginViewDescriptor["view_type"];
        data: PluginViewDescriptor["data"];
        refresh_after_ms?: number | null;
      };
      const refreshAfterMs =
        typeof presentation.refresh_after_ms === "number"
          ? presentation.refresh_after_ms
          : null;
      if (refreshAfterMs !== null) {
        commandViewLiveRef.current = {
          action: cmd,
          args: { ...args },
          revisionId,
        };
      } else {
        commandViewLiveRef.current = null;
      }
      setCommandView({
        sessionId: issueSession,
        epoch,
        clientKey,
        revisionId,
        descriptor: {
          owner: descriptor.owner,
          name: cmd,
          title: presentation.title,
          view_type: presentation.view_type,
          data: presentation.data,
        },
        liveSource:
          refreshAfterMs !== null
            ? { action: cmd, args: { ...args } }
            : undefined,
        refreshAfterMs,
      });
    } catch (err) {
      if (
        isCurrent(issueSession, epoch, clientKey) &&
        serial === commandViewRequestRef.current
      ) {
        setStatusMessage(
          t("core.statusInvokeFailed", {
            message: err instanceof Error ? err.message : String(err),
          }),
        );
      }
    }
  };

  const handleViewAction = async (
    actionName: string,
    args: Record<string, unknown>,
    revisionId: string,
    prepareInput = false,
  ) => {
    if (prepareInput) {
      await prepareCommandInput(actionName, revisionId, args);
      return;
    }
    if (!activeSessionId) return;
    const interactionSerial = ++commandViewRequestRef.current;
    const issueSession = activeSessionId;
    const epoch = epochRef.current;
    const clientKey = renderedClientKey;
    setCommandInFlight(true);
    const displayName = displaySlashPath(catalog.commands, actionName);
    setStatusMessage(t("core.statusInvoking", { name: displayName }));
    try {
      const res = await client.invoke(
        issueSession,
        actionName,
        args,
        revisionId,
      );
      if (!isCurrent(issueSession, epoch, clientKey)) {
        return; // session or epoch changed while the command ran: drop the reply
      }
      setStatusMessage(
        res.status === "error"
          ? t("core.statusInvokeFailed", { message: res.error || res.status })
          : t("core.statusInvoked", { name: displayName, status: res.status }),
      );
      if (res.status === "pending_confirmation" && res.confirmation) {
        const confirmation = res.confirmation;
        const requestId =
          confirmation.request_id ?? confirmation.ui_data?.request_id;
        if (typeof requestId === "string" && requestId) {
          retainPendingConfirmation(requestId, {
            owner: "core",
            name: "pending_confirmation",
            title: t("core.approvalRequired"),
            view_type: confirmation.ui_type,
            data: {
              ...confirmation.ui_data,
              request_id: requestId,
            },
            revisionId,
          });
        }
      }
      if (
        res.status === "ok" &&
        commandViewRequestRef.current === interactionSerial
      ) {
        // A direct invoke receipt replaces the view: it never inherits polling.
        commandViewLiveRef.current = null;
        const presentation = res.presentation;
        setCommandView(
          presentation
            ? {
                sessionId: issueSession,
                epoch,
                clientKey,
                revisionId,
                descriptor: {
                  owner: "command",
                  name: actionName,
                  ...presentation,
                },
                refreshAfterMs: null,
              }
            : null,
        );
      }
      refreshViews(issueSession, epoch, clientKey);
    } catch (err: unknown) {
      if (!isCurrent(issueSession, epoch, clientKey)) return;
      if (err instanceof CoreApiError && err.code === "upgrade_pending") {
        setStatusMessage(
          t("core.statusUpgradePendingAction", { name: displayName }),
        );
        return;
      }
      const msg = err instanceof Error ? err.message : String(err);
      setStatusMessage(t("core.statusInvokeFailed", { message: msg }));
    } finally {
      if (isCurrent(issueSession, epoch, clientKey)) {
        setCommandInFlight(false);
      }
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const text = promptInput.trim();
    if (!text || !activeSessionId || isRunning) return;
    if (upgrade?.fenced) {
      // Never queue a prompt behind a fence: the user re-sends after reconnect.
      setStatusMessage(t("core.statusUpgradePending"));
      return;
    }
    setPromptInput("");
    if (text.startsWith("/")) {
      // Slash dispatch matches the longest registered public path (`/goal pause`
      // wins over `/goal`); the tail after it is the verbatim argument string.
      const body = text.slice(1).trimStart();
      const tokens = body.split(/\s+/).filter((token) => token !== "");
      const match =
        tokens.length > 0 ? matchSlashPath(catalog.commands, tokens) : null;

      // Admitted plugin commands always win: local controls never shadow a
      // catalog command that happens to share the name.
      if (match === null) {
        const cmd = tokens[0] ?? "";
        if (cmd === "cancel") {
          await handleCancelRun();
          return;
        }
        if (cmd === "views") {
          refreshViews(activeSessionId, epochRef.current, renderedClientKey);
          return;
        }
        if (cmd === "new") {
          await handleCreateSession();
          return;
        }
        setStatusMessage(t("core.statusUnknownCommand", { name: body }));
        return;
      }
      if ("ambiguous" in match) {
        setStatusMessage(t("core.statusUnknownCommand", { name: body }));
        return;
      }

      const descriptor = match.descriptor;
      const cmd = descriptor.name;
      // Consume the matched tokens, then one whitespace run; the rest (JSON,
      // paths, dots) stays verbatim.
      let rest = body;
      for (let index = 0; index < match.length; index++) {
        rest = rest.slice(tokens[index].length).trimStart();
      }
      const argStr = rest.trim();

      if (!argStr && descriptor.has_input) {
        await prepareCommandInput(cmd, catalog.revisionId);
        return;
      }

      const schema = descriptor.argument_schema ?? {};
      const properties =
        schema.properties !== null && typeof schema.properties === "object"
          ? (schema.properties as Record<string, { type?: string }>)
          : {};
      const required = Array.isArray(schema.required)
        ? schema.required.filter((r): r is string => typeof r === "string")
        : [];

      let parsedArgs: Record<string, unknown>;
      if (
        !argStr &&
        required.length > 0 &&
        Object.keys(properties).length > 0
      ) {
        const types = {
          string: "text",
          integer: "integer",
          number: "number",
          boolean: "boolean",
        } as const;
        if (
          Object.values(properties).every(
            (spec) =>
              spec &&
              typeof spec === "object" &&
              spec.type &&
              spec.type in types,
          )
        ) {
          const fields: FormField[] = Object.entries(properties).map(
            ([name, spec]) => ({
              name,
              label: name,
              required: required.includes(name),
              field_type: types[spec.type as keyof typeof types],
            }),
          );
          commandViewRequestRef.current += 1;
          commandViewLiveRef.current = null;
          setCommandView({
            sessionId: activeSessionId,
            epoch: epochRef.current,
            clientKey: renderedClientKey,
            revisionId: catalog.revisionId,
            descriptor: {
              owner: descriptor.owner,
              name: descriptor.name,
              title: descriptor.description,
              view_type: "form",
              data: { fields, action: cmd, initial_values: {} },
            },
            refreshAfterMs: null,
          });
          return;
        }
      }
      if (!argStr) {
        parsedArgs = {};
      } else if (
        descriptor.text_argument &&
        !argStr.trimStart().startsWith("{")
      ) {
        parsedArgs = { [descriptor.text_argument]: argStr };
      } else if (
        Object.keys(properties).length === 1 &&
        required.length === 1 &&
        properties[required[0]]?.type === "string"
      ) {
        parsedArgs = { [required[0]]: argStr };
      } else {
        try {
          parsedArgs = JSON.parse(argStr) as Record<string, unknown>;
        } catch {
          if (Object.keys(properties).length === 1) {
            const propName = Object.keys(properties)[0];
            parsedArgs = { [propName]: argStr };
          } else {
            setStatusMessage(t("core.statusInvalidJsonArgs", { args: argStr }));
            return;
          }
        }
      }
      await handleViewAction(cmd, parsedArgs, catalog.revisionId);
    } else {
      // Normal run prompt (fully epoch-guarded: success, failure and finally).
      const issueSession = activeSessionId;
      const epoch = epochRef.current;
      const clientKey = renderedClientKey;
      runActivityRef.current += 1;
      setIsRunning(true);
      setStatusMessage(t("core.statusDispatching"));
      try {
        const res = await client.run(issueSession, text);
        if (!isCurrent(issueSession, epoch, clientKey)) return;
        const parked = parkedApprovalOf(res.pending_approval);
        if (parked) {
          setPendingApproval({
            sessionId: issueSession,
            epoch,
            clientKey,
            requestId: parked.requestId,
            toolName: parked.toolName ?? t("core.unknownLabel"),
            toolArguments: JSON.stringify(parked.toolArguments),
          });
        }
        setStatusMessage(
          res.status === "waiting"
            ? t("core.statusWaitingApproval")
            : t("core.statusRun", { status: res.status }),
        );
        refreshViews(issueSession, epoch, clientKey);
      } catch (err: unknown) {
        if (!isCurrent(issueSession, epoch, clientKey)) return;
        if (err instanceof CoreApiError && err.code === "upgrade_pending") {
          refreshHealth(issueSession, epoch, clientKey);
          // Definite non-dispatch: restore the exact draft the user sent —
          // but never overwrite a newer draft typed while it was in flight,
          // and never auto-submit.
          setPromptInput((current) => (current.trim() === "" ? text : current));
          setStatusMessage(t("core.statusUpgradePending"));
          return;
        }
        const msg = err instanceof Error ? err.message : String(err);
        setStatusMessage(t("core.statusRunFailed", { message: msg }));
      } finally {
        if (isCurrent(issueSession, epoch, clientKey)) {
          setIsRunning(false);
        }
      }
    }
  };

  const slashCommands = useMemo(
    () => catalog.commands.filter((entry) => entry.slash_visible !== false),
    [catalog],
  );

  // Catalog-declared shortcut actions, computed once per catalog (never per
  // render/keystroke). `actions` lists every declared shortcut command so all
  // stay clickable as buttons; a chord with 2+ claimants binds no key and the
  // conflict is shown, never silently first-wins. This round binds no browser
  // keyboard chord: entry is buttons only.
  const shortcutEntries = useMemo(() => {
    const claimants = new Map<string, typeof catalog.commands>();
    for (const entry of catalog.commands) {
      const chord = entry.default_shortcut ?? null;
      if (typeof chord !== "string" || !chord) continue;
      const list = claimants.get(chord) ?? [];
      list.push(entry);
      claimants.set(chord, list);
    }
    const actions: Array<{
      chord: string;
      entry: (typeof catalog.commands)[number];
      conflicted: boolean;
    }> = [];
    const conflicts: Array<{ chord: string; names: string[] }> = [];
    for (const [chord, entries] of claimants) {
      const conflicted = entries.length > 1;
      for (const entry of entries) actions.push({ chord, entry, conflicted });
      if (conflicted)
        conflicts.push({ chord, names: entries.map((e) => e.name) });
    }
    actions.sort(
      (a, b) =>
        a.chord.localeCompare(b.chord) ||
        a.entry.name.localeCompare(b.entry.name),
    );
    conflicts.sort((a, b) => a.chord.localeCompare(b.chord));
    return { actions, conflicts };
  }, [catalog]);

  // Live command-input polling: only explicit refresh_after_ms on the open
  // view enables it, and only while that view is mounted for this scope.
  // Gates and command flights own the region while open, so the loop pauses
  // while one is mounted: a refresh never steals focus or resets a draft.
  // The loop never touches the interaction serial: navigation, prompt edits,
  // and invokes must neither freeze it nor be invalidated by it. Staleness
  // is the captured live-source identity (navigation/close replaces it) plus
  // the scope guards every other read already uses.
  useEffect(() => {
    if (commandView === null) return;
    if (
      commandView.sessionId !== activeSessionId ||
      commandView.epoch !== scope.epoch ||
      commandView.clientKey !== renderedClientKey ||
      commandView.revisionId !== catalog.revisionId
    )
      return;
    if (typeof commandView.refreshAfterMs !== "number") return;
    if (pendingApproval !== null || pendingConfirmation !== null) return;
    if (commandInFlight) return;
    if (
      commandView.descriptor.view_type !== "choices" &&
      commandView.descriptor.view_type !== "markdown"
    )
      return;
    const live = commandViewLiveRef.current;
    if (live === null) return;
    const issueSession = activeSessionId;
    const epoch = scope.epoch;
    const clientKey = renderedClientKey;
    const cadence = commandView.refreshAfterMs;
    let cancelled = false;
    let timer: number | undefined;
    // A reply is current only while its captured source is still the live
    // one: any navigation or close replaces the ref object and orphans it.
    const isLiveReply = () =>
      !cancelled &&
      isCurrent(issueSession, epoch, clientKey) &&
      commandViewLiveRef.current === live;
    const readOnce = () => {
      if (!isLiveReply()) return;
      client
        .commandInput(issueSession, live.action, live.revisionId, live.args)
        .then((snapshot) => {
          if (!isLiveReply()) return;
          if (snapshot.revision_id !== live.revisionId) return;
          const presentation = snapshot.presentation as {
            title: string;
            view_type: PluginViewDescriptor["view_type"];
            data: PluginViewDescriptor["data"];
            refresh_after_ms?: number | null;
          };
          const nextCadence =
            typeof presentation.refresh_after_ms === "number"
              ? presentation.refresh_after_ms
              : cadence;
          setCommandView((prev) =>
            prev === null ||
            prev.sessionId !== issueSession ||
            prev.epoch !== epoch ||
            prev.clientKey !== clientKey
              ? prev
              : {
                  ...prev,
                  descriptor: {
                    ...prev.descriptor,
                    title: presentation.title,
                    view_type: presentation.view_type,
                    data: presentation.data,
                  },
                  refreshAfterMs: nextCadence,
                },
          );
          // Reschedule from the cadence the source carries: success and
          // failure alike, so one bad read never freezes the panel.
          if (isLiveReply()) timer = window.setTimeout(readOnce, nextCadence);
        })
        .catch((err) => {
          if (!isLiveReply()) return;
          setStatusMessage(
            t("core.statusInvokeFailed", {
              message: err instanceof Error ? err.message : String(err),
            }),
          );
          if (isLiveReply()) timer = window.setTimeout(readOnce, cadence);
        });
    };
    timer = window.setTimeout(readOnce, cadence);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [
    commandView,
    activeSessionId,
    scope.epoch,
    renderedClientKey,
    catalog.revisionId,
    pendingApproval,
    pendingConfirmation,
    commandInFlight,
    client,
    isCurrent,
    t,
  ]);
  // Autocomplete hints
  const slashSuggestions = useMemo(() => {
    if (
      completionDismissed ||
      !promptInput.startsWith("/") ||
      promptInput.includes("\n")
    ) {
      return [];
    }
    const query = promptInput.slice(1).toLowerCase();
    const builtins = ["help", "cancel", "views", "new"];
    const fromCatalog = slashCommands.map(slashPathOf);
    const all = Array.from(new Set([...builtins, ...fromCatalog]));
    const split = query.lastIndexOf(" ");
    const base = split < 0 ? "" : query.slice(0, split + 1);
    const partial = query.slice(split + 1);
    return Array.from(
      new Set(
        all
          .filter(
            (name) =>
              name.toLowerCase().startsWith(base) &&
              name.slice(base.length).toLowerCase().startsWith(partial),
          )
          .map((name) => base + name.slice(base.length).split(" ")[0]),
      ),
    )
      .filter(Boolean)
      .sort();
  }, [promptInput, slashCommands, completionDismissed]);

  return (
    <div
      data-testid="core-app"
      className="flex h-screen w-screen bg-neutral-950 text-neutral-100 font-sans overflow-hidden"
    >
      {/* Sidebar */}
      <aside className="w-64 border-r border-neutral-800 bg-neutral-900/50 flex flex-col justify-between">
        <div>
          <div className="p-4 border-b border-neutral-800 flex items-center justify-between">
            <span className="font-bold text-sm tracking-wide text-neutral-200 uppercase">
              {t("core.title")} — VoidCode
            </span>
            <button
              data-testid="new-session-btn"
              type="button"
              onClick={handleCreateSession}
              className="px-2.5 py-1 text-xs rounded bg-cyan-600 hover:bg-cyan-500 text-white font-medium transition-colors"
            >
              + {t("core.newSession")}
            </button>
          </div>
          <div className="p-2 overflow-y-auto max-h-[calc(100vh-140px)]">
            <div className="text-xs uppercase text-neutral-500 font-semibold px-2 py-1 mb-1 tracking-wider">
              {t("core.sessions")}
            </div>
            {sessions.map((s) => (
              <button
                key={s.session_id}
                data-testid={`session-tab-${s.session_id}`}
                type="button"
                onClick={() => setActiveSessionId(s.session_id)}
                className={`w-full text-left px-3 py-2 rounded text-xs mb-1 transition-colors flex items-center justify-between ${
                  s.session_id === activeSessionId
                    ? "bg-neutral-800 text-cyan-400 font-semibold"
                    : "text-neutral-400 hover:bg-neutral-800/60 hover:text-neutral-200"
                }`}
              >
                <span className="truncate">{s.session_id}</span>
                <span className="text-[10px] text-neutral-500 ml-2">
                  t:{s.turn}
                </span>
              </button>
            ))}
          </div>
        </div>

        <div className="p-3 border-t border-neutral-800 text-[11px] text-neutral-500 truncate">
          {t("core.statusPrefix")}:{" "}
          <span className="text-neutral-300">{statusMessage}</span>
        </div>
      </aside>

      {/* Main chat & views */}
      <main className="flex-1 flex flex-col h-full overflow-hidden bg-neutral-950">
        {/* Header */}
        <header className="h-12 border-b border-neutral-800 px-4 flex items-center justify-between bg-neutral-900/20">
          <div className="flex items-center gap-2">
            <span className="text-xs text-neutral-400 font-mono">
              {t("core.sessionLabel")}:
            </span>
            <span className="text-xs font-semibold text-neutral-200 font-mono">
              {activeSessionId || t("core.noneLabel")}
            </span>
          </div>
          <div className="flex items-center gap-2">
            {isRunning && (
              <span className="text-xs text-cyan-400 animate-pulse font-medium">
                {t("core.runningLabel")}
              </span>
            )}
            {shortcutEntries.actions.map(({ chord, entry, conflicted }) => (
              <button
                key={entry.name}
                data-testid={`shortcut-action-${entry.name}`}
                type="button"
                disabled={
                  !activeSessionId ||
                  pendingApproval !== null ||
                  pendingConfirmation !== null
                }
                title={
                  conflicted
                    ? `${entry.description} (${t("core.shortcutConflicted")})`
                    : `${entry.description} (${chord})`
                }
                aria-label={
                  conflicted
                    ? `${entry.description} (${t("core.shortcutConflicted")})`
                    : `${entry.description} (${chord})`
                }
                onClick={() => {
                  if (entry.has_input) {
                    void prepareCommandInput(
                      entry.name,
                      catalog.revisionId,
                      {},
                    );
                  } else {
                    void handleViewAction(entry.name, {}, catalog.revisionId);
                  }
                }}
                className="px-2.5 py-1 text-xs rounded border border-neutral-700 bg-neutral-800 hover:bg-neutral-700 disabled:opacity-40 text-neutral-300"
              >
                {entry.description}
              </button>
            ))}
            <button
              data-testid="cancel-btn"
              type="button"
              onClick={handleCancelRun}
              className="px-2.5 py-1 text-xs rounded border border-neutral-700 bg-neutral-800 hover:bg-neutral-700 text-neutral-300"
            >
              {t("core.cancel")}
            </button>
          </div>
        </header>
        {/* Shortcut conflicts: explicit, never silent first-wins. */}
        {shortcutEntries.conflicts.map(({ chord, names }) => (
          <div
            key={chord}
            data-testid={`shortcut-conflict-${chord}`}
            className="px-4 py-1.5 bg-amber-950/60 border-b border-amber-800 text-[11px] text-amber-200 font-mono truncate"
          >
            {t("core.shortcutConflict", { chord, names: names.join(", ") })}
          </div>
        ))}
        {/* Controlled-upgrade banner: fence state and server identity */}
        {upgrade && (upgrade.fenced || upgrade.activeUpgrade) && (
          <div
            data-testid="upgrade-banner"
            className="px-4 py-1.5 bg-amber-950/60 border-b border-amber-800 text-[11px] text-amber-200 font-mono truncate"
          >
            {upgrade.fenced
              ? t("core.upgradeFenced", {
                  stage: upgrade.activeUpgrade?.stage ?? upgrade.stage,
                })
              : t("core.upgradeActive", {
                  stage: upgrade.activeUpgrade?.stage ?? "",
                })}{" "}
            <span className="text-amber-400/70">
              {upgrade.coreVersion} · gen {upgrade.generation}
            </span>
          </div>
        )}

        {/* Content area: Transcript + Plugin Views */}
        <div className="flex-1 flex overflow-hidden">
          {/* Transcript */}
          <div className="flex-1 overflow-y-auto p-4 space-y-3">
            {events.map((ev) => {
              // Conversation rows select on the canonical content kind, never
              // the core storage event name: continuation/wakeup facts project
              // to the same message kind as user turns.
              const content = ev.transcript?.content;
              if (content?.kind === "message") {
                const isUser = content.body.role === "user";
                return (
                  <div
                    key={ev.sequence}
                    data-testid={`event-${ev.sequence}`}
                    className={`flex flex-col ${isUser ? "items-end" : "items-start"}`}
                  >
                    <div
                      className={`max-w-2xl px-4 py-2.5 rounded-2xl text-sm whitespace-pre-wrap ${
                        isUser
                          ? "bg-cyan-700/80 text-white"
                          : "bg-neutral-800 text-neutral-100"
                      }`}
                    >
                      {content.body.content ?? ""}
                    </div>
                  </div>
                );
              }
              // The dynamic payload keeps gates/commands only.
              if (content?.kind === "turn") {
                const output = content.body.output ?? null;
                const toolCalls = content.body.tool_calls ?? [];
                return (
                  <div
                    key={ev.sequence}
                    data-testid={`event-${ev.sequence}`}
                    className="flex flex-col items-start space-y-2 max-w-2xl"
                  >
                    {output && (
                      <div className="px-4 py-2.5 rounded-2xl bg-neutral-800 text-neutral-100 text-sm whitespace-pre-wrap">
                        {output}
                      </div>
                    )}
                    {toolCalls.map((tc, idx) => (
                      <div
                        key={idx}
                        className="px-3 py-1.5 rounded-md bg-neutral-900 border border-neutral-700 text-xs font-mono text-amber-300"
                      >
                        ▶ {t("core.toolCallLabel")}:{" "}
                        {tc.tool_name || t("core.unknownLabel")}
                        <span className="text-neutral-400 ml-2">
                          {JSON.stringify(tc.arguments ?? {})}
                        </span>
                      </div>
                    ))}
                  </div>
                );
              }
              if (content?.kind === "tool") {
                return (
                  <ToolResultCard
                    key={ev.sequence}
                    evSequence={ev.sequence}
                    tool={content.body}
                    t={t}
                  />
                );
              }
              if (ev.type === "core.command_completed") {
                const name =
                  typeof ev.payload.name === "string" ? ev.payload.name : null;
                const result =
                  ev.payload.result !== null &&
                  typeof ev.payload.result === "object"
                    ? (ev.payload.result as Record<string, unknown>)
                    : null;
                const status =
                  typeof result?.status === "string"
                    ? result.status
                    : "unknown";
                const detail =
                  typeof result?.output === "string"
                    ? result.output
                    : typeof result?.error === "string"
                      ? result.error
                      : "";
                const tone =
                  status === "error"
                    ? "text-red-400 bg-red-950/20 border-red-900/50"
                    : status === "pending_confirmation"
                      ? "text-amber-300 bg-amber-950/20 border-amber-900/50"
                      : "text-cyan-400 bg-cyan-950/20 border-cyan-900/50";
                return (
                  <div
                    key={ev.sequence}
                    data-testid={`event-${ev.sequence}`}
                    className={`text-xs font-mono whitespace-pre-wrap break-words px-3 py-1.5 rounded border max-w-2xl ${tone}`}
                  >
                    {status === "ok" ? "✔" : status === "error" ? "✖" : "…"}{" "}
                    {t("core.commandLabel")} /
                    {name === null
                      ? t("core.unknownLabel")
                      : displaySlashPath(catalog.commands, name)}{" "}
                    [{status}]: {detail}
                  </div>
                );
              }
              if (ev.type === "core.approval_decided") {
                return (
                  <div
                    key={ev.sequence}
                    data-testid={`event-${ev.sequence}`}
                    className="text-xs font-mono text-amber-300 bg-amber-950/20 px-3 py-1.5 rounded border border-amber-900/50 max-w-2xl"
                  >
                    {t("core.approvalDecided", {
                      decision: String(
                        ev.payload.decision ?? t("core.unknownLabel"),
                      ),
                    })}
                  </div>
                );
              }
              if (
                content?.kind === "run" &&
                content.body.status === "interrupted"
              ) {
                const cause =
                  typeof ev.payload.stop_cause === "string"
                    ? ev.payload.stop_cause
                    : t("core.unknownLabel");
                return (
                  <div
                    key={ev.sequence}
                    data-testid={`event-${ev.sequence}`}
                    className="text-xs font-mono text-amber-300 bg-amber-950/20 px-3 py-1.5 rounded border border-amber-900/50 max-w-2xl"
                  >
                    {t("core.runInterrupted", {
                      cause: String(cause),
                    })}
                  </div>
                );
              }
              if (
                ev.type === "core.activation_committed" ||
                ev.type === "core.revision_adopted"
              ) {
                // Generation changes are user-visible transcript facts (the TUI
                // renders the same row): an upgrade must be observable, not
                // just a catalog swap behind the scenes.
                const target =
                  typeof ev.payload.to_revision_id === "string" &&
                  ev.payload.to_revision_id
                    ? ev.payload.to_revision_id
                    : ev.revision_id;
                return (
                  <div
                    key={ev.sequence}
                    data-testid={`event-${ev.sequence}`}
                    className="text-xs font-mono text-amber-300 bg-amber-950/20 px-3 py-1.5 rounded border border-amber-900/50 max-w-2xl"
                  >
                    {t("core.revisionAdopted", {
                      revision: String(target).slice(0, 12),
                    })}
                  </div>
                );
              }
              return null;
            })}
            <div ref={messagesEndRef} />
          </div>

          {/* Plugin Views sidebar */}
          {pluginViews.length > 0 && (
            <div className="w-80 border-l border-neutral-800 bg-neutral-900/30 p-3 overflow-y-auto">
              <div className="text-xs uppercase font-semibold text-neutral-400 mb-2 tracking-wider flex items-center justify-between">
                <span data-testid="plugin-views-header">
                  {t("core.pluginViews")}
                </span>
                <button
                  data-testid="plugin-views-refresh"
                  type="button"
                  onClick={() =>
                    refreshViews(
                      activeSessionId,
                      epochRef.current,
                      renderedClientKey,
                    )
                  }
                  className="text-[10px] text-cyan-400 hover:underline"
                >
                  {t("core.refresh")}
                </button>
              </div>
              {pluginViews.map((v, idx) => (
                <PluginViewRenderer
                  key={`${activeSessionId}:${v.owner}-${v.name}-${idx}`}
                  descriptor={v}
                  sessionId={activeSessionId}
                  revisionId={viewScope.revisionId}
                  client={client}
                  catalog={catalog.commands}
                  onAction={handleViewAction}
                  onConfirm={handleConfirmDecision}
                />
              ))}
            </div>
          )}
        </div>

        {commandView &&
          commandView.sessionId === activeSessionId &&
          commandView.epoch === scope.epoch &&
          commandView.clientKey === renderedClientKey &&
          commandView.revisionId === catalog.revisionId && (
            <section
              data-testid="command-presentation"
              className="border-t border-neutral-700 p-4 max-h-80 overflow-y-auto"
            >
              <div className="flex justify-between items-center">
                <h2 className="font-semibold">
                  {commandView.descriptor.title}
                </h2>
                <button
                  type="button"
                  data-testid="command-view-close"
                  onClick={() => {
                    commandViewRequestRef.current += 1;
                    commandViewLiveRef.current = null;
                    setCommandView(null);
                    // Return focus to the composer without touching its draft:
                    // the value state is untouched, only the caret is restored.
                    requestAnimationFrame(() => {
                      const input = document.querySelector<HTMLInputElement>(
                        '[data-testid="prompt-input"]',
                      );
                      input?.focus();
                    });
                  }}
                >
                  {t("core.cancel")}
                </button>
              </div>
              <PluginViewRenderer
                descriptor={commandView.descriptor}
                sessionId={activeSessionId}
                revisionId={commandView.revisionId}
                client={client}
                catalog={catalog.commands}
                onAction={handleViewAction}
                onCancel={() => {
                  commandViewRequestRef.current += 1;
                  commandViewLiveRef.current = null;
                  setCommandView(null);
                }}
                disabled={
                  commandInFlight ||
                  pendingApproval !== null ||
                  pendingConfirmation !== null
                }
              />
            </section>
          )}

        {/* Native tool approval banner (core permission gate) */}
        {pendingApproval && (
          <div
            data-testid="pending-approval-banner"
            className="p-3 bg-amber-950/80 border-t border-amber-500 flex items-center justify-between gap-4"
          >
            <div className="min-w-0">
              <div className="text-xs font-semibold uppercase tracking-wider text-amber-300">
                {t("core.approvalTitle")}
              </div>
              <div className="text-sm text-neutral-100 truncate">
                {t("core.approvalTool")}: {pendingApproval.toolName}
              </div>
              <div className="text-xs text-neutral-400 font-mono truncate">
                {t("core.approvalArguments")}: {pendingApproval.toolArguments}
              </div>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              <button
                data-testid="approval-allow-btn"
                type="button"
                onClick={() => void handleApprovalDecision("allow")}
                className="px-3 py-1.5 rounded bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-medium uppercase tracking-wide"
              >
                {t("core.approvalAllow")}
              </button>
              <button
                data-testid="approval-deny-btn"
                type="button"
                onClick={() => void handleApprovalDecision("deny")}
                className="px-3 py-1.5 rounded bg-neutral-700 hover:bg-neutral-600 text-neutral-200 text-xs font-medium uppercase tracking-wide border border-neutral-600"
              >
                {t("core.approvalDeny")}
              </button>
            </div>
          </div>
        )}

        {/* Pending Confirmation Overlay Banner */}
        {pendingConfirmation && (
          <div
            data-testid="pending-confirmation-banner"
            className="p-3 bg-red-950/90 border-t border-red-500"
          >
            <PluginViewRenderer
              descriptor={{
                owner: pendingConfirmation.owner,
                name: pendingConfirmation.name,
                title: pendingConfirmation.title,
                view_type:
                  pendingConfirmation.view_type as PluginViewDescriptor["view_type"],
                data: pendingConfirmation.data as unknown as PluginViewDescriptor["data"],
              }}
              sessionId={activeSessionId}
              revisionId={pendingConfirmation.revisionId}
              client={client}
              catalog={catalog.commands}
              onConfirm={handleConfirmDecision}
            />
          </div>
        )}

        {/* Input composer with slash command autocomplete */}
        <div className="p-3 border-t border-neutral-800 bg-neutral-900/40 relative">
          {slashSuggestions.length > 0 && (
            <div
              data-testid="slash-suggestions"
              className="absolute bottom-full mb-1 left-3 bg-neutral-900 border border-neutral-700 rounded-md shadow-xl py-1 w-64 max-h-48 overflow-y-auto z-50 text-xs"
            >
              {slashSuggestions.map((cmd, index) => (
                <button
                  key={cmd}
                  type="button"
                  onClick={() => {
                    setPromptInput(`/${cmd} `);
                    setSuggestionIndex(0);
                  }}
                  className={`w-full text-left px-3 py-1.5 hover:bg-neutral-800 text-neutral-200 font-mono ${index === suggestionIndex % slashSuggestions.length ? "bg-neutral-700" : ""}`}
                >
                  /{cmd}
                  {slashCommands.some((entry) =>
                    slashPathOf(entry).startsWith(cmd + " "),
                  )
                    ? " ›"
                    : ""}
                  <span className="ml-3 text-neutral-200">
                    {argumentHints(
                      catalog.commands.find(
                        (entry) => slashPathOf(entry) === cmd,
                      )?.argument_schema,
                    )}
                  </span>
                  <span className="ml-3 font-sans text-neutral-300">
                    {
                      catalog.commands.find(
                        (entry) => slashPathOf(entry) === cmd,
                      )?.description
                    }
                  </span>
                </button>
              ))}
            </div>
          )}

          <form onSubmit={handleSubmit} className="flex gap-2">
            <input
              data-testid="prompt-input"
              type="text"
              value={promptInput}
              disabled={isRunning || !activeSessionId}
              onChange={(e) => {
                commandViewRequestRef.current += 1;
                setPromptInput(e.target.value);
                setSuggestionIndex(0);
                setCompletionDismissed(false);
              }}
              onKeyDown={(e) => {
                if (
                  !slashSuggestions.length ||
                  e.currentTarget.selectionStart !== promptInput.length ||
                  e.currentTarget.selectionEnd !== promptInput.length
                )
                  return;
                if (e.key === "Escape") {
                  e.preventDefault();
                  setCompletionDismissed(true);
                } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
                  e.preventDefault();
                  setSuggestionIndex(
                    (index) =>
                      (index +
                        (e.key === "ArrowDown" ? 1 : -1) +
                        slashSuggestions.length) %
                      slashSuggestions.length,
                  );
                } else if (e.key === "Tab" || e.key === "Enter") {
                  const selected =
                    slashSuggestions[suggestionIndex % slashSuggestions.length];
                  const exact = promptInput === `/${selected}`;
                  const executable =
                    slashCommands.some(
                      (entry) => slashPathOf(entry) === selected,
                    ) || ["help", "cancel", "views", "new"].includes(selected);
                  if (e.key === "Tab" || !exact || !executable) {
                    e.preventDefault();
                    const children = slashCommands.some((entry) =>
                      slashPathOf(entry).startsWith(selected + " "),
                    );
                    setPromptInput(
                      `/${selected}${children || e.key === "Tab" ? " " : ""}`,
                    );
                    setSuggestionIndex(0);
                    setCompletionDismissed(!children);
                  }
                }
              }}
              placeholder={t("core.promptPlaceholder")}
              className="flex-1 px-4 py-2.5 rounded-lg bg-neutral-800 border border-neutral-700 text-sm text-neutral-100 placeholder-neutral-500 focus:outline-none focus:border-cyan-500"
            />
            <button
              data-testid="send-btn"
              type="submit"
              disabled={isRunning || !activeSessionId || !promptInput.trim()}
              className="px-5 py-2.5 rounded-lg bg-cyan-600 hover:bg-cyan-500 active:bg-cyan-700 disabled:opacity-40 text-white font-medium text-sm transition-colors"
            >
              {t("core.send")}
            </button>
          </form>
        </div>
      </main>
    </div>
  );
};
