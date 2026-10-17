import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import { X } from "lucide-react";
import { ControlButton } from "./ui";

export interface InfoDialogRow {
  label: string;
  value: string;
  indent?: number;
  action?: {
    label: string;
    disabled?: boolean;
    onClick: () => void;
  };
}

export interface InfoDialogState {
  title: string;
  rows: InfoDialogRow[];
  hint?: string | null;
}

interface InfoDialogProps {
  dialog: InfoDialogState | null;
  onClose: () => void;
}

export function InfoDialog({ dialog, onClose }: InfoDialogProps) {
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
        tabIndex={-1}
        className="relative w-full max-w-lg bg-[var(--vc-bg)] border border-[color:var(--vc-border-subtle)] rounded-xl shadow-2xl flex flex-col max-h-[80vh]"
      >
        <div className="flex items-center justify-between px-5 h-14 border-b border-[color:var(--vc-border-subtle)] shrink-0">
          <h2 className="text-base font-semibold text-[var(--vc-text-primary)] truncate">
            {dialog.title}
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

        <div className="overflow-y-auto px-5 py-4 text-sm">
          <dl>
            {dialog.rows.map((row, index) => (
              <div
                key={`${row.label}-${index}`}
                className="flex items-center justify-between gap-3 py-1"
                style={{ paddingLeft: (row.indent ?? 0) * 16 }}
              >
                <div className="flex gap-3 min-w-0 flex-1">
                  <dt className="shrink-0 w-28 text-[var(--vc-text-muted)] truncate">
                    {row.label}
                  </dt>
                  <dd className="text-[var(--vc-text-primary)] break-words min-w-0">
                    {row.value}
                  </dd>
                </div>
                {row.action && (
                  <ControlButton
                    compact
                    variant="secondary"
                    disabled={row.action.disabled}
                    onClick={row.action.onClick}
                  >
                    {row.action.label}
                  </ControlButton>
                )}
              </div>
            ))}
          </dl>
        </div>

        {dialog.hint && (
          <div className="shrink-0 border-t border-[color:var(--vc-border-subtle)] px-5 py-2 text-[11px] text-[var(--vc-text-subtle)]">
            {dialog.hint}
          </div>
        )}
      </div>
    </div>
  );
}
