import { QueryClient } from "@tanstack/react-query";

import { RuntimeClientError } from "../runtime/client";

/**
 * Freshness and retry policy for the whole shell, stated once.
 *
 * Why `staleTime` is not zero: this is a local-first single-user app whose live
 * updates arrive over the SSE run/follow streams, and every mutation that can
 * change a payload invalidates it explicitly (`invalidateQueries` after a run,
 * an approval, an answer, a settings change, a delegated-task frame). Automatic
 * refetching is therefore a *fallback*, not the delivery path — and a window as
 * short as "always stale" would make every panel open, and every re-mount of the
 * child-session panel, fire a request that competes with the stream. 15s keeps
 * those re-mounts free while still bounding how stale an untouched payload can
 * get.
 *
 * Why window focus and reconnect refetching are off: the SSE channels are the
 * push path, and a focus-driven refetch races the stream's own projection — it
 * can only ever replace data the stream is already maintaining. Refetching is
 * driven by mount and by explicit invalidation instead (`refetchOnMount` stays
 * on, so an invalidated or stale payload reloads the next time it is rendered).
 *
 * Why queries retry at most once, and not at all for client errors: the GETs are
 * idempotent reads, so one retry after a transport failure or a 5xx is safe and
 * hides a flaky socket. A 4xx is an answer, not a failure to deliver (a 409
 * conflict, a 404 routing miss), so retrying it would only repeat the same
 * answer. An aborted request is a deliberate cancellation and is never retried.
 *
 * Why mutations never retry: every mutation in this app is an agent-control
 * call — run cancellation, approval and question resolution, workspace open,
 * background-task cancel/retry/steer, settings save, credential validation and
 * MCP retry. A silent retry after a lost response would replay a
 * state transition the runtime may already have applied (double-cancel,
 * double-approve, double-steer), so mutations fail loudly to the caller instead.
 */
export const QUERY_STALE_TIME_MS = 15_000;
export const QUERY_GC_TIME_MS = 5 * 60_000;
export const QUERY_MAX_RETRIES = 1;

export function shouldRetryQuery(
  failureCount: number,
  error: unknown,
): boolean {
  if (failureCount >= QUERY_MAX_RETRIES) return false;
  // A deliberate cancellation (`AbortError` from `fetch`, or a runtime's own
  // `DOMException`) is not a failure. The name is read structurally because a
  // `DOMException` is not always an `Error` instance.
  if (
    typeof error === "object" &&
    error !== null &&
    "name" in error &&
    (error as { name?: unknown }).name === "AbortError"
  ) {
    return false;
  }
  if (error instanceof RuntimeClientError) return error.status >= 500;
  // A rejected `fetch` (offline, connection reset) is worth one retry.
  return true;
}

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: QUERY_STALE_TIME_MS,
        gcTime: QUERY_GC_TIME_MS,
        retry: shouldRetryQuery,
        retryDelay: (attempt) => Math.min(1_000 * 2 ** attempt, 8_000),
        refetchOnWindowFocus: false,
        refetchOnReconnect: false,
        refetchOnMount: true,
      },
      mutations: {
        retry: false,
      },
    },
  });
}

/**
 * The process-wide query client. One cache per shell instance: the store reads
 * workspace-scoped payloads out of it (never out of React state), so both the
 * providers and the store must share this exact instance.
 */
export const queryClient = createQueryClient();
