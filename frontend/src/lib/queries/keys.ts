/**
 * Typed query keys for every plain-HTTP payload the shell reads.
 *
 * Workspace isolation
 * -------------------
 * The runtime is leased per workspace: `/api/workspaces/open` swaps the process
 * runtime, so providers, agents, commands, status, review, sessions, settings
 * and tasks are all answers *about the active workspace*. Every such
 * key therefore carries the active workspace path right after the `workspace`
 * root, which makes a workspace switch a *key change* instead of an overwrite:
 *
 * - a payload fetched for workspace A is stored under `workspace/A/...`, so a
 *   component scoped to B (its own key) can never read it, not even for the tick
 *   before B's own fetch lands;
 * - `workspaceScope(scope)` is the key *prefix* of every scoped payload, so the
 *   switch action can cancel every in-flight request of the superseded
 *   workspace with one `cancelQueries` call;
 * - the workspace registry itself (`/api/workspaces` — the list of openable
 *   workspaces) is not scoped: it is the input that *produces* the scope, and it
 *   lives under a different first segment so the prefix never matches it.
 *
 * Keys are only ever built through this table, so no call site can forget the
 * scope.
 */
export type WorkspaceScope = string | null;

export const queryKeys = {
  /** The workspace registry: current/recent/candidate workspaces. Unscoped. */
  workspaceRegistry: () => ["workspace-registry"] as const,
  /** Prefix of every workspace-scoped payload; used to isolate a switch. */
  workspaceScope: (scope: WorkspaceScope) => ["workspace", scope] as const,

  sessions: (scope: WorkspaceScope) =>
    [...queryKeys.workspaceScope(scope), "sessions"] as const,
  sessionDebug: (scope: WorkspaceScope, sessionId: string) =>
    [...queryKeys.workspaceScope(scope), "session-debug", sessionId] as const,

  /** Providers plus their model catalogs, fetched as one runtime answer. */
  providerCatalog: (scope: WorkspaceScope) =>
    [...queryKeys.workspaceScope(scope), "provider-catalog"] as const,
  /** Prefix of all provider credential validations in one workspace. */
  providerValidationRoot: (scope: WorkspaceScope) =>
    [...queryKeys.workspaceScope(scope), "provider-validation"] as const,
  providerValidation: (scope: WorkspaceScope, providerName: string) =>
    [...queryKeys.providerValidationRoot(scope), providerName] as const,

  agents: (scope: WorkspaceScope) =>
    [...queryKeys.workspaceScope(scope), "agents"] as const,
  commands: (scope: WorkspaceScope) =>
    [...queryKeys.workspaceScope(scope), "commands"] as const,

  status: (scope: WorkspaceScope) =>
    [...queryKeys.workspaceScope(scope), "status"] as const,

  review: (scope: WorkspaceScope) =>
    [...queryKeys.workspaceScope(scope), "review"] as const,
  /** Prefix of every selected file diff in one workspace. */
  reviewDiffRoot: (scope: WorkspaceScope) =>
    [...queryKeys.workspaceScope(scope), "review-diff"] as const,
  reviewDiff: (scope: WorkspaceScope, path: string) =>
    [...queryKeys.workspaceScope(scope), "review-diff", path] as const,

  notifications: (scope: WorkspaceScope) =>
    [...queryKeys.workspaceScope(scope), "notifications"] as const,

  /** Prefix of every background-task list (one entry per session scope). */
  backgroundTasksRoot: (scope: WorkspaceScope) =>
    [...queryKeys.workspaceScope(scope), "background-tasks"] as const,
  /**
   * Background tasks of one session scope. `sessionId === null` is the runtime's
   * global task list; any id is that session's own list, exactly as the two
   * runtime endpoints differ.
   */
  backgroundTasks: (scope: WorkspaceScope, sessionId: string | null) =>
    [...queryKeys.backgroundTasksRoot(scope), sessionId] as const,
  /** One task's output view, shared by the task panel and the child session. */
  taskOutput: (scope: WorkspaceScope, taskId: string) =>
    [...queryKeys.workspaceScope(scope), "task-output", taskId] as const,

  settings: (scope: WorkspaceScope) =>
    [...queryKeys.workspaceScope(scope), "settings"] as const,
} as const;
