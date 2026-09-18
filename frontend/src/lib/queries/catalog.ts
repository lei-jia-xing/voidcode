import { useQuery } from "@tanstack/react-query";

import { RuntimeClient } from "../runtime/client";
import { queryKeys, type WorkspaceScope } from "./keys";

/**
 * The workspace's agent and command catalogs.
 *
 * Both are workspace assets (repo-scoped agent manifests and commands), which is
 * why they carry the workspace scope and why a switch refetches them instead of
 * showing the previous workspace's catalog. `/api/skills` has no client in the
 * shell — nothing renders the skill catalog — so it is deliberately not queried.
 */
export function useAgentsQuery(scope: WorkspaceScope) {
  return useQuery({
    queryKey: queryKeys.agents(scope),
    queryFn: ({ signal }) => RuntimeClient.listAgents(signal),
    enabled: scope !== null,
  });
}

export function useCommandsQuery(scope: WorkspaceScope) {
  return useQuery({
    queryKey: queryKeys.commands(scope),
    queryFn: ({ signal }) => RuntimeClient.listCommands(signal),
    enabled: scope !== null,
  });
}
