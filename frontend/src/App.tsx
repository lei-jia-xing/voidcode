import { useEffect, useRef, useState, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { useAppStore } from './store';
import { Activity, Play, Settings, Code2, LayoutDashboard, CheckCircle2, Circle, Clock, Globe, AlertCircle, PauseCircle } from 'lucide-react';
import { RuntimeDebug } from './components/RuntimeDebug';
import { deriveTasksFromEvents, deriveActivitiesFromEvents } from './lib/runtime/event-parser';
import { EventEnvelope } from './lib/runtime/types';

function formatSessionUpdatedAt(updatedAt: number, now = Date.now()): string {
  const timestamp = updatedAt < 1_000_000_000_000 ? updatedAt * 1000 : updatedAt;
  const diffMs = Math.max(0, now - timestamp);
  const diffMinutes = Math.floor(diffMs / 60_000);
  const diffHours = Math.floor(diffMs / 3_600_000);
  const diffDays = Math.floor(diffMs / 86_400_000);

  if (diffMinutes < 1) {
    return 'just-now';
  }
  if (diffMinutes < 60) {
    return `minutes:${diffMinutes}`;
  }
  if (diffHours < 24) {
    return `hours:${diffHours}`;
  }
  return `days:${Math.max(1, diffDays)}`;
}

function getPendingApprovalEvent(events: EventEnvelope[]): EventEnvelope | null {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event.event_type === 'runtime.approval_requested') {
      return event;
    }
  }

  return null;
}

