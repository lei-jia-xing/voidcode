/* eslint-disable react-hooks/refs -- composition root: every value read here
 * is a stable hook-derived handle (transport resolver, session flags, view
 * state) that only changes via state updates. The mutable transport client is
 * never touched during render: it is resolved inside effects and event
 * handlers downstream. The rule over-approximates on this file (it flags
 * plain booleans/strings such as `isRunning` and `promptInput` as "refs" via
 * transitive containment through the session object), and no reasonable
 * architecture satisfies it: the client instance must be created in a hook
 * (lifecycle), must store subscription callbacks (transport design), and must
 * reach children through render. This file declares no refs, so disabling the
 * rule here cannot mask a genuine violation. */
import { useLayoutEffect } from "react";
import type React from "react";
import { useTranslation } from "react-i18next";
import type { PluginViewDescriptor } from "../lib/core/types";
import { PluginViewRenderer } from "./views/PluginViewRenderer";
import { ToolResultCard } from "./ToolResultCard";
import { TokenGate } from "./TokenGate";
import { useSession } from "./hooks/useSession";
import {
  argumentHints,
  displaySlashPath,
  matchNamespace,
  slashPathOf,
  useCatalog,
} from "./hooks/useCatalog";
import { useGates } from "./hooks/useGates";
import { useViews } from "./hooks/useViews";
import { useComposer } from "./hooks/useComposer";

export interface CoreAppProps {
  baseUrl?: string;
  token?: string;
}

