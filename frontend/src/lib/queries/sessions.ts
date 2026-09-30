import { useQuery } from "@tanstack/react-query";

import { RuntimeClient } from "../runtime/client";
import { queryKeys, type WorkspaceScope } from "./keys";

/**
 * The workspace's session list.
 *
 * Sessions are stored with a workspace id, so the list is scoped like every
 * other runtime payload: after a switch the shell reads the new workspace's key,
 * which starts empty, and never the previous workspace's sessions.
 */
export function useSessionsQuery(scope: WorkspaceScope) {
  return useQuery({
    queryKey: queryKeys.sessions(scope),
    queryFn: ({ signal }) => RuntimeClient.listSessions(signal),
    enabled: scope !== null,
  });
}

/** The context panel's debug snapshot for one session. */
export function useSessionDebugQuery(
  scope: WorkspaceScope,
  sessionId: string | null,
) {
  return useQuery({
    // The key is a placeholder while the query is disabled; the runtime's own
    // answer is only ever stored under a real session id.
    queryKey: queryKeys.sessionDebug(scope, sessionId ?? ""),
    queryFn: ({ signal }) =>
      RuntimeClient.getSessionDebug(sessionId as string, signal),
    enabled: scope !== null && sessionId !== null,
  });
}

export function useSessionEntriesQuery(
  scope: WorkspaceScope,
  sessionId: string | null,
  enabled: boolean,
) {
  return useQuery({
    queryKey: queryKeys.sessionEntries(scope, sessionId ?? ""),
    queryFn: ({ signal }) =>
      RuntimeClient.getSessionEntries(sessionId as string, signal),
    enabled: enabled && scope !== null && sessionId !== null,
  });
}
