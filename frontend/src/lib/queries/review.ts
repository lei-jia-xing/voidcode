import { useQuery } from "@tanstack/react-query";

import { RuntimeClient } from "../runtime/client";
import type { WorkspaceReviewSnapshot } from "../runtime/types";
import { queryKeys, type WorkspaceScope } from "./keys";

export function useReviewQuery(scope: WorkspaceScope) {
  return useQuery({
    queryKey: queryKeys.review(scope),
    queryFn: ({ signal }) => RuntimeClient.getReview(signal),
    enabled: scope !== null,
  });
}

/**
 * The diff of one selected path.
 *
 * The selection itself is client state (which path the reader picked); the
 * payload is server data keyed by that selection, so switching files is a key
 * change: the previous file's diff is neither shown nor re-fetched under the new
 * path's key, and a superseded request for the file the reader left is aborted
 * by the observer unmounting.
 */
export function useReviewDiffQuery(scope: WorkspaceScope, path: string | null) {
  return useQuery({
    queryKey: queryKeys.reviewDiff(scope, path ?? ""),
    queryFn: ({ signal }) =>
      RuntimeClient.getReviewDiff(path as string, signal),
    enabled: scope !== null && path !== null && path.length > 0,
  });
}

/**
 * The path whose diff is shown: the reader's pick while the reload still
 * contains it, otherwise the first changed file (or the first file in the tree).
 *
 * Derived at render time so a review reload cannot leave a selection pointing at
 * a file that no longer changed — the old store had to re-select inside every
 * refresh.
 */
export function resolveSelectedReviewPath(
  snapshot: WorkspaceReviewSnapshot | null,
  selectedPath: string | null,
): string | null {
  if (snapshot === null) return null;
  if (selectedPath && reviewSnapshotContainsPath(snapshot, selectedPath)) {
    return selectedPath;
  }
  return (
    snapshot.changed_files[0]?.path ?? firstTreeFilePath(snapshot.tree) ?? null
  );
}

function reviewSnapshotContainsPath(
  snapshot: WorkspaceReviewSnapshot,
  path: string,
): boolean {
  return (
    snapshot.changed_files.some((item) => item.path === path) ||
    reviewTreeContainsPath(snapshot.tree, path)
  );
}

function reviewTreeContainsPath(
  nodes: WorkspaceReviewSnapshot["tree"],
  targetPath: string,
): boolean {
  for (const node of nodes) {
    if (node.path === targetPath) return true;
    if (reviewTreeContainsPath(node.children, targetPath)) return true;
  }
  return false;
}

function firstTreeFilePath(
  nodes: WorkspaceReviewSnapshot["tree"],
): string | null {
  for (const node of nodes) {
    if (node.kind === "file") return node.path;
    const childPath = firstTreeFilePath(node.children);
    if (childPath) return childPath;
  }
  return null;
}
