import { useEffect, useRef, useState, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { useAppStore } from './store';
import { SessionSidebar } from './components/SessionSidebar';
import { ChatThread } from './components/ChatThread';
import { Composer } from './components/Composer';
import { SettingsPanel } from './components/SettingsPanel';
import { deriveChatMessages } from './lib/runtime/event-parser';
import { RuntimeClient } from './lib/runtime/client';
import { SlidersHorizontal, Loader2 } from 'lucide-react';

function App() {
  const {
    language, setLanguage,
    providerModel, setProviderModel,
    sessions, currentSessionId, currentSessionEvents, currentSessionOutput,
    currentSessionState,
    loadSessions, sessionsStatus, sessionsError, selectSession, runTask, resolveApproval, replayStatus, replayError, runStatus, runError,
    approvalStatus, approvalError,
    settings, settingsStatus, settingsError, loadSettings, updateSettings
  } = useAppStore();
  const { t, i18n } = useTranslation();

  const [showSettings, setShowSettings] = useState(false);
  const [runtimeTestStatus, setRuntimeTestStatus] = useState<'idle' | 'testing' | 'success' | 'error'>('idle');
  const [showModelControls, setShowModelControls] = useState(false);
  const hydratedInitialSessionRef = useRef(false);
  const chatScrollRef = useRef<HTMLDivElement>(null);
  const lastMessageCountRef = useRef(0);

  const isRunning = runStatus === 'running';
  const isReplayLoading = replayStatus === 'loading';
  const isApprovalSubmitting = approvalStatus === 'submitting';
  const isWaitingApproval = currentSessionState?.status === 'waiting';

  const chatMessages = useMemo(
    () => deriveChatMessages(currentSessionEvents, currentSessionOutput),
    [currentSessionEvents, currentSessionOutput]
  );

  useEffect(() => {
    i18n.changeLanguage(language);
  }, [language, i18n]);

  useEffect(() => {
    loadSessions();
  }, [loadSessions]);

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
    setLanguage(language === 'en' ? 'zh-CN' : 'en');
  };

  const testRuntime = async () => {
    setRuntimeTestStatus('testing');
    try {
      await RuntimeClient.listSessions();
      setRuntimeTestStatus('success');
      setTimeout(() => setRuntimeTestStatus('idle'), 3000);
    } catch (e) {
      console.error('Runtime test failed:', e);
      setRuntimeTestStatus('error');
      setTimeout(() => setRuntimeTestStatus('idle'), 3000);
    }
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

  const handleResolveApproval = (decision: 'allow' | 'deny') => {
    void resolveApproval(decision);
  };

  const composerDisabled = isRunning || isReplayLoading || isWaitingApproval || isApprovalSubmitting;

  return (
    <div className="flex h-screen bg-[#09090b] text-slate-300 font-sans overflow-hidden selection:bg-indigo-500/30">
      <SessionSidebar
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
        onOpenSettings={() => setShowSettings(true)}
        onTestRuntime={testRuntime}
      />

      <div className="flex-1 flex flex-col min-w-0">
        <header className="h-14 flex items-center justify-between px-4 border-b border-slate-800 bg-[#0c0c0e] flex-shrink-0">
          <div className="flex items-center gap-2 min-w-0">
            {isReplayLoading && <Loader2 className="w-4 h-4 animate-spin text-indigo-400 flex-shrink-0" />}
            {currentSessionId ? (
              <div className="flex flex-col min-w-0">
                <span className="text-sm font-medium text-slate-200 truncate">{currentSessionTitle}</span>
                <span className="text-[11px] text-slate-500 font-mono truncate">{currentSessionId}</span>
              </div>
            ) : (
              <span className="text-sm font-medium text-slate-400">{t('chat.newChat')}</span>
            )}
          </div>

          <div className="flex items-center gap-2 flex-shrink-0">
            <div className="relative">
              <button
                type="button"
                onClick={() => setShowModelControls(!showModelControls)}
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
                  showModelControls ? 'bg-indigo-500/20 text-indigo-300' : 'text-slate-400 hover:bg-slate-800 hover:text-slate-200'
                }`}
              >
                <SlidersHorizontal className="w-3.5 h-3.5" />
                {providerModel}
              </button>

              {showModelControls && (
                <div className="absolute right-0 top-full mt-2 w-72 bg-[#0c0c0e] border border-slate-700 rounded-xl shadow-xl p-4 z-40">
                  <div className="space-y-3">
                    <div className="space-y-1.5">
                      <label htmlFor="model-input" className="text-xs font-medium text-slate-400">{t('config.providerModel')}</label>
                      <input
                        id="model-input"
                        type="text"
                        value={providerModel}
                        onChange={(e) => setProviderModel(e.target.value)}
                        disabled={isRunning || isReplayLoading || isWaitingApproval || isApprovalSubmitting}
                        placeholder="e.g. opencode-go/glm-5.1"
                        className="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-sm text-slate-200 focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 transition-colors disabled:opacity-50"
                      />
                    </div>
                    <button
                      type="button"
                      onClick={() => setShowModelControls(false)}
                      className="w-full text-xs text-slate-500 hover:text-slate-300 transition-colors text-center"
                    >
                      {t('common.done')}
                    </button>
                  </div>
                </div>
              )}
            </div>

            <span className={`px-2.5 py-1 rounded-full text-[11px] font-medium flex items-center border ${
              isRunning ? 'bg-amber-500/10 text-amber-400 border-amber-500/20' : 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20'
            }`}>
              <span className={`w-1.5 h-1.5 rounded-full mr-1.5 ${isRunning ? 'bg-amber-500 animate-pulse' : 'bg-emerald-500'}`} />
              {isRunning ? t('session.agentBusy') : t('session.agentIdle')}
            </span>
          </div>
        </header>

        {showModelControls && (
          <button type="button" className="fixed inset-0 z-30 bg-transparent" onClick={() => setShowModelControls(false)} aria-label={t('common.close')} />
        )}

        {replayError && (
          <div className="flex-shrink-0 bg-amber-500/10 border-b border-amber-500/20 px-4 py-2 text-xs text-amber-300">
            {t('session.replayError', { message: replayError })}
          </div>
        )}
        {runError && (
          <div className="flex-shrink-0 bg-rose-500/10 border-b border-rose-500/20 px-4 py-2 text-xs text-rose-300">
            {t('common.errorWithMessage', { message: runError })}
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
          onSubmit={handleSendMessage}
        />
      </div>

      <SettingsPanel
        isOpen={showSettings}
        settings={settings}
        settingsStatus={settingsStatus}
        settingsError={settingsError}
        onClose={() => setShowSettings(false)}
        onLoad={loadSettings}
        onSave={updateSettings}
      />
    </div>
  );
}

export default App;
