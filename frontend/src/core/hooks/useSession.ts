import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { useTranslation } from "react-i18next";
import { CoreApiError, CoreClient } from "../../lib/core/client";
import type {
  HealthStatus,
  SessionFrame,
  SessionRow,
  StreamEvent,
  TranscriptEntry,
} from "../../lib/core/types";
import { parkedApprovalOf } from "./useGates";

/**
 * One bound session scope: plain data only (unique key + session id), so the
 * token itself is safe to hold in state and pass through render. Liveness
 * lives beside it: selecting another session — or reselecting the same id —
 * aborts the previous scope's AbortController, so no epoch/clientKey/serial
 * counters are needed to orphan stale results.
 */
export interface SessionScope {
  key: string;
  sessionId: string;
}

export interface SessionEventRow {
  sequence: number;
  type: string;
  revision_id: string;
  payload: Record<string, unknown>;
  transcript?: TranscriptEntry | null;
}

export interface UpgradeSnapshot {
  fenced: boolean;
  stage: string;
  coreVersion: string;
  generation: number;
  activeUpgrade: { upgrade_id: string; stage: string } | null;
}

/**
 * Sibling-hook seams the session stream calls into. Wired by the composer
 * after every render; read at event time so wiring never re-subscribes the
 * stream and never goes stale.
 */
export interface SessionHandlers {
  projectEvent: (ev: StreamEvent, scope: SessionScope) => void;
  activationCommitted: (targetRevision: string, scope: SessionScope) => void;
  serverIdentityChanged: (scope: SessionScope) => void;
  refreshCatalog: () => void;
  refreshViews: (scope: SessionScope) => void;
  adoptParkedApproval: (
    parked: {
      requestId: string;
      toolName: string | null;
      toolArguments: unknown;
    } | null,
    scope: SessionScope,
  ) => void;
}

export interface CapturedIssue {
  scope: SessionScope | null;
  client: CoreClient;
}

function readQueryServer(): string {
  if (typeof window === "undefined") return "";
  return new URLSearchParams(window.location.search).get("server") || "";
}

function storageKey(baseUrl: string): string {
  return `voidcode:bearer:${baseUrl || "same-origin"}`;
}

function readStoredToken(key: string): string | undefined {
  try {
    if (typeof window === "undefined") return undefined;
    return window.sessionStorage.getItem(key) || undefined;
  } catch {
    return undefined;
  }
}

const clientIds = new WeakMap<CoreClient, number>();
let nextClientId = 0;

/** Stable per-client numeric id for React keys (never the token value). */
export function clientIdentity(client: CoreClient): number {
  let id = clientIds.get(client);
  if (id === undefined) {
    id = ++nextClientId;
    clientIds.set(client, id);
  }
  return id;
}

/** Unique allocator for scope binding keys (identity, not a race guard). */
let nextScopeKey = 0;
function allocScopeKey(): string {
  nextScopeKey += 1;
  return `scope-${nextScopeKey}`;
}

function snapshotUpgrade(snapshot: HealthStatus): UpgradeSnapshot {
  return {
    fenced: "fenced" in snapshot && snapshot.fenced === true,
    stage: String(snapshot.stage ?? ""),
    coreVersion: String(snapshot.core_version ?? ""),
    generation:
      typeof snapshot.generation === "number" ? snapshot.generation : -1,
    activeUpgrade:
      snapshot.active_upgrade && typeof snapshot.active_upgrade === "object"
        ? {
            upgrade_id: String(snapshot.active_upgrade.upgrade_id ?? ""),
            stage: String(snapshot.active_upgrade.stage ?? ""),
          }
        : null,
  };
}

/**
 * Owns the transport client, bearer-token resolution, session list/selection,
 * the bound scope (AbortController-guarded), the event stream and rows, server
 * health/upgrade identity, the root run indicator, and the status line.
 */
