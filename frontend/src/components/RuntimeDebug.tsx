import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Server, Loader2, CheckCircle2, XCircle } from "lucide-react";
import { RuntimeClient } from "../lib/runtime/client";

export function RuntimeDebug() {
  const { t } = useTranslation();
  const [status, setStatus] = useState<
    "idle" | "testing" | "success" | "error"
  >("idle");

  const testConnection = async () => {
    setStatus("testing");
    try {
      // Just a shallow integration to prove the client can be called
      await RuntimeClient.listSessions();
      setStatus("success");
      setTimeout(() => setStatus("idle"), 3000);
    } catch (e) {
      console.error("Runtime test failed:", e);
      setStatus("error");
      setTimeout(() => setStatus("idle"), 3000);
    }
  };

  return (
    <button
      type="button"
      onClick={testConnection}
      disabled={status === "testing"}
      className="w-full flex items-center justify-center md:justify-start md:px-4 py-3 md:py-2.5 rounded-lg text-[var(--vc-text-muted)] hover:bg-[var(--vc-surface-1)] hover:text-[var(--vc-text-primary)] transition-colors"
      title={t("debug.testRuntime")}
    >
      {status === "testing" ? (
        <Loader2 className="w-5 h-5 md:mr-3 animate-spin text-[var(--vc-text-muted)]" />
      ) : status === "success" ? (
        <CheckCircle2 className="w-5 h-5 md:mr-3 text-[var(--vc-confirm-text)]" />
      ) : status === "error" ? (
        <XCircle className="w-5 h-5 md:mr-3 text-[var(--vc-danger-text)]" />
      ) : (
        <Server className="w-5 h-5 md:mr-3" />
      )}
      <span className="hidden md:block font-medium">
        {status === "testing"
          ? t("debug.testing")
          : status === "success"
            ? t("debug.success")
            : status === "error"
              ? t("debug.error")
              : t("debug.testRuntime")}
      </span>
    </button>
  );
}
