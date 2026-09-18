import { useCallback } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";

import { RuntimeClient } from "../runtime/client";
import { queryClient } from "./client";
import { queryKeys, type WorkspaceScope } from "./keys";

export function useRuntimeStatusQuery(scope: WorkspaceScope) {
  return useQuery({
    queryKey: queryKeys.status(scope),
    queryFn: ({ signal }) => RuntimeClient.getStatus(signal),
    enabled: scope !== null,
  });
}

/**
 * Retry the MCP connections and adopt the status the runtime answers with.
 *
 * A POST — it (re)connects servers — so it never retries, and its answer is
 * written into the status entry rather than kept beside it.
 */
export function useRetryMcpConnections(scope: WorkspaceScope) {
  const mutation = useMutation({
    mutationFn: () => RuntimeClient.retryMcpConnections(),
    onSuccess: (snapshot) => {
      queryClient.setQueryData(queryKeys.status(scope), snapshot);
    },
  });

  // `mutate` is stable across renders (TanStack v5 returns a bound closure),
  // so the callback the shell hands to a memoized button keeps its identity.
  const { mutate } = mutation;
  const retry = useCallback(() => {
    mutate();
  }, [mutate]);

  return { retry, isPending: mutation.isPending, error: mutation.error };
}
