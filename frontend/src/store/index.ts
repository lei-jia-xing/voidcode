import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import { RuntimeClient } from '../lib/runtime/client';
import { ApprovalDecision, StoredSessionSummary, SessionState, EventEnvelope, RuntimeSettings, RuntimeSettingsUpdate } from '../lib/runtime/types';

interface AppState {
  language: 'en' | 'zh-CN';

  agentPreset: 'leader';
  providerModel: string;

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

  settings: RuntimeSettings | null;
  settingsStatus: 'idle' | 'loading' | 'success' | 'error';
  settingsError: string | null;

  setLanguage: (lang: 'en' | 'zh-CN') => void;
  setAgentPreset: (preset: 'leader') => void;
  setProviderModel: (model: string) => void;
  loadSessions: () => Promise<void>;
  selectSession: (sessionId: string) => Promise<void>;
  runTask: (
    prompt: string,
    options?: {
      sessionId?: string | null;
      metadata?: {
        skills?: string[];
        provider_stream?: boolean;
        [key: string]: unknown;
      };
    }
  ) => Promise<void>;
  resolveApproval: (decision: ApprovalDecision) => Promise<void>;
  loadSettings: () => Promise<void>;
  updateSettings: (settings: RuntimeSettingsUpdate) => Promise<void>;
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
      providerModel: 'opencode-go/glm-5.1',

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

      settings: null,
      settingsStatus: 'idle',
      settingsError: null,

      setLanguage: (language) => set({ language }),
      setAgentPreset: (agentPreset) => set({ agentPreset }),
      setProviderModel: (providerModel) => set({ providerModel }),

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

        const rawMetadata = options?.metadata ?? {};
        const rawAgentMetadata =
          rawMetadata.agent && typeof rawMetadata.agent === 'object'
            ? (rawMetadata.agent as Record<string, unknown>)
            : {};
        const { max_steps: _ignoredMaxSteps, agent: _ignoredAgent, ...forwardMetadata } = rawMetadata;
        const { leader_mode: _ignoredLeaderMode, ...forwardAgentMetadata } = rawAgentMetadata;

        const metadata = {
          ...forwardMetadata,
          agent: {
            preset: get().agentPreset,
            model: get().providerModel,
            ...forwardAgentMetadata
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
      },

      loadSettings: async () => {
        set({ settingsStatus: 'loading', settingsError: null });
        try {
          const settings = await RuntimeClient.getSettings();
          set({ settings, settingsStatus: 'success' });
          if (settings.model) {
            set({ providerModel: settings.model });
          }
        } catch (err) {
          set({ settingsStatus: 'error', settingsError: (err as Error).message });
        }
      },

      updateSettings: async (settings) => {
        set({ settingsStatus: 'loading', settingsError: null });
        try {
          const updated = await RuntimeClient.updateSettings(settings);
          set({ settings: updated, settingsStatus: 'success' });
          if (updated.model) {
            set({ providerModel: updated.model });
          }
        } catch (err) {
          set({ settingsStatus: 'error', settingsError: (err as Error).message });
        }
      }
    }),
    {
      name: 'app-storage',
      partialize: (state) => ({
        language: state.language,
        agentPreset: state.agentPreset,
        providerModel: state.providerModel,
        currentSessionId: state.currentSessionId
      })
    }
  )
);
