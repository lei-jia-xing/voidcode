import { queryClient } from "./client";
import { queryKeys, type WorkspaceScope } from "./keys";
import { currentWorkspaceScope } from "./scope";

/** The server surfaces a runtime mutation can change. */
export interface MutationRefresh {
  sessions?: boolean;
  status?: boolean;
  review?: boolean;
  backgroundTasks?: boolean;
  debug?: boolean;
  sessionId?: string | null;
}

/**
 * Refresh the server surfaces a completed mutation can have changed.
 *
 * This is invalidation, not an unconditional refetch: entries a mounted
 * component is watching reload at once, entries nothing is rendering are only
 * marked stale and reload the next time they are rendered (`refetchOnMount`
 * keeps that guarantee). The old store refetched all of them eagerly, which
 * meant a completed run always re-read the code review for a panel that might
 * not even be open.
 *
 * Callers pass exactly what their mutation can move — a run settles the session
 * list and the workspace status, not the provider catalog — so an unrelated
 * surface is never disturbed.
 */
export async function refreshAfterMutation(
  refresh: MutationRefresh = {},
  workspaceScope?: WorkspaceScope,
): Promise<void> {
  const scope = workspaceScope ?? currentWorkspaceScope();
  const pending: Promise<void>[] = [];

  if (refresh.sessions) {
    pending.push(
      queryClient.invalidateQueries({ queryKey: queryKeys.sessions(scope) }),
    );
  }
  if (refresh.status) {
    pending.push(
      queryClient.invalidateQueries({ queryKey: queryKeys.status(scope) }),
    );
  }
  if (refresh.review) {
    pending.push(
      queryClient.invalidateQueries({ queryKey: queryKeys.review(scope) }),
      queryClient.invalidateQueries({
        queryKey: queryKeys.reviewDiffRoot(scope),
      }),
    );
  }
  if (refresh.backgroundTasks) {
    pending.push(
      queryClient.invalidateQueries({
        queryKey: queryKeys.backgroundTasksRoot(scope),
      }),
    );
  }
  if (refresh.debug && refresh.sessionId) {
    pending.push(
      queryClient.invalidateQueries({
        queryKey: queryKeys.sessionDebug(scope, refresh.sessionId),
      }),
    );
  }

  await Promise.all(pending);
}

/** What a delegated-task push can have changed since the last read. */
export interface DelegatedTaskRefresh {
  outputId?: string | null;
}

/**
 * Refresh the surfaces a delegated background-task frame can move.
 *
 * The shell's follow stream coalesces a burst of `runtime.background_task_*`
 * frames into one of these per window, which is the one place an SSE frame
 * changes *server data* rather than the run projection — so it invalidates the
 * task list (every session scope in this workspace) and the task output the
 * reader has selected, instead of loading them into the store.
 */
export async function refreshDelegatedTaskSurfaces(
  refresh: DelegatedTaskRefresh = {},
  workspaceScope?: WorkspaceScope,
): Promise<void> {
  const scope = workspaceScope ?? currentWorkspaceScope();
  const pending: Promise<void>[] = [
    queryClient.invalidateQueries({
      queryKey: queryKeys.backgroundTasksRoot(scope),
    }),
  ];
  if (refresh.outputId) {
    pending.push(
      queryClient.invalidateQueries({
        queryKey: queryKeys.taskOutput(scope, refresh.outputId),
      }),
    );
  }
  await Promise.all(pending);
}
