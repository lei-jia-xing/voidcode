import { useCallback, useLayoutEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { CoreApiError, type CoreClient } from "../../lib/core/client";
import type { StreamEvent } from "../../lib/core/types";
import type { CapturedIssue, SessionScope } from "./useSession";

/** Narrow one dynamic pending_approval object to its display fields. */
export function parkedApprovalOf(value: unknown): {
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
export interface PendingConfirmationEntry {
  owner: string;
  name: string;
  title: string;
  view_type: string;
  data: Record<string, unknown>;
  revisionId: string;
}

export interface PendingApproval {
  scope: SessionScope;
  requestId: string;
  toolName: string;
  toolArguments: string;
}

export interface GatesHandlers {
  refreshViews: (scope: SessionScope) => void;
}

/**
 * Owns the event→gate projection: the native tool-approval gate and the typed
 * plugin-confirmation envelopes, plus their decisions. Gates bind to the scope
 * that raised them; a superseded scope's gate never renders or decides.
 */
export function useGates({
  activeSessionId,
  getClient,
  capture,
  isCurrent,
  isCurrentScope,
  setStatusMessage,
  setIsRunning,
  bumpRunActivity,
  refreshHealth,
}: {
  activeSessionId: string;
  getClient: () => CoreClient;
  capture: () => CapturedIssue;
  isCurrent: (issue: CapturedIssue) => boolean;
  isCurrentScope: (scope: SessionScope) => boolean;
  setStatusMessage: (message: string) => void;
  setIsRunning: (running: boolean) => void;
  bumpRunActivity: () => void;
  refreshHealth: (scope: SessionScope) => void;
}) {
  const { t } = useTranslation();
  // Pending confirmations are a typed UI envelope per request (ui_type +
  // validated ui_data), never parsed product data: the client only renders the
  // declared view kind. They are held by request id in request order, so a
  // second request never displaces the first, and each keeps the revision it
  // was displayed under. A request id already decided can never come back.
  const [pendingConfirmations, setPendingConfirmations] = useState<
    Record<string, PendingConfirmationEntry>
  >({});
  const resolvedConfirmationsRef = useRef<Set<string>>(new Set());
  // Request ids with a decision worker in flight: a held click while one is
  // outstanding is ignored, never a second request. Cleared on response,
  // decided fact, or error — never resurrecting a resolved id.
  const inFlightRef = useRef<Set<string>>(new Set());
  // Action confirmation: it resumes with the approval request id, never /confirm.
  const [pendingApproval, setPendingApproval] =
    useState<PendingApproval | null>(null);
  const gatesHandlersRef = useRef<GatesHandlers | null>(null);
  const [boundSession, setBoundSession] = useState(activeSessionId);

  // Render-phase selection adjustment: a new selection strands every gate
  // before paint. Gates are re-projected from the stream backlog, never
  // carried over; the resolved-set clear stays in the layout below.
  if (boundSession !== activeSessionId) {
    setBoundSession(activeSessionId);
    setPendingConfirmations({});
    setPendingApproval(null);
  }

  const registerGatesHandlers = useCallback(
    (handlers: GatesHandlers | null) => {
      gatesHandlersRef.current = handlers;
    },
    [],
  );

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
    (ev: StreamEvent, scope: SessionScope) => {
      if (ev.type === "core.approval_requested") {
        const parked = parkedApprovalOf(ev.payload);
        if (parked) {
          setPendingApproval({
            scope,
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
    [retainPendingConfirmation, setStatusMessage, t],
  );

  const handleApprovalDecision = async (decision: "allow" | "deny") => {
    const approval = pendingApproval;
    if (!approval) return;
    if (!isCurrentScope(approval.scope)) {
      setPendingApproval(null);
      return;
    }
    // The gate stays mounted until the server accepts or a durable decided
    // fact closes it: clearing early would strand the approval, because
    // applied-cursor dedup never replays the old request fact.
    if (inFlightRef.current.has(approval.requestId)) return;
    inFlightRef.current.add(approval.requestId);
    bumpRunActivity();
    setIsRunning(true);
    try {
      // Native approvals resume the parked run with the exact request identity;
      // /confirm is only for plugin Action confirmations.
      const res = await getClient().resume(
        approval.scope.sessionId,
        approval.requestId,
        decision,
      );
      if (!isCurrentScope(approval.scope)) return;
      setPendingApproval(null);
      const reparked = parkedApprovalOf(res.pending_approval);
      if (reparked) {
        setPendingApproval({
          scope: approval.scope,
          requestId: reparked.requestId,
          toolName: reparked.toolName ?? t("core.unknownLabel"),
          toolArguments: JSON.stringify(reparked.toolArguments),
        });
      }
      setStatusMessage(
        t("core.statusApproved", { decision, status: res.status }),
      );
      gatesHandlersRef.current?.refreshViews(approval.scope);
    } catch (err: unknown) {
      if (!isCurrentScope(approval.scope)) return;
      if (err instanceof CoreApiError && err.code === "upgrade_pending") {
        // The gate stays armed with the same request id: no auto-resend, the
        // user re-decides after the restart. Refresh health for the banner.
        refreshHealth(approval.scope);
        setStatusMessage(t("core.statusUpgradePending"));
        return;
      }
      const msg = err instanceof Error ? err.message : String(err);
      setStatusMessage(t("core.statusApprovalFailed", { message: msg }));
    } finally {
      inFlightRef.current.delete(approval.requestId);
      if (isCurrentScope(approval.scope)) {
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
    const issue = capture();
    // The entry stays mounted until the server accepts or the durable decided
    // fact closes it: removing early would strand the request, because
    // applied-cursor dedup never replays the old requested fact.
    if (inFlightRef.current.has(requestId)) return;
    inFlightRef.current.add(requestId);
    try {
      const res = await issue.client.confirm(
        activeSessionId,
        requestId,
        decision,
        revisionId,
      );
      if (!isCurrent(issue)) return;
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
      if (!isCurrent(issue)) return;
      if (err instanceof CoreApiError && err.code === "upgrade_pending") {
        if (issue.scope) refreshHealth(issue.scope);
        setStatusMessage(t("core.statusUpgradePending"));
        return;
      }
      const msg = err instanceof Error ? err.message : String(err);
      setStatusMessage(t("core.statusConfirmFailed", { message: msg }));
    } finally {
      inFlightRef.current.delete(requestId);
    }
  };

  /** A run/confirm receipt parked a new approval: adopt it under its scope. */
  const adoptParkedApproval = (
    parked: {
      requestId: string;
      toolName: string | null;
      toolArguments: unknown;
    } | null,
    scope: SessionScope,
  ) => {
    if (!parked) return;
    setPendingApproval({
      scope,
      requestId: parked.requestId,
      toolName: parked.toolName ?? t("core.unknownLabel"),
      toolArguments: JSON.stringify(parked.toolArguments),
    });
  };

  /** Activation commit: only a request from the committed generation survives. */
  const handleActivationCommitted = useCallback((targetRevision: string) => {
    setPendingConfirmations((prev) =>
      // Only a request that belongs to the committed generation survives;
      // the others could never be resolved against it.
      Object.fromEntries(
        Object.entries(prev).filter(
          ([, entry]) => entry.revisionId === targetRevision,
        ),
      ),
    );
  }, []);

  // A new selection strands every gate: the resolved set resets with it.
  useLayoutEffect(() => {
    resolvedConfirmationsRef.current.clear();
  }, [boundSession]);

  return {
    pendingApproval,
    pendingConfirmation,
    registerGatesHandlers,
    retainPendingConfirmation,
    projectEvent,
    handleApprovalDecision,
    handleConfirmDecision,
    adoptParkedApproval,
    handleActivationCommitted,
  };
}
