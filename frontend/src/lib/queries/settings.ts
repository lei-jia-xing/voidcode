import { useCallback } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";

import { RuntimeClient } from "../runtime/client";
import type { RuntimeSettingsUpdate } from "../runtime/types";
import { queryClient } from "./client";
import { queryKeys, type WorkspaceScope } from "./keys";

export function useSettingsQuery(scope: WorkspaceScope) {
  return useQuery({
    queryKey: queryKeys.settings(scope),
    queryFn: ({ signal }) => RuntimeClient.getSettings(signal),
    enabled: scope !== null,
  });
}

/**
 * Save runtime-owned settings.
 *
 * The save can change what the runtime reports: providers (a key or model
 * default), runtime status, and every credential validation result recorded for
 * the previous configuration. The mutation writes its answer into the settings
 * entry and invalidates those three surfaces; a settings update is a POST, so it
 * never retries.
 */
export function useUpdateSettings(scope: WorkspaceScope) {
  const mutation = useMutation({
    mutationFn: (settings: RuntimeSettingsUpdate) =>
      RuntimeClient.updateSettings(settings),
    onSuccess: (updated) => {
      queryClient.setQueryData(queryKeys.settings(scope), updated);
      queryClient.removeQueries({
        queryKey: queryKeys.providerValidationRoot(scope),
      });
      void queryClient.invalidateQueries({
        queryKey: queryKeys.providerCatalog(scope),
      });
      void queryClient.invalidateQueries({ queryKey: queryKeys.status(scope) });
    },
  });

  const { mutateAsync } = mutation;
  const save = useCallback(
    async (settings: RuntimeSettingsUpdate) => {
      await mutateAsync(settings);
    },
    [mutateAsync],
  );

  return {
    save,
    status: mutation.isPending
      ? "loading"
      : mutation.isError
        ? "error"
        : mutation.isSuccess
          ? "success"
          : "idle",
    error: mutation.error,
  };
}
