import { useEffect, useRef, useState, useMemo } from "react";
import { useTranslation } from "react-i18next";
import { useAppStore } from "./store";
import { SessionSidebar } from "./components/SessionSidebar";
import { ChatThread } from "./components/ChatThread";
import { Composer } from "./components/Composer";
import { SettingsPanel } from "./components/SettingsPanel";
import { OpenProjectModal } from "./components/OpenProjectModal";
import { ReviewPanel } from "./components/ReviewPanel";
import { deriveChatMessages } from "./lib/runtime/event-parser";
import { RuntimeClient } from "./lib/runtime/client";
import { Loader2 } from "lucide-react";

function App() {
  const {
    language,
    setLanguage,
    agentPreset,
    setAgentPreset,
    providerModel,
    setProviderModel,
    workspaces,
    workspacesStatus,
    workspacesError,
    workspaceSwitchStatus,
    workspaceSwitchError,
    providers,
    providersStatus,
    providersError,
    providerModels,
    agentPresets,
    loadWorkspaces,
    switchWorkspace,
    loadProviders,
    loadAgents,
    statusSnapshot,
    statusStatus,
    statusError,
    mcpRetryStatus,
    mcpRetryError,
    loadStatus,
    retryMcpConnections,
    reviewSnapshot,
    reviewStatus,
    reviewError,
    reviewSelectedPath,
    reviewDiff,
    reviewDiffStatus,
    reviewDiffError,
    reviewMode,
    loadReview,
    selectReviewPath,
    setReviewMode,
    sessions,
    currentSessionId,
    currentSessionEvents,
    currentSessionOutput,
    currentSessionState,
    loadSessions,
    sessionsStatus,
    sessionsError,
    selectSession,
    runTask,
    resolveApproval,
    replayStatus,
    replayError,
    runStatus,
    runError,
    approvalStatus,
    approvalError,
    settings,
    settingsStatus,
    settingsError,
    loadSettings,
    updateSettings,
  } = useAppStore();
  const { t, i18n } = useTranslation();

  const [showSettings, setShowSettings] = useState(false);
  const [showProjects, setShowProjects] = useState(false);
  const [showReview, setShowReview] = useState(false);
  const [runtimeTestStatus, setRuntimeTestStatus] = useState<
    "idle" | "testing" | "success" | "error"
  >("idle");
  const hydratedInitialSessionRef = useRef(false);
  const chatScrollRef = useRef<HTMLDivElement>(null);
  const lastMessageCountRef = useRef(0);

  const isRunning = runStatus === "running";
  const isReplayLoading = replayStatus === "loading";
  const isApprovalSubmitting = approvalStatus === "submitting";
  const isWaitingApproval = currentSessionState?.status === "waiting";

  const chatMessages = useMemo(
    () =>
      deriveChatMessages(
        currentSessionEvents,
        currentSessionOutput,
        currentSessionId,
      ),
    [currentSessionEvents, currentSessionId, currentSessionOutput],
  );

  useEffect(() => {
    i18n.changeLanguage(language);
  }, [language, i18n]);

  useEffect(() => {
    loadSessions();
  }, [loadSessions]);

  useEffect(() => {
    void loadWorkspaces?.();
    void loadProviders?.();
    void loadAgents?.();
    void loadSettings?.();
    void loadStatus?.();
    void loadReview?.();
  }, [
    loadAgents,
    loadProviders,
    loadReview,
    loadSettings,
    loadStatus,
    loadWorkspaces,
  ]);

  useEffect(() => {
    if (hydratedInitialSessionRef.current || sessionsStatus !== "success") {
      return;
    }
    hydratedInitialSessionRef.current = true;
    if (!currentSessionId || isRunning) {
      return;
    }
    void selectSession(currentSessionId);
  }, [currentSessionId, isRunning, selectSession, sessionsStatus]);

  useEffect(() => {
    const nextLength = chatMessages.length;
    if (nextLength > lastMessageCountRef.current) {
      lastMessageCountRef.current = nextLength;
      const el = chatScrollRef.current;
      if (el) {
        el.scrollTop = el.scrollHeight;
      }
    }
  }, [chatMessages.length]);

  const handleSendMessage = async (message: string) => {
    await runTask(message);
  };

  const toggleLanguage = () => {
    setLanguage(language === "en" ? "zh-CN" : "en");
  };

  const testRuntime = async () => {
    setRuntimeTestStatus("testing");
    try {
      await RuntimeClient.listSessions();
      setRuntimeTestStatus("success");
      setTimeout(() => setRuntimeTestStatus("idle"), 3000);
    } catch (e) {
      console.error("Runtime test failed:", e);
      setRuntimeTestStatus("error");
      setTimeout(() => setRuntimeTestStatus("idle"), 3000);
    }
  };

  const currentSessionSummary = useMemo(
    () => sessions.find((s) => s.session.id === currentSessionId),
    [sessions, currentSessionId],
  );

  const currentSessionTitle = useMemo(() => {
    if (!currentSessionId) return null;
    if (currentSessionSummary?.prompt) return currentSessionSummary.prompt;
    const latestReq = [...currentSessionEvents]
      .reverse()
      .find((e) => e.event_type === "runtime.request_received");
    return (latestReq?.payload?.prompt as string) || currentSessionId;
  }, [currentSessionId, currentSessionSummary, currentSessionEvents]);

  const handleResolveApproval = (decision: "allow" | "deny") => {
    void resolveApproval(decision);
  };

  const composerDisabled =
    isRunning || isReplayLoading || isWaitingApproval || isApprovalSubmitting;
  const hasCurrentWorkspace = Boolean(workspaces?.current);

  return (
    <div className="flex h-screen bg-[#09090b] text-slate-300 font-sans overflow-hidden selection:bg-indigo-500/30">
      <SessionSidebar
        workspaces={workspaces}
        sessions={sessions}
        currentSessionId={currentSessionId}
        sessionsStatus={sessionsStatus}
        sessionsError={sessionsError}
        isRunning={isRunning}
        isReplayLoading={isReplayLoading}
        language={language}
        runtimeTestStatus={runtimeTestStatus}
        onSelectSession={selectSession}
        onToggleLanguage={toggleLanguage}
        onOpenProjects={() => setShowProjects(true)}
        onToggleReview={() => setShowReview((value) => !value)}
        onOpenSettings={() => setShowSettings(true)}
        onTestRuntime={testRuntime}
        showReview={showReview}
        statusSnapshot={statusSnapshot}
        statusStatus={statusStatus}
        statusError={statusError}
        mcpRetryStatus={mcpRetryStatus}
        mcpRetryError={mcpRetryError}
        onRetryMcp={() => {
          void retryMcpConnections();
        }}
      />

      <div className="flex-1 flex flex-col min-w-0">
        {hasCurrentWorkspace ? (
          <>
            <header className="h-14 flex items-center justify-between px-4 border-b border-slate-800 bg-[#0c0c0e] flex-shrink-0">
              <div className="flex items-center gap-2 min-w-0">
                {isReplayLoading && (
                  <Loader2 className="w-4 h-4 animate-spin text-indigo-400 flex-shrink-0" />
                )}
                {currentSessionId ? (
                  <div className="flex flex-col min-w-0">
                    <span className="text-sm font-medium text-slate-200 truncate">
                      {currentSessionTitle}
                    </span>
                    <span className="text-[11px] text-slate-500 font-mono truncate">
                      {currentSessionId}
                    </span>
                  </div>
                ) : (
                  <span className="text-sm font-medium text-slate-400">
                    {t("chat.newChat")}
                  </span>
                )}
              </div>

              <div className="flex items-center gap-2 flex-shrink-0">
                <span
                  className={`px-2.5 py-1 rounded-full text-[11px] font-medium flex items-center border ${
                    isRunning
                      ? "bg-amber-500/10 text-amber-400 border-amber-500/20"
                      : "bg-emerald-500/10 text-emerald-400 border-emerald-500/20"
                  }`}
                >
                  <span
                    className={`w-1.5 h-1.5 rounded-full mr-1.5 ${isRunning ? "bg-amber-500 animate-pulse" : "bg-emerald-500"}`}
                  />
                  {isRunning ? t("session.agentBusy") : t("session.agentIdle")}
                </span>
              </div>
            </header>

            {replayError && (
              <div className="flex-shrink-0 bg-amber-500/10 border-b border-amber-500/20 px-4 py-2 text-xs text-amber-300">
                {t("session.replayError", { message: replayError })}
              </div>
            )}
            {runError && (
              <div className="flex-shrink-0 bg-rose-500/10 border-b border-rose-500/20 px-4 py-2 text-xs text-rose-300">
                {t("common.errorWithMessage", { message: runError })}
              </div>
            )}

            <div ref={chatScrollRef} className="flex-1 overflow-y-auto min-h-0">
              <ChatThread
                messages={chatMessages}
                isRunning={isRunning}
                isWaitingApproval={isWaitingApproval}
                isApprovalSubmitting={isApprovalSubmitting}
                approvalError={approvalError}
                onResolveApproval={handleResolveApproval}
              />
            </div>

            <Composer
              disabled={composerDisabled}
              isRunning={isRunning}
              agentPreset={agentPreset}
              onSubmit={handleSendMessage}
              onAgentPresetChange={setAgentPreset}
              providerModel={providerModel}
              providers={providers}
              providerModels={providerModels}
              agentPresets={agentPresets}
              onProviderModelChange={setProviderModel}
            />
          </>
        ) : (
          <div className="flex flex-1 items-center justify-center p-6">
            <div className="w-full max-w-md rounded-2xl border border-slate-800 bg-[#0c0c0e] p-6 text-center shadow-[0_0_30px_rgba(0,0,0,0.25)]">
              <div className="text-lg font-semibold text-slate-100">
                {t("project.emptyStateTitle")}
              </div>
              <p className="mt-2 text-sm text-slate-400">
                {t("project.emptyStateBody")}
              </p>
              <button
                type="button"
                onClick={() => setShowProjects(true)}
                className="mt-5 inline-flex items-center justify-center rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-indigo-500"
              >
                {t("project.openTitle")}
              </button>
            </div>
          </div>
        )}
      </div>

      <ReviewPanel
        isOpen={showReview}
        mode={reviewMode}
        snapshot={reviewSnapshot}
        status={reviewStatus}
        error={reviewError}
        selectedPath={reviewSelectedPath}
        diff={reviewDiff}
        diffStatus={reviewDiffStatus}
        diffError={reviewDiffError}
        onClose={() => setShowReview(false)}
        onModeChange={setReviewMode}
        onRefresh={loadReview}
        onSelectPath={(path) => {
          void selectReviewPath(path);
        }}
      />

      <SettingsPanel
        isOpen={showSettings}
        settings={settings}
        settingsStatus={settingsStatus}
        settingsError={settingsError}
        providers={providers}
        providersStatus={providersStatus}
        providersError={providersError}
        onClose={() => setShowSettings(false)}
        onLoad={loadSettings}
        onLoadProviders={loadProviders}
        onSave={updateSettings}
      />

      <OpenProjectModal
        isOpen={showProjects}
        onClose={() => setShowProjects(false)}
        recentWorkspaces={workspaces?.recent ?? []}
        candidateWorkspaces={workspaces?.candidates ?? []}
        workspacesStatus={workspacesStatus}
        workspacesError={workspacesError}
        workspaceSwitchStatus={workspaceSwitchStatus}
        workspaceSwitchError={workspaceSwitchError}
        currentWorkspacePath={workspaces?.current?.path ?? null}
        onSwitchWorkspace={switchWorkspace}
      />
    </div>
  );
}

export default App;
