import { useState, useEffect } from "react";
import { useTranslation } from "react-i18next";
import {
  AlertCircle,
  CheckCircle2,
  Cog,
  Globe,
  KeyRound,
  Layers,
  Loader2,
  Save,
  Sparkles,
  X,
  XCircle,
} from "lucide-react";
import {
  AsyncStatus,
  ProviderModelsResult,
  ProviderSummary,
  ProviderValidationResult,
  RuntimeSettings,
  RuntimeSettingsUpdate,
} from "../lib/runtime/types";
import { ControlButton } from "./ui";

interface SettingsPanelProps {
  isOpen: boolean;
  settings: RuntimeSettings | null;
  settingsStatus: string;
  settingsError: string | null;
  providers?: ProviderSummary[];
  providersStatus?: string;
  providersError?: string | null;
  providerModels?: Record<string, ProviderModelsResult>;
  providerValidationResults?: Record<string, ProviderValidationResult>;
  providerValidationStatus?: Record<string, AsyncStatus>;
  providerValidationError?: Record<string, string | null>;
  language: string;
  onToggleLanguage: () => void;
  onClose: () => void;
  onLoad: () => void;
  onLoadProviders?: () => void;
  onValidateProvider?: (providerName: string) => void;
  onSave: (settings: RuntimeSettingsUpdate) => void;
}

type SettingsSectionKey = "general" | "provider";

function canonicalModelReference(providerName: string, model: string): string {
  return model.startsWith(`${providerName}/`)
    ? model
    : `${providerName}/${model}`;
}

function displayModelName(model: string, providerName: string): string {
  return model.startsWith(`${providerName}/`)
    ? model.slice(providerName.length + 1)
    : model;
}

function modelBelongsToProvider(model: string, providerName: string): boolean {
  if (!model) return false;
  return model.startsWith(`${providerName}/`);
}

function NavItem({
  active,
  icon,
  label,
  description,
  onSelect,
}: {
  active: boolean;
  icon: React.ReactNode;
  label: string;
  description: string;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-current={active ? "page" : undefined}
      className={`w-full rounded-lg border px-3 py-2.5 text-left transition-colors ${
        active
          ? "border-[color:var(--vc-border-strong)] bg-[var(--vc-surface-2)]"
          : "border-transparent bg-transparent hover:border-[color:var(--vc-border-subtle)] hover:bg-[var(--vc-surface-1)]"
      }`}
    >
      <div className="flex items-center gap-2">
        <span
          className={`flex h-7 w-7 items-center justify-center rounded-md ${
            active
              ? "bg-[var(--vc-bg)] text-[var(--vc-text-primary)]"
              : "bg-[var(--vc-surface-1)] text-[var(--vc-text-muted)]"
          }`}
        >
          {icon}
        </span>
        <div className="min-w-0">
          <div
            className={`text-sm font-medium ${
              active
                ? "text-[var(--vc-text-primary)]"
                : "text-[var(--vc-text-muted)]"
            }`}
          >
            {label}
          </div>
          <div className="text-[11px] text-[var(--vc-text-subtle)] truncate">
            {description}
          </div>
        </div>
      </div>
    </button>
  );
}

function SectionHeader({
  icon,
  title,
  description,
}: {
  icon: React.ReactNode;
  title: string;
  description?: string;
}) {
  return (
    <div className="flex items-start gap-3 border-b border-[color:var(--vc-border-subtle)] pb-4">
      <span className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-lg bg-[var(--vc-surface-1)] text-[var(--vc-text-primary)]">
        {icon}
      </span>
      <div className="min-w-0">
        <h2 className="text-base font-semibold text-[var(--vc-text-primary)]">
          {title}
        </h2>
        {description && (
          <p className="mt-0.5 text-xs text-[var(--vc-text-muted)]">
            {description}
          </p>
        )}
      </div>
    </div>
  );
}

function FieldGroup({
  label,
  htmlFor,
  hint,
  children,
}: {
  label: string;
  htmlFor?: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-2">
      <label
        htmlFor={htmlFor}
        className="text-xs font-medium uppercase tracking-wide text-[var(--vc-text-muted)]"
      >
        {label}
      </label>
      {children}
      {hint && (
        <p className="text-[11px] text-[var(--vc-text-subtle)]">{hint}</p>
      )}
    </div>
  );
}

