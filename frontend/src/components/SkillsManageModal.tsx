import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { X } from "lucide-react";
import { ControlButton } from "./ui";
import { RuntimeClient } from "../lib/runtime/client";
import type { SessionSkillsListing } from "../lib/runtime/types";

interface SkillsManageModalProps {
  isOpen: boolean;
  listing: SessionSkillsListing | null;
  onClose: () => void;
  onSkillsUpdated?: () => void;
}

export function SkillsManageModal({
  isOpen,
  listing,
  onClose,
  onSkillsUpdated,
}: SkillsManageModalProps) {
  const { t } = useTranslation();
  const dialogRef = useRef<HTMLDivElement>(null);
  const [overrides, setOverrides] = useState<Record<string, boolean>>({});
  const [updating, setUpdating] = useState<Record<string, boolean>>({});

  const skills = (listing?.skills ?? []).map((skill) => ({
    ...skill,
    enabled: overrides[skill.name] ?? skill.enabled !== false,
  }));

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

  const handleToggle = async (skillName: string, currentEnabled: boolean) => {
    const nextEnabled = !currentEnabled;
    setUpdating((prev) => ({ ...prev, [skillName]: true }));
    setOverrides((prev) => ({ ...prev, [skillName]: nextEnabled }));
    try {
      await RuntimeClient.toggleSkill(skillName, nextEnabled);
      onSkillsUpdated?.();
    } catch {
      setOverrides((prev) => ({ ...prev, [skillName]: currentEnabled }));
    } finally {
      setUpdating((prev) => ({ ...prev, [skillName]: false }));
    }
  };

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
          <div className="min-w-0 pr-3">
            <h2 className="text-base font-semibold text-[var(--vc-text-primary)] truncate">
              {t("composer.nativeSkillsManageTitle")}
            </h2>
            <p className="text-xs text-[var(--vc-text-muted)] truncate">
              {t("composer.nativeSkillsManageSubtitle")}
            </p>
          </div>
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

        <div className="overflow-y-auto px-5 py-4 text-sm divide-y divide-[color:var(--vc-border-subtle)]">
          {skills.length === 0 ? (
            <p className="py-2 text-[var(--vc-text-muted)]">
              {t("composer.nativeSkillsEmpty")}
            </p>
          ) : (
            skills.map((skill) => (
              <label
                key={skill.name}
                className="flex items-start gap-3 py-3 cursor-pointer hover:bg-[var(--vc-surface-1)] rounded-lg px-2 -mx-2 transition-colors"
              >
                <input
                  type="checkbox"
                  className="mt-1 h-4 w-4 rounded border-[color:var(--vc-border-subtle)] text-[var(--vc-accent)] focus:ring-[var(--vc-accent)]"
                  checked={skill.enabled !== false}
                  disabled={updating[skill.name]}
                  onChange={() =>
                    handleToggle(skill.name, skill.enabled !== false)
                  }
                />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-[var(--vc-text-primary)] truncate">
                      {skill.name}
                    </span>
                    <span className="text-xs px-1.5 py-0.5 rounded bg-[var(--vc-surface-2)] text-[var(--vc-text-muted)]">
                      {skill.origin}
                    </span>
                  </div>
                  {skill.description && (
                    <p className="text-xs text-[var(--vc-text-muted)] mt-0.5 break-words line-clamp-2">
                      {skill.description}
                    </p>
                  )}
                </div>
              </label>
            ))
          )}
        </div>

        <div className="shrink-0 border-t border-[color:var(--vc-border-subtle)] px-5 py-2 text-[11px] text-[var(--vc-text-subtle)]">
          {t("composer.nativeSkillsManageHint")}
        </div>
      </div>
    </div>
  );
}
