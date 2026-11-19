import { useState } from "react";
import { useTranslation } from "react-i18next";

/**
 * Bearer-token entry that never touches the URL. The token is pasted here
 * (from the `serve` console line for local opens, or from the operator for a
 * remote attach) and kept in per-tab session storage — out of history,
 * Referer headers, and server access logs.
 */
export function TokenGate({
  serverLabel,
  onSubmit,
}: {
  serverLabel: string;
  onSubmit: (token: string) => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState("");
  return (
    <div
      data-testid="token-gate"
      role="region"
      aria-label={t("core.tokenGateTitle")}
      className="px-4 py-2 bg-amber-950/60 border-b border-amber-800 text-[11px] text-amber-200"
    >
      <div className="font-semibold uppercase tracking-wider">
        {t("core.tokenGateTitle")}
      </div>
      <div className="font-mono truncate">
        {t("core.tokenGateHint", {
          server: serverLabel || t("core.noneLabel"),
        })}
      </div>
      <form
        className="flex gap-2 mt-1"
        onSubmit={(e) => {
          e.preventDefault();
          onSubmit(draft);
          setDraft("");
        }}
      >
        <input
          data-testid="token-gate-input"
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={t("core.tokenGatePlaceholder")}
          className="flex-1 px-2 py-1 rounded bg-neutral-900 border border-neutral-700 text-xs text-neutral-100 placeholder-neutral-500 focus:outline-none focus:border-cyan-500"
        />
        <button
          data-testid="token-gate-submit"
          type="submit"
          disabled={draft.trim() === ""}
          className="px-3 py-1 rounded bg-cyan-600 hover:bg-cyan-500 disabled:opacity-40 text-white text-xs font-medium"
        >
          {t("core.tokenGateConnect")}
        </button>
      </form>
    </div>
  );
}
