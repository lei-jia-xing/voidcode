import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import { Task, Activity } from '../types';
import { RuntimeClient } from '../lib/runtime/client';
import { StoredSessionSummary, SessionState, EventEnvelope } from '../lib/runtime/types';

interface AppState {
  tasks: Task[];
  activities: Activity[];
  language: 'en' | 'zh-CN';

  sessions: StoredSessionSummary[];
  currentSessionId: string | null;
  currentSessionState: SessionState | null;
  currentSessionEvents: EventEnvelope[];

  sessionsStatus: 'idle' | 'loading' | 'success' | 'error';
  sessionsError: string | null;
  runStatus: 'idle' | 'running' | 'success' | 'error';
  runError: string | null;

  setLanguage: (lang: 'en' | 'zh-CN') => void;
  loadSessions: () => Promise<void>;
  selectSession: (sessionId: string) => Promise<void>;
  runTask: (prompt: string) => Promise<void>;
}

export const useAppStore = create<AppState>()(
  persist(
    (set, get) => ({
      tasks: [
        { id: 'mock-1', title: 'Task queue UI placeholder', status: 'pending', createdAt: new Date().toISOString() }
      ],
      activities: [
        { id: 'mock-1', type: 'log', message: 'Activity log UI placeholder (non-integrated)', timestamp: new Date().toISOString() }
      ],
      language: 'en',

      sessions: [],
      currentSessionId: null,
      currentSessionState: null,
      currentSessionEvents: [],

      sessionsStatus: 'idle',
      sessionsError: null,
      runStatus: 'idle',
      runError: null,

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
        if (!sessionId) {
          set({
            currentSessionId: null,
            currentSessionState: null,
            currentSessionEvents: [],
            runStatus: 'idle',
            runError: null
          });
          return;
        }

        set({ currentSessionId: sessionId, runStatus: 'idle', runError: null });
        try {
          const replay = await RuntimeClient.getSessionReplay(sessionId);
          set({
            currentSessionState: replay.session,
            currentSessionEvents: replay.events
          });
        } catch (err) {
          console.error("Failed to load session:", err);
        }
      },

      runTask: async (prompt: string) => {
        set({ runStatus: 'running', runError: null });
        const { currentSessionId } = get();
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
