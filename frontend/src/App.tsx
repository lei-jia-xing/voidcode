import { useCallback, useEffect, useRef, useState, useMemo } from "react";
import { useTranslation } from "react-i18next";
import { useAppStore } from "./store";
import { useShallow } from "zustand/react/shallow";
import {
  asyncStatusFromQuery,
  backgroundTaskIdFromControlResponse,
  queryErrorMessage,
  refreshDelegatedTaskSurfaces,
  resolveProviderModelReference,
  resolveSelectedReviewPath,
  useAcknowledgeNotification,
  useAgentsQuery,
  useBackgroundTaskAction,
  useBackgroundTasksQuery,
  useCommandsQuery,
  useNotificationsQuery,
  useProviderCatalogQuery,
  useProviderValidation,
  useRetryMcpConnections,
  useReviewDiffQuery,
  useReviewQuery,
  useRuntimeStatusQuery,
  useSessionDebugQuery,
  useSessionsQuery,
  useSettingsQuery,
  useSwitchWorkspace,
  useTaskOutputQuery,
  useUpdateSettings,
  useWorkspacesQuery,
} from "./lib/queries";
import type { BackgroundTaskActionRequest } from "./lib/queries";
import { SessionSidebar } from "./components/SessionSidebar";
import { ChildSessionSidebar } from "./components/ChildSessionSidebar";
import { ChatThread } from "./components/ChatThread";
import { Composer, type SessionContextUsage } from "./components/Composer";
import { SettingsPanel } from "./components/SettingsPanel";
import { OpenProjectModal } from "./components/OpenProjectModal";
import { ReviewPanel } from "./components/ReviewPanel";
import { ContextPanel } from "./components/ContextPanel";
import { TodoPanel } from "./components/TodoPanel";
import { deriveLatestTodoSnapshot } from "./components/todoPanelModel";
import { ControlButton } from "./components/ui";
import { deriveChatMessages } from "./lib/runtime/event-parser";
import {
  providerContextTokens,
  providerTotalTokens,
  providerCacheHitRate,
} from "./lib/runtime/providerUsage";
import { RuntimeClient } from "./lib/runtime/client";
import {
  FileCodeCorner,
  FolderTree,
  GitCompare,
  LoaderCircle,
  MoveLeft,
} from "lucide-react";
import { StatusBar } from "./components/StatusBar";
import { buildSessionDisplayTitle } from "./components/sessionTitle";
import { errorMessage } from "./lib/errorMessage";

// Auto-follow the chat only when the user is within this many pixels of the
// bottom; scrolled-up readers are never yanked back down.
const SCROLL_FOLLOW_THRESHOLD_PX = 80;

// Keys that scroll the transcript on their own. Any of them means the reader is
// driving the viewport, which is what stops the follow (see the scroll-intent
// effect in App).
const SCROLL_FOLLOW_KEYS: Record<string, true> = {
  ArrowUp: true,
  ArrowDown: true,
  PageUp: true,
  PageDown: true,
  Home: true,
  End: true,
  " ": true,
};

// Pushed delegated-task frames are coalesced into a single background-task (and
// notification) refresh per window, instead of one request pair per frame.
const DELEGATED_REFRESH_INTERVAL_MS = 250;

function SubsessionTimelineHeader({
  childPrompt,
  onReturn,
}: {
  childPrompt: string | null;
  onReturn: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="mx-auto max-w-[var(--vc-chat-content-width)] px-4 pt-4">
      <div className="flex items-center justify-between gap-3">
        <div className="text-[11px] uppercase tracking-wide text-[var(--vc-text-subtle)]">
          {t("subsession.timelineTitle")}
        </div>
        <ControlButton
          compact
          variant="ghost"
          onClick={onReturn}
          title={t("common.altUp")}
        >
          <MoveLeft className="h-4 w-4" />
          <span>{t("subsession.backToParent")}</span>
        </ControlButton>
      </div>
      {childPrompt ? (
        <div className="mt-1 text-sm text-[var(--vc-text-muted)]">
          {childPrompt}
        </div>
      ) : null}
    </div>
  );
}

