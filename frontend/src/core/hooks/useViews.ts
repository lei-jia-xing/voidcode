import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useTranslation } from "react-i18next";
import { CoreApiError, type CoreClient } from "../../lib/core/client";
import type {
  CommandDescriptor,
  FormField,
  PluginViewDescriptor,
} from "../../lib/core/types";
import { displaySlashPath } from "./useCatalog";
import type { SessionScope } from "./useSession";
import type { PendingConfirmationEntry } from "./useGates";

export interface ViewScopeState {
  scope: SessionScope | null;
  revisionId: string;
  views: PluginViewDescriptor[];
}

export interface CommandViewState {
  scope: SessionScope;
  revisionId: string;
  descriptor: PluginViewDescriptor;
  liveSource?: { action: string; args: Record<string, unknown> };
  refreshAfterMs?: number | null;
}

/**
 * Owns the view panels: the session's plugin-views snapshot (only actionable
 * when validated against the SAME catalog revision), the open command
 * presentation, command invocations in flight, and live command-input polling.
 */
export function useViews({
  getClient,
  transportKey,
  activeSessionId,
  scope,
  isCurrentScope,
  catalogRevision,
  catalogCommands,
  setStatusMessage,
  retainPendingConfirmation,
  pendingApproval,
  pendingConfirmation,
}: {
  getClient: () => CoreClient;
  transportKey: [string, string | undefined];
  activeSessionId: string;
  scope: SessionScope | null;
  isCurrentScope: (scope: SessionScope) => boolean;
  catalogRevision: string;
  catalogCommands: CommandDescriptor[];
  setStatusMessage: (message: string) => void;
  retainPendingConfirmation: (
    requestId: string,
    entry: PendingConfirmationEntry,
  ) => void;
  pendingApproval: unknown;
  pendingConfirmation: unknown;
}) {
  const { t } = useTranslation();
  const [viewScope, setViewScope] = useState<ViewScopeState>({
    scope: null,
    revisionId: "",
    views: [],
  });
  const [commandView, setCommandView] = useState<CommandViewState | null>(null);
  const commandViewLiveRef = useRef<{
    action: string;
    args: Record<string, unknown>;
    revisionId: string;
  } | null>(null);
  // Only the newest views snapshot may render, so a reply issued before an
  // activation commit can never overwrite the one issued after it.
  const viewsOpRef = useRef<AbortController | null>(null);
  // Only the newest command-input/interaction reply may render: navigation,
  // typing, and invokes orphan the previous one.
  const inputOpRef = useRef<AbortController | null>(null);
  // Command invocations in flight: separate from the authoritative root-run
  // state, so a monitor action can never reset the main run indicator.
  const [commandInFlight, setCommandInFlight] = useState<boolean>(false);
  const [boundTransport, setBoundTransport] = useState(transportKey);
  const [boundScope, setBoundScope] = useState(scope);

  // Render-phase invalidation: a new transport or scope strands every panel
  // before paint. Aborts + the scope's refetch stay in the layouts below.
  if (boundTransport !== transportKey) {
    setBoundTransport(transportKey);
    setCommandView(null);
    setViewScope({ scope: null, revisionId: "", views: [] });
    setCommandInFlight(false);
  }
  if (boundScope !== scope) {
    setBoundScope(scope);
    setCommandView(null);
    setViewScope({ scope, revisionId: "", views: [] });
    setCommandInFlight(false);
  }

  // Views are only actionable when the catalog they are validated against was
  // captured under the SAME revision: after an activation commit both are
  // cleared and refetched, and no form/choice can be rendered from a v1 views
  // envelope while the v2 catalog (or the reverse) is what the client holds.
  const pluginViews = useMemo(() => {
    if (!scope || scope.sessionId !== activeSessionId) return [];
    if (viewScope.scope !== scope) return [];
    if (
      viewScope.revisionId === "" ||
      viewScope.revisionId !== catalogRevision
    ) {
      return [];
    }
    return viewScope.views;
  }, [activeSessionId, catalogRevision, scope, viewScope]);

  const activeCommandView = useMemo(() => {
    if (!commandView || !scope) return null;
    if (commandView.scope !== scope) return null;
    if (scope.sessionId !== activeSessionId) return null;
    if (commandView.revisionId !== catalogRevision) return null;
    return commandView;
  }, [activeSessionId, catalogRevision, commandView, scope]);

  const refreshViews = useCallback(
    (scope: SessionScope) => {
      viewsOpRef.current?.abort();
      const op = new AbortController();
      viewsOpRef.current = op;
      getClient()
        .views(scope.sessionId)
        .then((snapshot) => {
          // A newer snapshot (e.g. the one right after an activation commit) has
          // already won: an older in-flight reply never overwrites it.
          if (op.signal.aborted) return;
          if (!isCurrentScope(scope)) {
            return; // stale scope: never render these views
          }
          setViewScope({
            scope,
            revisionId: snapshot.revisionId,
            views: snapshot.views,
          });
        })
        .catch((err) => {
          if (!isCurrentScope(scope)) {
            return; // a superseded read's failure must not touch this view
          }
          console.error("Failed to fetch views", err);
        });
    },
    [getClient, isCurrentScope],
  );

  /** Activation commit: clear immediately, then refetch together with catalog. */
  const handleActivationCommitted = useCallback(
    (scope: SessionScope) => {
      setViewScope((prev) =>
        prev.scope === scope ? { scope, revisionId: "", views: [] } : prev,
      );
      refreshViews(scope);
    },
    [refreshViews],
  );

  /** Server identity changed under us: nothing stays actionable. */
  const invalidate = useCallback((scope: SessionScope) => {
    setViewScope((prev) =>
      prev.scope === scope ? { scope, revisionId: "", views: [] } : prev,
    );
  }, []);

  const closeCommandView = useCallback(() => {
    inputOpRef.current?.abort();
    commandViewLiveRef.current = null;
    setCommandView(null);
  }, []);

  /** Typing in the composer orphans any in-flight command-input reply. */
  const orphanCommandInput = useCallback(() => {
    inputOpRef.current?.abort();
  }, []);

  const prepareCommandInput = async (
    cmd: string,
    revisionId: string,
    args: Record<string, unknown> = {},
  ) => {
    const issueScope = scope;
    if (!issueScope || issueScope.sessionId !== activeSessionId) return;
    const descriptor = catalogCommands.find((entry) => entry.name === cmd);
    if (!descriptor?.has_input || catalogRevision !== revisionId) {
      setStatusMessage(t("core.commandInputRevisionChanged"));
      return;
    }
    inputOpRef.current?.abort();
    const op = new AbortController();
    inputOpRef.current = op;
    try {
      const snapshot = await getClient().commandInput(
        issueScope.sessionId,
        cmd,
        revisionId,
        args,
      );
      if (!isCurrentScope(issueScope) || op.signal.aborted) return;
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
        scope: issueScope,
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
      if (isCurrentScope(issueScope) && !op.signal.aborted) {
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
    const issueScope = scope;
    if (!issueScope || issueScope.sessionId !== activeSessionId) return;
    inputOpRef.current?.abort();
    const interactionOp = new AbortController();
    inputOpRef.current = interactionOp;
    setCommandInFlight(true);
    const displayName = displaySlashPath(catalogCommands, actionName);
    setStatusMessage(t("core.statusInvoking", { name: displayName }));
    try {
      const res = await getClient().invoke(
        issueScope.sessionId,
        actionName,
        args,
        revisionId,
      );
      if (!isCurrentScope(issueScope)) {
        return; // scope changed while the command ran: drop the reply
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
      if (res.status === "ok" && !interactionOp.signal.aborted) {
        // A direct invoke receipt replaces the view: it never inherits polling.
        commandViewLiveRef.current = null;
        const presentation = res.presentation;
        setCommandView(
          presentation
            ? {
                scope: issueScope,
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
      refreshViews(issueScope);
    } catch (err: unknown) {
      if (!isCurrentScope(issueScope)) return;
      if (err instanceof CoreApiError && err.code === "upgrade_pending") {
        setStatusMessage(
          t("core.statusUpgradePendingAction", { name: displayName }),
        );
        return;
      }
      const msg = err instanceof Error ? err.message : String(err);
      setStatusMessage(t("core.statusInvokeFailed", { message: msg }));
    } finally {
      if (isCurrentScope(issueScope)) {
        setCommandInFlight(false);
      }
    }
  };

  /**
   * Slash form synthesis: a bare `/command` whose admitted schema is a flat
   * set of scalar fields opens a form instead of dispatching. Returns true
   * when it showed the form and the caller must stop.
   */
  const maybeShowSlashForm = (
    descriptor: CommandDescriptor,
    issueScope: SessionScope,
  ): boolean => {
    const schema = descriptor.argument_schema ?? {};
    const properties =
      schema.properties !== null && typeof schema.properties === "object"
        ? (schema.properties as Record<string, { type?: string }>)
        : {};
    const required = Array.isArray(schema.required)
      ? schema.required.filter((r): r is string => typeof r === "string")
      : [];
    if (required.length === 0 || Object.keys(properties).length === 0) {
      return false;
    }
    const types = {
      string: "text",
      integer: "integer",
      number: "number",
      boolean: "boolean",
    } as const;
    if (
      !Object.values(properties).every(
        (spec) =>
          spec && typeof spec === "object" && spec.type && spec.type in types,
      )
    ) {
      return false;
    }
    const fields: FormField[] = Object.entries(properties).map(
      ([name, spec]) => ({
        name,
        label: name,
        required: required.includes(name),
        field_type: types[spec.type as keyof typeof types],
      }),
    );
    inputOpRef.current?.abort();
    commandViewLiveRef.current = null;
    setCommandView({
      scope: issueScope,
      revisionId: catalogRevision,
      descriptor: {
        owner: descriptor.owner,
        name: descriptor.name,
        title: descriptor.description,
        view_type: "form",
        data: { fields, action: descriptor.name, initial_values: {} },
      },
      refreshAfterMs: null,
    });
    return true;
  };

  // Strand in-flight panel reads with every bound generation.
  useLayoutEffect(() => {
    viewsOpRef.current?.abort();
    inputOpRef.current?.abort();
    commandViewLiveRef.current = null;
  }, [boundTransport, boundScope]);

  // Fetch the bound scope's views. Stale-scope replies are refused by the
  // op + scope guards.
  useLayoutEffect(() => {
    if (boundScope) refreshViews(boundScope);
  }, [boundScope, refreshViews]);

  // Live command-input polling: only explicit refresh_after_ms on the open
  // view enables it, and only while that view is mounted for this scope.
  // Gates and command flights own the region while open, so the loop pauses
  // while one is mounted: a refresh never steals focus or resets a draft.
  // The loop never touches the interaction op: navigation, prompt edits,
  // and invokes must neither freeze it nor be invalidated by it. Staleness
  // is the captured live-source identity (navigation/close replaces it) plus
  // the scope guards every other read already uses.
  useEffect(() => {
    if (commandView === null) return;
    if (
      !scope ||
      commandView.scope !== scope ||
      scope.sessionId !== activeSessionId ||
      commandView.revisionId !== catalogRevision
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
    const issueScope = scope;
    const cadence = commandView.refreshAfterMs;
    let cancelled = false;
    let timer: number | undefined;
    // A reply is current only while its captured source is still the live
    // one: any navigation or close replaces the ref object and orphans it.
    const isLiveReply = () =>
      !cancelled &&
      isCurrentScope(issueScope) &&
      commandViewLiveRef.current === live;
    const readOnce = () => {
      if (!isLiveReply()) return;
      getClient()
        .commandInput(
          issueScope.sessionId,
          live.action,
          live.revisionId,
          live.args,
        )
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
            prev === null || prev.scope !== issueScope
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
    activeSessionId,
    catalogRevision,
    commandInFlight,
    getClient,
    commandView,
    isCurrentScope,
    pendingApproval,
    pendingConfirmation,
    scope,
    setStatusMessage,
    t,
  ]);

  return {
    viewScope,
    pluginViews,
    commandView: activeCommandView,
    commandInFlight,
    refreshViews,
    handleActivationCommitted,
    invalidate,
    prepareCommandInput,
    handleViewAction,
    maybeShowSlashForm,
    closeCommandView,
    orphanCommandInput,
  };
}
