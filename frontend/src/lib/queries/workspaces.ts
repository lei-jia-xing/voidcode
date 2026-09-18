import { useCallback } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";

import { RuntimeClient } from "../runtime/client";
import type { AsyncStatus } from "../runtime/types";
import { queryClient } from "./client";
import { queryKeys } from "./keys";
import { cancelWorkspaceRequests, currentWorkspaceScope } from "./scope";
import { queryErrorMessage } from "./state";

/**
 * The workspace registry: which workspace is open, and which can be opened.
 *
 * Unscoped on purpose: this is the payload that *produces* the scope every other
 * key is built from.
 */
export function useWorkspacesQuery() {
  return useQuery({
    queryKey: queryKeys.workspaceRegistry(),
    queryFn: ({ signal }) => RuntimeClient.listWorkspaces(signal),
  });
}

export interface WorkspaceSwitch {
  switchTo: (path: string) => Promise<void>;
  status: AsyncStatus;
  error: string | null;
}

// Newest switch wins: a slower POST that a newer switch superseded must not
// write its snapshot over the newer one.
let workspaceSwitchToken = 0;

/**
 * Open (or switch to) a workspace.
 *
 * The switch is where the workspace-isolation rules meet:
 *
 * 1. `prepare` runs first, so the shell drops the previous workspace's client
 *    state (selected session, child view, run) before anything else moves.
 * 2. Every in-flight request of the superseded scope is *cancelled* — aborted
 *    through the query's `AbortSignal` — so a slow answer for the old workspace
 *    is torn down instead of being decoded and discarded. Nothing waits for it.
 * 3. The registry entry is replaced with the runtime's answer, which re-keys
 *    every scoped query: the shell then reads the new workspace's (empty) keys,
 *    and the old workspace's cached payloads are unreachable by construction.
 *    They are left in the cache rather than removed, because removing entries
 *    whose observers are still mounted for one more render would make those
 *    observers re-create and re-fetch the superseded keys.
 */
export function useSwitchWorkspace(prepare?: () => void): WorkspaceSwitch {
  const mutation = useMutation({
    mutationFn: (path: string) => RuntimeClient.openWorkspace(path),
    onMutate: async () => {
      const previousScope = currentWorkspaceScope();
      const token = workspaceSwitchToken + 1;
      workspaceSwitchToken = token;
      prepare?.();
      await cancelWorkspaceRequests(previousScope);
      return { previousScope, token };
    },
    onSuccess: (snapshot, _path, context) => {
      // A slower switch that a newer one superseded must not write its snapshot,
      // and the registry must still be the one this switch started from.
      if (context === undefined || context.token !== workspaceSwitchToken)
        return;
      if (context.previousScope !== currentWorkspaceScope()) return;
      queryClient.setQueryData(queryKeys.workspaceRegistry(), snapshot);
    },
  });

  // Stable identity: the shell hands this to a memoized modal.
  const { mutateAsync } = mutation;
  const switchTo = useCallback(
    async (path: string) => {
      await mutateAsync(path);
    },
    [mutateAsync],
  );

  return {
    switchTo,
    status: mutation.isPending
      ? "loading"
      : mutation.isError
        ? "error"
        : mutation.isSuccess
          ? "success"
          : "idle",
    error: queryErrorMessage(mutation),
  };
}
