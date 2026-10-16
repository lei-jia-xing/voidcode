import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import { LoaderCircle, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ControlButton } from "./ui";

const REMARK_PLUGINS = [remarkGfm];

export interface BtwDialogState {
  question: string;
  answer: string | null;
  loading: boolean;
}

interface BtwDialogProps {
  dialog: BtwDialogState | null;
  onClose: () => void;
}

export function BtwDialog({ dialog, onClose }: BtwDialogProps) {
  const { t } = useTranslation();
  const dialogRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!dialog) return;
    dialogRef.current?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" || event.key === "Enter") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [dialog, onClose]);

  if (!dialog) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <button
        type="button"
        className="absolute inset-0 bg-[var(--vc-overlay-bg)] backdrop-blur-xs"
        onClick={onClose}
        aria-label={t("common.close")}
      />

      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="btw-dialog-title"
        tabIndex={-1}
        className="relative w-full max-w-lg bg-[var(--vc-bg)] border border-[color:var(--vc-border-subtle)] rounded-xl shadow-2xl flex flex-col max-h-[80vh]"
      >
        <div className="flex items-center justify-between px-5 h-14 border-b border-[color:var(--vc-border-subtle)] shrink-0">
          <h2
            id="btw-dialog-title"
            className="text-base font-semibold text-[var(--vc-text-primary)] truncate"
          >
            <span className="text-[var(--vc-warning-text)]">/btw </span>
            <span className="font-normal text-[var(--vc-text-muted)]">
              {dialog.question}
            </span>
          </h2>
          <ControlButton
            compact
            icon
            variant="ghost"
            onClick={onClose}
            aria-label={t("common.close")}
          >
            <X className="w-5 h-5" />
          </ControlButton>
        </div>

        <div className="overflow-y-auto px-5 py-4 text-sm text-[var(--vc-text-primary)]">
          {dialog.loading || dialog.answer === null ? (
            <div className="flex items-center gap-2 text-[var(--vc-text-muted)]">
              <LoaderCircle className="h-4 w-4 animate-spin" />
              {t("composer.nativeBtwBusy")}
            </div>
          ) : (
            <ReactMarkdown remarkPlugins={REMARK_PLUGINS}>
              {dialog.answer}
            </ReactMarkdown>
          )}
        </div>

        <div className="shrink-0 border-t border-[color:var(--vc-border-subtle)] px-5 py-2 text-[11px] text-[var(--vc-text-subtle)]">
          {t("composer.nativeBtwHint")}
        </div>
      </div>
    </div>
  );
}
