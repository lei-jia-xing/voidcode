import React, {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useTranslation } from "react-i18next";
import { KernelClient } from "../lib/kernel/client";
import type {
  CommandDescriptor,
  PluginViewDescriptor,
  SessionRecord,
  SseEventFrame,
} from "../lib/kernel/types";
import { PluginViewRenderer } from "./views/PluginViewRenderer";

export interface KernelAppProps {
  baseUrl?: string;
  token?: string;
}

export const KernelApp: React.FC<KernelAppProps> = ({
  baseUrl = "",
  token,
}) => {
  const searchParams =
    typeof window !== "undefined"
      ? new URLSearchParams(window.location.search)
      : null;
  const effectiveBaseUrl = baseUrl || searchParams?.get("server") || "";
  const effectiveToken = token || searchParams?.get("token") || undefined;

  const client = useMemo(
    () => new KernelClient(effectiveBaseUrl, effectiveToken),
    [effectiveBaseUrl, effectiveToken],
  );
  const [sessions, setSessions] = useState<SessionRecord[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string>("");
  const [catalog, setCatalog] = useState<CommandDescriptor[]>([]);
  const [events, setEvents] = useState<
    Array<{
      sequence: number;
      type: string;
      payload: Record<string, unknown>;
    }>
  >([]);
  const [viewScope, setViewScope] = useState<{
    sessionId: string;
    epoch: number;
    clientKey: string;
    views: PluginViewDescriptor[];
  }>({ sessionId: "", epoch: -1, clientKey: "", views: [] });
  // A pending confirmation is a typed UI envelope (ui_type + validated ui_data),
  // never parsed product data: the client only renders the declared view kind.
  const [pendingConfirmation, setPendingConfirmation] = useState<{
    owner: string;
    name: string;
    title: string;
    view_type: string;
    data: Record<string, unknown>;
  } | null>(null);
  const [promptInput, setPromptInput] = useState<string>("");
  // Every async result is bound to the (session, epoch) it was issued for: a
  // late A reply is invalid even after the user returns to A, because the epoch
  // advanced in between.
  const streamReadyRef = useRef(false);
  const sessionRef = useRef<string>("");
  const epochRef = useRef<number>(0);
  // A client identity change (different server/token) invalidates every result
  // from the previous transport, on top of the session and epoch.
  const clientKeyRef = useRef<string>("");
  const selectionRef = useRef<string>("");
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
  const pluginViews =
    scope.sessionId === activeSessionId &&
    scope.clientKey === renderedClientKey &&
    viewScope.sessionId === activeSessionId &&
    viewScope.clientKey === renderedClientKey &&
    viewScope.epoch === scope.epoch
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
  // Native tool approval (a core permission gate) is distinct from a plugin
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
    t("kernel.statusReady"),
  );

  /**
   * Project one persisted event into the client's interaction state. Used for
   * the initial backlog and for live frames alike, so a page reload or a
   * reconnect while a run is parked still surfaces the pending gate.
   */
  const projectEvent = useCallback(
    (
      ev: { sequence: number; type: string; payload: Record<string, unknown> },
      session: string,
      epoch: number,
      clientKey: string,
    ) => {
      if (ev.type === "kernel.approval_requested") {
        const approval = ev.payload as {
          request_id?: string;
          call?: { tool_name?: string; arguments?: unknown };
        };
        if (approval.request_id) {
          setPendingApproval({
            sessionId: session,
            epoch,
            clientKey,
            requestId: approval.request_id,
            toolName: approval.call?.tool_name ?? t("kernel.unknownLabel"),
            toolArguments: JSON.stringify(approval.call?.arguments ?? {}),
          });
          setStatusMessage(t("kernel.statusWaitingApproval"));
        }
        return;
      }
      if (ev.type === "kernel.approval_decided") {
        setPendingApproval(null);
        return;
      }
      if (
        ev.type === "kernel.run_interrupted" ||
        ev.type === "kernel.run_failed" ||
        ev.type === "kernel.run_completed"
      ) {
        // An authentic terminal fact means nothing is parked any more; a stale
        // approval modal must never outlive the run that asked for it.
        setPendingApproval(null);
        return;
      }
      if (ev.type === "kernel.confirmation_requested") {
        const p = ev.payload as {
          request_id?: string;
          ui_type?: string;
          ui_data?: Record<string, unknown>;
        };
        if (p.request_id && p.ui_type && p.ui_data) {
          setPendingConfirmation({
            owner: "kernel",
            name: "pending_confirmation",
            title: t("kernel.approvalRequired"),
            view_type: p.ui_type,
            data: { ...p.ui_data, request_id: p.request_id },
          });
        }
      }
    },
    [t],
  );

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const lastSequenceRef = useRef<number>(0);
  const refreshViews = useCallback(
    (sessionId: string, epoch: number, clientKey: string) => {
      client
        .views(sessionId)
        .then((views) => {
          if (!isCurrent(sessionId, epoch, clientKey)) {
            return; // stale session/epoch/transport: never render these views
          }
          setViewScope({ sessionId, epoch, clientKey, views });
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
      setStatusMessage(t("kernel.statusCreateFailed", { message: msg }));
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
          ? t("kernel.cancellationRequested")
          : t("kernel.noActiveRun"),
      );
    } catch (err: unknown) {
      if (!isCurrent(issueSession, epoch, clientKey)) return;
      const msg = err instanceof Error ? err.message : String(err);
      setStatusMessage(t("kernel.cancelFailed", { message: msg }));
    }
  };

  // A transport change invalidates every in-flight result before paint, and does
  // so even when no session is selected yet (the layout session effect returns
  // early on an empty id, so client refs must be cleared here).
  const bootstrapRef = useRef<KernelClient | null>(null);
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
      views: [],
    });
    // The previous transport's catalog and session rows must not stay actionable
    // under the new one.
    setCatalog([]);
    setSessions([]);
    setEvents([]);
    setPendingApproval(null);
    setPendingConfirmation(null);
    setIsRunning(false);
    streamReadyRef.current = false;
  }, [renderedClientKey]);

  // Bootstrap fetches the catalog and the session list once per client object;
  // invalidation itself happened in the layout effect above.
  useEffect(() => {
    const issued = client;
    if (bootstrapRef.current === issued) {
      return; // StrictMode remount must not re-issue these reads
    }
    bootstrapRef.current = issued;

    issued
      .commandCatalog()
      .then((cat) => {
        if (bootstrapRef.current !== issued) return;
        setCatalog(cat);
      })
      .catch((err) => {
        if (bootstrapRef.current !== issued) return;
        setStatusMessage(
          t("kernel.statusCatalogFailed", { message: String(err) }),
        );
      });

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
        setStatusMessage(
          t("kernel.statusListFailed", { message: String(err) }),
        );
      });
  }, [client, handleCreateSession, t]);

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
    setViewScope({ sessionId: activeSessionId, epoch, clientKey, views: [] });
    setEvents([]);
    lastSequenceRef.current = 0;
    setPendingConfirmation(null);
    setPendingApproval(null);
    setIsRunning(false);
    streamReadyRef.current = false;
    setStatusMessage(
      t("kernel.statusLoadingSession", { sessionId: activeSessionId }),
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
      (frame: SseEventFrame) => {
        if (!isCurrent(boundSession, boundEpoch, boundClientKey)) {
          return; // a follower from a superseded session/epoch: never render it
        }
        if (frame.session && frame.session.session_id !== boundSession) {
          return;
        }
        const backlog =
          frame.events && Array.isArray(frame.events) ? frame.events : null;
        const live = frame.event ?? null;

        if (!streamReadyRef.current) {
          // The first frame is the head: the stream is genuinely live now.
          streamReadyRef.current = true;
          setStatusMessage(
            t("kernel.statusConnected", { sessionId: boundSession }),
          );
        }

        if (backlog || live) {
          setEvents((prev) => {
            const seen = new Set(prev.map((e) => e.sequence));
            const incoming = (backlog ?? [live!]).filter(
              (e) => !seen.has(e.sequence),
            );
            if (incoming.length === 0) {
              return prev;
            }
            const merged = [
              ...prev,
              ...incoming.map((e) => ({
                sequence: e.sequence,
                type: e.type,
                payload: e.payload,
              })),
            ].sort((a, b) => a.sequence - b.sequence);
            lastSequenceRef.current = merged[merged.length - 1].sequence;
            // State projections use the same path for backlog and live events.
            for (const ev of incoming) {
              projectEvent(ev, boundSession, boundEpoch, boundClientKey);
            }
            return merged;
          });
        }
      },
      (err) => {
        if (!isCurrent(boundSession, boundEpoch, boundClientKey)) {
          return; // a superseded follower's failure must not touch this view
        }
        setStatusMessage(t("kernel.streamError", { message: err.message }));
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
    refreshViews,
    isCurrent,
    projectEvent,
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
    setPendingApproval(null);
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
      const reparked = (
        res as {
          pending_approval?: {
            request_id?: string;
            call?: { tool_name?: string; arguments?: unknown };
          };
        }
      ).pending_approval;
      if (reparked?.request_id) {
        setPendingApproval({
          sessionId: approval.sessionId,
          epoch: approval.epoch,
          clientKey: approval.clientKey,
          requestId: reparked.request_id,
          toolName: reparked.call?.tool_name ?? t("kernel.unknownLabel"),
          toolArguments: JSON.stringify(reparked.call?.arguments ?? {}),
        });
      }
      setStatusMessage(
        t("kernel.statusApproved", { decision, status: res.status }),
      );
      refreshViews(approval.sessionId, approval.epoch, approval.clientKey);
    } catch (err: unknown) {
      if (!isCurrent(approval.sessionId, approval.epoch, approval.clientKey))
        return;
      const msg = err instanceof Error ? err.message : String(err);
      setStatusMessage(t("kernel.statusApprovalFailed", { message: msg }));
    } finally {
      if (isCurrent(approval.sessionId, approval.epoch, approval.clientKey)) {
        setIsRunning(false);
      }
    }
  };

  const handleConfirmDecision = async (
    requestId: string,
    decision: "allow" | "deny",
  ) => {
    if (!activeSessionId) return;
    const issueSession = activeSessionId;
    const epoch = epochRef.current;
    const clientKey = renderedClientKey;
    setPendingConfirmation(null);
    try {
      const res = await client.confirm(issueSession, requestId, decision);
      if (!isCurrent(issueSession, epoch, clientKey)) return;
      setStatusMessage(
        t("kernel.statusConfirm", { decision, status: res.status }),
      );
      refreshViews(issueSession, epoch, clientKey);
    } catch (err: unknown) {
      if (!isCurrent(issueSession, epoch, clientKey)) return;
      const msg = err instanceof Error ? err.message : String(err);
      setStatusMessage(t("kernel.statusConfirmFailed", { message: msg }));
    }
  };

  const handleViewAction = async (
    actionName: string,
    args: Record<string, unknown>,
  ) => {
    if (!activeSessionId) return;
    const issueSession = activeSessionId;
    const epoch = epochRef.current;
    const clientKey = renderedClientKey;
    setIsRunning(true);
    setStatusMessage(t("kernel.statusInvoking", { name: actionName }));
    try {
      const res = await client.invoke(issueSession, actionName, args);
      if (!isCurrent(issueSession, epoch, clientKey)) {
        return; // session or epoch changed while the command ran: drop the reply
      }
      setStatusMessage(
        t("kernel.statusInvoked", { name: actionName, status: res.status }),
      );
      if (res.status === "pending_confirmation" && res.confirmation) {
        const confirmation = res.confirmation as {
          request_id?: string;
          ui_type?: string;
          ui_data?: Record<string, unknown>;
        };
        if (confirmation.ui_type && confirmation.ui_data) {
          setPendingConfirmation({
            owner: "kernel",
            name: "pending_confirmation",
            title: t("kernel.approvalRequired"),
            view_type: confirmation.ui_type,
            data: {
              ...confirmation.ui_data,
              request_id:
                confirmation.request_id ?? confirmation.ui_data.request_id,
            },
          });
        }
      }
      refreshViews(issueSession, epoch, clientKey);
    } catch (err: unknown) {
      if (!isCurrent(issueSession, epoch, clientKey)) return;
      const msg = err instanceof Error ? err.message : String(err);
      setStatusMessage(t("kernel.statusInvokeFailed", { message: msg }));
    } finally {
      if (isCurrent(issueSession, epoch, clientKey)) {
        setIsRunning(false);
      }
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const text = promptInput.trim();
    if (!text || !activeSessionId || isRunning) return;
    setPromptInput("");

    if (text.startsWith("/")) {
      // Slash command dispatch
      const [cmd, ...restParts] = text.slice(1).split(" ");
      const argStr = restParts.join(" ").trim();

      // Admitted plugin commands always win: local controls never shadow a
      // catalog command that happens to share the name.
      const descriptor = catalog.find((c) => c.name === cmd);
      if (!descriptor) {
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
        setStatusMessage(t("kernel.statusUnknownCommand", { name: cmd }));
        return;
      }

      const schema =
        (descriptor.argument_schema as Record<string, unknown> | undefined) ||
        {};
      const properties =
        (schema.properties as Record<string, { type?: string }> | undefined) ||
        {};
      const required = (schema.required as string[] | undefined) || [];

      let parsedArgs: Record<string, unknown>;
      if (!argStr) {
        parsedArgs = {};
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
            setStatusMessage(
              t("kernel.statusInvalidJsonArgs", { args: argStr }),
            );
            return;
          }
        }
      }
      await handleViewAction(cmd, parsedArgs);
    } else {
      // Normal run prompt (fully epoch-guarded: success, failure and finally).
      const issueSession = activeSessionId;
      const epoch = epochRef.current;
      const clientKey = renderedClientKey;
      setIsRunning(true);
      setStatusMessage(t("kernel.statusDispatching"));
      try {
        const res = await client.run(issueSession, text);
        if (!isCurrent(issueSession, epoch, clientKey)) return;
        const parked = (
          res as {
            pending_approval?: {
              request_id?: string;
              call?: { tool_name?: string; arguments?: unknown };
            };
          }
        ).pending_approval;
        if (parked?.request_id) {
          setPendingApproval({
            sessionId: issueSession,
            epoch,
            clientKey,
            requestId: parked.request_id,
            toolName: parked.call?.tool_name ?? t("kernel.unknownLabel"),
            toolArguments: JSON.stringify(parked.call?.arguments ?? {}),
          });
        }
        setStatusMessage(
          res.status === "waiting"
            ? t("kernel.statusWaitingApproval")
            : t("kernel.statusRun", { status: res.status }),
        );
        refreshViews(issueSession, epoch, clientKey);
      } catch (err: unknown) {
        if (!isCurrent(issueSession, epoch, clientKey)) return;
        const msg = err instanceof Error ? err.message : String(err);
        setStatusMessage(t("kernel.statusRunFailed", { message: msg }));
      } finally {
        if (isCurrent(issueSession, epoch, clientKey)) {
          setIsRunning(false);
        }
      }
    }
  };

  // Autocomplete hints
  const slashSuggestions = useMemo(() => {
    if (!promptInput.startsWith("/") || promptInput.includes(" ")) {
      return [];
    }
    const query = promptInput.slice(1).toLowerCase();
    const builtins = ["help", "cancel", "views", "new"];
    const fromCatalog = catalog.map((c) => c.name);
    const all = Array.from(new Set([...builtins, ...fromCatalog]));
    return all.filter((name) => name.toLowerCase().startsWith(query));
  }, [promptInput, catalog]);

  return (
    <div
      data-testid="kernel-app"
      className="flex h-screen w-screen bg-neutral-950 text-neutral-100 font-sans overflow-hidden"
    >
      {/* Sidebar */}
      <aside className="w-64 border-r border-neutral-800 bg-neutral-900/50 flex flex-col justify-between">
        <div>
          <div className="p-4 border-b border-neutral-800 flex items-center justify-between">
            <span className="font-bold text-sm tracking-wide text-neutral-200 uppercase">
              {t("kernel.title")} — VoidCode
            </span>
            <button
              data-testid="new-session-btn"
              type="button"
              onClick={handleCreateSession}
              className="px-2.5 py-1 text-xs rounded bg-cyan-600 hover:bg-cyan-500 text-white font-medium transition-colors"
            >
              + {t("kernel.newSession")}
            </button>
          </div>
          <div className="p-2 overflow-y-auto max-h-[calc(100vh-140px)]">
            <div className="text-xs uppercase text-neutral-500 font-semibold px-2 py-1 mb-1 tracking-wider">
              {t("kernel.sessions")}
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
          {t("kernel.statusPrefix")}:{" "}
          <span className="text-neutral-300">{statusMessage}</span>
        </div>
      </aside>

      {/* Main chat & views */}
      <main className="flex-1 flex flex-col h-full overflow-hidden bg-neutral-950">
        {/* Header */}
        <header className="h-12 border-b border-neutral-800 px-4 flex items-center justify-between bg-neutral-900/20">
          <div className="flex items-center gap-2">
            <span className="text-xs text-neutral-400 font-mono">
              {t("kernel.sessionLabel")}:
            </span>
            <span className="text-xs font-semibold text-neutral-200 font-mono">
              {activeSessionId || t("kernel.noneLabel")}
            </span>
          </div>
          <div className="flex items-center gap-2">
            {isRunning && (
              <span className="text-xs text-cyan-400 animate-pulse font-medium">
                {t("kernel.runningLabel")}
              </span>
            )}
            <button
              data-testid="cancel-btn"
              type="button"
              onClick={handleCancelRun}
              className="px-2.5 py-1 text-xs rounded border border-neutral-700 bg-neutral-800 hover:bg-neutral-700 text-neutral-300"
            >
              {t("kernel.cancel")}
            </button>
          </div>
        </header>

        {/* Content area: Transcript + Plugin Views */}
        <div className="flex-1 flex overflow-hidden">
          {/* Transcript */}
          <div className="flex-1 overflow-y-auto p-4 space-y-3">
            {events.map((ev) => {
              if (ev.type === "kernel.user_message") {
                return (
                  <div
                    key={ev.sequence}
                    data-testid={`event-${ev.sequence}`}
                    className="flex flex-col items-end"
                  >
                    <div className="max-w-2xl px-4 py-2.5 rounded-2xl bg-cyan-700/80 text-white text-sm whitespace-pre-wrap">
                      {String(ev.payload.content || "")}
                    </div>
                  </div>
                );
              }
              // Canonical persisted payloads only: provider turns live under
              // payload.result (output / reasoning / tool_calls[].tool_name|arguments),
              // commands under payload.name + payload.result.{status,output,error,data},
              // and tool results under payload.result.{status,tool_name,output,error}.
              if (ev.type === "kernel.model_turn") {
                const turn = (ev.payload.result ?? {}) as {
                  output?: string | null;
                  reasoning?: string | null;
                  tool_calls?: Array<{
                    tool_name?: string;
                    arguments?: unknown;
                  }>;
                };
                const toolCalls = Array.isArray(turn.tool_calls)
                  ? turn.tool_calls
                  : [];
                return (
                  <div
                    key={ev.sequence}
                    data-testid={`event-${ev.sequence}`}
                    className="flex flex-col items-start space-y-2 max-w-2xl"
                  >
                    {turn.output && (
                      <div className="px-4 py-2.5 rounded-2xl bg-neutral-800 text-neutral-100 text-sm whitespace-pre-wrap">
                        {turn.output}
                      </div>
                    )}
                    {toolCalls.map((tc, idx) => (
                      <div
                        key={idx}
                        className="px-3 py-1.5 rounded-md bg-neutral-900 border border-neutral-700 text-xs font-mono text-amber-300"
                      >
                        ▶ {t("kernel.toolCallLabel")}:{" "}
                        {tc.tool_name || t("kernel.unknownLabel")}
                        <span className="text-neutral-400 ml-2">
                          {JSON.stringify(tc.arguments ?? {})}
                        </span>
                      </div>
                    ))}
                  </div>
                );
              }
              if (
                ev.type === "kernel.tool_completed" ||
                ev.type === "kernel.tool_interrupted"
              ) {
                const toolResult = (ev.payload.result ?? {}) as {
                  status?: string;
                  tool_name?: string;
                  output?: { type?: string; text?: string; mime?: string };
                  error?: string | null;
                };
                const output = toolResult.output ?? {};
                const text =
                  output.type === "text"
                    ? (output.text ?? "")
                    : output.type === "attachment"
                      ? `[${output.mime ?? "attachment"}]`
                      : "";
                const failed =
                  ev.type === "kernel.tool_interrupted" ||
                  toolResult.status === "error";
                return (
                  <div
                    key={ev.sequence}
                    data-testid={`event-${ev.sequence}`}
                    className={`text-xs font-mono px-3 py-1.5 rounded border max-w-2xl ${
                      failed
                        ? "text-red-400 bg-red-950/20 border-red-900/50"
                        : "text-emerald-400 bg-emerald-950/20 border-emerald-900/50"
                    }`}
                  >
                    {failed ? "✖" : "✔"} {t("kernel.toolLabel")} [
                    {toolResult.tool_name ?? t("kernel.unknownLabel")}]:{" "}
                    {failed ? (toolResult.error ?? text ?? "") : text}
                  </div>
                );
              }
              if (
                ev.type === "kernel.command_intent" ||
                ev.type === "kernel.command_completed"
              ) {
                const command = (ev.payload ?? {}) as {
                  name?: string;
                  result?: {
                    status?: string;
                    output?: string | null;
                    error?: string | null;
                  };
                };
                if (ev.type === "kernel.command_intent") {
                  return (
                    <div
                      key={ev.sequence}
                      data-testid={`event-${ev.sequence}`}
                      className="text-xs font-mono text-cyan-400 bg-cyan-950/20 px-3 py-1.5 rounded border border-cyan-900/50 max-w-2xl"
                    >
                      ▷{" "}
                      {t("kernel.commandStarted", {
                        name: command.name ?? t("kernel.unknownLabel"),
                      })}
                    </div>
                  );
                }
                const status = command.result?.status ?? "unknown";
                const detail =
                  command.result?.output ?? command.result?.error ?? "";
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
                    className={`text-xs font-mono px-3 py-1.5 rounded border max-w-2xl ${tone}`}
                  >
                    ✔ {t("kernel.commandLabel")} /
                    {command.name ?? t("kernel.unknownLabel")} [{status}]:{" "}
                    {String(detail)}
                  </div>
                );
              }
              if (ev.type === "kernel.approval_decided") {
                return (
                  <div
                    key={ev.sequence}
                    data-testid={`event-${ev.sequence}`}
                    className="text-xs font-mono text-amber-300 bg-amber-950/20 px-3 py-1.5 rounded border border-amber-900/50 max-w-2xl"
                  >
                    {t("kernel.approvalDecided", {
                      decision: String(
                        ev.payload.decision ?? t("kernel.unknownLabel"),
                      ),
                    })}
                  </div>
                );
              }
              if (ev.type === "kernel.run_interrupted") {
                return (
                  <div
                    key={ev.sequence}
                    data-testid={`event-${ev.sequence}`}
                    className="text-xs font-mono text-amber-300 bg-amber-950/20 px-3 py-1.5 rounded border border-amber-900/50 max-w-2xl"
                  >
                    {t("kernel.runInterrupted", {
                      cause: String(
                        ev.payload.stop_cause ?? t("kernel.unknownLabel"),
                      ),
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
                  {t("kernel.pluginViews")}
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
                  {t("kernel.refresh")}
                </button>
              </div>
              {pluginViews.map((v, idx) => (
                <PluginViewRenderer
                  key={`${activeSessionId}:${v.owner}-${v.name}-${idx}`}
                  descriptor={v}
                  sessionId={activeSessionId}
                  client={client}
                  catalog={catalog}
                  onAction={handleViewAction}
                  onConfirm={handleConfirmDecision}
                />
              ))}
            </div>
          )}
        </div>

        {/* Native tool approval banner (core permission gate) */}
        {pendingApproval && (
          <div
            data-testid="pending-approval-banner"
            className="p-3 bg-amber-950/80 border-t border-amber-500 flex items-center justify-between gap-4"
          >
            <div className="min-w-0">
              <div className="text-xs font-semibold uppercase tracking-wider text-amber-300">
                {t("kernel.approvalTitle")}
              </div>
              <div className="text-sm text-neutral-100 truncate">
                {t("kernel.approvalTool")}: {pendingApproval.toolName}
              </div>
              <div className="text-xs text-neutral-400 font-mono truncate">
                {t("kernel.approvalArguments")}: {pendingApproval.toolArguments}
              </div>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              <button
                data-testid="approval-allow-btn"
                type="button"
                onClick={() => void handleApprovalDecision("allow")}
                className="px-3 py-1.5 rounded bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-medium uppercase tracking-wide"
              >
                {t("kernel.approvalAllow")}
              </button>
              <button
                data-testid="approval-deny-btn"
                type="button"
                onClick={() => void handleApprovalDecision("deny")}
                className="px-3 py-1.5 rounded bg-neutral-700 hover:bg-neutral-600 text-neutral-200 text-xs font-medium uppercase tracking-wide border border-neutral-600"
              >
                {t("kernel.approvalDeny")}
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
              client={client}
              catalog={catalog}
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
              {slashSuggestions.map((cmd) => (
                <button
                  key={cmd}
                  type="button"
                  onClick={() => setPromptInput(`/${cmd} `)}
                  className="w-full text-left px-3 py-1.5 hover:bg-neutral-800 text-neutral-200 font-mono"
                >
                  /{cmd}
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
              onChange={(e) => setPromptInput(e.target.value)}
              placeholder={t("kernel.promptPlaceholder")}
              className="flex-1 px-4 py-2.5 rounded-lg bg-neutral-800 border border-neutral-700 text-sm text-neutral-100 placeholder-neutral-500 focus:outline-none focus:border-cyan-500"
            />
            <button
              data-testid="send-btn"
              type="submit"
              disabled={isRunning || !activeSessionId || !promptInput.trim()}
              className="px-5 py-2.5 rounded-lg bg-cyan-600 hover:bg-cyan-500 active:bg-cyan-700 disabled:opacity-40 text-white font-medium text-sm transition-colors"
            >
              {t("kernel.send")}
            </button>
          </form>
        </div>
      </main>
    </div>
  );
};