function GeneralSection({
  language,
  onToggleLanguage,
}: {
  language: string;
  onToggleLanguage: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="space-y-6">
      <SectionHeader
        icon={<Cog className="h-5 w-5" />}
        title={t("settings.section.general")}
        description={t("settings.section.generalDescription")}
      />
      <FieldGroup
        label={t("language.switch")}
        hint={t("settings.languageHint")}
      >
        <div className="grid grid-cols-2 gap-2">
          {(
            [
              { code: "en", label: t("language.en") },
              { code: "zh-CN", label: t("language.zh") },
            ] as const
          ).map((option) => {
            const active = language === option.code;
            return (
              <button
                type="button"
                key={option.code}
                onClick={() => {
                  if (!active) onToggleLanguage();
                }}
                aria-pressed={active}
                className={`flex items-center justify-between rounded-lg border px-3 py-2.5 text-sm transition-colors ${
                  active
                    ? "border-[color:var(--vc-border-strong)] bg-[var(--vc-surface-2)] text-[var(--vc-text-primary)]"
                    : "border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] text-[var(--vc-text-muted)] hover:border-[color:var(--vc-border-strong)] hover:bg-[var(--vc-surface-2)] hover:text-[var(--vc-text-primary)]"
                }`}
              >
                <span>{option.label}</span>
                <span
                  className={`text-[10px] font-mono ${
                    active
                      ? "text-[var(--vc-text-muted)]"
                      : "text-[var(--vc-text-subtle)]"
                  }`}
                >
                  {option.code}
                </span>
              </button>
            );
          })}
        </div>
      </FieldGroup>
    </div>
  );
}

interface ProviderSectionProps {
  settings: RuntimeSettings | null;
  settingsError: string | null;
  providers: ProviderSummary[];
  providersStatus?: string;
  providersError?: string | null;
  providerModels: Record<string, ProviderModelsResult>;
  providerValidationResults: Record<string, ProviderValidationResult>;
  providerValidationStatus: Record<string, AsyncStatus>;
  providerValidationError: Record<string, string | null>;
  isLoading: boolean;
  provider: string;
  setProvider: (value: string) => void;
  apiKey: string;
  setApiKey: (value: string) => void;
  model: string;
  setModel: (value: string | ((prev: string) => string)) => void;
  onValidateProvider?: (providerName: string) => void;
}

