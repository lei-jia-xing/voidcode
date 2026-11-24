import { useMemo, useState } from "react";
import type React from "react";
import { useTranslation } from "react-i18next";
import type { CommandDescriptor } from "../../lib/core/types";
import {
  matchNamespace,
  matchSlashPath,
  slashDispatchable,
  slashPathOf,
} from "./useCatalog";
import type { SessionScope } from "./useSession";

/**
 * Owns the input composer: the prompt draft, slash autocomplete state, slash
 * dispatch (longest registered public path wins; builtins never shadow an
 * admitted catalog command), and normal run prompts.
 */
export function useComposer({
  activeSessionId,
  scope,
  isRunning,
  upgradeFenced,
  catalogRevision,
  catalogCommands,
  slashCommands,
  setStatusMessage,
  createSession,
  cancelRun,
  runPrompt,
  refreshViews,
  prepareCommandInput,
  handleViewAction,
  maybeShowSlashForm,
  orphanCommandInput,
}: {
  activeSessionId: string;
  scope: SessionScope | null;
  isRunning: boolean;
  upgradeFenced: boolean;
  catalogRevision: string;
  catalogCommands: CommandDescriptor[];
  slashCommands: CommandDescriptor[];
  setStatusMessage: (message: string) => void;
  createSession: () => Promise<void>;
  cancelRun: () => Promise<void>;
  runPrompt: (text: string) => Promise<boolean>;
  refreshViews: (scope: SessionScope) => void;
  prepareCommandInput: (
    cmd: string,
    revisionId: string,
    args?: Record<string, unknown>,
  ) => Promise<void>;
  handleViewAction: (
    actionName: string,
    args: Record<string, unknown>,
    revisionId: string,
    prepareInput?: boolean,
  ) => Promise<void>;
  maybeShowSlashForm: (
    descriptor: CommandDescriptor,
    scope: SessionScope,
  ) => boolean;
  orphanCommandInput: () => void;
}) {
  const { t } = useTranslation();
  const [promptInput, setPromptInput] = useState<string>("");
  const [suggestionIndex, setSuggestionIndex] = useState(0);
  const [completionDismissed, setCompletionDismissed] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const text = promptInput.trim();
    if (!text || !activeSessionId || isRunning) return;
    if (upgradeFenced) {
      // Never queue a prompt behind a fence: the user re-sends after reconnect.
      setStatusMessage(t("core.statusUpgradePending"));
      return;
    }
    setPromptInput("");
    if (text.startsWith("/")) {
      // Slash dispatch matches the longest registered public path (`/goal pause`
      // wins over `/goal`); the tail after it is the verbatim argument string.
      const body = text.slice(1).trimStart();
      const tokens = body.split(/\s+/).filter((token) => token !== "");
      const match =
        tokens.length > 0
          ? matchSlashPath(slashDispatchable(catalogCommands), tokens)
          : null;

      // Admitted plugin commands always win: local controls never shadow a
      // catalog command that happens to share the name.
      if (match === null) {
        const cmd = tokens[0] ?? "";
        if (cmd === "cancel") {
          await cancelRun();
          return;
        }
        if (cmd === "views") {
          if (scope) refreshViews(scope);
          return;
        }
        if (cmd === "new") {
          await createSession();
          return;
        }
        const namespace = matchNamespace(slashCommands, tokens);
        setStatusMessage(
          namespace
            ? t("core.statusCommandNamespace", {
                name: body,
                path: namespace.path,
                names: namespace.children.join(", "),
              })
            : t("core.statusUnknownCommand", { name: body }),
        );
        return;
      }
      if ("ambiguous" in match) {
        setStatusMessage(t("core.statusUnknownCommand", { name: body }));
        return;
      }

      const descriptor = match.descriptor;
      const cmd = descriptor.name;
      // Consume the matched tokens, then one whitespace run; the rest (JSON,
      // paths, dots) stays verbatim.
      let rest = body;
      for (let index = 0; index < match.length; index++) {
        rest = rest.slice(tokens[index].length).trimStart();
      }
      const argStr = rest.trim();

      if (!argStr && descriptor.has_input) {
        await prepareCommandInput(cmd, catalogRevision);
        return;
      }

      const schema = descriptor.argument_schema ?? {};
      const properties =
        schema.properties !== null && typeof schema.properties === "object"
          ? (schema.properties as Record<string, { type?: string }>)
          : {};
      const required = Array.isArray(schema.required)
        ? schema.required.filter((r): r is string => typeof r === "string")
        : [];

      let parsedArgs: Record<string, unknown>;
      if (
        !argStr &&
        required.length > 0 &&
        Object.keys(properties).length > 0 &&
        scope &&
        maybeShowSlashForm(descriptor, scope)
      ) {
        return;
      }
      if (!argStr) {
        parsedArgs = {};
      } else if (
        descriptor.text_argument &&
        !argStr.trimStart().startsWith("{")
      ) {
        parsedArgs = { [descriptor.text_argument]: argStr };
      } else if (
        Object.keys(properties).length === 1 &&
        required.length === 1 &&
        properties[required[0]]?.type === "string"
      ) {
        parsedArgs = { [required[0]]: argStr };
      } else {
        try {
          parsedArgs = JSON.parse(argStr) as Record<string, unknown>;
        } catch {
          if (Object.keys(properties).length === 1) {
            const propName = Object.keys(properties)[0];
            parsedArgs = { [propName]: argStr };
          } else {
            setStatusMessage(t("core.statusInvalidJsonArgs", { args: argStr }));
            return;
          }
        }
      }
      await handleViewAction(cmd, parsedArgs, catalogRevision);
    } else {
      // Normal run prompt. Definite non-dispatch (fence) restores the exact
      // draft the user sent — but never overwrites a newer draft typed while
      // it was in flight, and never auto-submits.
      const dispatched = await runPrompt(text);
      if (!dispatched) {
        setPromptInput((current) => (current.trim() === "" ? text : current));
      }
    }
  };

  const handlePromptChange = (value: string) => {
    orphanCommandInput();
    setPromptInput(value);
    setSuggestionIndex(0);
    setCompletionDismissed(false);
  };

  const selectSuggestion = (cmd: string) => {
    setPromptInput(`/${cmd} `);
    setSuggestionIndex(0);
  };

  // Autocomplete hints
  const slashSuggestions = useMemo(() => {
    if (
      completionDismissed ||
      !promptInput.startsWith("/") ||
      promptInput.includes("\n")
    ) {
      return [];
    }
    const query = promptInput.slice(1).toLowerCase();
    const builtins = ["cancel", "views", "new"];
    const fromCatalog = slashCommands.map(slashPathOf);
    const all = Array.from(new Set([...builtins, ...fromCatalog]));
    const split = query.lastIndexOf(" ");
    const base = split < 0 ? "" : query.slice(0, split + 1);
    const partial = query.slice(split + 1);
    return Array.from(
      new Set(
        all
          .filter(
            (name) =>
              name.toLowerCase().startsWith(base) &&
              name.slice(base.length).toLowerCase().startsWith(partial),
          )
          .map((name) => base + name.slice(base.length).split(" ")[0]),
      ),
    )
      .filter(Boolean)
      .sort();
  }, [promptInput, slashCommands, completionDismissed]);

  const handlePromptKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (
      !slashSuggestions.length ||
      e.currentTarget.selectionStart !== promptInput.length ||
      e.currentTarget.selectionEnd !== promptInput.length
    )
      return;
    if (e.key === "Escape") {
      e.preventDefault();
      setCompletionDismissed(true);
    } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      setSuggestionIndex(
        (index) =>
          (index + (e.key === "ArrowDown" ? 1 : -1) + slashSuggestions.length) %
          slashSuggestions.length,
      );
    } else if (e.key === "Tab" || e.key === "Enter") {
      const selected =
        slashSuggestions[suggestionIndex % slashSuggestions.length];
      const exact = promptInput === `/${selected}`;
      const executable =
        slashCommands.some((entry) => slashPathOf(entry) === selected) ||
        ["cancel", "views", "new"].includes(selected);
      if (e.key === "Tab" || !exact || !executable) {
        e.preventDefault();
        const children = slashCommands.some((entry) =>
          slashPathOf(entry).startsWith(selected + " "),
        );
        setPromptInput(`/${selected}${children || e.key === "Tab" ? " " : ""}`);
        setSuggestionIndex(0);
        setCompletionDismissed(!children);
      }
    }
  };

  return {
    promptInput,
    suggestionIndex,
    slashSuggestions,
    handleSubmit,
    handlePromptChange,
    handlePromptKeyDown,
    selectSuggestion,
  };
}
