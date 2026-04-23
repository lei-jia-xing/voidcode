import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import { RuntimeClient } from '../lib/runtime/client';
import { ApprovalDecision, StoredSessionSummary, SessionState, EventEnvelope } from '../lib/runtime/types';

interface AppState {
  language: 'en' | 'zh-CN';

  agentPreset: 'leader';
  leaderMode: 'direct_execute' | 'plan_first';
  providerModel: string;
  maxSteps: number;

  sessions: StoredSessionSummary[];
  currentSessionId: string | null;
  currentSessionState: SessionState | null;
  currentSessionEvents: EventEnvelope[];
  currentSessionOutput: string | null;

  sessionsStatus: 'idle' | 'loading' | 'success' | 'error';
  sessionsError: string | null;
  replayStatus: 'idle' | 'loading' | 'success' | 'error';
  replayError: string | null;
  runStatus: 'idle' | 'running' | 'success' | 'error';
  runError: string | null;
  approvalStatus: 'idle' | 'submitting' | 'success' | 'error';
  approvalError: string | null;
  replayRequestId: number;

  setLanguage: (lang: 'en' | 'zh-CN') => void;
  setAgentPreset: (preset: 'leader') => void;
  setLeaderMode: (mode: 'direct_execute' | 'plan_first') => void;
  setProviderModel: (model: string) => void;
  setMaxSteps: (maxSteps: number) => void;
  loadSessions: () => Promise<void>;
  selectSession: (sessionId: string) => Promise<void>;
  runTask: (
    prompt: string,
    options?: {
      sessionId?: string | null;
      metadata?: {
        skills?: string[];
        max_steps?: number;
        provider_stream?: boolean;
        [key: string]: unknown;
      };
    }
  ) => Promise<void>;
  resolveApproval: (decision: ApprovalDecision) => Promise<void>;
}

function getPendingApprovalRequestId(events: EventEnvelope[]): string | null {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event.event_type !== 'runtime.approval_requested') {
      continue;
    }

    const requestId = event.payload.request_id;
    if (typeof requestId === 'string' && requestId.length > 0) {
      return requestId;
    }
  }

  return null;
}

