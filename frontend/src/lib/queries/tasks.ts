import { useCallback } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";

import { RuntimeClient } from "../runtime/client";
import { errorMessage } from "../errorMessage";
import type { AsyncStatus } from "../runtime/types";
import { queryClient } from "./client";
import { queryKeys, type WorkspaceScope } from "./keys";

/**
 * Background tasks of one session scope.
 *
 * `sessionId` mirrors the two runtime endpoints: an id asks for that session's
 * delegated tasks, `null` for the runtime's global list. The id is part of the
 * key, so changing the selection fetches the new scope's list and can never
 * render the previous scope's tasks under the new one.
 */
export function useBackgroundTasksQuery(
  scope: WorkspaceScope,
  sessionId: string | null,
) {
  return useQuery({
    queryKey: queryKeys.backgroundTasks(scope, sessionId),
    queryFn: ({ signal }) =>
      sessionId === null
        ? RuntimeClient.listBackgroundTasks(signal)
        : RuntimeClient.listSessionBackgroundTasks(sessionId, signal),
    enabled: scope !== null,
  });
}

/**
 * One task's output view.
 *
 * `/api/tasks/{id}/output` and `/api/sessions/{id}/delegated-context` answer
 * with the same shape and agree on its content (the runtime derives both from
 * one task result), so the delegated-context probe may seed this same entry when
 * it selects a child session — one key, one payload, whichever surface read it.
 */
export function useTaskOutputQuery(
  scope: WorkspaceScope,
  taskId: string | null,
) {
  return useQuery({
    queryKey: queryKeys.taskOutput(scope, taskId ?? ""),
    queryFn: ({ signal }) =>
      RuntimeClient.getBackgroundTaskOutput(taskId as string, signal),
    enabled: scope !== null && taskId !== null,
  });
}

export type BackgroundTaskActionRequest =
  | { kind: "cancel"; taskId: string }
  | { kind: "retry"; taskId: string }
  | { kind: "steer"; taskId: string; prompt: string };

export interface BackgroundTaskActionView {
  run: (request: BackgroundTaskActionRequest) => Promise<unknown>;
  pendingTaskId: string | null;
  status: AsyncStatus;
  error: string | null;
}

/**
 * Cancel/retry/steer one background task.
 *
 * Each is an agent-control POST, so the mutation never retries: a silent replay
 * could cancel a task twice, or queue the same steering message twice. The task
 * list and the acted task's output are invalidated on success, which is how the
 * panel shows the action's effect (an active task list refetches immediately, a
 * closed panel refetches when it is next opened).
 */
export function useBackgroundTaskAction(
  scope: WorkspaceScope,
): BackgroundTaskActionView {
  const mutation = useMutation({
    mutationFn: async (request: BackgroundTaskActionRequest) => {
      if (request.kind === "cancel") {
        return RuntimeClient.cancelBackgroundTask(request.taskId);
      }
      if (request.kind === "retry") {
        return RuntimeClient.retryBackgroundTask(request.taskId);
      }
      return RuntimeClient.steerBackgroundTask(request.taskId, request.prompt);
    },
    onSuccess: (result, request) => {
      void queryClient.invalidateQueries({
        queryKey: queryKeys.backgroundTasksRoot(scope),
      });
      const actedTaskId = request.taskId;
      const answeredTaskId =
        backgroundTaskIdFromControlResponse(result) ?? actedTaskId;
      void queryClient.invalidateQueries({
        queryKey: queryKeys.taskOutput(scope, actedTaskId),
      });
      if (answeredTaskId !== actedTaskId) {
        void queryClient.invalidateQueries({
          queryKey: queryKeys.taskOutput(scope, answeredTaskId),
        });
      }
    },
  });

  const { mutateAsync } = mutation;
  const run = useCallback(
    (request: BackgroundTaskActionRequest) => mutateAsync(request),
    [mutateAsync],
  );

  return {
    run,
    pendingTaskId: mutation.isPending
      ? (mutation.variables?.taskId ?? null)
      : null,
    status: mutation.isPending
      ? "loading"
      : mutation.isError
        ? "error"
        : "idle",
    error: mutation.error ? errorMessage(mutation.error) : null,
  };
}

/** A retry answers with the task it created; every other control answers in place. */
export function backgroundTaskIdFromControlResponse(
  result: unknown,
): string | null {
  if (result === null || typeof result !== "object" || !("task" in result)) {
    return null;
  }
  const task = result.task;
  if (task === null || typeof task !== "object" || !("id" in task)) return null;
  return typeof task.id === "string" && task.id.length > 0 ? task.id : null;
}
