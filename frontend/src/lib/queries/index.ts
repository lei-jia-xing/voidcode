/**
 * The query layer: every plain-HTTP payload the shell reads.
 *
 * Reads come from here (components call these hooks, the store reads the same
 * entries out of `queryClient`), writes to the cache come from the mutations
 * here and from the store's run/selection lifecycle, and `refreshAfterMutation`
 * is the one place a completed mutation decides which surfaces must reload.
 */
export {
  createQueryClient,
  queryClient,
  QUERY_GC_TIME_MS,
  QUERY_MAX_RETRIES,
  QUERY_STALE_TIME_MS,
  shouldRetryQuery,
} from "./client";
export { cancelWorkspaceRequests, currentWorkspaceScope } from "./scope";
export { asyncStatusFromQuery, queryErrorMessage } from "./state";
export { queryKeys } from "./keys";
export type { WorkspaceScope } from "./keys";
export {
  refreshAfterMutation,
  refreshDelegatedTaskSurfaces,
} from "./invalidation";
export type { DelegatedTaskRefresh, MutationRefresh } from "./invalidation";

export { useSwitchWorkspace, useWorkspacesQuery } from "./workspaces";
export type { WorkspaceSwitch } from "./workspaces";
export {
  readProviderCatalog,
  resolveProviderModelReference,
  useProviderCatalogQuery,
  useProviderValidation,
} from "./providers";
export type { ProviderCatalog, ProviderValidationView } from "./providers";
export { useAgentsQuery, useCommandsQuery } from "./catalog";
export { useRetryMcpConnections, useRuntimeStatusQuery } from "./status";
export {
  resolveSelectedReviewPath,
  useReviewDiffQuery,
  useReviewQuery,
} from "./review";
export { useSessionDebugQuery, useSessionsQuery } from "./sessions";
export {
  backgroundTaskIdFromControlResponse,
  useBackgroundTaskAction,
  useBackgroundTasksQuery,
  useTaskOutputQuery,
} from "./tasks";
export type {
  BackgroundTaskActionRequest,
  BackgroundTaskActionView,
} from "./tasks";
export { useSettingsQuery, useUpdateSettings } from "./settings";
