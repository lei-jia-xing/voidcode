import type { AsyncStatus } from "../runtime/types";
import { errorMessage } from "../errorMessage";

/**
 * The shell's `AsyncStatus` vocabulary, derived from a query's state rather
 * than stored next to it.
 *
 * A query that is fetching without data is "loading"; one that already carries
 * data stays "success" while it refreshes in the background (the old store
 * flipped such a refresh to "loading" and made every panel flash); a disabled
 * query (no workspace scope, or no selection) is "idle" rather than "loading".
 */
export function asyncStatusFromQuery(query: {
  status: "pending" | "error" | "success";
  fetchStatus: "fetching" | "paused" | "idle";
}): AsyncStatus {
  if (query.status === "error") return "error";
  if (query.status === "success") return "success";
  return query.fetchStatus === "fetching" ? "loading" : "idle";
}

export function queryErrorMessage(query: { error: unknown }): string | null {
  return query.error === null || query.error === undefined
    ? null
    : errorMessage(query.error);
}