function ProviderSection({
  settings,
  settingsError,
  providers,
  providersStatus,
  providersError,
  providerModels,
  providerValidationResults,
  providerValidationStatus,
  providerValidationError,
  isLoading,
  provider,
  setProvider,
  apiKey,
  setApiKey,
  model,
  setModel,
  onValidateProvider,
}: ProviderSectionProps) {
  const { t } = useTranslation();

  const configuredProviders = providers.filter((item) => item.configured);
  const unconfiguredProviders = providers.filter((item) => !item.configured);
  const selectedProvider = provider
    ? providers.find((item) => item.name === provider)
    : undefined;
  const providerGroups = [
    {
      label: t("settings.configuredProviders"),
      providers: configuredProviders,
    },
    {
      label: t("settings.unconfiguredProviders"),
      providers: unconfiguredProviders,
    },
  ];
  const selectedProviderModels = provider
    ? providerModels[provider]
    : undefined;
  const selectedModels = selectedProviderModels?.models ?? [];
  const validationStatus = provider
    ? providerValidationStatus[provider]
    : undefined;
  const validationResult = provider
    ? providerValidationResults[provider]
    : undefined;
  const validationError = provider
    ? providerValidationError[provider]
    : undefined;
  const apiKeyConfigured =
    selectedProvider?.configured ||
    (settings?.provider === provider && settings?.provider_api_key_present);

  return (
    <div className="space-y-6">
      <SectionHeader
        icon={<Sparkles className="h-5 w-5" />}
        title={t("settings.section.provider")}
        description={t("settings.section.providerDescription")}
      />

      {settingsError && (
        <div className="flex items-start gap-2 rounded-lg border border-[color:var(--vc-danger-border)] bg-[var(--vc-danger-bg)] p-3 text-sm text-[var(--vc-danger-text)]">
          <AlertCircle className="mt-0.5 h-4 w-4 flex-shrink-0" />
          {settingsError}
        </div>
      )}
      {providersError && (
        <div className="flex items-start gap-2 rounded-lg border border-[color:var(--vc-danger-border)] bg-[var(--vc-danger-bg)] p-3 text-sm text-[var(--vc-danger-text)]">
          <AlertCircle className="mt-0.5 h-4 w-4 flex-shrink-0" />
          {providersError}
        </div>
      )}

      <FieldGroup label={t("settings.provider")}>
        {providers.length === 0 && providersStatus !== "loading" ? (
          <div className="rounded-lg border border-dashed border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] px-3 py-3 text-sm text-[var(--vc-text-subtle)]">
            {t("settings.noProviders")}
          </div>
        ) : (
          <div className="space-y-3">
            {providerGroups.map(({ label, providers: group }) =>
              group.length > 0 ? (
                <div key={label} className="space-y-2">
                  <div className="text-[10px] uppercase tracking-wide text-[var(--vc-text-subtle)]">
                    {label}
                  </div>
                  <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                    {group.map((p) => (
                      <button
                        type="button"
                        key={p.name}
                        onClick={() => {
                          setProvider(p.name);
                          setModel((current) =>
                            modelBelongsToProvider(current, p.name)
                              ? current
                              : "",
                          );
                        }}
                        aria-pressed={provider === p.name}
                        className={`flex flex-col items-start justify-center rounded-xl border p-3 text-left transition-colors ${
                          provider === p.name
                            ? "border-[color:var(--vc-border-strong)] bg-[var(--vc-surface-2)] ring-1 ring-[color:var(--vc-border-strong)]"
                            : "border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] hover:border-[color:var(--vc-border-strong)] hover:bg-[var(--vc-surface-2)]"
                        }`}
                      >
                        <div className="mb-1 flex w-full items-center justify-between">
                          <div className="text-sm font-medium text-[var(--vc-text-primary)]">
                            {p.label}
                          </div>
                          <span className="inline-flex items-center">
                            <span
                              className={`h-2 w-2 rounded-full ${
                                p.configured
                                  ? "bg-[var(--vc-confirm-text)]"
                                  : "bg-[var(--vc-text-subtle)]"
                              }`}
                              aria-hidden="true"
                            />
                            <span className="sr-only">
                              {p.configured
                                ? t("settings.configured")
                                : t("settings.notConfigured")}
                            </span>
                          </span>
                        </div>
                        <div className="font-mono text-[11px] text-[var(--vc-text-subtle)]">
                          {p.name}
                        </div>
                      </button>
                    ))}
                  </div>
                </div>
              ) : null,
            )}
          </div>
        )}
      </FieldGroup>

      <FieldGroup
        label={t("settings.model")}
        htmlFor="settings-model"
        hint={
          selectedProvider
            ? t("settings.modelHintForProvider", {
                provider: selectedProvider.label,
                count: selectedModels.length,
              })
            : t("settings.modelHint")
        }
      >
        <div className="relative">
          <Layers className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--vc-text-subtle)]" />
          <select
            id="settings-model"
            value={model}
            onChange={(event) => setModel(event.target.value)}
            disabled={isLoading || !provider || selectedModels.length === 0}
            className="w-full rounded-lg border border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] py-2.5 pl-9 pr-3 text-sm text-[var(--vc-text-primary)] transition-colors focus:border-[color:var(--vc-border-strong)] focus:outline-none focus:ring-1 focus:ring-[color:var(--vc-border-strong)] disabled:opacity-50"
          >
            <option value="">{t("settings.modelPlaceholder")}</option>
            {selectedModels.map((modelId) => {
              const value = canonicalModelReference(provider, modelId);
              return (
                <option key={`${provider}:${modelId}`} value={value}>
                  {displayModelName(modelId, provider)}
                </option>
              );
            })}
          </select>
        </div>
      </FieldGroup>

      {provider && (
        <div className="rounded-xl border border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] p-3 text-xs text-[var(--vc-text-muted)]">
          <div className="flex items-center justify-between gap-3">
            <div className="min-w-0">
              <div className="font-medium text-[var(--vc-text-muted)]">
                {t("settings.discoveryStatus")}
              </div>
              <div className="mt-1 truncate font-mono">
                {selectedProviderModels?.last_refresh_status ??
                  t("status.unknown")}
                {selectedProviderModels?.discovery_mode
                  ? ` · ${selectedProviderModels.discovery_mode}`
                  : ""}
              </div>
            </div>
            <ControlButton
              compact
              variant="secondary"
              onClick={() => onValidateProvider?.(provider)}
              disabled={isLoading || validationStatus === "loading"}
            >
              {validationStatus === "loading" ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : validationStatus === "success" ? (
                <CheckCircle2 className="h-4 w-4 text-[var(--vc-confirm-text)]" />
              ) : validationStatus === "error" ? (
                <XCircle className="h-4 w-4 text-[var(--vc-danger-text)]" />
              ) : null}
              {t("settings.testCredentials")}
            </ControlButton>
          </div>
          {selectedProviderModels?.last_error && (
            <div className="mt-2 text-[var(--vc-danger-text)]">
              {selectedProviderModels.last_error}
            </div>
          )}
          {(validationResult || validationError) && (
            <div
              className={`mt-2 ${
                validationResult?.ok
                  ? "text-[var(--vc-confirm-text)]"
                  : "text-[var(--vc-danger-text)]"
              }`}
            >
              {validationResult?.message ?? validationError}
            </div>
          )}
        </div>
      )}

      <FieldGroup
        label={t("settings.apiKey")}
        htmlFor="settings-api-key"
        hint={
          apiKeyConfigured
            ? t("settings.apiKeyConfiguredHint")
            : t("settings.apiKeyHint")
        }
      >
        <div className="relative">
          <KeyRound className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--vc-text-subtle)]" />
          <input
            id="settings-api-key"
            type="password"
            autoComplete="off"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder={
              apiKeyConfigured
                ? t("settings.apiKeyPlaceholderConfigured")
                : t("settings.apiKeyPlaceholder")
            }
            disabled={isLoading}
            spellCheck={false}
            className="w-full rounded-lg border border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] py-2.5 pl-9 pr-3 text-sm text-[var(--vc-text-primary)] placeholder:text-[var(--vc-text-subtle)] transition-colors focus:border-[color:var(--vc-border-strong)] focus:outline-none focus:ring-1 focus:ring-[color:var(--vc-border-strong)] disabled:opacity-50"
          />
        </div>
      </FieldGroup>
    </div>
  );
}