export function useSession({
  baseUrl = "",
  token,
}: {
  baseUrl?: string;
  token?: string;
}) {
  const { t } = useTranslation();
  // The bearer token never travels in the URL (history/Referer leak): an
  // explicit prop wins, else the per-tab sessionStorage entry for this server,
  // else the user pastes it into the token gate.
  const queryServer = readQueryServer();
  const effectiveBaseUrl = baseUrl || queryServer;
  const tokenKey = storageKey(effectiveBaseUrl);
  const [storedToken, setStoredToken] = useState<string | undefined>(() =>
    readStoredToken(tokenKey),
  );
  const [tokenKeyBound, setTokenKeyBound] = useState(tokenKey);
  if (tokenKeyBound !== tokenKey) {
    setTokenKeyBound(tokenKey);
    setStoredToken(readStoredToken(tokenKey));
  }
  const effectiveToken = token ?? storedToken;
  const needsToken = effectiveToken == null;
  const [tokenGateOpen, setTokenGateOpen] = useState(false);
  const showTokenGate = tokenGateOpen || needsToken;

  const saveToken = useCallback(
    (value: string) => {
      const trimmed = value.trim();
      if (!trimmed) return;
      try {
        window.sessionStorage.setItem(tokenKey, trimmed);
      } catch {
        // Private-mode storage failure: the token still works for this render.
      }
      setStoredToken(trimmed);
    },
    [tokenKey],
  );

  // The transport client is state, never a memo: a memo holding a mutable
  // instance reads as a ref, which render must not touch. The constructor is
  // side-effect free (the socket opens lazily), so minting during the
  // adjusting render is safe; the discarded twin never connects.
  const [transportKey, setTransportKey] = useState<
    [string, string | undefined]
  >([effectiveBaseUrl, effectiveToken]);
  const [client, setClient] = useState(
    () => new CoreClient(effectiveBaseUrl, effectiveToken),
  );
  if (
    transportKey[0] !== effectiveBaseUrl ||
    transportKey[1] !== effectiveToken
  ) {
    setTransportKey([effectiveBaseUrl, effectiveToken]);
    setClient(new CoreClient(effectiveBaseUrl, effectiveToken));
  }
  useEffect(() => () => client.close(), [client]);

  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string>("");
  const [scope, setScope] = useState<SessionScope | null>(null);
  const scopeRef = useRef<SessionScope | null>(null);
  // Per-scope AbortControllers by binding key: aborting the previous binding
  // orphans its in-flight reads without touching the plain scope token.
  const scopeControllers = useRef(new Map<string, AbortController>());
  const clientRef = useRef<CoreClient>(client);
  const selectionRef = useRef<string>("");
  const bootstrapRef = useRef<CoreClient | null>(null);
  const [events, setEvents] = useState<SessionEventRow[]>([]);
  const lastSequenceRef = useRef<number>(0);
  // Latched terminal stream error for this session scope: once set, no
  // reconnect reconcile may overwrite the visible failure message.
  const terminalErrorRef = useRef<string | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  // Only the newest health/reconcile reply may render: one AbortController
  // per operation replaces the shared health serial.
  const healthOpRef = useRef<AbortController | null>(null);
  // The last health answer this scope accepted, mirrored in a ref so the
  // identity compare reads a stable snapshot instead of calling setState
  // inside another state updater (StrictMode may replay updaters).
  const upgradeRef = useRef<UpgradeSnapshot | null>(null);
  const [upgrade, setUpgrade] = useState<UpgradeSnapshot | null>(null);
  const [isRunning, setIsRunning] = useState<boolean>(false);
  // Monotonic marker of the newest user-issued run activity: a stale `show`
  // from an older reconcile round must never overwrite it.
  const runActivityRef = useRef<number>(0);
  const [statusMessage, setStatusMessage] = useState<string>(() =>
    t("core.statusReady"),
  );
  const handlersRef = useRef<SessionHandlers | null>(null);
  const [boundClient, setBoundClient] = useState(client);

  // Render-phase transport adjustment: a new transport owns every later fetch,
  // so state resets here (no paint with mixed generations); ref sync + abort
  // stay in the layout below, which cannot call setState.
  if (boundClient !== client) {
    setBoundClient(client);
    setActiveSessionId((current) => (current === "" ? current : ""));
    setScope(null);
    // The previous transport's session rows must not stay actionable
    // under the new one. Catalog, views, and gates reset themselves on the
    // same client identity in their own hooks.
    setSessions([]);
    setEvents([]);
    setIsRunning(false);
    setUpgrade(null);
  }

  // Render-phase scope mint: the scope object lives in state (never a ref),
  // so concurrent replays converge on one bound scope. Never while a
  // transport adjustment is still settling in this same pass.
  const needsScopeRebind =
    boundClient === client &&
    activeSessionId !== "" &&
    (scope === null || scope.sessionId !== activeSessionId);
  if (needsScopeRebind) {
    const next: SessionScope = {
      key: allocScopeKey(),
      sessionId: activeSessionId,
    };
    setScope(next);
    setEvents([]);
    setIsRunning(false);
    setStatusMessage(
      t("core.statusLoadingSession", { sessionId: activeSessionId }),
    );
  }

  const attachMessagesEnd = useCallback((node: HTMLDivElement | null) => {
    messagesEndRef.current = node;
  }, []);
  const registerStreamHandlers = useCallback(
    (handlers: SessionHandlers | null) => {
      handlersRef.current = handlers;
    },
    [],
  );
  // Render never touches the mutable client instance: children resolve the
  // current transport through this stable callback at their own effect/handler
  // use sites. It closes over the client ref (never the instance), so the
  // callback itself carries no mutable value into render.
  const getClient = useCallback(() => clientRef.current, []);

  const capture = useCallback(
    (): CapturedIssue => ({ scope: scopeRef.current, client }),
    [client],
  );
  const scopeAlive = useCallback((scope: SessionScope | null): boolean => {
    if (scope === null) return true;
    if (scopeRef.current?.key !== scope.key) return false;
    return scopeControllers.current.get(scope.key)?.signal.aborted === false;
  }, []);
  const isCurrent = useCallback(
    (issue: CapturedIssue): boolean => {
      if (clientRef.current !== issue.client) return false;
      return scopeAlive(issue.scope);
    },
    [scopeAlive],
  );
  const isCurrentScope = useCallback(
    (scope: SessionScope): boolean => scopeAlive(scope),
    [scopeAlive],
  );
  const bumpRunActivity = useCallback(() => {
    runActivityRef.current += 1;
  }, []);

  const acquireHealthOp = useCallback((): AbortController => {
    healthOpRef.current?.abort();
    const next = new AbortController();
    healthOpRef.current = next;
    return next;
  }, []);

  // Append-once application for one head backlog or live event batch: filter
  // to fresh sequences, commit rows, project gates, and invalidate the
  // displayed generation on activation commits. Called outside any updater.
  const applyEvents = useCallback(
    (batch: StreamEvent[], scope: SessionScope) => {
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
      const handlers = handlersRef.current;
      for (const ev of fresh) {
        handlers?.projectEvent(ev, scope);
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
        handlers?.activationCommitted(targetRevision, scope);
      }
    },
    [t],
  );

  // Read server identity/liveness. Only the newest reply for the current scope
  // may render: a stale health answer must never overwrite a newer one or
  // clear a fence the supervisor already raised.
  const refreshHealth = useCallback(
    (scope: SessionScope) => {
      const op = acquireHealthOp();
      client
        .health()
        .then((snapshot: HealthStatus) => {
          if (op.signal.aborted) return;
          if (!isCurrentScope(scope)) return;
          setUpgrade(snapshotUpgrade(snapshot));
        })
        .catch((err) => {
          // Health is best-effort corroboration: unreachable during a handoff
          // gap is expected and must not clobber the last known identity.
          if (!isCurrentScope(scope)) return;
          console.error("Failed to fetch core health", err);
        });
    },
    [acquireHealthOp, client, isCurrentScope],
  );

  /**
   * ONE connection-established reconciliation path for the initial stream and
   * every reconnect — announced restart or silent drop. Fetches identity,
   * compares it against the last known server, refetches catalog+views, and
   * reconciles the running indicator from durable `show`. Never resends the
   * prompt and never touches the applied cursor. Scope-, op-, and
   * activity-guarded: a stale reply from a superseded scope, an older
   * reconcile round, or an older run activity never renders.
   */
  const reconcileConnection = useCallback(
    (scope: SessionScope) => {
      const op = acquireHealthOp();
      client
        .health()
        .then((snapshot: HealthStatus) => {
          if (op.signal.aborted) return;
          if (!isCurrentScope(scope)) return;
          const next = snapshotUpgrade(snapshot);
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
            handlersRef.current?.serverIdentityChanged(scope);
          }
          upgradeRef.current = next;
          setUpgrade(next);
          if (terminalErrorRef.current === null) {
            setStatusMessage(
              next.fenced
                ? t("core.upgradeFenced", {
                    stage: next.activeUpgrade?.stage ?? next.stage,
                  })
                : t("core.statusConnected", { sessionId: scope.sessionId }),
            );
          }
        })
        .catch((err) => {
          // Unreachable during a handoff gap is expected and must not clobber
          // the last known identity.
          if (!isCurrentScope(scope)) return;
          console.error("Failed to fetch core health", err);
        });
      handlersRef.current?.refreshCatalog();
      handlersRef.current?.refreshViews(scope);
      const activityAtIssue = runActivityRef.current;
      const cursorAtIssue = lastSequenceRef.current;
      client
        .show(scope.sessionId)
        .then((snapshot) => {
          if (op.signal.aborted) return;
          if (!isCurrentScope(scope)) return;
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
          if (!isCurrentScope(scope)) return;
          console.error("Failed to reconcile session status", err);
        });
    },
    [acquireHealthOp, client, isCurrentScope, t],
  );

  const createSession = useCallback(async () => {
    const issue = capture();
    try {
      const newSession = await client.createSession();
      if (!isCurrent(issue)) {
        return; // another switch or transport happened while it was created
      }
      setSessions((prev) => [newSession, ...prev]);
      setActiveSessionId(newSession.session_id);
    } catch (err: unknown) {
      if (!isCurrent(issue)) return;
      const msg = err instanceof Error ? err.message : String(err);
      setStatusMessage(t("core.statusCreateFailed", { message: msg }));
    }
  }, [capture, client, isCurrent, t]);

  const cancelRun = async () => {
    if (!activeSessionId) return;
    const issue = capture();
    try {
      const res = await client.cancel(activeSessionId);
      if (!isCurrent(issue)) return;
      setStatusMessage(
        res.cancellation_requested
          ? t("core.cancellationRequested")
          : t("core.noActiveRun"),
      );
    } catch (err: unknown) {
      if (!isCurrent(issue)) return;
      const msg = err instanceof Error ? err.message : String(err);
      setStatusMessage(t("core.cancelFailed", { message: msg }));
    }
  };

  /**
   * Normal run prompt (fully scope-guarded: success, failure and finally).
   * Returns false only when the core fenced the dispatch, so the caller can
   * restore the exact draft — never overwriting a newer draft, never
   * auto-submitting.
   */
  const runPrompt = async (text: string): Promise<boolean> => {
    const scope = scopeRef.current;
    if (!scope || scope.sessionId !== activeSessionId) return true;
    runActivityRef.current += 1;
    setIsRunning(true);
    setStatusMessage(t("core.statusDispatching"));
    try {
      const res = await client.run(scope.sessionId, text);
      if (!isCurrentScope(scope)) return true;
      handlersRef.current?.adoptParkedApproval(
        parkedApprovalOf(res.pending_approval),
        scope,
      );
      setStatusMessage(
        res.status === "waiting"
          ? t("core.statusWaitingApproval")
          : t("core.statusRun", { status: res.status }),
      );
      handlersRef.current?.refreshViews(scope);
      return true;
    } catch (err: unknown) {
      if (!isCurrentScope(scope)) return true;
      if (err instanceof CoreApiError && err.code === "upgrade_pending") {
        refreshHealth(scope);
        setStatusMessage(t("core.statusUpgradePending"));
        return false;
      }
      const msg = err instanceof Error ? err.message : String(err);
      setStatusMessage(t("core.statusRunFailed", { message: msg }));
      return true;
    } finally {
      if (isCurrentScope(scope)) {
        setIsRunning(false);
      }
    }
  };

  // A transport change invalidates every in-flight result before paint, and does
  // so even when no session is selected yet (the scope binder returns early on
  // an empty id, so client refs must be cleared here). State resets happened
  // during render (above); only ref sync + abort stay in the effect.
  useLayoutEffect(() => {
    if (clientRef.current === boundClient) {
      return;
    }
    // A new transport owns every later fetch: the previous client's callbacks are
    // refused by identity even if they resolve after the switch.
    bootstrapRef.current = null;
    clientRef.current = boundClient;
    scopeControllers.current.get(scopeRef.current?.key ?? "")?.abort();
    scopeRef.current = null;
    selectionRef.current = "";
    healthOpRef.current?.abort();
    upgradeRef.current = null;
  }, [boundClient]);

  // Bootstrap fetches the catalog and the session list once per client object,
  // in that issue order; invalidation itself happened in the render adjustment
  // above. The wiring layout (composer) runs before this passive, so the
  // catalog seam is already registered on the first mount.
  useEffect(() => {
    const issued = client;
    if (bootstrapRef.current === issued) {
      return; // StrictMode remount must not re-issue these reads
    }
    bootstrapRef.current = issued;

    handlersRef.current?.refreshCatalog();
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
          void createSession();
        }
      })
      .catch((err) => {
        if (bootstrapRef.current !== issued) return;
        setStatusMessage(t("core.statusListFailed", { message: String(err) }));
      });
  }, [client, createSession, t]);

  // Bind the committed scope to its guards: abort the predecessor and sync
  // refs. Minting + state resets happened during render (above).
  useLayoutEffect(() => {
    if (!scope || scopeRef.current?.key === scope.key) return;
    // Abort the previous scope: anything still in flight from the previous
    // session (or a return to a previously visited session id) is stale here.
    scopeControllers.current.get(scopeRef.current?.key ?? "")?.abort();
    scopeRef.current = scope;
    scopeControllers.current.set(scope.key, new AbortController());
    selectionRef.current = scope.sessionId;
    lastSequenceRef.current = 0;
    terminalErrorRef.current = null;
  }, [scope]);

  // Handle session change: subscribe to the event stream of the bound scope.
  useEffect(() => {
    if (!scope) return;
    const bound = scope;
    const cleanup = client.streamEvents(
      bound.sessionId,
      0,
      (frame: SessionFrame) => {
        if (!isCurrentScope(bound)) {
          return; // a follower from a superseded scope: never render it
        }
        if ("events" in frame) {
          if (frame.session.session_id !== bound.sessionId) {
            return;
          }
          applyEvents(frame.events, bound);
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
        applyEvents([frame.event], bound);
      },
      (err) => {
        if (!isCurrentScope(bound)) {
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
        if (!isCurrentScope(bound)) {
          return; // a superseded follower's reconnect must not touch this view
        }
        reconcileConnection(bound);
      },
    );
    return () => {
      cleanup();
    };
  }, [applyEvents, client, isCurrentScope, reconcileConnection, scope, t]);

  // Scroll to bottom on new events
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [events]);

  return {
    getClient,
    transportKey,
    clientId: clientIdentity(client),
    effectiveBaseUrl,
    effectiveToken,
    needsToken,
    showTokenGate,
    setTokenGateOpen,
    saveToken,
    sessions,
    activeSessionId,
    setActiveSessionId,
    scope,
    capture,
    isCurrent,
    isCurrentScope,
    events,
    attachMessagesEnd,
    upgrade,
    isRunning,
    setIsRunning,
    bumpRunActivity,
    statusMessage,
    setStatusMessage,
    registerStreamHandlers,
    createSession,
    cancelRun,
    runPrompt,
    refreshHealth,
  };
}
