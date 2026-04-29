import { useEffect, useRef, useState, useMemo } from "react";
import { useTranslation } from "react-i18next";
import { useAppStore } from "./store";
import { SessionSidebar } from "./components/SessionSidebar";
import { ChatThread } from "./components/ChatThread";
import { Composer } from "./components/Composer";
import { SettingsPanel } from "./components/SettingsPanel";
import { OpenProjectModal } from "./components/OpenProjectModal";
import { ReviewPanel } from "./components/ReviewPanel";
import { RuntimeOpsPanel } from "./components/RuntimeOpsPanel";
import { ControlButton } from "./components/ui";
import { deriveChatMessages } from "./lib/runtime/event-parser";
import { RuntimeClient } from "./lib/runtime/client";
import {
  Loader2,
  Server,
  CheckCircle2,
  XCircle,
  GitCompare,
  FolderTree,
  PanelLeft,
} from "lucide-react";
import { StatusBar } from "./components/StatusBar";
import { buildSessionDisplayTitle } from "./components/sessionTitle";

function App() {
  const {
    language,
    setLanguage,
    agentPreset,
    setAgentPreset,
    providerModel,
    setProviderModel,
    reasoningEffort,
    setReasoningEffort,
    workspaces,
    workspacesStatus,
    workspacesError,
    workspaceSwitchStatus,
    workspaceSwitchError,
    providers,
    providersStatus,
    providersError,
    providerModels,
    providerValidationResults,
    providerValidationStatus,
    providerValidationError,
    agentPresets,
    loadWorkspaces,
    switchWorkspace,
    loadProviders,
    validateProviderCredentials,
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
    sessionSidebarWidth,
    setSessionSidebarWidth,
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
    questionStatus,
    questionError,
    answerQuestion,
    notifications,
    notificationsStatus,
    notificationsError,
    loadNotifications,
    acknowledgeNotification,
    backgroundTasks,
    backgroundTasksStatus,
    backgroundTasksError,
    loadBackgroundTasks,
    cancelBackgroundTask,
    sessionDebug,
    sessionDebugStatus,
    sessionDebugError,
    loadSessionDebug,
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
  const [showRuntimeOps, setShowRuntimeOps] = useState(false);
  const [isSessionSidebarExpanded, setIsSessionSidebarExpanded] =
    useState(true);
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
  const isQuestionSubmitting = questionStatus === "submitting";
  const latestWaitingEvent = [...currentSessionEvents]
    .reverse()
    .find(
      (event) =>
        event.event_type === "runtime.approval_requested" ||
        event.event_type === "runtime.question_requested",
    );
  const isWaitingQuestion =
    currentSessionState?.status === "waiting" &&
    latestWaitingEvent?.event_type === "runtime.question_requested";

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
    void loadNotifications?.();
    void loadBackgroundTasks?.();
  }, [
    loadAgents,
    loadProviders,
    loadReview,
    loadBackgroundTasks,
    loadSettings,
    loadStatus,
    loadWorkspaces,
    loadNotifications,
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
    if (currentSessionSummary?.prompt) {
      return buildSessionDisplayTitle(
        currentSessionSummary.prompt,
        currentSessionId,
      );
    }
    const latestReq = [...currentSessionEvents]
      .reverse()
      .find((e) => e.event_type === "runtime.request_received");
    return buildSessionDisplayTitle(
      latestReq?.payload?.prompt as string | undefined,
      currentSessionId,
    );
  }, [currentSessionId, currentSessionSummary, currentSessionEvents]);

  const handleResolveApproval = (decision: "allow" | "deny") => {
    void resolveApproval(decision);
  };

  const handleLoadSessionDebug = () => {
    void loadSessionDebug(currentSessionId);
  };

  const handleReviewSurfaceToggle = (mode: "changes" | "files") => {
    if (showReview && reviewMode === mode) {
      setShowReview(false);
      return;
    }

    setReviewMode(mode);
    setShowReview(true);
  };

  const composerDisabled =
    isRunning ||
    isReplayLoading ||
    isWaitingApproval ||
    isApprovalSubmitting ||
    isQuestionSubmitting;
  const hasCurrentWorkspace = Boolean(workspaces?.current);

  return (
    <div className="flex h-screen bg-[var(--vc-bg)] text-[var(--vc-text-muted)] font-sans overflow-hidden selection:bg-[var(--vc-border-strong)] selection:text-[var(--vc-text-primary)]">
      <SessionSidebar
        workspaces={workspaces}
        sessions={sessions}
        currentSessionId={currentSessionId}
        sidebarWidth={sessionSidebarWidth}
        sessionsStatus={sessionsStatus}
        sessionsError={sessionsError}
        isRunning={isRunning}
        isReplayLoading={isReplayLoading}
        isExpanded={isSessionSidebarExpanded}
        onSidebarWidthChange={setSessionSidebarWidth}
        onExpandedChange={setIsSessionSidebarExpanded}
        onSelectSession={selectSession}
        onOpenProjects={() => setShowProjects(true)}
        onOpenSettings={() => setShowSettings(true)}
      />

      <div className="flex-1 flex flex-col min-w-0">
        {hasCurrentWorkspace ? (
          <>
            <header className="h-14 flex items-center justify-between px-4 border-b border-[color:var(--vc-border-subtle)] bg-[var(--vc-bg)] flex-shrink-0">
              <div className="flex items-center gap-2 min-w-0">
                <ControlButton
                  compact
                  variant={isSessionSidebarExpanded ? "secondary" : "ghost"}
                  onClick={() => setIsSessionSidebarExpanded((value) => !value)}
                  aria-label={t("sidebar.toggle")}
                  aria-expanded={isSessionSidebarExpanded}
                >
                  <PanelLeft className="w-4 h-4" />
                  <span>{t("sidebar.sessions")}</span>
                </ControlButton>
                {isReplayLoading && (
                  <Loader2 className="w-4 h-4 animate-spin text-[var(--vc-text-muted)] flex-shrink-0" />
                )}
                {currentSessionId ? (
                  <div className="flex flex-col min-w-0">
                    <span className="text-sm font-medium text-[var(--vc-text-primary)] truncate">
                      {currentSessionTitle}
                    </span>
                    <span className="text-[11px] text-[var(--vc-text-subtle)] font-mono truncate">
                      {currentSessionId}
                    </span>
                  </div>
                ) : (
                  <span className="text-sm font-medium text-[var(--vc-text-muted)]">
                    {t("chat.newChat")}
                  </span>
                )}
              </div>

              <div className="flex items-center gap-2 flex-shrink-0">
                <span
                  className={`px-2.5 py-1 rounded-full text-[11px] font-medium flex items-center border ${
                    isRunning
                      ? "bg-[var(--vc-surface-2)] text-[var(--vc-text-muted)] border-[color:var(--vc-border-subtle)]"
                      : "bg-[var(--vc-surface-1)] text-[var(--vc-text-primary)] border-[color:var(--vc-border-strong)]"
                  }`}
                >
                  <span
                    className={`w-1.5 h-1.5 rounded-full mr-1.5 ${isRunning ? "bg-[var(--vc-text-subtle)] animate-pulse" : "bg-[var(--vc-text-muted)]"}`}
                  />
                  {isRunning ? t("session.agentBusy") : t("session.agentIdle")}
                </span>

                <div className="w-px h-4 bg-[var(--vc-border-subtle)] mx-1" />

                <StatusBar
                  snapshot={statusSnapshot}
                  status={statusStatus}
                  error={statusError}
                  mcpRetryStatus={mcpRetryStatus}
                  mcpRetryError={mcpRetryError}
                  onRetryMcp={() => {
                    void retryMcpConnections();
                  }}
                />

                <ControlButton
                  compact
                  variant={
                    showReview && reviewMode === "files" ? "secondary" : "ghost"
                  }
                  onClick={() => handleReviewSurfaceToggle("files")}
                  aria-label={t("review.toggleFileTree")}
                  aria-expanded={showReview && reviewMode === "files"}
                  aria-pressed={showReview && reviewMode === "files"}
                >
                  <FolderTree className="w-4 h-4" />
                  <span>{t("review.fileTree")}</span>
                </ControlButton>

                <ControlButton
                  compact
                  variant={
                    showReview && reviewMode === "changes"
                      ? "secondary"
                      : "ghost"
                  }
                  onClick={() => handleReviewSurfaceToggle("changes")}
                  aria-label={t("review.toggleCodeReview")}
                  aria-expanded={showReview && reviewMode === "changes"}
                  aria-pressed={showReview && reviewMode === "changes"}
                >
                  <GitCompare className="w-4 h-4" />
                  <span>{t("review.codeReview")}</span>
                </ControlButton>

                <ControlButton
                  compact
                  icon
                  variant={showRuntimeOps ? "secondary" : "ghost"}
                  onClick={() => setShowRuntimeOps((value) => !value)}
                  aria-label={t("runtimeOps.title")}
                  aria-expanded={showRuntimeOps}
                >
                  <Server className="w-4 h-4" />
                </ControlButton>

                <ControlButton
                  compact
                  icon
                  variant="ghost"
                  onClick={testRuntime}
                  disabled={runtimeTestStatus === "testing"}
                  aria-label={t("debug.testRuntime")}
                >
                  {runtimeTestStatus === "testing" ? (
                    <Loader2 className="w-4 h-4 animate-spin text-[var(--vc-text-muted)]" />
                  ) : runtimeTestStatus === "success" ? (
                    <CheckCircle2 className="w-4 h-4 text-[var(--vc-confirm-text)]" />
                  ) : runtimeTestStatus === "error" ? (
                    <XCircle className="w-4 h-4 text-[var(--vc-danger-text)]" />
                  ) : (
                    <Server className="w-4 h-4" />
                  )}
                </ControlButton>
              </div>
            </header>

            {replayError && (
              <div className="flex-shrink-0 bg-[var(--vc-surface-1)] border-b border-[color:var(--vc-border-subtle)] px-4 py-2 text-xs text-[var(--vc-text-muted)]">
                {t("session.replayError", { message: replayError })}
              </div>
            )}
            {runError && (
              <div className="flex-shrink-0 bg-[var(--vc-surface-1)] border-b border-[color:var(--vc-border-subtle)] px-4 py-2 text-xs text-[var(--vc-danger-text)]">
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
                isWaitingQuestion={isWaitingQuestion}
                isQuestionSubmitting={isQuestionSubmitting}
                questionError={questionError}
                onAnswerQuestion={answerQuestion}
              />
            </div>

            <Composer
              disabled={composerDisabled}
              isRunning={isRunning}
              agentPreset={agentPreset}
              onSubmit={handleSendMessage}
              onAgentPresetChange={setAgentPreset}
              providerModel={providerModel}
              reasoningEffort={reasoningEffort}
              providers={providers}
              providerModels={providerModels}
              agentPresets={agentPresets}
              onProviderModelChange={setProviderModel}
              onReasoningEffortChange={setReasoningEffort}
            />
          </>
        ) : (
          <div className="flex flex-1 flex-col relative">
            <header className="h-14 flex items-center justify-end px-4 border-b border-transparent flex-shrink-0 absolute top-0 left-0 right-0 z-10">
              <div className="flex items-center gap-2">
                <ControlButton
                  compact
                  icon
                  variant="ghost"
                  onClick={testRuntime}
                  disabled={runtimeTestStatus === "testing"}
                  aria-label={t("debug.testRuntime")}
                >
                  {runtimeTestStatus === "testing" ? (
                    <Loader2 className="w-4 h-4 animate-spin text-[var(--vc-text-muted)]" />
                  ) : runtimeTestStatus === "success" ? (
                    <CheckCircle2 className="w-4 h-4 text-[var(--vc-confirm-text)]" />
                  ) : runtimeTestStatus === "error" ? (
                    <XCircle className="w-4 h-4 text-[var(--vc-danger-text)]" />
                  ) : (
                    <Server className="w-4 h-4" />
                  )}
                </ControlButton>
              </div>
            </header>

            <div className="flex flex-1 items-center justify-center p-6 pt-14">
              <div className="w-full max-w-md rounded-2xl border border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] p-6 text-center shadow-[0_0_30px_rgba(0,0,0,0.25)]">
                <div className="text-lg font-semibold text-[var(--vc-text-primary)]">
                  {t("project.emptyStateTitle")}
                </div>
                <p className="mt-2 text-sm text-[var(--vc-text-muted)]">
                  {t("project.emptyStateBody")}
                </p>
                <ControlButton
                  variant="primary"
                  onClick={() => setShowProjects(true)}
                  className="mt-5"
                >
                  {t("project.openTitle")}
                </ControlButton>
              </div>
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

      <RuntimeOpsPanel
        isOpen={showRuntimeOps}
        currentSessionId={currentSessionId}
        debugSnapshot={sessionDebug}
        debugStatus={sessionDebugStatus}
        debugError={sessionDebugError}
        notifications={notifications}
        notificationsStatus={notificationsStatus}
        notificationsError={notificationsError}
        backgroundTasks={backgroundTasks}
        backgroundTasksStatus={backgroundTasksStatus}
        backgroundTasksError={backgroundTasksError}
        onClose={() => setShowRuntimeOps(false)}
        onRefreshNotifications={loadNotifications}
        onAcknowledgeNotification={(notificationId) => {
          void acknowledgeNotification(notificationId);
        }}
        onRefreshTasks={loadBackgroundTasks}
        onCancelTask={(taskId) => {
          void cancelBackgroundTask(taskId);
        }}
        onRefreshDebug={handleLoadSessionDebug}
      />

      <SettingsPanel
        isOpen={showSettings}
        settings={settings}
        settingsStatus={settingsStatus}
        settingsError={settingsError}
        providers={providers}
        providersStatus={providersStatus}
        providersError={providersError}
        providerModels={providerModels}
        providerValidationResults={providerValidationResults}
        providerValidationStatus={providerValidationStatus}
        providerValidationError={providerValidationError}
        language={language}
        onToggleLanguage={() => setLanguage(language === "en" ? "zh-CN" : "en")}
        onClose={() => setShowSettings(false)}
        onLoad={loadSettings}
        onLoadProviders={loadProviders}
        onValidateProvider={validateProviderCredentials}
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
