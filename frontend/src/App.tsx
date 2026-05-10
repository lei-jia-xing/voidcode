import { useCallback, useEffect, useRef, useState, useMemo } from "react";
import { useTranslation } from "react-i18next";
import { useAppStore } from "./store";
import { SessionSidebar } from "./components/SessionSidebar";
import { ChatThread } from "./components/ChatThread";
import { Composer, type SessionContextUsage } from "./components/Composer";
import { SettingsPanel } from "./components/SettingsPanel";
import { OpenProjectModal } from "./components/OpenProjectModal";
import { ReviewPanel } from "./components/ReviewPanel";
import { TodoPanel } from "./components/TodoPanel";
import { deriveLatestTodoSnapshot } from "./components/todoPanelModel";
import { ControlButton } from "./components/ui";
import { deriveChatMessages } from "./lib/runtime/event-parser";
import {
  FolderTree,
  GitCompare,
  Loader2,
  MoveLeft,
  Sparkles,
  PanelLeft,
} from "lucide-react";
import { StatusBar } from "./components/StatusBar";
import { buildSessionDisplayTitle } from "./components/sessionTitle";

function SubsessionBanner({
  parentPrompt,
  childPrompt,
  taskStatus,
  subagentType,
  summaryOutput,
  approvalBlocked,
  questionRequestId,
  approvalRequestId,
  hookReminder,
}: {
  parentPrompt: string | null;
  childPrompt: string | null;
  taskStatus: string | null;
  subagentType: string | null;
  summaryOutput: string | null;
  approvalBlocked: boolean;
  questionRequestId: string | null;
  approvalRequestId: string | null;
  hookReminder?: {
    active?: boolean;
    task_status?: string;
    child_status?: string;
    lifecycle_status?: string;
    approval_blocked?: boolean;
    result_available?: boolean;
    approval_request_id?: string;
    question_request_id?: string;
    message?: string;
  } | null;
}) {
  const { t } = useTranslation();
  return (
    <div className="border-b border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] px-4 py-3 text-sm">
      <div className="mx-auto max-w-[var(--vc-chat-content-width)] space-y-2">
        <div className="flex items-center gap-2 text-[var(--vc-text-primary)]">
          <Sparkles className="h-4 w-4" />
          <span className="font-medium">{t("subsession.bannerTitle")}</span>
        </div>
        {parentPrompt ? (
          <div>
            <div className="text-[11px] uppercase tracking-wide text-[var(--vc-text-subtle)]">
              {t("subsession.parentPrompt")}
            </div>
            <div className="mt-1 text-[var(--vc-text-muted)]">
              {parentPrompt}
            </div>
          </div>
        ) : null}
        {childPrompt ? (
          <div>
            <div className="text-[11px] uppercase tracking-wide text-[var(--vc-text-subtle)]">
              {t("subsession.childPrompt")}
            </div>
            <div className="mt-1 text-[var(--vc-text-primary)]">
              {childPrompt}
            </div>
          </div>
        ) : null}
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-[var(--vc-text-subtle)]">
          {subagentType ? (
            <span>{t("subsession.subagent", { value: subagentType })}</span>
          ) : null}
          {taskStatus ? (
            <span>{t("subsession.status", { value: taskStatus })}</span>
          ) : null}
          {approvalBlocked ? (
            <span>{t("subsession.approvalBlocked")}</span>
          ) : null}
          {approvalRequestId ? (
            <span>{t("subsession.approvalPending")}</span>
          ) : null}
          {questionRequestId ? (
            <span>{t("subsession.questionPending")}</span>
          ) : null}
        </div>
        {summaryOutput ? (
          <div className="text-xs text-[var(--vc-text-muted)]">
            <span className="text-[var(--vc-text-subtle)]">
              {t("subsession.summary")}:{" "}
            </span>
            {summaryOutput}
          </div>
        ) : null}
        {hookReminder?.message ? (
          <div className="rounded-[var(--vc-radius-control)] border border-[color:var(--vc-border-subtle)] bg-[var(--vc-bg)] px-3 py-2 text-xs text-[var(--vc-text-muted)]">
            <div className="text-[11px] uppercase tracking-wide text-[var(--vc-text-subtle)]">
              {t("subsession.hookReminder")}
            </div>
            <div className="mt-1">{hookReminder.message}</div>
          </div>
        ) : null}
      </div>
    </div>
  );
}

