import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { X, ArrowLeft } from "lucide-react";
import { ControlButton } from "./ui";

export interface ChoicePickerOption {
  id: string;
  label: string;
  hint?: string;
}

interface ChoicePickerModalProps {
  title: string;
  options: ChoicePickerOption[];
  isOpen: boolean;
  filenameStep?: { prompt: string; initial: string } | null;
  onPick: (id: string) => void;
  onFilename?: (filename: string) => void;
  onClose: () => void;
}

export function ChoicePickerModal({
  title,
  options,
  isOpen,
  filenameStep,
  onPick,
  onFilename,
  onClose,
}: ChoicePickerModalProps) {
  const { t } = useTranslation();
  const dialogRef = useRef<HTMLDivElement>(null);
  const initial = filenameStep?.initial ?? "";
  const [filenameState, setFilenameState] = useState<{
    initial: string;
    filename: string;
  }>({ initial, filename: initial });
  // Reset the draft when a new filename step opens (render-time adjustment,
  // not an effect, so no cascading render).
  const filename =
    filenameState.initial === initial ? filenameState.filename : initial;
  const setFilename = (value: string) =>
    setFilenameState({ initial, filename: value });

  useEffect(() => {
    if (!isOpen) return;
    dialogRef.current?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isOpen, onClose]);

  if (!isOpen) return null;

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
        className="relative w-full max-w-md bg-[var(--vc-bg)] border border-[color:var(--vc-border-subtle)] rounded-xl shadow-2xl flex flex-col max-h-[80vh]"
      >
        <div className="flex items-center justify-between px-5 h-14 border-b border-[color:var(--vc-border-subtle)] shrink-0">
          <h2 className="text-base font-semibold text-[var(--vc-text-primary)] truncate">
            {filenameStep ? (
              <span className="flex items-center gap-2">
                <ArrowLeft className="w-4 h-4" />
                {filenameStep.prompt}
              </span>
            ) : (
              title
            )}
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

        <div className="overflow-y-auto px-3 py-3">
          {filenameStep ? (
            <form
              onSubmit={(event) => {
                event.preventDefault();
                if (filename.trim()) onFilename?.(filename.trim());
              }}
              className="flex flex-col gap-3 px-2"
            >
              <input
                autoFocus
                value={filename}
                onChange={(event) => setFilename(event.target.value)}
                className="w-full rounded-md border border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] px-3 py-2 text-sm text-[var(--vc-text-primary)]"
              />
              <div className="flex justify-end">
                <ControlButton type="submit">
                  {t("composer.nativeExportFile")}
                </ControlButton>
              </div>
            </form>
          ) : (
            options.map((option) => (
              <button
                key={option.id}
                type="button"
                onClick={() => onPick(option.id)}
                className="w-full text-left rounded-lg px-3 py-2 hover:bg-[var(--vc-surface-1)]"
              >
                <div className="text-sm text-[var(--vc-text-primary)]">
                  {option.label}
                </div>
                {option.hint && (
                  <div className="text-xs text-[var(--vc-text-muted)] truncate">
                    {option.hint}
                  </div>
                )}
              </button>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