export const CoreApp: React.FC<CoreAppProps> = ({ baseUrl = "", token }) => {
  const { t } = useTranslation();
  const session = useSession({ baseUrl, token });
  const catalog = useCatalog({
    getClient: session.getClient,
    transportKey: session.transportKey,
    onStatus: session.setStatusMessage,
  });
  const gates = useGates({
    activeSessionId: session.activeSessionId,
    getClient: session.getClient,
    capture: session.capture,
    isCurrent: session.isCurrent,
    isCurrentScope: session.isCurrentScope,
    setStatusMessage: session.setStatusMessage,
    setIsRunning: session.setIsRunning,
    bumpRunActivity: session.bumpRunActivity,
    refreshHealth: session.refreshHealth,
  });
  const views = useViews({
    getClient: session.getClient,
    transportKey: session.transportKey,
    activeSessionId: session.activeSessionId,
    scope: session.scope,
    isCurrentScope: session.isCurrentScope,
    catalogRevision: catalog.catalog.revisionId,
    catalogCommands: catalog.catalog.commands,
    setStatusMessage: session.setStatusMessage,
    retainPendingConfirmation: gates.retainPendingConfirmation,
    pendingApproval: gates.pendingApproval,
    pendingConfirmation: gates.pendingConfirmation,
  });
  const composer = useComposer({
    activeSessionId: session.activeSessionId,
    scope: session.scope,
    isRunning: session.isRunning,
    upgradeFenced: session.upgrade?.fenced ?? false,
    catalogRevision: catalog.catalog.revisionId,
    catalogCommands: catalog.catalog.commands,
    slashCommands: catalog.slashCommands,
    setStatusMessage: session.setStatusMessage,
    createSession: session.createSession,
    cancelRun: session.cancelRun,
    runPrompt: session.runPrompt,
    refreshViews: views.refreshViews,
    prepareCommandInput: views.prepareCommandInput,
    handleViewAction: views.handleViewAction,
    maybeShowSlashForm: views.maybeShowSlashForm,
    orphanCommandInput: views.orphanCommandInput,
  });

  // Cross-hook seams: session-stream events fan out to gates/catalog/views.
  // Assigned in layout (before passives) after every render and read at event
  // time, so the bootstrap passive already sees them and wiring never
  // re-subscribes the stream and callbacks never go stale.
  useLayoutEffect(() => {
    session.registerStreamHandlers({
      projectEvent: gates.projectEvent,
      activationCommitted: (targetRevision, scope) => {
        catalog.handleActivationCommitted();
        views.handleActivationCommitted(scope);
        gates.handleActivationCommitted(targetRevision);
      },
      serverIdentityChanged: (scope) => {
        catalog.invalidate();
        views.invalidate(scope);
      },
      refreshCatalog: catalog.refreshCatalog,
      refreshViews: views.refreshViews,
      adoptParkedApproval: gates.adoptParkedApproval,
    });
    gates.registerGatesHandlers({
      refreshViews: views.refreshViews,
    });
  });

  const upgrade = session.upgrade;
  const pendingApproval = gates.pendingApproval;
  const pendingConfirmation = gates.pendingConfirmation;
  const commandView = views.commandView;
  const activeSessionId = session.activeSessionId;

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
              onClick={session.createSession}
              className="px-2.5 py-1 text-xs rounded bg-cyan-600 hover:bg-cyan-500 text-white font-medium transition-colors"
            >
              + {t("core.newSession")}
            </button>
          </div>
          <div className="p-2 overflow-y-auto max-h-[calc(100vh-140px)]">
            <div className="text-xs uppercase text-neutral-500 font-semibold px-2 py-1 mb-1 tracking-wider">
              {t("core.sessions")}
            </div>
            {session.sessions.map((s) => (
              <button
                key={s.session_id}
                data-testid={`session-tab-${s.session_id}`}
                type="button"
                onClick={() => session.setActiveSessionId(s.session_id)}
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
          <span className="text-neutral-300">{session.statusMessage}</span>
          {!session.showTokenGate && (
            <button
              data-testid="token-edit-btn"
              type="button"
              title={t("core.tokenGateTitle")}
              onClick={() => session.setTokenGateOpen(true)}
              className="ml-2 text-[10px] text-cyan-400 hover:underline"
            >
              {t("core.tokenGateEdit")}
            </button>
          )}
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
            {session.isRunning && (
              <span className="text-xs text-cyan-400 animate-pulse font-medium">
                {t("core.runningLabel")}
              </span>
            )}
            {catalog.shortcutEntries.actions.map(
              ({ chord, entry, conflicted }) => (
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
                      void views.prepareCommandInput(
                        entry.name,
                        catalog.catalog.revisionId,
                        {},
                      );
                    } else {
                      void views.handleViewAction(
                        entry.name,
                        {},
                        catalog.catalog.revisionId,
                      );
                    }
                  }}
                  className="px-2.5 py-1 text-xs rounded border border-neutral-700 bg-neutral-800 hover:bg-neutral-700 disabled:opacity-40 text-neutral-300"
                >
                  {entry.description}
                </button>
              ),
            )}
            <button
              data-testid="cancel-btn"
              type="button"
              onClick={session.cancelRun}
              className="px-2.5 py-1 text-xs rounded border border-neutral-700 bg-neutral-800 hover:bg-neutral-700 text-neutral-300"
            >
              {t("core.cancel")}
            </button>
          </div>
        </header>
        {/* Bearer-token entry: pasted, never in the URL. */}
        {session.showTokenGate && (
          <TokenGate
            serverLabel={session.effectiveBaseUrl}
            onSubmit={session.saveToken}
          />
        )}
        {/* Shortcut conflicts: explicit, never silent first-wins. */}
        {catalog.shortcutEntries.conflicts.map(({ chord, names }) => (
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
            {session.events.map((ev) => {
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
                    key={`${session.clientId}:${activeSessionId}:${ev.sequence}`}
                    evSequence={ev.sequence}
                    tool={content.body}
                    getClient={session.getClient}
                    sessionId={activeSessionId}
                    revisionId={catalog.catalog.revisionId}
                    coreStatus={session.statusMessage}
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
                      : displaySlashPath(catalog.catalog.commands, name)}{" "}
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
              if (content?.kind === "run" && content.body.status === "failed") {
                const errorDetail =
                  content.body.error ||
                  (typeof ev.payload.error === "string"
                    ? ev.payload.error
                    : null) ||
                  t("core.unknownLabel");
                return (
                  <div
                    key={ev.sequence}
                    data-testid={`event-${ev.sequence}`}
                    className="text-xs font-mono text-red-300 bg-red-950/25 px-3 py-1.5 rounded border border-red-900/60 max-w-2xl"
                  >
                    {t("core.runFailed", {
                      error: String(errorDetail),
                    })}
                  </div>
                );
              }
              if (
                content?.kind === "run" &&
                content.body.status === "interrupted"
              ) {
                const cause =
                  content.body.reason ||
                  content.body.stop_cause ||
                  (typeof ev.payload.reason === "string"
                    ? ev.payload.reason
                    : null) ||
                  (typeof ev.payload.stop_cause === "string"
                    ? ev.payload.stop_cause
                    : null) ||
                  t("core.unknownLabel");
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
            <div ref={session.attachMessagesEnd} />
          </div>

          {/* Plugin Views sidebar */}
          {views.pluginViews.length > 0 && (
            <div className="w-80 border-l border-neutral-800 bg-neutral-900/30 p-3 overflow-y-auto">
              <div className="text-xs uppercase font-semibold text-neutral-400 mb-2 tracking-wider flex items-center justify-between">
                <span data-testid="plugin-views-header">
                  {t("core.pluginViews")}
                </span>
                <button
                  data-testid="plugin-views-refresh"
                  type="button"
                  onClick={() => {
                    if (session.scope) views.refreshViews(session.scope);
                  }}
                  className="text-[10px] text-cyan-400 hover:underline"
                >
                  {t("core.refresh")}
                </button>
              </div>
              {views.pluginViews.map((v, idx) => (
                <PluginViewRenderer
                  key={`${activeSessionId}:${v.owner}-${v.name}-${idx}`}
                  descriptor={v}
                  sessionId={activeSessionId}
                  revisionId={views.viewScope.revisionId}
                  getClient={session.getClient}
                  catalog={catalog.catalog.commands}
                  onAction={views.handleViewAction}
                  onConfirm={gates.handleConfirmDecision}
                />
              ))}
            </div>
          )}
        </div>

        {commandView && (
          <section
            data-testid="command-presentation"
            className="border-t border-neutral-700 p-4 max-h-80 overflow-y-auto"
          >
            <div className="flex justify-between items-center">
              <h2 className="font-semibold">{commandView.descriptor.title}</h2>
              <button
                type="button"
                data-testid="command-view-close"
                onClick={() => {
                  views.closeCommandView();
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
              getClient={session.getClient}
              catalog={catalog.catalog.commands}
              onAction={views.handleViewAction}
              onCancel={() => {
                views.closeCommandView();
              }}
              disabled={
                views.commandInFlight ||
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
                onClick={() => void gates.handleApprovalDecision("allow")}
                className="px-3 py-1.5 rounded bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-medium uppercase tracking-wide"
              >
                {t("core.approvalAllow")}
              </button>
              <button
                data-testid="approval-deny-btn"
                type="button"
                onClick={() => void gates.handleApprovalDecision("deny")}
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
              getClient={session.getClient}
              catalog={catalog.catalog.commands}
              onConfirm={gates.handleConfirmDecision}
            />
          </div>
        )}

        {/* Input composer with slash command autocomplete */}
        <div className="p-3 border-t border-neutral-800 bg-neutral-900/40 relative">
          {composer.slashSuggestions.length > 0 && (
            <div
              data-testid="slash-suggestions"
              className="absolute bottom-full mb-1 left-3 bg-neutral-900 border border-neutral-700 rounded-md shadow-xl py-1 w-64 max-h-48 overflow-y-auto z-50 text-xs"
            >
              {composer.slashSuggestions.map((cmd, index) => {
                const descriptor = catalog.catalog.commands.find(
                  (entry) => slashPathOf(entry) === cmd,
                );
                const namespace = descriptor
                  ? null
                  : matchNamespace(catalog.slashCommands, cmd.split(" "));
                return (
                  <button
                    key={cmd}
                    type="button"
                    onClick={() => {
                      composer.selectSuggestion(cmd);
                    }}
                    className={`w-full text-left px-3 py-1.5 hover:bg-neutral-800 text-neutral-200 font-mono ${index === composer.suggestionIndex % composer.slashSuggestions.length ? "bg-neutral-700" : ""}`}
                  >
                    /{cmd}
                    {catalog.slashCommands.some((entry) =>
                      slashPathOf(entry).startsWith(cmd + " "),
                    )
                      ? " ›"
                      : ""}
                    <span className="ml-3 text-neutral-200">
                      {argumentHints(descriptor?.argument_schema)}
                    </span>
                    <span className="ml-3 font-sans text-neutral-300">
                      {descriptor?.description ??
                        (namespace
                          ? t("core.subcommands", {
                              names: namespace.children.join(", "),
                            })
                          : "")}
                    </span>
                  </button>
                );
              })}
            </div>
          )}

          <form onSubmit={composer.handleSubmit} className="flex gap-2">
            <input
              data-testid="prompt-input"
              type="text"
              value={composer.promptInput}
              disabled={session.isRunning || !activeSessionId}
              onChange={(e) => {
                composer.handlePromptChange(e.target.value);
              }}
              onKeyDown={composer.handlePromptKeyDown}
              placeholder={t("core.promptPlaceholder")}
              className="flex-1 px-4 py-2.5 rounded-lg bg-neutral-800 border border-neutral-700 text-sm text-neutral-100 placeholder-neutral-500 focus:outline-none focus:border-cyan-500"
            />
            <button
              data-testid="send-btn"
              type="submit"
              disabled={
                session.isRunning ||
                !activeSessionId ||
                !composer.promptInput.trim()
              }
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
