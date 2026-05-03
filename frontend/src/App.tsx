import { useEffect, useRef, useState, useMemo } from "react";
import { useTranslation } from "react-i18next";
import { useAppStore } from "./store";
import { SessionSidebar } from "./components/SessionSidebar";
import { ChatThread } from "./components/ChatThread";
import { Composer, type SessionContextUsage } from "./components/Composer";
import { ChildSessionSidebar } from "./components/ChildSessionSidebar";
import { SettingsPanel } from "./components/SettingsPanel";
import { OpenProjectModal } from "./components/OpenProjectModal";
import { ReviewPanel } from "./components/ReviewPanel";
import { RuntimeOpsPanel } from "./components/RuntimeOpsPanel";
import { TodoPanel } from "./components/TodoPanel";
import { deriveLatestTodoSnapshot } from "./components/todoPanelModel";
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
    loadReview,
    selectReviewPath,
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
    cancelCurrentRun,
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
    selectedBackgroundTaskOutputId,
    backgroundTaskOutput,
    backgroundTaskOutputStatus,
    backgroundTaskOutputError,
    loadBackgroundTasks,
    loadBackgroundTaskOutput,
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
  const [showFileTree, setShowFileTree] = useState(false);
  const [showCodeReview, setShowCodeReview] = useState(false);
  const [showRuntimeOps, setShowRuntimeOps] = useState(false);
  const [isSessionSidebarExpanded, setIsSessionSidebarExpanded] =
    useState(true);
  const [runtimeTestStatus, setRuntimeTestStatus] = useState<
    "idle" | "testing" | "success" | "error"
  >("idle");
  const hydratedInitialSessionRef = useRef(false);
  const chatScrollRef = useRef<HTMLDivElement>(null);
  const lastMessageCountRef = useRef(0);

  const isRunning = runStatus === "running" || runStatus === "cancelling";
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
  const childSessionMessages = useMemo(() => {
    const childResult = backgroundTaskOutput?.session_result;
    if (!selectedBackgroundTaskOutputId || !childResult) {
      return null;
    }
    return deriveChatMessages(
      childResult.transcript,
      childResult.output ?? backgroundTaskOutput.output,
      childResult.session.session.id,
    );
  }, [backgroundTaskOutput, selectedBackgroundTaskOutputId]);
  const displayedMessages = childSessionMessages ?? chatMessages;
  const displayedIsChildSession = childSessionMessages !== null;
  const activeTodoSnapshot = useMemo(
    () => deriveLatestTodoSnapshot(displayedMessages),
    [displayedMessages],
  );
  const composerContextUsage = useMemo(
    () =>
      sessionContextUsageFromMetadata(
        currentSessionState?.metadata,
        providerModel,
        providerModels,
      ),
    [currentSessionState?.metadata, providerModel, providerModels],
  );

  useEffect(() => {
    i18n.changeLanguage(language);
  }, [language, i18n]);

  useEffect(() => {
    loadSessions();
  }, [loadSessions]);

  useEffect(() => {
    if (!currentSessionId) return;
    const hasActiveChildTask = backgroundTasks.some(
      (task) => task.status === "queued" || task.status === "running",
    );
    if (!isRunning && !hasActiveChildTask) return;
    const timer = window.setInterval(() => {
      void loadBackgroundTasks();
    }, 2000);
    return () => window.clearInterval(timer);
  }, [backgroundTasks, currentSessionId, isRunning, loadBackgroundTasks]);

  useEffect(() => {
    void loadWorkspaces?.();
    void loadProviders?.();
    void loadAgents?.();
    void loadSettings?.();
    void loadStatus?.();
    void loadNotifications?.();
    void loadBackgroundTasks?.();
  }, [
    loadAgents,
    loadProviders,
    loadBackgroundTasks,
    loadSettings,
    loadStatus,
    loadWorkspaces,
    loadNotifications,
  ]);

  useEffect(() => {
    if (!showFileTree && !showCodeReview) return;
    if (reviewStatus !== "idle") return;
    void loadReview();
  }, [loadReview, reviewStatus, showCodeReview, showFileTree]);

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
    const nextLength = displayedMessages.length;
    if (nextLength > lastMessageCountRef.current) {
      lastMessageCountRef.current = nextLength;
      const el = chatScrollRef.current;
      if (el) {
        el.scrollTop = el.scrollHeight;
      }
    }
  }, [displayedMessages.length]);

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

  const handleFileTreePathSelect = (path: string) => {
    void selectReviewPath(path);
    setShowCodeReview(true);
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
            <header className="relative z-20 h-14 flex items-center justify-between px-4 border-b border-[color:var(--vc-border-subtle)] bg-[var(--vc-bg)] flex-shrink-0">
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
                  variant={showFileTree ? "secondary" : "ghost"}
                  onClick={() => setShowFileTree((value) => !value)}
                  aria-label={t("review.toggleFileTree")}
                  aria-expanded={showFileTree}
                  aria-pressed={showFileTree}
                >
                  <FolderTree className="w-4 h-4" />
                  <span>{t("review.fileTree")}</span>
                </ControlButton>

                <ControlButton
                  compact
                  variant={showCodeReview ? "secondary" : "ghost"}
                  onClick={() => setShowCodeReview((value) => !value)}
                  aria-label={t("review.toggleCodeReview")}
                  aria-expanded={showCodeReview}
                  aria-pressed={showCodeReview}
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
            {isRunning && (
              <div
                role="status"
                aria-label={t("session.modelWorking")}
                className="relative h-0.5 flex-shrink-0 overflow-hidden bg-transparent"
              >
                <div className="vc-model-working-bar" />
              </div>
            )}

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

            <div className="flex min-h-0 flex-1">
              <div
                ref={chatScrollRef}
                className="min-h-0 min-w-0 flex-1 overflow-y-auto"
              >
                <ChatThread
                  messages={displayedMessages}
                  isRunning={!displayedIsChildSession && isRunning}
                  isWaitingApproval={
                    !displayedIsChildSession && isWaitingApproval
                  }
                  isApprovalSubmitting={
                    !displayedIsChildSession && isApprovalSubmitting
                  }
                  approvalError={displayedIsChildSession ? null : approvalError}
                  onResolveApproval={handleResolveApproval}
                  isWaitingQuestion={
                    !displayedIsChildSession && isWaitingQuestion
                  }
                  isQuestionSubmitting={
                    !displayedIsChildSession && isQuestionSubmitting
                  }
                  questionError={displayedIsChildSession ? null : questionError}
                  onAnswerQuestion={answerQuestion}
                />
              </div>
              <ChildSessionSidebar
                parentSessionId={currentSessionId}
                tasks={backgroundTasks}
                status={backgroundTasksStatus}
                error={backgroundTasksError}
                selectedTaskId={selectedBackgroundTaskOutputId}
                taskOutput={backgroundTaskOutput}
                taskOutputStatus={backgroundTaskOutputStatus}
                taskOutputError={backgroundTaskOutputError}
                onSelectParent={() => {
                  void loadBackgroundTaskOutput(null);
                }}
                onSelectTask={(taskId) => {
                  void loadBackgroundTaskOutput(taskId);
                }}
                onRefresh={() => {
                  void loadBackgroundTasks();
                }}
              />
            </div>

            <TodoPanel snapshot={activeTodoSnapshot} />

            <Composer
              disabled={composerDisabled}
              isRunning={isRunning}
              agentPreset={agentPreset}
              onSubmit={handleSendMessage}
              onCancel={cancelCurrentRun}
              onAgentPresetChange={setAgentPreset}
              providerModel={providerModel}
              reasoningEffort={reasoningEffort}
              providers={providers}
              providerModels={providerModels}
              sessionContextUsage={composerContextUsage}
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
        isOpen={showFileTree}
        surface="file-tree"
        snapshot={reviewSnapshot}
        status={reviewStatus}
        error={reviewError}
        selectedPath={reviewSelectedPath}
        diff={reviewDiff}
        diffStatus={reviewDiffStatus}
        diffError={reviewDiffError}
        onClose={() => setShowFileTree(false)}
        onRefresh={loadReview}
        onSelectPath={handleFileTreePathSelect}
      />

      <ReviewPanel
        isOpen={showCodeReview}
        surface="code-review"
        snapshot={reviewSnapshot}
        status={reviewStatus}
        error={reviewError}
        selectedPath={reviewSelectedPath}
        diff={reviewDiff}
        diffStatus={reviewDiffStatus}
        diffError={reviewDiffError}
        onClose={() => setShowCodeReview(false)}
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
        selectedBackgroundTaskOutputId={selectedBackgroundTaskOutputId}
        backgroundTaskOutput={backgroundTaskOutput}
        backgroundTaskOutputStatus={backgroundTaskOutputStatus}
        backgroundTaskOutputError={backgroundTaskOutputError}
        onClose={() => setShowRuntimeOps(false)}
        onRefreshNotifications={loadNotifications}
        onAcknowledgeNotification={(notificationId) => {
          void acknowledgeNotification(notificationId);
        }}
        onRefreshTasks={loadBackgroundTasks}
        onLoadTaskOutput={(taskId) => {
          void loadBackgroundTaskOutput(taskId);
        }}
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

function sessionContextUsageFromMetadata(
  metadata: Record<string, unknown> | undefined,
  providerModel: string,
  providerModels: Record<
    string,
    { model_metadata?: Record<string, { context_window?: number | null }> }
  >,
): SessionContextUsage {
  const providerTokens = providerContextTokens(metadata);
  const estimatedTokens = contextWindowEstimatedTokens(metadata);
  return {
    usedTokens: providerTokens ?? estimatedTokens,
    totalTokens: providerTotalTokens(metadata),
    estimated: providerTokens === null && estimatedTokens !== null,
    contextWindow:
      selectedModelContextWindow(providerModel, providerModels) ??
      modelContextWindowFromMetadata(metadata) ??
      contextWindowBudget(metadata),
  };
}

function providerContextTokens(
  metadata: Record<string, unknown> | undefined,
): number | null {
  const providerUsage = objectValue(metadata, "provider_usage");
  const latest = objectValue(providerUsage, "latest");
  if (!latest) {
    return null;
  }
  const total =
    numericValue(latest, "input_tokens") +
    numericValue(latest, "cache_creation_tokens") +
    numericValue(latest, "cache_read_tokens");
  return total > 0 ? total : null;
}

function providerTotalTokens(
  metadata: Record<string, unknown> | undefined,
): number | null {
  const providerUsage = objectValue(metadata, "provider_usage");
  const cumulative = objectValue(providerUsage, "cumulative");
  if (!cumulative) {
    return null;
  }
  const total =
    numericValue(cumulative, "input_tokens") +
    numericValue(cumulative, "output_tokens") +
    numericValue(cumulative, "cache_creation_tokens") +
    numericValue(cumulative, "cache_read_tokens");
  return total > 0 ? total : null;
}

function contextWindowEstimatedTokens(
  metadata: Record<string, unknown> | undefined,
): number | null {
  const contextWindow = objectValue(metadata, "context_window");
  return positiveNumericValue(contextWindow, "estimated_context_tokens");
}

function modelContextWindowFromMetadata(
  metadata: Record<string, unknown> | undefined,
): number | null {
  const contextWindow = objectValue(metadata, "context_window");
  return positiveNumericValue(contextWindow, "model_context_window_tokens");
}

function contextWindowBudget(
  metadata: Record<string, unknown> | undefined,
): number | null {
  const contextWindow = objectValue(metadata, "context_window");
  return positiveNumericValue(contextWindow, "token_budget");
}

function selectedModelContextWindow(
  providerModel: string,
  providerModels: Record<
    string,
    { model_metadata?: Record<string, { context_window?: number | null }> }
  >,
): number | null {
  const [providerName, ...modelParts] = providerModel.trim().split("/");
  const modelName = modelParts.join("/");
  if (!providerName || !modelName) {
    return null;
  }
  const metadata = providerModels[providerName]?.model_metadata ?? {};
  const candidate = metadata[modelName] ?? metadata[providerModel];
  const contextWindow = candidate?.context_window;
  return typeof contextWindow === "number" && contextWindow > 0
    ? contextWindow
    : null;
}

function objectValue(
  source: Record<string, unknown> | undefined,
  key: string,
): Record<string, unknown> | undefined {
  const value = source?.[key];
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function numericValue(
  source: Record<string, unknown> | undefined,
  key: string,
): number {
  const value = source?.[key];
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? value
    : 0;
}

function positiveNumericValue(
  source: Record<string, unknown> | undefined,
  key: string,
): number | null {
  const value = numericValue(source, key);
  return value > 0 ? value : null;
}

export default App;