// Statuses after which the session transcript is static for display. The
// backend closes the session-event follow stream for {completed, failed,
// interrupted}; stopping on any terminal status here keeps the frontend
// resilient even if a future status (or an older backend) does not close the
// stream.
const TERMINAL_DISPLAY_SESSION_STATUSES: Record<string, true> = {
  completed: true,
  failed: true,
  interrupted: true,
};

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
        <LoaderCircle className="h-4 w-4 animate-spin" />
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
    sessionSidebarWidth,
    setSessionSidebarWidth,
    reviewSelectedPath,
    setReviewSelectedPath,
    selectedBackgroundTaskOutputId,
    selectBackgroundTaskOutput,
    currentSessionId,
    childSessionParentId,
    currentSessionEvents,
    currentSessionOutput,
    currentSessionState,
    selectSession,
    replayTargetSessionId,
    runTask,
    cancelCurrentRun,
    resolveApproval,
    replayStatus,
    replayError,
    runStatus,
    runOrigin,
    runError,
    approvalStatus,
    approvalError,
    questionStatus,
    questionError,
    answerQuestion,
    resumeSession,
    resumeStatus,
    resumeError,
  } = useAppStore(
    // The shell subscribes to client state and the streamed-run projection only:
    // every server payload is read from the query cache by the hooks below, so a
    // write to a store slice this shell does not paint (a skill catalog, the
    // review mode, the cancel flag) can never re-render the whole app, and
    // `useShallow` keeps the object identity stable while none of them changed.
    useShallow((state) => ({
      language: state.language,
      setLanguage: state.setLanguage,
      agentPreset: state.agentPreset,
      setAgentPreset: state.setAgentPreset,
      providerModel: state.providerModel,
      setProviderModel: state.setProviderModel,
      reasoningEffort: state.reasoningEffort,
      setReasoningEffort: state.setReasoningEffort,
      sessionSidebarWidth: state.sessionSidebarWidth,
      setSessionSidebarWidth: state.setSessionSidebarWidth,
      reviewSelectedPath: state.reviewSelectedPath,
      setReviewSelectedPath: state.setReviewSelectedPath,
      selectedBackgroundTaskOutputId: state.selectedBackgroundTaskOutputId,
      selectBackgroundTaskOutput: state.selectBackgroundTaskOutput,
      currentSessionId: state.currentSessionId,
      childSessionParentId: state.childSessionParentId,
      currentSessionEvents: state.currentSessionEvents,
      currentSessionOutput: state.currentSessionOutput,
      currentSessionState: state.currentSessionState,
      selectSession: state.selectSession,
      replayTargetSessionId: state.replayTargetSessionId,
      runTask: state.runTask,
      cancelCurrentRun: state.cancelCurrentRun,
      resolveApproval: state.resolveApproval,
      replayStatus: state.replayStatus,
      replayError: state.replayError,
      runStatus: state.runStatus,
      runOrigin: state.runOrigin,
      runError: state.runError,
      approvalStatus: state.approvalStatus,
      approvalError: state.approvalError,
      questionStatus: state.questionStatus,
      questionError: state.questionError,
      answerQuestion: state.answerQuestion,
      resumeSession: state.resumeSession,
      resumeStatus: state.resumeStatus,
      resumeError: state.resumeError,
    })),
  );
  const { t, i18n } = useTranslation();

  const [showSettings, setShowSettings] = useState(false);
  const [showProjects, setShowProjects] = useState(false);
  const [showFileTree, setShowFileTree] = useState(false);
  const [showCodeReview, setShowCodeReview] = useState(false);
  const [showContext, setShowContext] = useState(false);
  const [sessionEventError, setSessionEventError] = useState<string | null>(
    null,
  );

  // Server data. Every payload below is read from the query cache; the active
  // workspace path is the scope every other key is built from, so a switch
  // re-keys them all instead of overwriting entries. Statuses are derived from
  // the query state, so there is no second copy of them in the store.
  const workspacesQuery = useWorkspacesQuery();
  const workspaces = workspacesQuery.data ?? null;
  const workspacesStatus = asyncStatusFromQuery(workspacesQuery);
  const workspacesError = queryErrorMessage(workspacesQuery);
  const scope = workspaces?.current?.path ?? null;

  const prepareWorkspaceSwitch = useCallback(() => {
    useAppStore.getState().prepareWorkspaceSwitch();
  }, []);
  const {
    switchTo: switchWorkspaceTo,
    status: workspaceSwitchStatus,
    error: workspaceSwitchError,
  } = useSwitchWorkspace(prepareWorkspaceSwitch);

  const providerCatalogQuery = useProviderCatalogQuery(scope);
  // Memoized so the derived maps keep their identity across renders: they are
  // dependencies of the composer's own memos.
  const providers = useMemo(
    () => providerCatalogQuery.data?.providers ?? [],
    [providerCatalogQuery.data],
  );
  const providerModels = useMemo(
    () => providerCatalogQuery.data?.models ?? {},
    [providerCatalogQuery.data],
  );
  const providersStatus = asyncStatusFromQuery(providerCatalogQuery);
  const providersError = queryErrorMessage(providerCatalogQuery);
  const providerNames = useMemo(
    () => providers.map((provider) => provider.name),
    [providers],
  );
  const providerValidation = useProviderValidation(scope, providerNames);
  const resolvedProviderModel = resolveProviderModelReference(
    providerModel,
    providers,
    providerModels,
  );

  const agentsQuery = useAgentsQuery(scope);
  const agentPresets = agentsQuery.data ?? [];
  const commandsQuery = useCommandsQuery(scope);
  const commands = commandsQuery.data ?? [];

  const statusQuery = useRuntimeStatusQuery(scope);
  const statusSnapshot = statusQuery.data ?? null;
  const statusStatus = asyncStatusFromQuery(statusQuery);
  const statusError = queryErrorMessage(statusQuery);
  const {
    retry: retryMcpConnections,
    isPending: isMcpRetryPending,
    error: mcpRetryFailure,
  } = useRetryMcpConnections(scope);
  const mcpRetryStatus = isMcpRetryPending
    ? "loading"
    : mcpRetryFailure
      ? "error"
      : "idle";
  const mcpRetryError = mcpRetryFailure ? errorMessage(mcpRetryFailure) : null;

  const reviewQuery = useReviewQuery(scope);
  const reviewSnapshot = reviewQuery.data ?? null;
  const reviewStatus = asyncStatusFromQuery(reviewQuery);
  const reviewError = queryErrorMessage(reviewQuery);
  // The selection is client state; the path actually shown is derived, so a
  // review reload can never leave it pointing at a file that no longer changed.
  const effectiveReviewPath = useMemo(
    () => resolveSelectedReviewPath(reviewSnapshot, reviewSelectedPath),
    [reviewSnapshot, reviewSelectedPath],
  );
  const reviewDiffQuery = useReviewDiffQuery(scope, effectiveReviewPath);
  const reviewDiff = reviewDiffQuery.data ?? null;
  const reviewDiffStatus = asyncStatusFromQuery(reviewDiffQuery);
  const reviewDiffError = queryErrorMessage(reviewDiffQuery);

  const sessionsQuery = useSessionsQuery(scope);
  const sessions = useMemo(
    () => sessionsQuery.data ?? [],
    [sessionsQuery.data],
  );
  const sessionsStatus = asyncStatusFromQuery(sessionsQuery);
  const sessionsError = queryErrorMessage(sessionsQuery);

  const notificationsQuery = useNotificationsQuery(scope);
  const notifications = useMemo(
    () => notificationsQuery.data ?? [],
    [notificationsQuery.data],
  );
  const notificationsStatus = asyncStatusFromQuery(notificationsQuery);
  const notificationsError = queryErrorMessage(notificationsQuery);
  const { acknowledge: acknowledgeNotification, isPending: isAcking } =
    useAcknowledgeNotification(scope);
  const notificationsBusy = notificationsQuery.isFetching || isAcking;

  const settingsQuery = useSettingsQuery(scope);
  const settings = settingsQuery.data ?? null;
  const {
    save: saveSettings,
    status: saveSettingsStatus,
    error: saveSettingsFailure,
  } = useUpdateSettings(scope);
  const settingsStatus =
    saveSettingsStatus === "loading"
      ? "loading"
      : saveSettingsStatus === "error"
        ? "error"
        : asyncStatusFromQuery(settingsQuery);
  const settingsError =
    saveSettingsStatus === "error"
      ? errorMessage(saveSettingsFailure)
      : queryErrorMessage(settingsQuery);

  const sessionDebugQuery = useSessionDebugQuery(
    scope,
    showContext ? currentSessionId : null,
  );
  const sessionDebug = sessionDebugQuery.data ?? null;
  const sessionDebugStatus = asyncStatusFromQuery(sessionDebugQuery);
  const sessionDebugError = queryErrorMessage(sessionDebugQuery);

  // The task surface follows the session being browsed: a delegated child uses
  // its parent's task list, everything else this session's own (or the global
  // list when nothing is selected). The id is part of the query key, so a
  // selection change can never show the previous scope's tasks.
  const taskSessionScopeId = childSessionParentId ?? currentSessionId ?? null;
  const backgroundTasksQuery = useBackgroundTasksQuery(
    scope,
    taskSessionScopeId,
  );
  const backgroundTasks = useMemo(
    () => backgroundTasksQuery.data ?? [],
    [backgroundTasksQuery.data],
  );
  const backgroundTasksStatus = asyncStatusFromQuery(backgroundTasksQuery);
  const backgroundTasksError = queryErrorMessage(backgroundTasksQuery);
  const taskOutputQuery = useTaskOutputQuery(
    scope,
    selectedBackgroundTaskOutputId,
  );
  const backgroundTaskOutput = taskOutputQuery.data ?? null;
  const backgroundTaskOutputStatus = asyncStatusFromQuery(taskOutputQuery);
  const backgroundTaskOutputError = queryErrorMessage(taskOutputQuery);
  const {
    run: runBackgroundTaskActionRequest,
    pendingTaskId: backgroundTaskActionTaskId,
    status: backgroundTaskActionStatus,
    error: backgroundTaskActionError,
  } = useBackgroundTaskAction(scope);
  const hydratedInitialSessionRef = useRef(false);
  const chatScrollRef = useRef<HTMLDivElement>(null);
  const lastMessageSignatureRef = useRef("");
  const scrollFollowFrameRef = useRef<number | null>(null);
  // Whether the reader is still following the tail of the transcript. It is
  // seeded "following" and only a reader gesture clears it: re-deriving intent
  // from the post-commit gap let one trip (a short viewport plus a fast stream)
  // stick for the rest of the run, because nothing ever pulled it back under
  // the threshold.
  const scrollFollowRef = useRef(true);
  // The scrollTop this follow last wrote (how its own scroll events are told
  // apart from the reader's) and the reader's previous position (how the
  // direction of their scroll is told).
  const scrollFollowPinnedTopRef = useRef<number | null>(null);
  const scrollFollowReaderTopRef = useRef<number | null>(null);
  const sessionEventCursorRef = useRef(0);
  // Mirrors selectedBackgroundTaskOutputId for the follow-stream effect. The
  // effect must NOT depend on that state directly: its own body refreshes the
  // selected child output, which would mutate the dependency and tear the
  // stream down/re-establish it in a cycle.
  const selectedBackgroundTaskOutputIdRef = useRef(
    selectedBackgroundTaskOutputId,
  );
  useEffect(() => {
    selectedBackgroundTaskOutputIdRef.current = selectedBackgroundTaskOutputId;
  }, [selectedBackgroundTaskOutputId]);

  // Mirrors currentSessionState.status for the follow-stream effect. The effect
  // must NOT depend on that status directly: it applies the session row the
  // stream pushes, which would change the dependency and abort the stream
  // before its remaining replayed frames were consumed.
  const currentSessionStatusRef = useRef(currentSessionState?.status);
  useEffect(() => {
    currentSessionStatusRef.current = currentSessionState?.status;
  }, [currentSessionState?.status]);

  const isRunning =
    runStatus === "running" ||
    runStatus === "cancelling" ||
    currentSessionState?.status === "running";
  const isReplayLoading = replayStatus === "loading";
  const isApprovalSubmitting = approvalStatus === "submitting";
  const isWaitingApproval = currentSessionState?.status === "waiting";
  const isQuestionSubmitting = questionStatus === "submitting";
  const latestWaitingEvent = useMemo(() => {
    for (let index = currentSessionEvents.length - 1; index >= 0; index -= 1) {
      const event = currentSessionEvents[index];
      if (
        event.event_type === "runtime.approval_requested" ||
        event.event_type === "runtime.question_requested"
      ) {
        return event;
      }
    }
    return undefined;
  }, [currentSessionEvents]);
  const pendingNotifications = useMemo(
    () =>
      notifications.filter((notification) => notification.status === "unread"),
    [notifications],
  );
  const currentSessionResumable =
    currentSessionState?.status === "interrupted" ||
    (currentSessionState?.status === "failed" &&
      sessionDebug?.resumable === true);
  const isResumeLoading = resumeStatus === "loading";
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
      // The document types an optional field as possibly absent; the resolved
      // child output falls back to the task's own output when unset.
      childResult.output ?? backgroundTaskOutput.output ?? null,
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
    sessionEventCursorRef.current =
      currentSessionEvents[currentSessionEvents.length - 1]?.sequence ?? 0;
  }, [currentSessionEvents]);

  useEffect(() => {
    i18n.changeLanguage(language);
  }, [language, i18n]);

  const returnToParentSession = useCallback(() => {
    if (childSessionParentId) {
      void selectSession(childSessionParentId, scope);
      return;
    }
    selectBackgroundTaskOutput(null);
  }, [childSessionParentId, scope, selectBackgroundTaskOutput, selectSession]);

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
          selectBackgroundTaskOutput(childSessionTaskIds[0]);
        }
        return;
      }
      if (
        event.altKey &&
        event.key === "ArrowLeft" &&
        selectedChildTaskIndex > 0
      ) {
        event.preventDefault();
        selectBackgroundTaskOutput(
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
        selectBackgroundTaskOutput(
          childSessionTaskIds[selectedChildTaskIndex + 1],
        );
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [
    childSessionTaskIds,
    displayedIsChildSession,
    returnToParentSession,
    scope,
    selectBackgroundTaskOutput,
    selectedChildTaskIndex,
  ]);

  useEffect(() => {
    // If this session is already terminal-for-display (e.g. a run we just
    // completed locally already populated terminal data), there is nothing to
    // follow. Opening a follow stream here would immediately receive the
    // terminal snapshot and then re-select the session, causing a redundant
    // full reload right after every completed run.
    const sessionStatus = currentSessionStatusRef.current;
    if (
      !currentSessionId ||
      replayStatus === "loading" ||
      (runStatus === "running" && runOrigin !== "external") ||
      runStatus === "cancelling" ||
      (sessionStatus != null &&
        TERMINAL_DISPLAY_SESSION_STATUSES[sessionStatus] === true)
    ) {
      return;
    }
    const controller = new AbortController();
    const afterSequence = sessionEventCursorRef.current;
    let appliedEvents = 0;
    let appliedSessionState = false;
    let refreshTimer: number | undefined;
    let notificationsDirty = false;
    let lastDelegatedRefreshAt = 0;

    // Delegated task/notification state is refreshed once per window no matter
    // how many background-task frames the run pushes (a delegated child emits
    // progress continuously). The refresh is fire-and-forget and never awaited
    // inside the SSE loop, so a slow request cannot stall event delivery; the
    // child output is allowed to lag by this window.
    const scheduleDelegatedRefresh = (notifications: boolean) => {
      notificationsDirty = notificationsDirty || notifications;
      if (controller.signal.aborted) return;
      window.clearTimeout(refreshTimer);
      refreshTimer = window.setTimeout(
        () => {
          lastDelegatedRefreshAt = Date.now();
          if (controller.signal.aborted) return;
          const refreshNotifications = notificationsDirty;
          notificationsDirty = false;
          void refreshDelegatedTaskSurfaces(
            {
              outputId: selectedBackgroundTaskOutputIdRef.current,
              notifications: refreshNotifications,
            },
            scope,
          );
        },
        Math.max(
          0,
          lastDelegatedRefreshAt + DELEGATED_REFRESH_INTERVAL_MS - Date.now(),
        ),
      );
    };

    void (async () => {
      try {
        for await (const chunk of RuntimeClient.sessionEvents(
          currentSessionId,
          afterSequence,
          controller.signal,
        )) {
          if (controller.signal.aborted) return;
          // The frames are the delivery path: apply them in arrival order, and
          // let the store reject anything the initial replay already holds.
          if (chunk.session !== null) {
            appliedSessionState =
              useAppStore.getState().mergeSessionState(chunk.session) ||
              appliedSessionState;
          }
          if (chunk.event !== null) {
            if (useAppStore.getState().mergeSessionEvent(chunk.event)) {
              appliedEvents += 1;
            }
            if (chunk.event.event_type.startsWith("runtime.background_task_")) {
              scheduleDelegatedRefresh(
                chunk.event.event_type ===
                  "runtime.background_task_notification_enqueued",
              );
            }
          }
        }
        if (
          controller.signal.aborted ||
          useAppStore.getState().currentSessionId !== currentSessionId
        )
          return;
        const outputId = selectedBackgroundTaskOutputIdRef.current;
        if (outputId) {
          // The user is browsing a delegated child session: refresh its output
          // in place. Re-selecting the underlying session here would clear the
          // child view state and flip the transcript back to the parent.
          await refreshDelegatedTaskSurfaces({ outputId }, scope);
          return;
        }
        if (useAppStore.getState().replayStatus === "loading") {
          // A selectSession for this session is already in flight (its fetch
          // has not resolved yet). Re-selecting now would clear the view and
          // discard the in-flight result, flashing the transcript.
          return;
        }
        // The stream closing is how the runtime reports that the session no
        // longer needs a follower: it closes on a terminal state, or the
        // transport dropped. Either way the replay is the authoritative
        // transcript/output, so reconcile exactly once — unless the frames
        // already told us everything and the session is terminal, in which case
        // a reload would only repeat what is on screen.
        const storeStatus = useAppStore.getState().currentSessionState?.status;
        const sessionMayStillBeLive =
          storeStatus === undefined ||
          TERMINAL_DISPLAY_SESSION_STATUSES[storeStatus] !== true;
        if (appliedEvents > 0 || appliedSessionState || sessionMayStillBeLive) {
          await selectSession(currentSessionId, scope);
        }
      } catch (error) {
        if (!controller.signal.aborted) {
          setSessionEventError(errorMessage(error));
        }
      }
    })();
    return () => {
      controller.abort();
      window.clearTimeout(refreshTimer);
    };
  }, [
    currentSessionId,
    runOrigin,
    runStatus,
    replayStatus,
    scope,
    selectSession,
  ]);

  // The catalogs and settings adjust client state the store owns: which agent
  // preset is still selectable, and the runtime's default model while the user
  // has not chosen one. The store stays their single owner; the loaded payloads
  // only inform it.
  useEffect(() => {
    if (agentsQuery.data) {
      useAppStore.getState().reconcileAgentPreset(agentsQuery.data);
    }
  }, [agentsQuery.data]);

  useEffect(() => {
    useAppStore.getState().hydrateModelFromSettings(settings?.model);
  }, [settings?.model]);

  // The authoritative session list decides whether a selection with a resolved
  // transcript behind it still exists. That is lifecycle (it clears the shell's
  // own selection), so it stays in the store; the list itself is server data.
  useEffect(() => {
    if (sessionsQuery.data) {
      useAppStore.getState().reconcileSessionList(sessionsQuery.data);
    }
  }, [sessionsQuery.data]);

  useEffect(() => {
    if (hydratedInitialSessionRef.current || sessionsStatus !== "success") {
      return;
    }
    hydratedInitialSessionRef.current = true;
    if (!currentSessionId || isRunning) {
      return;
    }
    void selectSession(currentSessionId, scope);
  }, [currentSessionId, isRunning, scope, selectSession, sessionsStatus]);

  useEffect(() => {
    // A reader gesture hands them the viewport: stop following straight away so
    // the next content frame cannot yank the scroll back. Coming back to the
    // tail re-engages the follow, decided by the direction of the scroll rather
    // than by a single re-check — one wheel gesture arrives as many scroll
    // events, and a mid-gesture step is still close to the bottom it left.
    const handleReaderGesture = (event: Event) => {
      const scroller = chatScrollRef.current;
      if (
        !scroller ||
        !(event.target instanceof Node) ||
        !scroller.contains(event.target)
      ) {
        return;
      }
      scrollFollowRef.current = false;
    };

    const handleScrollKeyDown = (event: KeyboardEvent) => {
      if (SCROLL_FOLLOW_KEYS[event.key] !== true) return;
      handleReaderGesture(event);
    };

    // Our own pins also produce scroll events; the position this follow pinned
    // tells them apart, so a gap that only opened because content arrived can
    // never read as "the reader scrolled away" and abandon the follow.
    const handleReaderScroll = (event: Event) => {
      const scroller = chatScrollRef.current;
      if (!scroller || event.target !== scroller) return;
      const top = scroller.scrollTop;
      if (top === scrollFollowPinnedTopRef.current) return;
      // The reader owns the viewport from here; a later pin re-records it.
      scrollFollowPinnedTopRef.current = null;
      const previousTop = scrollFollowReaderTopRef.current;
      scrollFollowReaderTopRef.current = top;
      const gap = scroller.scrollHeight - top - scroller.clientHeight;
      if (previousTop !== null && top < previousTop) {
        // Moving away from the tail, however little: the follow gives way
        // instead of pulling the reader back down.
        scrollFollowRef.current = false;
        return;
      }
      if (gap < SCROLL_FOLLOW_THRESHOLD_PX) {
        // Back at the tail: follow again, from the tail.
        scrollFollowRef.current = true;
        scroller.scrollTop = scroller.scrollHeight;
        scrollFollowPinnedTopRef.current = scroller.scrollTop;
      }
    };

    window.addEventListener("wheel", handleReaderGesture, { passive: true });
    window.addEventListener("touchmove", handleReaderGesture, {
      passive: true,
    });
    window.addEventListener("keydown", handleScrollKeyDown);
    // Scroll events do not bubble, so listen on the capture phase.
    document.addEventListener("scroll", handleReaderScroll, {
      capture: true,
      passive: true,
    });
    return () => {
      window.removeEventListener("wheel", handleReaderGesture);
      window.removeEventListener("touchmove", handleReaderGesture);
      window.removeEventListener("keydown", handleScrollKeyDown);
      document.removeEventListener("scroll", handleReaderScroll, {
        capture: true,
      });
    };
  }, []);

  // Switching transcripts starts following the new tail again; inside one
  // transcript only a reader gesture can stop the follow.
  useEffect(() => {
    scrollFollowRef.current = true;
  }, [currentSessionId]);

  useEffect(() => {
    // Only the tail of the transcript grows while a run streams, so a
    // last-message signature is enough to know that the scroller needs a look —
    // without rebuilding an O(messages) string on every streamed frame.
    const last = displayedMessages[displayedMessages.length - 1];
    const signature = last ? `${last.id}:${last.content.length}` : "";
    if (signature === lastMessageSignatureRef.current) return;
    lastMessageSignatureRef.current = signature;
    if (scrollFollowFrameRef.current !== null) return;
    // Writing scrollTop forces a layout; coalesce every frame of a fast delta
    // stream into a single write on the next frame. The follow intent is read at
    // frame time, so a gesture that lands in between wins.
    scrollFollowFrameRef.current = window.requestAnimationFrame(() => {
      scrollFollowFrameRef.current = null;
      const el = chatScrollRef.current;
      if (el && scrollFollowRef.current) {
        el.scrollTop = el.scrollHeight;
        scrollFollowPinnedTopRef.current = el.scrollTop;
      }
    });
  }, [displayedMessages]);

  useEffect(() => {
    return () => {
      if (scrollFollowFrameRef.current !== null) {
        window.cancelAnimationFrame(scrollFollowFrameRef.current);
      }
    };
  }, []);

  // Stable identities: the memoized Composer and the side panels compare props
  // shallowly, so a callback rebuilt on every render would defeat them.
  const handleSendMessage = useCallback(
    async (message: string, options?: { skills?: string[] }) => {
      await runTask(message, scope, {
        metadata: options?.skills?.length
          ? { skills: options.skills }
          : undefined,
      });
    },
    [runTask, scope],
  );

  const handleSteer = useCallback(
    async (content: string) => {
      if (!currentSessionId) {
        throw new Error(t("chat.steerNoSession"));
      }
      return RuntimeClient.steerSession(currentSessionId, content);
    },
    [currentSessionId, t],
  );

  const currentSessionSummary = useMemo(
    () => sessions.find((s) => s.session.id === currentSessionId),
    [sessions, currentSessionId],
  );
  const selectedChildContext = useMemo(() => {
    if (!displayedIsChildSession || !backgroundTaskOutput) return null;
    return {
      childPrompt: backgroundTaskOutput.task.delegated_prompt ?? null,
      taskStatus:
        backgroundTaskOutput.task.status ??
        selectedChildTaskSummary?.status ??
        null,
      childStatus: backgroundTaskOutput.session_result?.status ?? null,
      lifecycleStatus:
        typeof backgroundTaskOutput.task.delegation?.lifecycle_status ===
        "string"
          ? backgroundTaskOutput.task.delegation.lifecycle_status
          : null,
    };
  }, [backgroundTaskOutput, displayedIsChildSession, selectedChildTaskSummary]);

  const runBackgroundTaskAction = useCallback(
    async (request: BackgroundTaskActionRequest) => {
      try {
        const result = await runBackgroundTaskActionRequest(request);
        const answeredTaskId =
          backgroundTaskIdFromControlResponse(result) ?? request.taskId;
        if (
          selectedBackgroundTaskOutputIdRef.current === request.taskId &&
          answeredTaskId !== request.taskId
        ) {
          // A retry answers with the task it created; follow it.
          selectBackgroundTaskOutput(answeredTaskId);
        }
      } catch {
        // The action's own state carries the failure to the task panel.
      }
    },
    [runBackgroundTaskActionRequest, selectBackgroundTaskOutput],
  );
  const currentSessionTitle = useMemo(() => {
    if (!currentSessionId) return null;
    if (currentSessionSummary?.prompt) {
      return buildSessionDisplayTitle(
        currentSessionSummary.prompt,
        currentSessionId,
      );
    }
    let latestPrompt: string | undefined;
    for (let index = currentSessionEvents.length - 1; index >= 0; index -= 1) {
      const event = currentSessionEvents[index];
      if (event.event_type !== "runtime.request_received") continue;
      if (typeof event.payload?.prompt === "string") {
        latestPrompt = event.payload.prompt;
      }
      break;
    }
    return buildSessionDisplayTitle(latestPrompt, currentSessionId);
  }, [currentSessionId, currentSessionSummary, currentSessionEvents]);

  const handleResolveApproval = useCallback(
    (decision: "allow" | "deny") => {
      void resolveApproval(decision);
    },
    [resolveApproval],
  );
  // Stable identity so the memoized ChatThread's shallow props comparison can
  // skip re-renders when nothing chat-related changed.
  const handleSelectSession = useCallback(
    (sessionId: string) => {
      void selectSession(sessionId, scope);
    },
    [scope, selectSession],
  );
  const handleFileTreePathSelect = useCallback(
    (path: string) => {
      setReviewSelectedPath(path);
      setShowCodeReview(true);
    },
    [setReviewSelectedPath],
  );
  const handleSelectBackgroundTaskOutput = useCallback(
    (taskId: string) => {
      selectBackgroundTaskOutput(taskId);
    },
    [selectBackgroundTaskOutput],
  );
  // `refetch` is stable across renders, so these handlers keep their identity as
  // props of memoized panels.
  const { refetch: refetchBackgroundTasks } = backgroundTasksQuery;
  const handleRefreshBackgroundTasks = useCallback(() => {
    void refetchBackgroundTasks();
  }, [refetchBackgroundTasks]);
  const { refetch: refetchSessions } = sessionsQuery;
  const handleRefreshSessions = useCallback(async () => {
    await refetchSessions();
  }, [refetchSessions]);
  const { refetch: refetchReview } = reviewQuery;
  const handleRefreshReview = useCallback(() => {
    void refetchReview();
  }, [refetchReview]);
  const { refetch: refetchSettings } = settingsQuery;
  const handleRefreshSettings = useCallback(() => {
    void refetchSettings();
  }, [refetchSettings]);
  const { refetch: refetchProviderCatalog } = providerCatalogQuery;
  const handleRefreshProviders = useCallback(() => {
    void refetchProviderCatalog();
  }, [refetchProviderCatalog]);
  const handleSwitchWorkspace = useCallback(
    (path: string) => switchWorkspaceTo(path),
    [switchWorkspaceTo],
  );
  const handleRetryMcpConnections = useCallback(() => {
    retryMcpConnections();
  }, [retryMcpConnections]);
  // Panel open/close handlers are props of memoized children, so they must keep
  // their identity across the stream: an inline arrow would be a new prop on
  // every streamed frame and re-render the panel it points at.
  const handleOpenProjects = useCallback(() => setShowProjects(true), []);
  const handleCloseProjects = useCallback(() => setShowProjects(false), []);
  const handleOpenSettings = useCallback(() => setShowSettings(true), []);
  const handleCloseSettings = useCallback(() => setShowSettings(false), []);
  const handleCloseFileTree = useCallback(() => setShowFileTree(false), []);
  const handleCloseCodeReview = useCallback(() => setShowCodeReview(false), []);
  const handleCloseContext = useCallback(() => setShowContext(false), []);
  const { refetch: refetchSessionDebug } = sessionDebugQuery;
  const handleRefreshContext = useCallback(() => {
    void refetchSessionDebug();
  }, [refetchSessionDebug]);
  const handleToggleLanguage = useCallback(
    () => setLanguage(language === "en" ? "zh-CN" : "en"),
    [language, setLanguage],
  );
  const cancelBackgroundTask = useCallback(
    (taskId: string) => runBackgroundTaskAction({ kind: "cancel", taskId }),
    [runBackgroundTaskAction],
  );
  const retryBackgroundTask = useCallback(
    (taskId: string) => runBackgroundTaskAction({ kind: "retry", taskId }),
    [runBackgroundTaskAction],
  );
  const steerBackgroundTask = useCallback(
    (taskId: string, prompt: string) =>
      runBackgroundTaskAction({ kind: "steer", taskId, prompt }),
    [runBackgroundTaskAction],
  );
  const composerDisabled =
    isReplayLoading ||
    isWaitingApproval ||
    isApprovalSubmitting ||
    isQuestionSubmitting;
  const hasCurrentWorkspace = Boolean(workspaces?.current);
  const isWorkspaceBootLoading =
    !hasCurrentWorkspace &&
    (workspacesStatus === "idle" || workspacesStatus === "loading");
  const currentBranch = statusSnapshot?.git?.branch?.trim() || null;
  const workspaceLoadError =
    !hasCurrentWorkspace && workspacesStatus === "error"
      ? workspacesError
      : null;
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
        onSidebarWidthChange={setSessionSidebarWidth}
        onSelectSession={handleSelectSession}
        onOpenProjects={handleOpenProjects}
        onOpenSettings={handleOpenSettings}
        onRefreshSessions={handleRefreshSessions}
      />

      <div className="flex-1 flex flex-col min-w-0">
        {hasCurrentWorkspace ? (
          <>
            <header className="relative z-20 h-14 flex items-center justify-between px-4 border-b border-[color:var(--vc-border-subtle)] bg-[var(--vc-bg)] shrink-0">
              <div className="flex items-center gap-2 min-w-0">
                {isReplayLoading && (
                  <LoaderCircle className="w-4 h-4 animate-spin text-[var(--vc-text-muted)] shrink-0" />
                )}
                {displayedIsChildSession && (
                  <ControlButton
                    compact
                    variant="ghost"
                    onClick={returnToParentSession}
                    aria-label={t("childSessions.parent")}
                    title={t("common.altUp")}
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

              <div className="flex items-center gap-2 shrink-0">
                <StatusBar
                  snapshot={statusSnapshot}
                  status={statusStatus}
                  error={statusError}
                  mcpRetryStatus={mcpRetryStatus}
                  mcpRetryError={mcpRetryError}
                  onRetryMcp={handleRetryMcpConnections}
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
                  variant={showContext ? "secondary" : "ghost"}
                  onClick={() => setShowContext((value) => !value)}
                  aria-label={t("context.title")}
                  aria-expanded={showContext}
                  aria-pressed={showContext}
                >
                  <FileCodeCorner className="w-4 h-4" />
                  <span>{t("context.title")}</span>
                </ControlButton>
              </div>
            </header>
            {isRunning && (
              <output
                aria-label={t("session.modelWorking")}
                className="relative h-0.5 shrink-0 overflow-hidden bg-transparent"
              >
                <div className="vc-model-working-bar" />
              </output>
            )}
            {(notificationsStatus !== "idle" ||
              pendingNotifications.length > 0) && (
              <section
                aria-label={t("runtimeOps.notifications")}
                className="shrink-0 border-b border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] px-4 py-2"
              >
                <div className="mx-auto flex max-w-[var(--vc-chat-content-width)] flex-col gap-2 text-xs">
                  {notificationsStatus === "loading" ? (
                    <p className="text-[var(--vc-text-muted)]">
                      {t("runtimeOps.loading")}
                    </p>
                  ) : null}
                  {notificationsError ? (
                    <p className="text-[var(--vc-danger-text)]">
                      {notificationsError}
                    </p>
                  ) : null}
                  {notificationsStatus === "success" &&
                  pendingNotifications.length === 0 ? (
                    <p className="text-[var(--vc-text-muted)]">
                      {t("runtimeOps.noNotifications")}
                    </p>
                  ) : null}
                  {pendingNotifications.map((notification) => (
                    <div
                      key={notification.id}
                      className="flex flex-wrap items-center justify-between gap-3"
                    >
                      <button
                        type="button"
                        className="min-w-0 flex-1 text-left text-[var(--vc-text-primary)] hover:underline"
                        onClick={() =>
                          void selectSession(notification.session.id, scope)
                        }
                      >
                        <span className="mr-2 rounded bg-[var(--vc-surface-2)] px-1.5 py-0.5 font-mono text-[10px] uppercase text-[var(--vc-text-subtle)]">
                          {notification.kind}
                        </span>
                        {notification.summary}
                      </button>
                      <ControlButton
                        compact
                        variant="ghost"
                        onClick={() => {
                          acknowledgeNotification(notification.id);
                        }}
                        disabled={notificationsBusy}
                      >
                        {t("runtimeOps.acknowledge")}
                      </ControlButton>
                    </div>
                  ))}
                </div>
              </section>
            )}
            {currentSessionResumable && !displayedIsChildSession ? (
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] px-4 py-2 text-xs">
                <span className="text-[var(--vc-text-muted)]">
                  {t("session.resumePrompt")}
                </span>
                <ControlButton
                  compact
                  variant="secondary"
                  disabled={isResumeLoading}
                  onClick={() => {
                    void resumeSession(currentSessionId);
                  }}
                >
                  {isResumeLoading
                    ? t("session.resuming")
                    : t("session.resume")}
                </ControlButton>
              </div>
            ) : null}
            {resumeError ? (
              <div className="shrink-0 border-b border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] px-4 py-2 text-xs text-[var(--vc-danger-text)]">
                {t("session.resumeError", { message: resumeError })}
              </div>
            ) : null}

            {replayError && (
              <div className="flex flex-wrap items-center justify-between gap-3 shrink-0 bg-[var(--vc-surface-1)] border-b border-[color:var(--vc-border-subtle)] px-4 py-2 text-xs text-[var(--vc-danger-text)]">
                <span>
                  {t("session.replayError", { message: replayError })}
                </span>
                <ControlButton
                  compact
                  variant="secondary"
                  onClick={() => {
                    if (replayTargetSessionId)
                      void selectSession(replayTargetSessionId, scope);
                  }}
                >
                  {t("common.retry")}
                </ControlButton>
              </div>
            )}
            {sessionEventError && (
              <div className="flex flex-wrap items-center justify-between gap-3 shrink-0 bg-[var(--vc-surface-1)] border-b border-[color:var(--vc-border-subtle)] px-4 py-2 text-xs text-[var(--vc-danger-text)]">
                <span>
                  {t("session.eventsFollowError", {
                    message: sessionEventError,
                  })}
                </span>
                <ControlButton
                  compact
                  variant="secondary"
                  onClick={() => setSessionEventError(null)}
                >
                  {t("common.dismiss")}
                </ControlButton>
              </div>
            )}
            {runError && (
              <div className="shrink-0 bg-[var(--vc-surface-1)] border-b border-[color:var(--vc-border-subtle)] px-4 py-2 text-xs text-[var(--vc-danger-text)]">
                {t("common.errorWithMessage", { message: runError })}
              </div>
            )}
            {selectedChildContext ? (
              <>
                <SubsessionLiveStatus
                  taskStatus={selectedChildContext.taskStatus}
                  childStatus={selectedChildContext.childStatus}
                  lifecycleStatus={selectedChildContext.lifecycleStatus}
                />
                <SubsessionTimelineHeader
                  childPrompt={selectedChildContext.childPrompt}
                  onReturn={returnToParentSession}
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
                  onSelectSession={handleSelectSession}
                />
              </div>
              <ChildSessionSidebar
                parentSessionId={childSessionParentId ?? currentSessionId}
                tasks={backgroundTasks}
                status={backgroundTasksStatus}
                error={backgroundTasksError}
                selectedTaskId={selectedBackgroundTaskOutputId}
                taskOutput={backgroundTaskOutput}
                taskOutputStatus={backgroundTaskOutputStatus}
                taskOutputError={backgroundTaskOutputError}
                onSelectParent={returnToParentSession}
                onSelectTask={handleSelectBackgroundTaskOutput}
                onRefresh={handleRefreshBackgroundTasks}
                onCancelTask={cancelBackgroundTask}
                onRetryTask={retryBackgroundTask}
                onSteerTask={steerBackgroundTask}
                actionTaskId={backgroundTaskActionTaskId}
                actionStatus={backgroundTaskActionStatus}
                actionError={backgroundTaskActionError}
              />
            </div>

            <TodoPanel snapshot={activeTodoSnapshot} />

            <Composer
              key={`${workspaces?.current?.path ?? "no-workspace"}:${currentSessionId ?? "new"}`}
              disabled={composerDisabled}
              isRunning={isRunning}
              agentPreset={agentPreset}
              onSubmit={handleSendMessage}
              onSteer={handleSteer}
              onCancel={cancelCurrentRun}
              onAgentPresetChange={setAgentPreset}
              providerModel={resolvedProviderModel}
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
        ) : workspaceLoadError ? (
          <div className="flex flex-1 items-center justify-center p-6">
            <div className="w-full max-w-md rounded-2xl border border-[color:var(--vc-danger-border)] bg-[var(--vc-surface-1)] p-6 text-center">
              <div className="text-lg font-semibold text-[var(--vc-text-primary)]">
                {t("project.loadFailedTitle")}
              </div>
              <p className="mt-2 text-sm text-[var(--vc-danger-text)]">
                {workspaceLoadError}
              </p>
              <ControlButton
                variant="primary"
                onClick={() => void workspacesQuery.refetch()}
                className="mt-5"
              >
                {t("common.retry")}
              </ControlButton>
            </div>
          </div>
        ) : isWorkspaceBootLoading ? (
          <div className="flex flex-1 items-center justify-center p-6">
            <div className="flex items-center gap-3 rounded-xl border border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] px-4 py-3 text-sm text-[var(--vc-text-muted)]">
              <LoaderCircle className="h-4 w-4 animate-spin" />
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
        selectedPath={effectiveReviewPath}
        diff={reviewDiff}
        diffStatus={reviewDiffStatus}
        diffError={reviewDiffError}
        onClose={handleCloseFileTree}
        onRefresh={handleRefreshReview}
        onSelectPath={handleFileTreePathSelect}
      />

      <ReviewPanel
        isOpen={showCodeReview}
        surface="code-review"
        snapshot={reviewSnapshot}
        status={reviewStatus}
        error={reviewError}
        selectedPath={effectiveReviewPath}
        diff={reviewDiff}
        diffStatus={reviewDiffStatus}
        diffError={reviewDiffError}
        onClose={handleCloseCodeReview}
        onRefresh={handleRefreshReview}
        onSelectPath={handleFileTreePathSelect}
      />

      <ContextPanel
        isOpen={showContext}
        debug={sessionDebug}
        status={sessionDebugStatus}
        error={sessionDebugError}
        onClose={handleCloseContext}
        onRefresh={handleRefreshContext}
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
        providerValidationResults={providerValidation.results}
        providerValidationStatus={providerValidation.status}
        providerValidationError={providerValidation.error}
        language={language}
        onToggleLanguage={handleToggleLanguage}
        onClose={handleCloseSettings}
        onLoad={handleRefreshSettings}
        onLoadProviders={handleRefreshProviders}
        onValidateProvider={providerValidation.validate}
        onSave={saveSettings}
      />

      <OpenProjectModal
        isOpen={showProjects}
        onClose={handleCloseProjects}
        recentWorkspaces={workspaces?.recent ?? []}
        candidateWorkspaces={workspaces?.candidates ?? []}
        workspacesStatus={workspacesStatus}
        workspacesError={workspacesError}
        workspaceSwitchStatus={workspaceSwitchStatus}
        workspaceSwitchError={workspaceSwitchError}
        currentWorkspacePath={workspaces?.current?.path ?? null}
        onSwitchWorkspace={handleSwitchWorkspace}
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
  return {
    usedTokens: providerTokens,
    totalTokens: providerTotalTokens(metadata),
    cacheHitRate: providerCacheHitRate(metadata),
    contextWindow: selectedModelContextWindow(providerModel, providerModels),
  };
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

export default App;