export function SettingsPanel({
  isOpen,
  settings,
  settingsStatus,
  settingsError,
  providers = [],
  providersStatus,
  providersError,
  providerModels = {},
  providerValidationResults = {},
  providerValidationStatus = {},
  providerValidationError = {},
  language,
  onToggleLanguage,
  onClose,
  onLoad,
  onLoadProviders,
  onValidateProvider,
  onSave,
}: SettingsPanelProps) {
  const { t } = useTranslation();
  const [provider, setProvider] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState("");
  const [activeSection, setActiveSection] =
    useState<SettingsSectionKey>("general");

  useEffect(() => {
    if (isOpen) {
      onLoad();
      onLoadProviders?.();
    }
  }, [isOpen, onLoad, onLoadProviders]);

  useEffect(() => {
    if (settings) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- initialising local form state from external settings
      setProvider(settings.provider || "");
      setModel(settings.model || "");
      setApiKey("");
    }
  }, [settings]);

  const handleSave = () => {
    onSave({
      provider: provider || undefined,
      provider_api_key: apiKey || undefined,
      model: model || undefined,
    });
  };

  if (!isOpen) return null;

  const isLoading = settingsStatus === "loading";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <button
        type="button"
        className="absolute inset-0 bg-[var(--vc-overlay-bg)] backdrop-blur-sm"
        onClick={onClose}
        aria-label={t("common.close")}
      />
      <form
        className="relative flex max-h-[90vh] w-full max-w-3xl flex-col overflow-hidden rounded-2xl border border-[color:var(--vc-border-subtle)] bg-[var(--vc-bg)] shadow-2xl"
        onSubmit={(event) => {
          event.preventDefault();
          handleSave();
        }}
      >
        <div className="flex h-14 items-center justify-between border-b border-[color:var(--vc-border-subtle)] px-6">
          <h2 className="text-base font-semibold text-[var(--vc-text-primary)]">
            {t("settings.title")}
          </h2>
          <ControlButton
            compact
            icon
            variant="ghost"
            onClick={onClose}
            aria-label={t("common.close")}
          >
            <X className="h-5 w-5" />
          </ControlButton>
        </div>

        <div className="flex min-h-0 flex-1">
          <nav className="hidden w-56 flex-shrink-0 flex-col gap-1 border-r border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] p-3 sm:flex">
            <NavItem
              active={activeSection === "general"}
              icon={<Globe className="h-4 w-4" />}
              label={t("settings.section.general")}
              description={t("settings.section.generalNavHint")}
              onSelect={() => setActiveSection("general")}
            />
            <NavItem
              active={activeSection === "provider"}
              icon={<Sparkles className="h-4 w-4" />}
              label={t("settings.section.provider")}
              description={t("settings.section.providerNavHint")}
              onSelect={() => setActiveSection("provider")}
            />
          </nav>

          {/* Mobile section pill bar */}
          <div className="absolute left-0 right-0 top-14 z-10 flex border-b border-[color:var(--vc-border-subtle)] bg-[var(--vc-bg)] px-3 py-2 sm:hidden">
            <div className="flex w-full gap-1 rounded-lg border border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] p-1">
              <button
                type="button"
                onClick={() => setActiveSection("general")}
                aria-pressed={activeSection === "general"}
                className={`flex-1 rounded-md px-2 py-1.5 text-xs ${
                  activeSection === "general"
                    ? "bg-[var(--vc-surface-2)] text-[var(--vc-text-primary)]"
                    : "text-[var(--vc-text-muted)]"
                }`}
              >
                {t("settings.section.general")}
              </button>
              <button
                type="button"
                onClick={() => setActiveSection("provider")}
                aria-pressed={activeSection === "provider"}
                className={`flex-1 rounded-md px-2 py-1.5 text-xs ${
                  activeSection === "provider"
                    ? "bg-[var(--vc-surface-2)] text-[var(--vc-text-primary)]"
                    : "text-[var(--vc-text-muted)]"
                }`}
              >
                {t("settings.section.provider")}
              </button>
            </div>
          </div>

          <div className="flex-1 overflow-y-auto p-6 pt-16 sm:pt-6">
            {activeSection === "general" && (
              <GeneralSection
                language={language}
                onToggleLanguage={onToggleLanguage}
              />
            )}
            {activeSection === "provider" && (
              <ProviderSection
                settings={settings}
                settingsError={settingsError}
                providers={providers}
                providersStatus={providersStatus}
                providersError={providersError}
                providerModels={providerModels}
                providerValidationResults={providerValidationResults}
                providerValidationStatus={providerValidationStatus}
                providerValidationError={providerValidationError}
                isLoading={isLoading}
                provider={provider}
                setProvider={setProvider}
                apiKey={apiKey}
                setApiKey={setApiKey}
                model={model}
                setModel={setModel}
                onValidateProvider={onValidateProvider}
              />
            )}
          </div>
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-[color:var(--vc-border-subtle)] bg-[var(--vc-surface-1)] p-4">
          <ControlButton
            type="button"
            variant="ghost"
            onClick={onClose}
            disabled={isLoading}
          >
            {t("common.close")}
          </ControlButton>
          <ControlButton
            type="submit"
            disabled={isLoading || activeSection !== "provider"}
            variant="primary"
          >
            {isLoading ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Save className="h-4 w-4" />
            )}
            {t("settings.save")}
          </ControlButton>
        </div>
      </form>
    </div>
  );
}
