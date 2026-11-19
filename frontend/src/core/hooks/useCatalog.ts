import { useCallback, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { CoreClient, CommandCatalog } from "../../lib/core/client";

/** Public slash paths use spaces; machine command IDs stay unchanged. */
export function slashPathOf(descriptor: {
  name: string;
  client_syntax?: string | null;
}): string {
  const syntax = descriptor.client_syntax;
  const prefix =
    typeof syntax === "string" && syntax.startsWith("/")
      ? syntax.slice(1).trim()
      : "";
  return (prefix || descriptor.name).replace(/\./g, " ");
}

/** Human argument hints derived from the admitted schema. */
export function argumentHints(
  schema: Record<string, unknown> | undefined,
): string {
  const properties = schema?.properties;
  if (!properties || typeof properties !== "object") return "";
  const required = Array.isArray(schema?.required) ? schema.required : [];
  return Object.keys(properties)
    .map((name) => (required.includes(name) ? `<${name}>` : `[${name}]`))
    .join(" ");
}

/** Public slash path for display: the catalog's preferred spelling when the
name is admitted, else the canonical name with dots as spaces. */
export function displaySlashPath(
  commands: Array<{ name: string; client_syntax?: string | null }>,
  canonicalName: string,
): string {
  const descriptor = commands.find((c) => c.name === canonicalName);
  return descriptor
    ? slashPathOf(descriptor)
    : canonicalName.replace(/\./g, " ");
}

/** Longest registered public path that matches the input tokens, or null on
no match or on an equally-longest ambiguous collision (never silent first). */
export function matchSlashPath<
  T extends { name: string; client_syntax?: string | null },
>(
  commands: T[],
  tokens: string[],
):
  { descriptor: T; path: string; length: number } | { ambiguous: true } | null {
  let best: { descriptor: T; path: string; length: number } | null = null;
  let ambiguous = false;
  for (const descriptor of commands) {
    const path = slashPathOf(descriptor);
    const parts = path.split(" ").filter((part) => part !== "");
    if (parts.length === 0 || parts.length > tokens.length) continue;
    if (!parts.every((part, index) => tokens[index] === part)) continue;
    if (best === null || parts.length > best.length) {
      best = { descriptor, path, length: parts.length };
      ambiguous = false;
    } else if (parts.length === best.length) {
      ambiguous = true;
    }
  }
  if (best === null) return null;
  return ambiguous ? { ambiguous: true } : best;
}

/**
 * Owns the command catalog (which travels with the revision it was captured
 * under: every command it advertises is dispatched with exactly that
 * revision), its newest-wins refetch, and the derived slash/shortcut indexes.
 * The catalog is transport-global: it resets on client change, never on
 * session switch.
 */
export function useCatalog({
  getClient,
  transportKey,
  onStatus,
}: {
  getClient: () => CoreClient;
  transportKey: [string, string | undefined];
  onStatus: (message: string) => void;
}) {
  const { t } = useTranslation();
  const [catalog, setCatalog] = useState<CommandCatalog>({
    revisionId: "",
    commands: [],
  });
  // Only the newest catalog snapshot may render, so a reply issued before an
  // activation commit can never overwrite the one issued after it.
  const catalogOpRef = useRef<AbortController | null>(null);
  const [boundTransport, setBoundTransport] = useState(transportKey);

  // Render-phase transport adjustment: no paint with the previous
  // transport's catalog; abort + refetch stay in the layout below.
  if (boundTransport !== transportKey) {
    setBoundTransport(transportKey);
    setCatalog({ revisionId: "", commands: [] });
  }

  // Refetch the catalog after an activation commit: descriptors and revision are
  // replaced together, so no displayed command keeps the old generation.
  const refreshCatalog = useCallback(() => {
    catalogOpRef.current?.abort();
    const op = new AbortController();
    catalogOpRef.current = op;
    getClient()
      .commandCatalog()
      .then((snapshot) => {
        if (op.signal.aborted) return; // a newer catalog won
        setCatalog(snapshot);
      })
      .catch((err) => {
        if (op.signal.aborted) return;
        onStatus(t("core.statusCatalogFailed", { message: String(err) }));
      });
  }, [getClient, onStatus, t]);

  /** Activation commit: clear immediately, then refetch together with views. */
  const handleActivationCommitted = useCallback(() => {
    setCatalog({ revisionId: "", commands: [] });
    refreshCatalog();
  }, [refreshCatalog]);

  /** Server identity changed under us: nothing stays actionable. */
  const invalidate = useCallback(() => {
    setCatalog({ revisionId: "", commands: [] });
  }, []);

  // The previous transport's catalog must not stay actionable under the new
  // one: strand its in-flight read. Fetches are owned by the session
  // bootstrap (mount + transport change), reconcile, and activation commits —
  // the same issue points as before the split.
  useLayoutEffect(() => {
    catalogOpRef.current?.abort();
  }, [boundTransport]);

  const slashCommands = useMemo(
    () => catalog.commands.filter((entry) => entry.slash_visible !== false),
    [catalog],
  );

  // Catalog-declared shortcut actions, computed once per catalog (never per
  // render/keystroke). `actions` lists every declared shortcut command so all
  // stay clickable as buttons; a chord with 2+ claimants binds no key and the
  // conflict is shown, never silently first-wins. This round binds no browser
  // keyboard chord: entry is buttons only.
  const shortcutEntries = useMemo(() => {
    const claimants = new Map<string, typeof catalog.commands>();
    for (const entry of catalog.commands) {
      const chord = entry.default_shortcut ?? null;
      if (typeof chord !== "string" || !chord) continue;
      const list = claimants.get(chord) ?? [];
      list.push(entry);
      claimants.set(chord, list);
    }
    const actions: Array<{
      chord: string;
      entry: (typeof catalog.commands)[number];
      conflicted: boolean;
    }> = [];
    const conflicts: Array<{ chord: string; names: string[] }> = [];
    for (const [chord, entries] of claimants) {
      const conflicted = entries.length > 1;
      for (const entry of entries) actions.push({ chord, entry, conflicted });
      if (conflicted)
        conflicts.push({ chord, names: entries.map((e) => e.name) });
    }
    actions.sort(
      (a, b) =>
        a.chord.localeCompare(b.chord) ||
        a.entry.name.localeCompare(b.entry.name),
    );
    conflicts.sort((a, b) => a.chord.localeCompare(b.chord));
    return { actions, conflicts };
  }, [catalog]);

  return {
    catalog,
    refreshCatalog,
    handleActivationCommitted,
    invalidate,
    slashCommands,
    shortcutEntries,
  };
}
