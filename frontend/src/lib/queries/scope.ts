import type { WorkspaceRegistrySnapshot } from "../runtime/types";
import { queryClient } from "./client";
import { queryKeys, type WorkspaceScope } from "./keys";

/**
 * The active workspace path, read from the registry payload in the query cache.
 *
 * The scope is not mirrored into the store: the cache is its only home. The shell
 * and the store's user-path actions take it as an explicit argument (the shell
 * always knows it and a store action must not depend on a hidden global read);
 * this function is the documented fallback for a caller that cannot receive one
 * (an imperative helper, a non-shell caller). It answers `null` — never a guessed
 * workspace — when the registry has not loaded, and every scoped key then belongs
 * to the `null` scope, so a degraded caller can only ever touch scopes that hold
 * no payload for a real workspace.
 */
export function currentWorkspaceScope(): WorkspaceScope {
  return (
    queryClient.getQueryData<WorkspaceRegistrySnapshot>(
      queryKeys.workspaceRegistry(),
    )?.current?.path ?? null
  );
}

/**
 * Supersede every in-flight request of a workspace, ahead of a switch.
 *
 * Aborting is what makes the switch a real cancellation instead of a "discard
 * the answer later": each in-flight `fetch` rejects with `AbortError`, the
 * awaited cancellation is not a query error, and the superseded payload can
 * never land under the new workspace's keys.
 */
export async function cancelWorkspaceRequests(scope: WorkspaceScope) {
  if (scope === null) return;
  await queryClient.cancelQueries({
    queryKey: queryKeys.workspaceScope(scope),
  });
}