function App() {
  const {
    language, setLanguage,
    agentPreset, leaderMode, providerModel,
    setLeaderMode, setProviderModel,
    sessions, currentSessionId, currentSessionEvents, currentSessionOutput,
    currentSessionState,
    loadSessions, sessionsStatus, sessionsError, selectSession, runTask, resolveApproval, replayStatus, replayError, runStatus, runError,
    approvalStatus, approvalError
  } = useAppStore();
  const { t, i18n } = useTranslation();

  const [promptInput, setPromptInput] = useState('');
  const hydratedInitialSessionRef = useRef(false);

  useEffect(() => {
    i18n.changeLanguage(language);
  }, [language, i18n]);

  useEffect(() => {
    loadSessions();
  }, [loadSessions]);

  const toggleLanguage = () => {
    setLanguage(language === 'en' ? 'zh-CN' : 'en');
  };

  const handleRunTask = async () => {
    if (!promptInput.trim()) return;
    const nextPrompt = promptInput;
    await runTask(nextPrompt);
    const { runStatus: latestRunStatus } = useAppStore.getState();
    if (latestRunStatus !== 'error') {
      setPromptInput('');
    }
  };

  const isRunning = runStatus === 'running';
  const isReplayLoading = replayStatus === 'loading';
  const isApprovalSubmitting = approvalStatus === 'submitting';
  const isWaitingApproval = currentSessionState?.status === 'waiting';
  const pendingApprovalEvent = useMemo(() => getPendingApprovalEvent(currentSessionEvents), [currentSessionEvents]);
  const pendingApprovalSummary = typeof pendingApprovalEvent?.payload?.target_summary === 'string'
    ? pendingApprovalEvent.payload.target_summary
    : typeof pendingApprovalEvent?.payload?.tool === 'string'
      ? pendingApprovalEvent.payload.tool
      : null;

  useEffect(() => {
    if (hydratedInitialSessionRef.current || sessionsStatus !== 'success') {
      return;
    }

    hydratedInitialSessionRef.current = true;

    if (!currentSessionId || isRunning) {
      return;
    }

    void selectSession(currentSessionId);
  }, [currentSessionId, isRunning, selectSession, sessionsStatus]);

  const derivedTasks = useMemo(() => deriveTasksFromEvents(currentSessionEvents), [currentSessionEvents]);
  const derivedActivities = useMemo(() => deriveActivitiesFromEvents(currentSessionEvents), [currentSessionEvents]);

  const renderStatusIcon = (status: string) => {
    if (status === 'completed') return <CheckCircle2 className="w-5 h-5 text-emerald-500" />;
    if (status === 'in_progress') return <Clock className="w-5 h-5 text-indigo-400" />;
    if (status === 'failed') return <AlertCircle className="w-5 h-5 text-rose-500" />;
    if (status === 'waiting') return <PauseCircle className="w-5 h-5 text-amber-400" />;
    return <Circle className="w-5 h-5 text-slate-500" />;
  };

  const getStatusColorClass = (status: string) => {
    if (status === 'in_progress' || status === 'running') return 'bg-indigo-500/10 text-indigo-400 border-indigo-500/20';
    if (status === 'completed') return 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20';
    if (status === 'failed') return 'bg-rose-500/10 text-rose-400 border-rose-500/20';
    if (status === 'waiting') return 'bg-amber-500/10 text-amber-400 border-amber-500/20';
    return 'bg-slate-800 text-slate-400 border-slate-700';
  };

  const getSessionStatusDotClass = (status: string) => {
    if (status === 'running') return 'bg-indigo-400 shadow-[0_0_8px_rgba(129,140,248,0.5)] animate-pulse';
    if (status === 'completed') return 'bg-emerald-400';
    if (status === 'failed') return 'bg-rose-400';
    if (status === 'waiting') return 'bg-amber-400';
    return 'bg-slate-500';
  };

  const formatSessionUpdatedLabel = (updatedAt: number) => {
    const token = formatSessionUpdatedAt(updatedAt);
    if (token === 'just-now') {
      return t('session.updatedAtJustNow');
    }
    const [unit, value] = token.split(':');
    if (unit === 'minutes') {
      return t('session.updatedAtMinutesAgo', { count: Number(value) });
    }
    if (unit === 'hours') {
      return t('session.updatedAtHoursAgo', { count: Number(value) });
    }
    return t('session.updatedAtDaysAgo', { count: Number(value) });
  };

  const currentSessionSummary = useMemo(
    () => sessions.find((s) => s.session.id === currentSessionId),
    [sessions, currentSessionId]
  );

  const currentSessionTitle = useMemo(() => {
    if (!currentSessionId) return null;
    if (currentSessionSummary?.prompt) return currentSessionSummary.prompt;
    const latestReq = [...currentSessionEvents]
      .reverse()
      .find((e) => e.event_type === 'runtime.request_received');
    return (latestReq?.payload?.prompt as string) || currentSessionId;
  }, [currentSessionId, currentSessionSummary, currentSessionEvents]);

  return (
    <div className="flex h-screen bg-[#09090b] text-slate-300 font-sans overflow-hidden selection:bg-indigo-500/30">

      <aside className="w-16 md:w-64 border-r border-slate-800 bg-[#09090b] flex flex-col justify-between">
        <div className="flex-1 overflow-hidden flex flex-col">
          <div className="h-16 flex items-center justify-center md:justify-start md:px-6 border-b border-slate-800 text-indigo-400 font-bold tracking-tight">
            <Code2 className="w-6 h-6 md:mr-3" />
            <span className="hidden md:block text-lg">{t('app.title')}</span>
          </div>

          <nav className="p-3 space-y-2 flex-shrink-0">
            {[
              { icon: LayoutDashboard, label: t('nav.workspace'), active: true },
              { icon: Activity, label: t('nav.activity') },
            ].map((item) => (
              <button key={item.label} type="button" className={`w-full flex items-center justify-center md:justify-start md:px-4 py-3 md:py-2.5 rounded-lg transition-colors ${item.active ? 'bg-indigo-500/10 text-indigo-400' : 'text-slate-400 hover:bg-slate-800/50 hover:text-slate-200'}`}>
                <item.icon className="w-5 h-5 md:mr-3" />
                <span className="hidden md:block font-medium">{item.label}</span>
              </button>
            ))}
          </nav>

          <div className="p-3 flex-1 overflow-y-auto border-t border-slate-800 hidden md:block">
            <div className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-3 px-4">{t('session.listHeader')}</div>
            <div className="space-y-1">
              <button
                type="button"
                onClick={() => selectSession('')}
                disabled={isRunning || isReplayLoading}
                className={`w-full flex items-center justify-start px-4 py-2 rounded-lg transition-colors ${!currentSessionId ? 'bg-emerald-500/10 text-emerald-400' : 'text-slate-400 hover:bg-slate-800/50 hover:text-slate-200'}`}
              >
                <span className="font-medium text-sm">{t('session.newSession')}</span>
              </button>
              {sessions.map((s) => (
                <button
                  key={s.session.id}
                  type="button"
                  onClick={() => selectSession(s.session.id)}
                  disabled={isRunning || isReplayLoading}
                  className={`w-full flex flex-col items-start justify-center px-4 py-2.5 rounded-lg transition-colors overflow-hidden border ${
                    currentSessionId === s.session.id
                      ? 'bg-indigo-500/10 border-indigo-500/30 shadow-[0_0_15px_rgba(99,102,241,0.05)] text-indigo-100'
                      : 'border-transparent text-slate-400 hover:bg-slate-800/50 hover:text-slate-200'
                  }`}
                  title={s.prompt || s.session.id}
                >
                  <div className="w-full flex items-start justify-between gap-2 mb-1">
                    <div className="flex items-center truncate max-w-[75%]">
                       <div className={`w-1.5 h-1.5 rounded-full flex-shrink-0 mr-2.5 ${getSessionStatusDotClass(s.status)}`} />
                       <span className="font-medium text-sm truncate">
                         {s.prompt || <span className="font-mono">{s.session.id.substring(0, 8)}</span>}
                       </span>
                     </div>
                    <span className={`flex-shrink-0 text-[10px] px-2 py-0.5 rounded-md font-medium border ${getStatusColorClass(s.status)}`}>
                      {s.status === 'running' ? t('session.agentBusy') : t(`task.status.${s.status}`)}
                    </span>
                   </div>
                  <div className="w-full flex items-center justify-between gap-2 text-[11px] text-slate-500">
                    <div className="min-w-0 flex flex-col items-start">
                      <span className="font-mono truncate max-w-full" title={s.session.id}>
                        {s.session.id.substring(0, 8)}
                      </span>
                      <span className="truncate max-w-full">{formatSessionUpdatedLabel(s.updated_at)}</span>
                    </div>
                    <span className="flex-shrink-0 font-medium">T{s.turn}</span>
                  </div>
                </button>
              ))}
            </div>
            {sessionsStatus === 'loading' && (
              <p className="mt-3 px-4 text-xs text-slate-500">{t('session.loadingList')}</p>
            )}
            {sessionsError && (
              <p className="mt-3 px-4 text-xs text-rose-400">{t('session.loadError', { message: sessionsError })}</p>
            )}
          </div>
        </div>

        <div className="p-3 border-t border-slate-800 space-y-2">
           <button type="button" onClick={toggleLanguage} className="w-full flex items-center justify-center md:justify-start md:px-4 py-3 md:py-2.5 rounded-lg text-slate-400 hover:bg-slate-800/50 hover:text-slate-200 transition-colors">
             <Globe className="w-5 h-5 md:mr-3" />
             <span className="hidden md:block font-medium">{language === 'en' ? t('language.zh') : t('language.en')}</span>
           </button>
           <RuntimeDebug />
           <button type="button" className="w-full flex items-center justify-center md:justify-start md:px-4 py-3 md:py-2.5 rounded-lg text-slate-400 hover:bg-slate-800/50 hover:text-slate-200 transition-colors">
             <Settings className="w-5 h-5 md:mr-3" />
             <span className="hidden md:block font-medium">{t('nav.settings')}</span>
           </button>
        </div>
      </aside>

      <main className="flex-1 flex flex-col min-w-0">

        <header className="h-16 flex items-center justify-between px-6 border-b border-slate-800 bg-[#0c0c0e]">
          <h1 className="text-xl font-semibold text-slate-100 flex flex-col justify-center min-w-0">
            <div className="flex items-center space-x-2 truncate">
              <span>{t('session.current')}</span>
              {currentSessionId && (
                <>
                  <span className="text-slate-600">/</span>
                  <span className="text-slate-200 text-sm font-medium truncate max-w-[400px]">{currentSessionTitle}</span>
                </>
              )}
            </div>
            {currentSessionId && currentSessionTitle !== currentSessionId && (
              <div className="text-xs text-slate-500 font-mono truncate mt-0.5" title={currentSessionId}>
                {currentSessionId}
              </div>
            )}
          </h1>
          <div className="flex items-center space-x-4">
             <span className={`px-3 py-1 rounded-full text-xs font-medium flex items-center border ${isRunning ? 'bg-amber-500/10 text-amber-400 border-amber-500/20' : 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20'}`}>
               <span className={`w-2 h-2 rounded-full mr-2 ${isRunning ? 'bg-amber-500 animate-pulse' : 'bg-emerald-500'}`}></span>
                {isRunning ? t('session.agentBusy') : t('session.agentIdle')}
              </span>
           </div>
         </header>

        <div className="flex-1 flex overflow-hidden bg-[#0a0a0c]">

          <div className="flex-1 p-6 flex flex-col min-h-0">
            <div className="max-w-4xl w-full mx-auto flex-1 flex flex-col min-h-0 space-y-6">

              <div className="bg-slate-800/30 border border-slate-700/50 rounded-xl p-6 shadow-sm flex flex-col">
                <div className="flex items-center justify-between mb-4">
                  <h2 className="text-lg font-medium text-slate-200">{t('task.submitHeading')}</h2>
                </div>

                <div className="flex flex-wrap gap-4 mb-4 pb-4 border-b border-slate-700/50">
                  <div className="flex flex-col gap-1 flex-1 min-w-[120px] max-w-[200px]">
                    <label htmlFor="agentPreset" className="text-xs font-medium text-slate-400">{t('config.agentPreset')}</label>
                    <select
                      id="agentPreset"
                      disabled
                      value={agentPreset}
                      className="bg-slate-900 border border-slate-700 rounded-md px-3 py-1.5 text-sm text-slate-300 focus:outline-none cursor-not-allowed opacity-70"
                    >
                      <option value="leader">Leader</option>
                    </select>
                  </div>

                  <div className="flex flex-col gap-1 flex-1 min-w-[140px] max-w-[200px]">
                    <label htmlFor="leaderMode" className="text-xs font-medium text-slate-400">{t('config.leaderMode')}</label>
                    <select
                      id="leaderMode"
                      value={leaderMode}
                      onChange={(e) => setLeaderMode(e.target.value as 'direct_execute' | 'plan_first')}
                      disabled={isRunning || isReplayLoading || isWaitingApproval || isApprovalSubmitting}
                      className="bg-slate-900 border border-slate-700 rounded-md px-3 py-1.5 text-sm text-slate-300 focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      <option value="direct_execute">{t('config.leaderMode.direct')}</option>
                      <option value="plan_first">{t('config.leaderMode.plan')}</option>
                    </select>
                  </div>

                  <div className="flex flex-col gap-1 flex-[2] min-w-[200px]">
                    <label htmlFor="providerModel" className="text-xs font-medium text-slate-400">{t('config.providerModel')}</label>
                    <input
                      id="providerModel"
                      type="text"
                      value={providerModel}
                      onChange={(e) => setProviderModel(e.target.value)}
                      disabled={isRunning || isReplayLoading || isWaitingApproval || isApprovalSubmitting}
                      placeholder="e.g. opencode-go/glm-5"
                      className="bg-slate-900 border border-slate-700 rounded-md px-3 py-1.5 text-sm text-slate-300 focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 transition-colors w-full disabled:opacity-50 disabled:cursor-not-allowed"
                    />
                  </div>
                  <div className="w-full text-xs text-slate-500 flex items-start gap-1.5 mt-1">
                    <AlertCircle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                    <span>{t('config.helpText')}</span>
                  </div>
                </div>

                <div className="flex space-x-3">
                  <input
                    type="text"
                    value={promptInput}
                    onChange={(e) => setPromptInput(e.target.value)}
                    onKeyDown={(e) => { if (e.key === 'Enter') void handleRunTask() }}
                    placeholder={t('task.promptPlaceholder')}
                    className="flex-1 bg-slate-900 border border-slate-700 rounded-lg px-4 py-2 focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 transition-colors"
                    disabled={isRunning || isReplayLoading || isWaitingApproval || isApprovalSubmitting}
                  />
                  <button
                    type="button"
                    onClick={() => void handleRunTask()}
                    disabled={isRunning || isReplayLoading || isWaitingApproval || isApprovalSubmitting || !promptInput.trim()}
                    className="bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed text-white px-6 py-2 rounded-lg text-sm font-medium transition-colors flex items-center shadow-sm shadow-indigo-500/20"
                  >
                    <Play className="w-4 h-4 mr-2" />
                    {isRunning ? t('task.running') : t('task.newTask')}
                  </button>
                </div>
                {runError && <div className="mt-3 text-red-400 text-sm">{t('common.errorWithMessage', { message: runError })}</div>}
                {replayError && <div className="mt-3 text-amber-400 text-sm">{t('session.replayError', { message: replayError })}</div>}
                {approvalError && <div className="mt-3 text-amber-400 text-sm">{t('approval.error', { message: approvalError })}</div>}

                {isWaitingApproval && pendingApprovalEvent && (
                  <div className="mt-4 rounded-lg border border-amber-500/20 bg-amber-500/10 p-4">
                    <div className="flex items-center justify-between gap-4 flex-wrap">
                      <div>
                        <p className="text-sm font-medium text-amber-300">{t('approval.heading')}</p>
                        <p className="mt-1 text-sm text-slate-300">
                          {t('approval.message', { target: pendingApprovalSummary ?? t('approval.unknownTarget') })}
                        </p>
                      </div>
                      <div className="flex items-center gap-2">
                        <button
                          type="button"
                          onClick={() => void resolveApproval('deny')}
                          disabled={isApprovalSubmitting}
                          className="rounded-lg border border-rose-500/20 bg-rose-500/10 px-4 py-2 text-sm font-medium text-rose-300 transition-colors hover:bg-rose-500/20 disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          {isApprovalSubmitting ? t('approval.submitting') : t('approval.deny')}
                        </button>
                        <button
                          type="button"
                          onClick={() => void resolveApproval('allow')}
                          disabled={isApprovalSubmitting}
                          className="rounded-lg border border-emerald-500/20 bg-emerald-500/10 px-4 py-2 text-sm font-medium text-emerald-300 transition-colors hover:bg-emerald-500/20 disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          {isApprovalSubmitting ? t('approval.submitting') : t('approval.allow')}
                        </button>
                      </div>
                    </div>
                  </div>
                )}
              </div>

              <div className="bg-slate-800/30 border border-slate-700/50 rounded-xl p-6 shadow-sm flex-1 flex flex-col min-h-0">
                 <h2 className="text-lg font-medium text-slate-200 mb-4 flex-shrink-0">{t('task.queue')}</h2>
                 <div className="space-y-3 overflow-y-auto flex-1 min-h-0 pr-2">
                   {derivedTasks.map((task) => (
                     <div key={task.id} className="flex items-center justify-between p-4 bg-slate-900/50 rounded-lg border border-slate-800">
                       <div className="flex items-center space-x-4 flex-1 min-w-0 mr-4">
                         {renderStatusIcon(task.status)}
                         <span className="text-slate-300 font-medium text-sm truncate" title={t(task.titleKey, task.titleValues)}>
                           {t(task.titleKey, task.titleValues)}
                         </span>
                       </div>
                       <span className={`flex-shrink-0 text-xs px-2.5 py-1 rounded-md font-medium capitalize border ${getStatusColorClass(task.status)}`}>
                         {t(`task.status.${task.status}`)}
                       </span>
                     </div>
                   ))}
                   {derivedTasks.length === 0 && (
                     <div className="text-center py-8 text-slate-500 text-sm">
                       {t('activity.empty')}
                     </div>
                   )}
                 </div>
              </div>

              {currentSessionOutput !== null && (
                <div className="bg-slate-800/30 border border-slate-700/50 rounded-xl p-6 shadow-sm flex-1 flex flex-col min-h-0">
                  <h2 className="text-lg font-medium text-slate-200 mb-4 flex-shrink-0">{t('task.output')}</h2>
                  <div className="bg-slate-900 rounded-lg p-4 font-mono text-sm text-slate-300 overflow-y-auto whitespace-pre-wrap border border-slate-800 flex-1 min-h-0 custom-scrollbar">
                    {currentSessionOutput}
                  </div>
                </div>
              )}

            </div>
          </div>

          <aside className="w-80 border-l border-slate-800 bg-[#0c0c0e] hidden lg:flex flex-col">
             <div className="p-4 border-b border-slate-800">
               <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wider">{t('activity.log')}</h3>
             </div>
             <div className="flex-1 overflow-y-auto p-4 space-y-4">
               {derivedActivities.map((activity) => (
                 <div key={activity.id} className="relative pl-4 border-l border-slate-800">
                   <div className="absolute -left-1.5 top-1.5 w-3 h-3 rounded-full bg-slate-800 border-2 border-[#0c0c0e]"></div>
                    <div className="flex justify-between items-start mb-1">
                      <p className="text-sm text-slate-300 font-medium truncate mr-2" title={activity.message}>{activity.message}</p>
                      <span className="text-xs px-1.5 py-0.5 rounded bg-slate-800 text-slate-400 capitalize">{t(`activity.source.${activity.source}`)}</span>
                    </div>
                    {activity.payloadStr && activity.payloadStr !== '{}' && (
                      <div className="text-xs text-slate-500 font-mono mt-1 bg-slate-900/50 p-1.5 rounded truncate" title={activity.payloadStr}>
                        {activity.payloadStr}
                      </div>
                    )}
                  </div>
                ))}
                {derivedActivities.length === 0 && (
                  <p className="text-xs text-slate-500 text-center">{t('activity.empty')}</p>
                )}
             </div>
          </aside>

        </div>
      </main>
    </div>
  );
}

export default App;
