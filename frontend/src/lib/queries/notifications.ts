import { useCallback } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";

import { RuntimeClient } from "../runtime/client";
import { queryClient } from "./client";
import { queryKeys, type WorkspaceScope } from "./keys";

export function useNotificationsQuery(scope: WorkspaceScope) {
  return useQuery({
    queryKey: queryKeys.notifications(scope),
    queryFn: ({ signal }) => RuntimeClient.listNotifications(signal),
    enabled: scope !== null,
  });
}

/**
 * Acknowledge one notification, in place.
 *
 * The POST answers with the acknowledged row, so the cache is updated from that
 * answer instead of refetching the list — the stream pushes new notifications
 * through invalidation (see the follow-stream refresh in the shell).
 */
export function useAcknowledgeNotification(scope: WorkspaceScope) {
  const mutation = useMutation({
    mutationFn: (notificationId: string) =>
      RuntimeClient.ackNotification(notificationId),
    onSuccess: (acknowledged) => {
      const key = queryKeys.notifications(scope);
      queryClient.setQueryData(key, (current: unknown) =>
        Array.isArray(current)
          ? current.map((notification) =>
              notification !== null &&
              typeof notification === "object" &&
              "id" in notification &&
              notification.id === acknowledged.id
                ? acknowledged
                : notification,
            )
          : current,
      );
    },
  });

  const { mutate } = mutation;
  const acknowledge = useCallback(
    (notificationId: string) => {
      mutate(notificationId);
    },
    [mutate],
  );

  return { acknowledge, isPending: mutation.isPending };
}