function SubsessionHandoffCard({
  parentPrompt,
  childPrompt,
}: {
  parentPrompt: string | null;
  childPrompt: string | null;
}) {
  const { t } = useTranslation();
  if (!parentPrompt && !childPrompt) return null;

  return (
    <div className="mx-auto mt-4 max-w-[var(--vc-chat-content-width)] px-4">
      <div className="rounded-2xl border border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] p-4">
        <div className="text-sm font-medium text-[var(--vc-text-primary)]">
          {t("subsession.commandCardTitle")}
        </div>
        {parentPrompt ? (
          <div className="mt-3">
            <div className="text-[11px] uppercase tracking-wide text-[var(--vc-text-subtle)]">
              {t("subsession.parentPrompt")}
            </div>
            <div className="mt-1 text-sm text-[var(--vc-text-muted)]">
              {parentPrompt}
            </div>
          </div>
        ) : null}
        {childPrompt ? (
          <div className="mt-3 rounded-[var(--vc-radius-control)] border border-[color:var(--vc-border-subtle)] bg-[var(--vc-bg)] px-3 py-3">
            <div className="text-[11px] uppercase tracking-wide text-[var(--vc-text-subtle)]">
              {t("subsession.childPrompt")}
            </div>
            <div className="mt-1 text-sm text-[var(--vc-text-primary)]">
              {childPrompt}
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}

function SubsessionTimelineHeader({
  childPrompt,
}: {
  childPrompt: string | null;
}) {
  const { t } = useTranslation();
  return (
    <div className="mx-auto max-w-[var(--vc-chat-content-width)] px-4 pt-4">
      <div className="text-[11px] uppercase tracking-wide text-[var(--vc-text-subtle)]">
        {t("subsession.timelineTitle")}
      </div>
      {childPrompt ? (
        <div className="mt-1 text-sm text-[var(--vc-text-muted)]">
          {childPrompt}
        </div>
      ) : null}
    </div>
  );
}

function SubsessionLiveStatus({
  taskStatus,
  childStatus,
  lifecycleStatus,
}: {
  taskStatus: string | null;
  childStatus: string | null;
  lifecycleStatus?: string | null;
}) {
  const { t } = useTranslation();
  const active =
    taskStatus === "queued" ||
    taskStatus === "running" ||
    childStatus === "running" ||
    lifecycleStatus === "running";
  if (!active) return null;

  return (
    <div className="mx-auto mt-4 max-w-[var(--vc-chat-content-width)] px-4">
      <div className="flex items-center gap-2 rounded-[var(--vc-radius-control)] border border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] px-3 py-2 text-sm text-[var(--vc-text-primary)]">
        <Loader2 className="h-4 w-4 animate-spin" />
        <span>{t("subsession.liveWorking")}</span>
      </div>
    </div>
  );
}

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
    commands,
    loadWorkspaces,
    switchWorkspace,
    loadProviders,
    validateProviderCredentials,
    loadAgents,
    loadSkills,
    loadCommands,
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
    childSessionParentId,
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
    backgroundTasks,
    selectedBackgroundTaskOutputId,
    backgroundTaskOutput,
    loadBackgroundTasks,
    loadBackgroundTaskOutput,
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
  const [isSessionSidebarExpanded, setIsSessionSidebarExpanded] =
    useState(true);
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
  const backgroundTasksById = useMemo(() => {
    const map: Record<string, (typeof backgroundTasks)[number]> = {};
    for (const task of backgroundTasks) {
      map[task.task.id] = task;
    }
    return map;
  }, [backgroundTasks]);
  const selectedBackgroundTaskOutputForChat = useMemo(() => {
    if (!selectedBackgroundTaskOutputId || !backgroundTaskOutput) return null;
    return {
      taskId: selectedBackgroundTaskOutputId,
      durationSeconds: backgroundTaskOutput.task.duration_seconds ?? null,
      toolCallCount: backgroundTaskOutput.task.tool_call_count ?? null,
    };
  }, [backgroundTaskOutput, selectedBackgroundTaskOutputId]);
  const selectedChildTaskSummary = useMemo(() => {
    if (!selectedBackgroundTaskOutputId) return null;
    return (
      backgroundTasks.find(
        (task) => task.task.id === selectedBackgroundTaskOutputId,
      ) ?? null
    );
  }, [backgroundTasks, selectedBackgroundTaskOutputId]);
  const childSessionTaskIds = useMemo(
    () =>
      backgroundTasks
        .filter((task) => task.session_id)
        .map((task) => task.task.id),
    [backgroundTasks],
  );
  const selectedChildTaskIndex = useMemo(() => {
    if (!selectedBackgroundTaskOutputId) return -1;
    return childSessionTaskIds.indexOf(selectedBackgroundTaskOutputId);
  }, [childSessionTaskIds, selectedBackgroundTaskOutputId]);

  useEffect(() => {
    i18n.changeLanguage(language);
  }, [language, i18n]);

  useEffect(() => {
    loadSessions();
  }, [loadSessions]);

  const returnToParentSession = useCallback(() => {
    if (childSessionParentId) {
      void selectSession(childSessionParentId);
      return;
    }
    void loadBackgroundTaskOutput(null);
  }, [childSessionParentId, loadBackgroundTaskOutput, selectSession]);

  // While browsing a delegated child session, use OpenCode-style Alt+arrow navigation.
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (!displayedIsChildSession) return;
      const target = event.target;
      if (
        target instanceof HTMLElement &&
        target.closest("textarea, input, [contenteditable='true']")
      ) {
        return;
      }
      if (event.altKey && event.key === "ArrowUp") {
        event.preventDefault();
        returnToParentSession();
        return;
      }
      if (event.altKey && event.key === "ArrowDown") {
        event.preventDefault();
        if (childSessionTaskIds.length > 0) {
          void loadBackgroundTaskOutput(childSessionTaskIds[0]);
        }
        return;
      }
      if (
        event.altKey &&
        event.key === "ArrowLeft" &&
        selectedChildTaskIndex > 0
      ) {
        event.preventDefault();
        void loadBackgroundTaskOutput(
          childSessionTaskIds[selectedChildTaskIndex - 1],
        );
        return;
      }
      if (
        event.altKey &&
        event.key === "ArrowRight" &&
        selectedChildTaskIndex >= 0 &&
        selectedChildTaskIndex < childSessionTaskIds.length - 1
      ) {
        event.preventDefault();
        void loadBackgroundTaskOutput(
          childSessionTaskIds[selectedChildTaskIndex + 1],
        );
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [
    childSessionTaskIds,
    displayedIsChildSession,
    loadBackgroundTaskOutput,
    returnToParentSession,
    selectedChildTaskIndex,
  ]);

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

  // Refresh selected background-task output every 2s while it's still running.
  useEffect(() => {
    if (!selectedBackgroundTaskOutputId) return;
    const selectedSummary = backgroundTasks.find(
      (task) => task.task.id === selectedBackgroundTaskOutputId,
    );
    if (!selectedSummary) return;
    if (
      selectedSummary.status !== "queued" &&
      selectedSummary.status !== "running"
    )
      return;
    const timer = window.setInterval(() => {
      void loadBackgroundTaskOutput(selectedBackgroundTaskOutputId);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [
    backgroundTasks,
    loadBackgroundTaskOutput,
    selectedBackgroundTaskOutputId,
  ]);

  useEffect(() => {
    void loadWorkspaces?.();
    void loadProviders?.();
    void loadAgents?.();
    void loadSkills?.();
    void loadCommands?.();
    void loadSettings?.();
    void loadStatus?.();
    void loadBackgroundTasks?.();
  }, [
    loadAgents,
    loadSkills,
    loadCommands,
    loadProviders,
    loadBackgroundTasks,
    loadSettings,
    loadStatus,
    loadWorkspaces,
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

  const handleSendMessage = async (
    message: string,
    options?: { skills?: string[] },
  ) => {
    await runTask(message, {
      metadata: options?.skills?.length
        ? { skills: options.skills }
        : undefined,
    });
  };

  const currentSessionSummary = useMemo(
    () => sessions.find((s) => s.session.id === currentSessionId),
    [sessions, currentSessionId],
  );
  const latestParentRequestPrompt = useMemo(() => {
    const latestReq = [...currentSessionEvents]
      .reverse()
      .find((e) => e.event_type === "runtime.request_received");
    return typeof latestReq?.payload?.prompt === "string"
      ? latestReq.payload.prompt
      : null;
  }, [currentSessionEvents]);
  const selectedChildContext = useMemo(() => {
    if (!displayedIsChildSession || !backgroundTaskOutput) return null;
    return {
      parentPrompt: currentSessionSummary?.prompt ?? latestParentRequestPrompt,
      childPrompt: backgroundTaskOutput.task.delegated_prompt ?? null,
      taskStatus:
        backgroundTaskOutput.task.status ??
        selectedChildTaskSummary?.status ??
        null,
      subagentType: backgroundTaskOutput.task.routing?.subagent_type ?? null,
      summaryOutput: backgroundTaskOutput.task.summary_output ?? null,
      approvalBlocked: backgroundTaskOutput.task.approval_blocked === true,
      questionRequestId: backgroundTaskOutput.task.question_request_id ?? null,
      approvalRequestId: backgroundTaskOutput.task.approval_request_id ?? null,
      hookReminder: backgroundTaskOutput.task.hook_reminder ?? null,
      childStatus: backgroundTaskOutput.session_result?.status ?? null,
      lifecycleStatus:
        typeof backgroundTaskOutput.task.delegation?.lifecycle_status ===
        "string"
          ? backgroundTaskOutput.task.delegation.lifecycle_status
          : null,
    };
  }, [
    backgroundTaskOutput,
    currentSessionSummary,
    displayedIsChildSession,
    latestParentRequestPrompt,
    selectedChildTaskSummary,
  ]);

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
  const isWorkspaceBootLoading =
    !hasCurrentWorkspace &&
    (workspacesStatus === "idle" || workspacesStatus === "loading");
  const currentBranch = statusSnapshot?.git.branch?.trim() || null;

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
                {displayedIsChildSession && (
                  <ControlButton
                    compact
                    variant="ghost"
                    onClick={returnToParentSession}
                    aria-label={t("childSessions.parent")}
                    title="Alt+Up"
                  >
                    <MoveLeft className="w-4 h-4" />
                    <span>{t("childSessions.parent")}</span>
                  </ControlButton>
                )}
                {currentSessionId ? (
                  <div className="flex flex-col min-w-0">
                    <span className="flex items-center gap-2 min-w-0">
                      <span className="text-sm font-medium text-[var(--vc-text-primary)] truncate">
                        {currentSessionTitle}
                      </span>
                      {currentBranch ? (
                        <span className="shrink-0 rounded-md border border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] px-1.5 py-0.5 font-mono text-[10px] text-[var(--vc-text-subtle)]">
                          {currentBranch}
                        </span>
                      ) : null}
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
              </div>
            </header>
            {isRunning && (
              <output
                aria-label={t("session.modelWorking")}
                className="relative h-0.5 flex-shrink-0 overflow-hidden bg-transparent"
              >
                <div className="vc-model-working-bar" />
              </output>
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

            {selectedChildContext ? (
              <>
                <SubsessionBanner {...selectedChildContext} />
                <SubsessionHandoffCard
                  parentPrompt={selectedChildContext.parentPrompt}
                  childPrompt={selectedChildContext.childPrompt}
                />
                <SubsessionLiveStatus
                  taskStatus={selectedChildContext.taskStatus}
                  childStatus={selectedChildContext.childStatus}
                  lifecycleStatus={selectedChildContext.lifecycleStatus}
                />
                <SubsessionTimelineHeader
                  childPrompt={selectedChildContext.childPrompt}
                />
              </>
            ) : null}

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
                  backgroundTasksById={backgroundTasksById}
                  selectedBackgroundTaskOutput={
                    selectedBackgroundTaskOutputForChat
                  }
                  onSelectSession={(sessionId) => {
                    void selectSession(sessionId);
                  }}
                />
              </div>
            </div>

            <TodoPanel snapshot={activeTodoSnapshot} />

            <Composer
              disabled={composerDisabled}
              isRunning={isRunning}
              agentPreset={agentPreset}
              sessionMetadata={currentSessionState?.metadata}
              onSubmit={handleSendMessage}
              onCancel={cancelCurrentRun}
              onAgentPresetChange={setAgentPreset}
              providerModel={providerModel}
              reasoningEffort={reasoningEffort}
              providers={providers}
              providerModels={providerModels}
              sessionContextUsage={composerContextUsage}
              agentPresets={agentPresets}
              commands={commands}
              onProviderModelChange={setProviderModel}
              onReasoningEffortChange={setReasoningEffort}
            />
          </>
        ) : isWorkspaceBootLoading ? (
          <div className="flex flex-1 items-center justify-center p-6">
            <div className="flex items-center gap-3 rounded-xl border border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] px-4 py-3 text-sm text-[var(--vc-text-muted)]">
              <Loader2 className="h-4 w-4 animate-spin" />
              {t("project.loading")}
            </div>
          </div>
        ) : (
          <div className="flex flex-1 flex-col relative">
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
