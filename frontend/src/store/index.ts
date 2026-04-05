import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import { RuntimeClient } from '../lib/runtime/client';
import { StoredSessionSummary, SessionState, EventEnvelope } from '../lib/runtime/types';

interface AppState {
  language: 'en' | 'zh-CN';

  sessions: StoredSessionSummary[];
  currentSessionId: string | null;
  currentSessionState: SessionState | null;
  currentSessionEvents: EventEnvelope[];

  sessionsStatus: 'idle' | 'loading' | 'success' | 'error';
  sessionsError: string | null;
  replayStatus: 'idle' | 'loading' | 'success' | 'error';
  replayError: string | null;
  runStatus: 'idle' | 'running' | 'success' | 'error';
  runError: string | null;
  replayRequestId: number;

  setLanguage: (lang: 'en' | 'zh-CN') => void;
  loadSessions: () => Promise<void>;
  selectSession: (sessionId: string) => Promise<void>;
  runTask: (prompt: string) => Promise<void>;
}

export const useAppStore = create<AppState>()(
  persist(
    (set, get) => ({
      language: 'en',

      sessions: [],
      currentSessionId: null,
      currentSessionState: null,
      currentSessionEvents: [],

      sessionsStatus: 'idle',
      sessionsError: null,
      replayStatus: 'idle',
      replayError: null,
      runStatus: 'idle',
      runError: null,
      replayRequestId: 0,

      setLanguage: (language) => set({ language }),

      loadSessions: async () => {
        set({ sessionsStatus: 'loading', sessionsError: null });
        try {
          const sessions = await RuntimeClient.listSessions();
          set({ sessions, sessionsStatus: 'success' });
        } catch (err) {
          set({ sessionsStatus: 'error', sessionsError: (err as Error).message });
        }
      },

      selectSession: async (sessionId: string) => {
        if (get().runStatus === 'running') {
          return;
        }

        if (!sessionId) {
          set({
            currentSessionId: null,
            currentSessionState: null,
            currentSessionEvents: [],
            replayStatus: 'idle',
            replayError: null,
            runStatus: 'idle',
            runError: null
          });
          return;
        }

        const requestId = get().replayRequestId + 1;
        set({
          currentSessionId: sessionId,
          currentSessionState: null,
          currentSessionEvents: [],
          replayStatus: 'loading',
          replayError: null,
          replayRequestId: requestId,
          runStatus: 'idle',
          runError: null
        });

        try {
          const replay = await RuntimeClient.getSessionReplay(sessionId);
          if (get().replayRequestId !== requestId || get().currentSessionId !== sessionId) {
            return;
          }

          set({
            currentSessionState: replay.session,
            currentSessionEvents: replay.events,
            replayStatus: 'success'
          });
        } catch (err) {
          if (get().replayRequestId !== requestId || get().currentSessionId !== sessionId) {
            return;
          }

          set({
            replayStatus: 'error',
            replayError: (err as Error).message
          });
        }
      },

      runTask: async (prompt: string) => {
        if (get().replayStatus === 'loading') {
          return;
        }

        const nextReplayRequestId = get().replayRequestId + 1;
        set({ runStatus: 'running', runError: null });
        const { currentSessionId } = get();
        set({
          replayStatus: 'idle',
          replayError: null,
          replayRequestId: nextReplayRequestId
        });

        try {
          const stream = RuntimeClient.runStream({
            prompt,
            session_id: currentSessionId
          });

          for await (const chunk of stream) {
            set((state) => {
              const newEvents = chunk.event ? [...state.currentSessionEvents, chunk.event] : state.currentSessionEvents;
              return {
                currentSessionState: chunk.session,
                currentSessionEvents: newEvents,
                currentSessionId: chunk.session.session.id
              };
            });
          }

          set({ runStatus: 'success' });
          get().loadSessions();
        } catch (err) {
          set({ runStatus: 'error', runError: (err as Error).message });
        }
      }
    }),
    {
      name: 'app-storage',
      partialize: (state) => ({ language: state.language, currentSessionId: state.currentSessionId })
    }
  )
);