export const useAppStore = create<AppState>()(
  persist(
    (set, get) => ({
      language: 'en',
      agentPreset: 'leader',
      leaderMode: 'direct_execute',
      providerModel: 'opencode-go/glm-5.1',
      maxSteps: 16,

      sessions: [],
      currentSessionId: null,
      currentSessionState: null,
      currentSessionEvents: [],
      currentSessionOutput: null,

      sessionsStatus: 'idle',
      sessionsError: null,
      replayStatus: 'idle',
      replayError: null,
      runStatus: 'idle',
      runError: null,
      approvalStatus: 'idle',
      approvalError: null,
      replayRequestId: 0,

      setLanguage: (language) => set({ language }),
      setAgentPreset: (agentPreset) => set({ agentPreset }),
      setLeaderMode: (leaderMode) => set({ leaderMode }),
      setProviderModel: (providerModel) => set({ providerModel }),
      setMaxSteps: (maxSteps) => {
        const normalizedMaxSteps = Number.isFinite(maxSteps)
          ? Math.max(1, Math.floor(maxSteps))
          : 1;
        set({ maxSteps: normalizedMaxSteps });
      },

      loadSessions: async () => {
        set({ sessionsStatus: 'loading', sessionsError: null });
        try {
          const sessions = await RuntimeClient.listSessions();
          const { currentSessionId } = get();

          if (currentSessionId && !sessions.some(s => s.session.id === currentSessionId)) {
            set({
              sessions,
              sessionsStatus: 'success',
              currentSessionId: null,
              currentSessionState: null,
              currentSessionEvents: [],
              currentSessionOutput: null,
              replayStatus: 'idle',
              replayError: null
            });
          } else {
            set({ sessions, sessionsStatus: 'success' });
          }
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
            currentSessionOutput: null,
            replayStatus: 'idle',
            replayError: null,
            runStatus: 'idle',
            runError: null,
            approvalStatus: 'idle',
            approvalError: null
          });
          return;
        }

        const requestId = get().replayRequestId + 1;
        set({
          currentSessionId: sessionId,
          currentSessionState: null,
          currentSessionEvents: [],
          currentSessionOutput: null,
          replayStatus: 'loading',
          replayError: null,
          replayRequestId: requestId,
          runStatus: 'idle',
          runError: null,
          approvalStatus: 'idle',
          approvalError: null
        });

        try {
          const replay = await RuntimeClient.getSessionReplay(sessionId);
          if (get().replayRequestId !== requestId || get().currentSessionId !== sessionId) {
            return;
          }

          set({
            currentSessionState: replay.session,
            currentSessionEvents: replay.events,
            currentSessionOutput: replay.output,
            replayStatus: 'success'
          });
        } catch (err) {
          if (get().replayRequestId !== requestId || get().currentSessionId !== sessionId) {
            return;
          }

          set({
            currentSessionId: null,
            currentSessionState: null,
            currentSessionEvents: [],
            currentSessionOutput: null,
            replayStatus: 'idle',
            replayError: null
          });
        }
      },

      runTask: async (prompt: string, options) => {
        if (get().replayStatus === 'loading') {
          return;
        }

        const nextReplayRequestId = get().replayRequestId + 1;
        set({
          runStatus: 'running',
          runError: null,
          currentSessionOutput: null,
          approvalStatus: 'idle',
          approvalError: null
        });
        const effectiveSessionId = options?.sessionId !== undefined ? options.sessionId : get().currentSessionId;
        set({
          replayStatus: 'idle',
          replayError: null,
          replayRequestId: nextReplayRequestId
        });

        const metadata = {
          max_steps: get().maxSteps,
          ...options?.metadata,
          agent: {
            preset: get().agentPreset,
            leader_mode: get().leaderMode,
            model: get().providerModel,
            ...((options?.metadata?.agent as object) || {})
          }
        };

        try {
          const stream = RuntimeClient.runStream({
            prompt,
            session_id: effectiveSessionId,
            metadata: metadata,
          });

          for await (const chunk of stream) {
            set((state) => {
              const newEvents = chunk.event ? [...state.currentSessionEvents, chunk.event] : state.currentSessionEvents;
              return {
                currentSessionState: chunk.session,
                currentSessionEvents: newEvents,
                currentSessionId: chunk.session.session ? chunk.session.session.id : state.currentSessionId,
                currentSessionOutput: chunk.output !== null ? chunk.output : state.currentSessionOutput
              };
            });
          }

          set({ runStatus: 'success' });
          get().loadSessions();
        } catch (err) {
          set({ runStatus: 'error', runError: (err as Error).message });
        }
      },

      resolveApproval: async (decision) => {
        const {
          currentSessionId,
          currentSessionEvents,
          replayStatus,
          runStatus,
          approvalStatus,
          loadSessions
        } = get();

        if (
          !currentSessionId ||
          replayStatus === 'loading' ||
          runStatus === 'running' ||
          approvalStatus === 'submitting'
        ) {
          return;
        }

        const requestId = getPendingApprovalRequestId(currentSessionEvents);
        if (!requestId) {
          set({ approvalStatus: 'error', approvalError: 'No pending approval request found.' });
          return;
        }

        set({ approvalStatus: 'submitting', approvalError: null });

        try {
          const response = await RuntimeClient.resolveApproval(currentSessionId, requestId, decision);
          set({
            currentSessionId: response.session.session.id,
            currentSessionState: response.session,
            currentSessionEvents: response.events,
            currentSessionOutput: response.output,
            replayStatus: 'success',
            replayError: null,
            runStatus: 'idle',
            runError: null,
            approvalStatus: 'success',
            approvalError: null
          });
          await loadSessions();
          set({ approvalStatus: 'idle' });
        } catch (err) {
          set({
            approvalStatus: 'error',
            approvalError: (err as Error).message
          });
        }
      }
    }),
    {
      name: 'app-storage',
      partialize: (state) => ({
        language: state.language,
        agentPreset: state.agentPreset,
        leaderMode: state.leaderMode,
        providerModel: state.providerModel,
        maxSteps: state.maxSteps,
        currentSessionId: state.currentSessionId
      })
    }
  )
);
