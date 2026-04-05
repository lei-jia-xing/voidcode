import { useEffect, useRef, useState, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { useAppStore } from './store';
import { Activity, Play, Settings, Code2, LayoutDashboard, CheckCircle2, Circle, Clock, Globe, Hash, AlertCircle, PauseCircle } from 'lucide-react';
import { RuntimeDebug } from './components/RuntimeDebug';
import { deriveTasksFromEvents, deriveActivitiesFromEvents } from './lib/runtime/event-parser';

function App() {
  const {
    language, setLanguage,
    sessions, currentSessionId, currentSessionEvents,
    loadSessions, sessionsStatus, sessionsError, selectSession, runTask, replayStatus, replayError, runStatus, runError
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
    if (status === 'in_progress') return 'bg-indigo-500/10 text-indigo-400 border-indigo-500/20';
    if (status === 'completed') return 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20';
    if (status === 'failed') return 'bg-rose-500/10 text-rose-400 border-rose-500/20';
    if (status === 'waiting') return 'bg-amber-500/10 text-amber-400 border-amber-500/20';
    return 'bg-slate-800 text-slate-400 border-slate-700';
  };

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
                  className={`w-full flex items-center justify-start px-4 py-2 rounded-lg transition-colors truncate ${currentSessionId === s.session.id ? 'bg-indigo-500/10 text-indigo-400' : 'text-slate-400 hover:bg-slate-800/50 hover:text-slate-200'}`}
                >
                  <Hash className="w-4 h-4 mr-3 flex-shrink-0" />
                  <span className="font-medium text-sm truncate">{s.session.id}</span>
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
          <h1 className="text-xl font-semibold text-slate-100 flex items-center space-x-2 truncate">
            <span>{t('session.current')}</span>
            {currentSessionId && (
              <>
                <span className="text-slate-600">/</span>
                <span className="text-slate-400 text-sm font-normal truncate max-w-[200px]">{currentSessionId}</span>
              </>
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
                <div className="flex space-x-3">
                  <input
                    type="text"
                    value={promptInput}
                    onChange={(e) => setPromptInput(e.target.value)}
                    onKeyDown={(e) => { if (e.key === 'Enter') void handleRunTask() }}
                    placeholder={t('task.promptPlaceholder')}
                    className="flex-1 bg-slate-900 border border-slate-700 rounded-lg px-4 py-2 focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 transition-colors"
                    disabled={isRunning || isReplayLoading}
                  />
                  <button
                    type="button"
                    onClick={() => void handleRunTask()}
                    disabled={isRunning || isReplayLoading || !promptInput.trim()}
                    className="bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed text-white px-6 py-2 rounded-lg text-sm font-medium transition-colors flex items-center shadow-sm shadow-indigo-500/20"
                  >
                    <Play className="w-4 h-4 mr-2" />
                    {isRunning ? t('task.running') : t('task.newTask')}
                  </button>
                </div>
                {runError && <div className="mt-3 text-red-400 text-sm">{t('common.errorWithMessage', { message: runError })}</div>}
                {replayError && <div className="mt-3 text-amber-400 text-sm">{t('session.replayError', { message: replayError })}</div>}
              </div>

              <div className="bg-slate-800/30 border border-slate-700/50 rounded-xl p-6 shadow-sm flex-1 overflow-y-auto min-h-0">
                 <h2 className="text-lg font-medium text-slate-200 mb-4">{t('task.queue')}</h2>
                 <div className="space-y-3">
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
