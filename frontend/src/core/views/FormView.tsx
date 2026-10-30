import React, { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type {
  CommandDescriptor,
  FormField,
  FormViewData,
} from "../../lib/core/types";
import type { CoreClient } from "../../lib/core/client";
import { componentFg, componentStyle } from "./componentStyle";

export interface FormViewProps {
  title?: string;
  data: Omit<FormViewData, "load_action" | "initial_values"> & {
    load_action?: string | null;
    initial_values?: Record<string, unknown>;
  };
  /** The target command's argument schema: the authority on null vs. unset. */
  argumentSchema?: Record<string, unknown>;
  sessionId: string;
  /**
   * The revision of the views envelope that rendered this form. The declared
   * read action is invoked under exactly this revision, never under a newer one.
   */
  revisionId: string;
  client: CoreClient;
  catalog: CommandDescriptor[];
  onSubmit: (action: string, args: Record<string, unknown>) => void;
  onCancel?: () => void;
  disabled?: boolean;
}

/** SDK default is ``required=True``; a raw view that omits the key still means required. */
const isRequired = (field: FormField): boolean => field.required !== false;

const INTEGER_PATTERN = /^[+-]?\d+$/;
const NUMBER_PATTERN = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/;

/**
 * Validate one submitted value against its declared field type.
 * Returns the wire value or an error message; never fabricates or coerces.
 */
type Translator = (key: string, options?: Record<string, unknown>) => string;

/**
 * Whether the command's own schema admits `null` here (JSON Schema 2020-12):
 * a `"null"` type, a type array containing `"null"`, or a `null` anyOf/oneOf branch.
 */
function schemaAllowsNull(schema: unknown): boolean {
  if (!schema || typeof schema !== "object") return false;
  const candidate = schema as {
    type?: unknown;
    anyOf?: unknown;
    oneOf?: unknown;
  };
  if (candidate.type === "null") return true;
  if (Array.isArray(candidate.type) && candidate.type.includes("null"))
    return true;
  for (const keyword of ["anyOf", "oneOf"] as const) {
    const branches = candidate[keyword];
    if (
      Array.isArray(branches) &&
      branches.some((branch) => schemaAllowsNull(branch))
    ) {
      return true;
    }
  }
  return false;
}

function allowsNull(
  schema: Record<string, unknown> | undefined,
  name: string,
): boolean {
  const properties = (schema?.properties ?? {}) as Record<string, unknown>;
  return schemaAllowsNull(properties[name]);
}

function coerce(
  field: FormField,
  raw: unknown,
  t: Translator,
): { value: unknown } | { error: string } {
  const type = field.field_type || "text";
  const name = field.label || field.name;

  if (
    field.options != null &&
    raw !== undefined &&
    raw !== null &&
    raw !== "" &&
    !field.options.some((option) => option.id === raw)
  ) {
    return { error: t("core.formMustBeChoice", { field: name }) };
  }

  if (type === "boolean") {
    if (typeof raw === "boolean") return { value: raw };
    // Untouched (or blank select) is not a value: the caller decides whether it
    // is omitted, cleared with null, or refused as a missing required field.
    if (raw === undefined || raw === null || String(raw).trim() === "") {
      return { value: undefined };
    }
    return { error: t("core.formMustBeBoolean", { field: name }) };
  }

  if (type === "integer") {
    if (raw === undefined || raw === null) return { value: undefined };
    if (typeof raw === "string" && raw.trim() === "")
      return { value: undefined };
    if (typeof raw === "number") {
      // Only a safe, exactly-representable integer is acceptable: a value that
      // already lost precision upstream is surfaced, never silently accepted.
      if (!Number.isSafeInteger(raw)) {
        return { error: t("core.formOutOfRange", { field: name }) };
      }
      return { value: raw };
    }
    const candidate = String(raw).trim();
    if (!INTEGER_PATTERN.test(candidate)) {
      return { error: t("core.formMustBeInteger", { field: name }) };
    }
    const parsed = Number(candidate);
    return Number.isSafeInteger(parsed)
      ? { value: parsed }
      : { error: t("core.formOutOfRange", { field: name }) };
  }

  if (type === "number") {
    if (raw === undefined || raw === null) return { value: undefined };
    if (typeof raw === "string" && raw.trim() === "")
      return { value: undefined };
    const candidate = String(raw).trim();
    if (!NUMBER_PATTERN.test(candidate)) {
      return { error: t("core.formMustBeNumber", { field: name }) };
    }
    const parsed = Number(candidate);
    return Number.isFinite(parsed)
      ? { value: parsed }
      : { error: t("core.formMustBeFinite", { field: name }) };
  }

  if (raw === undefined || raw === null) return { value: undefined };
  const text = String(raw);
  if (text.trim() === "") {
    // An empty text box is blank, not a value: the caller decides null vs. omit.
    return { value: undefined };
  }
  // Non-empty text is preserved exactly as typed (whitespace included).
  return { value: text };
}

export const FormView: React.FC<FormViewProps> = ({
  title,
  data,
  sessionId,
  revisionId,
  client,
  catalog,
  argumentSchema,
  onSubmit,
  onCancel,
  disabled = false,
}) => {
  const { t } = useTranslation();
  const fields = useMemo(() => data.fields || [], [data.fields]);
  const fg = componentFg(data);

  // Raw editor text per field: no synthetic defaults are ever materialized.
  const [formValues, setFormValues] = useState<Record<string, unknown>>(() => ({
    ...(data.initial_values || {}),
  }));
  // Latest dirty set, read synchronously at load resolution (never a captured copy).
  const [dirtyFields, setDirtyFields] = useState<Record<string, true>>({});
  const dirtyRef = useRef<Record<string, true>>({});
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [validationError, setValidationError] = useState<string | null>(null);

  // The same form instance can be re-pointed at a different action (a goal that
  // did not exist becomes editable): re-seed from the new descriptor's initial
  // values, keeping only what the user actually drafted.
  const fixedArgsKey = JSON.stringify(data.arguments || {});
  const lastFixedArgsRef = useRef(fixedArgsKey);
  const descriptorKey = `${sessionId}::${revisionId}::${data.action}::${fields.map((f) => f.name).join(",")}::${fixedArgsKey}`;
  const lastDescriptorRef = useRef<string>("");
  useEffect(() => {
    if (lastDescriptorRef.current === descriptorKey) {
      return;
    }
    lastDescriptorRef.current = descriptorKey;
    if (lastFixedArgsRef.current !== fixedArgsKey) {
      lastFixedArgsRef.current = fixedArgsKey;
      dirtyRef.current = {};
      setDirtyFields({});
      setValidationError(null);
      setLoadError(null);
    }
    const seeded = (data.initial_values ?? {}) as Record<string, unknown>;
    setFormValues((prev) => {
      const next: Record<string, unknown> = {};
      for (const field of fields) {
        if (dirtyRef.current[field.name] && Object.hasOwn(prev, field.name)) {
          next[field.name] = prev[field.name];
        } else if (Object.hasOwn(seeded, field.name)) {
          next[field.name] = seeded[field.name];
        }
      }
      return next;
    });
  }, [descriptorKey, fields, data.initial_values, fixedArgsKey]);

  const attemptedRef = useRef<Record<string, true>>({});
  const loadAction = data.load_action ?? null;
  // The revision is part of the identity: an activation commit that re-renders
  // this form under a new generation reads its prefill under that generation,
  // never under the one the fields were first displayed with.
  const loadKey = `${sessionId}::${revisionId}::${loadAction ?? ""}::${data.action}::${fixedArgsKey}`;

  useEffect(() => {
    if (!loadAction || attemptedRef.current[loadKey]) {
      return;
    }
    attemptedRef.current[loadKey] = true;
    const requestSession = sessionId;
    const requestAction = loadAction;
    const requestRevision = revisionId;
    const attempted = attemptedRef;

    // The load action must be exactly READ: any additional effect (write/execute/
    // spawn/session) would turn an on-render load into an unapproved mutation.
    const descriptor = catalog.find((c) => c.name === requestAction);
    const effects = (descriptor?.effects || []).map((e) =>
      String(e).toLowerCase(),
    );
    const isExactlyRead = effects.length === 1 && effects[0] === "read";

    let cancelled = false;

    const fetchFields = async () => {
      if (!descriptor || !isExactlyRead) {
        setLoadError(
          !descriptor
            ? t("core.formLoadNotInCatalog", { action: requestAction })
            : t("core.formLoadNotReadOnly", { action: requestAction }),
        );
        return;
      }
      setLoading(true);
      setLoadError(null);
      try {
        const res = await client.invoke(
          requestSession,
          requestAction,
          {},
          requestRevision,
        );
        if (stale()) {
          return; // the form moved on while this read was in flight
        }
        if (res.status === "ok" && res.data && typeof res.data === "object") {
          const loaded = res.data as Record<string, unknown>;
          setFormValues((prev) => {
            const next = { ...prev };
            // Read the LATEST draft at resolution time: anything the user touched
            // while the request was in flight keeps their edit.
            for (const [key, value] of Object.entries(loaded)) {
              if (!dirtyRef.current[key]) {
                next[key] = value;
              }
            }
            return next;
          });
        } else if (res.status === "error" || res.error) {
          setLoadError(res.error ? String(res.error) : t("core.formLoadEmpty"));
        }
      } catch (err: unknown) {
        if (stale()) return;
        const msg = err instanceof Error ? err.message : String(err);
        setLoadError(t("core.formLoadFailed", { message: msg }));
      } finally {
        if (!stale()) {
          setLoading(false);
        }
      }
    };

    // A superseded load (session/action/client changed, or the component
    // unmounted) must never write into this form.
    const stale = () => cancelled;

    void fetchFields();
    return () => {
      // A cancelled run must not consume the one-shot marker, otherwise a
      // StrictMode remount (or an early rebind) silently loses the load.
      cancelled = true;
      delete attempted.current[loadKey];
    };
  }, [loadAction, loadKey, revisionId, catalog, client, sessionId, t]);

  const handleChange = (name: string, value: unknown) => {
    dirtyRef.current = { ...dirtyRef.current, [name]: true };
    setDirtyFields(dirtyRef.current);
    setFormValues((prev) => ({ ...prev, [name]: value }));
    setValidationError(null);
  };

  const displayValue = (field: FormField): string => {
    const value = formValues[field.name];
    return value === undefined || value === null ? "" : String(value);
  };

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    setValidationError(null);

    const args: Record<string, unknown> = { ...data.arguments };
    for (const field of fields) {
      // Absent (never loaded, never typed) is different from present-and-blank
      // (the user cleared it): only the latter may clear a nullable value.
      const present = Object.hasOwn(formValues, field.name);
      const raw = formValues[field.name];
      const result = coerce(field, raw, t);
      if ("error" in result) {
        setValidationError(result.error);
        return;
      }
      if (result.value === undefined) {
        const clearable = allowsNull(argumentSchema, field.name);
        if (isRequired(field)) {
          // A required value that is missing is refused even when null is legal:
          // "clear it" is a deliberate act, absence is not.
          if (!present || !clearable) {
            setValidationError(
              t("core.formRequired", { field: field.label || field.name }),
            );
            return;
          }
        }
        if (present && clearable) {
          args[field.name] = null;
          continue;
        }
        // Absent or optional-but-not-nullable: omit rather than fabricate a value.
        continue;
      }
      args[field.name] = result.value;
    }

    onSubmit(data.action, args);
  };

  return (
    <form
      data-testid="form-view"
      style={componentStyle(data)}
      onSubmit={handleSubmit}
      className="rounded-lg border border-neutral-700 bg-neutral-900/60 p-4 my-2 text-sm text-neutral-200"
    >
      <div className="flex items-center justify-between mb-3 border-b border-neutral-800 pb-2">
        <div
          style={fg}
          className="font-semibold text-neutral-300 uppercase tracking-wider text-xs"
        >
          {title || t("core.formTitle", { action: data.action })}
        </div>
        {loading && (
          <span
            data-testid="form-loading"
            className="text-xs text-cyan-400 animate-pulse"
          >
            {t("core.formLoading")}
          </span>
        )}
      </div>

      {loadError && (
        <div
          data-testid="form-load-error"
          className="mb-3 p-2 rounded bg-red-950/50 border border-red-500/80 text-red-200 text-xs"
        >
          <span>⚠ {loadError}</span>
        </div>
      )}

      {validationError && (
        <div
          data-testid="form-validation-error"
          className="mb-3 p-2 rounded bg-amber-950/50 border border-amber-500/80 text-amber-200 text-xs"
        >
          ✖ {validationError}
        </div>
      )}

      <div className="flex flex-col gap-4">
        {fields.map((field) => {
          const type = field.field_type || "text";
          const value = formValues[field.name];
          const dirty = dirtyFields[field.name] === true;

          return (
            <div key={field.name} className="flex flex-col gap-1">
              <label
                htmlFor={`form-field-${field.name}`}
                style={fg}
                className="text-xs font-medium text-neutral-400"
              >
                {field.label || field.name}
                {isRequired(field) &&
                  !allowsNull(argumentSchema, field.name) && (
                    <span className="text-red-400 ml-1">*</span>
                  )}
                {dirty && (
                  <span className="text-neutral-500 ml-2">
                    ({t("core.formEdited")})
                  </span>
                )}
              </label>

              {field.options != null ? (
                <select
                  id={`form-field-${field.name}`}
                  data-testid={`form-input-${field.name}`}
                  value={typeof value === "string" ? value : ""}
                  disabled={disabled}
                  onChange={(e) => handleChange(field.name, e.target.value)}
                  className="w-full px-3 py-1.5 rounded bg-neutral-800 border border-neutral-700 text-neutral-100 text-sm"
                >
                  <option value="">{t("core.formUnset")}</option>
                  {field.options.map((option) => (
                    <option key={option.id} value={option.id}>
                      {option.label}
                    </option>
                  ))}
                </select>
              ) : type === "boolean" ? (
                <select
                  id={`form-field-${field.name}`}
                  data-testid={`form-input-${field.name}`}
                  value={
                    value === true ? "true" : value === false ? "false" : ""
                  }
                  disabled={disabled}
                  onChange={(e) =>
                    handleChange(
                      field.name,
                      // "" is a present, deliberate blank; absence is untouched.
                      e.target.value === "" ? "" : e.target.value === "true",
                    )
                  }
                  className="w-full px-3 py-1.5 rounded bg-neutral-800 border border-neutral-700 text-neutral-100 text-sm focus:border-cyan-500 focus:outline-none"
                >
                  <option value="">{t("core.formUnset")}</option>
                  <option value="true">{t("core.formTrue")}</option>
                  <option value="false">{t("core.formFalse")}</option>
                </select>
              ) : type === "multiline" ? (
                <textarea
                  id={`form-field-${field.name}`}
                  data-testid={`form-input-${field.name}`}
                  rows={4}
                  value={displayValue(field)}
                  disabled={disabled}
                  onChange={(e) => handleChange(field.name, e.target.value)}
                  className="w-full px-3 py-2 rounded bg-neutral-800 border border-neutral-700 text-neutral-100 text-sm focus:border-cyan-500 focus:outline-none"
                />
              ) : (
                <input
                  id={`form-field-${field.name}`}
                  data-testid={`form-input-${field.name}`}
                  type="text"
                  inputMode={
                    type === "integer" || type === "number"
                      ? "decimal"
                      : undefined
                  }
                  value={displayValue(field)}
                  disabled={disabled}
                  onChange={(e) => handleChange(field.name, e.target.value)}
                  placeholder={
                    isRequired(field) ? undefined : t("core.formOptional")
                  }
                  className="w-full px-3 py-1.5 rounded bg-neutral-800 border border-neutral-700 text-neutral-100 text-sm focus:border-cyan-500 focus:outline-none"
                />
              )}
            </div>
          );
        })}
      </div>

      <div className="flex items-center justify-end gap-3 mt-5 pt-3 border-t border-neutral-800">
        {onCancel && (
          <button
            type="button"
            disabled={disabled}
            onClick={onCancel}
            className="px-4 py-1.5 rounded bg-neutral-800 hover:bg-neutral-700 text-neutral-300 text-xs font-medium uppercase tracking-wide border border-neutral-700 transition-colors"
          >
            {t("core.formCancel")}
          </button>
        )}
        <button
          data-testid="form-submit-btn"
          type="submit"
          disabled={disabled}
          className="px-4 py-1.5 rounded bg-cyan-600 hover:bg-cyan-500 text-white text-xs font-medium uppercase tracking-wide transition-colors"
        >
          {t("core.formSubmit")}
        </button>
      </div>
    </form>
  );
};
