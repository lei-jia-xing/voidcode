import { useCallback } from "react";
import { useMutation, useQueries, useQuery } from "@tanstack/react-query";

import { RuntimeClient } from "../runtime/client";
import type {
  AsyncStatus,
  ProviderModelsResult,
  ProviderSummary,
  ProviderValidationResult,
} from "../runtime/types";
import { errorMessage } from "../errorMessage";
import { queryClient } from "./client";
import { queryKeys, type WorkspaceScope } from "./keys";
import { currentWorkspaceScope } from "./scope";

/** Providers plus the model catalog of every configured provider. */
export interface ProviderCatalog {
  providers: ProviderSummary[];
  models: Record<string, ProviderModelsResult>;
}

/**
 * One query for the whole provider catalog.
 *
 * The catalog is a single runtime answer: `/api/providers` names the providers
 * and `/api/providers/{name}/models` describes each configured one, and the
 * shell needs the pair together (the composer resolves the selected model
 * against the catalog, the settings panel lists it). Fetching them as one entry
 * also makes "reload providers" and the settings-update invalidation one
 * operation instead of N.
 */
const EMPTY_PROVIDER_CATALOG: ProviderCatalog = { providers: [], models: {} };

/**
 * The provider catalog as the store reads it, out of the cache.
 *
 * The store builds a run request from the very catalog the composer renders, so
 * it reads the same entry instead of keeping a copy of providers and model
 * metadata beside it. An unloaded catalog (no workspace yet) is an empty one,
 * which is what the request builder already handled as "no model metadata known".
 */
export function readProviderCatalog(
  workspaceScope?: WorkspaceScope,
): ProviderCatalog {
  return (
    queryClient.getQueryData<ProviderCatalog>(
      queryKeys.providerCatalog(workspaceScope ?? currentWorkspaceScope()),
    ) ?? EMPTY_PROVIDER_CATALOG
  );
}

export function useProviderCatalogQuery(scope: WorkspaceScope) {
  return useQuery({
    queryKey: queryKeys.providerCatalog(scope),
    queryFn: async ({ signal }): Promise<ProviderCatalog> => {
      const providers = await RuntimeClient.listProviders(signal);
      const entries = await Promise.all(
        providers
          .filter((provider) => provider.configured)
          .map(
            async (provider) =>
              [
                provider.name,
                await RuntimeClient.listProviderModels(provider.name, signal),
              ] as const,
          ),
      );
      return { providers, models: Object.fromEntries(entries) };
    },
    enabled: scope !== null,
  });
}

/**
 * Qualify a bare model alias against the catalog, or return it unchanged.
 *
 * The runtime needs `provider/model` to route a run, while the stored preference
 * may be a bare alias the catalog happens to own. This is a derivation from the
 * catalog (unqualified preference in, routable reference out), so it happens
 * where the value is used — at send time in the store and at render time in the
 * composer — instead of being written back over the user's preference.
 */
export function resolveProviderModelReference(
  model: string,
  providers: ProviderSummary[],
  models: Record<string, ProviderModelsResult>,
): string {
  if (!model || model.includes("/")) {
    return model;
  }

  const currentProviderName = providers.find(
    (provider) => provider.current && provider.configured,
  )?.name;
  if (
    currentProviderName &&
    (models[currentProviderName]?.models ?? []).includes(model)
  ) {
    return `${currentProviderName}/${model}`;
  }

  const matchingProviderNames = Object.entries(models)
    .filter(([, result]) => result.models.includes(model))
    .map(([providerName]) => providerName);
  if (matchingProviderNames.length === 1) {
    return `${matchingProviderNames[0]}/${model}`;
  }

  return model;
}

export interface ProviderValidationView {
  results: Record<string, ProviderValidationResult>;
  status: Record<string, AsyncStatus>;
  error: Record<string, string | null>;
  validate: (providerName: string) => void;
}

/**
 * Credential validation results, one cache entry per provider.
 *
 * Validating is a POST, so the entries are write-only: the mutation writes its
 * answer into the cache and the entries never fetch. They stay *observable*
 * while disabled, which is what gives the settings panel one live result per
 * provider without a second copy in client state — and what lets a settings
 * update clear them all with one prefix removal.
 */
export function useProviderValidation(
  scope: WorkspaceScope,
  providerNames: string[],
): ProviderValidationView {
  const validations = useQueries({
    queries: providerNames.map((providerName) => ({
      queryKey: queryKeys.providerValidation(scope, providerName),
      queryFn: (): Promise<ProviderValidationResult | null> =>
        Promise.resolve(null),
      enabled: false,
      staleTime: Infinity,
    })),
  });

  const mutation = useMutation({
    mutationFn: (providerName: string) =>
      RuntimeClient.validateProviderCredentials(providerName),
    onSuccess: (result, providerName) => {
      // The result is recorded under the scope the panel was reading: a switch
      // during the POST writes to the workspace it was issued in, never to the
      // one that replaced it.
      queryClient.setQueryData(
        queryKeys.providerValidation(scope, providerName),
        result,
      );
    },
  });

  const results: Record<string, ProviderValidationResult> = {};
  const status: Record<string, AsyncStatus> = {};
  const error: Record<string, string | null> = {};

  providerNames.forEach((providerName, index) => {
    const result = validations[index]?.data ?? null;
    const pending = mutation.isPending && mutation.variables === providerName;
    const failed = mutation.isError && mutation.variables === providerName;
    if (result) results[providerName] = result;
    if (pending) {
      status[providerName] = "loading";
    } else if (failed) {
      status[providerName] = "error";
    } else if (result) {
      status[providerName] = result.ok ? "success" : "error";
    } else {
      status[providerName] = "idle";
    }
    error[providerName] = pending
      ? null
      : failed
        ? errorMessage(mutation.error)
        : result && !result.ok
          ? result.message
          : null;
  });

  const { mutate } = mutation;
  const validate = useCallback(
    (providerName: string) => {
      mutate(providerName);
    },
    [mutate],
  );

  return { results, status, error, validate };
}
