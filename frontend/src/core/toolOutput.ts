import type {
  ArtifactReference,
  ToolOutput,
  ToolOutputPart,
  ToolResult,
} from "../lib/core/types";

export type ArtifactSource = {
  label: string;
  sha256: string;
  reference?: ArtifactReference;
};

/** One composite part's ordered readable text; unknown kinds render an explicit marker. */
export function toolPartText(part: ToolOutputPart): string {
  switch (part.kind) {
    case "text": {
      const uri = part.reference ? `artifact://${part.reference.sha256}` : null;
      if (uri)
        return part.text
          ? `${part.text}\n[full text: ${uri}]`
          : `[full text: ${uri}]`;
      return part.text;
    }
    case "media": {
      const label = part.mime.startsWith("image/")
        ? "Image"
        : part.mime.startsWith("audio/")
          ? "Audio"
          : "Media";
      return `[${label}: ${part.mime}, ${part.reference.bytes} bytes, artifact://${part.reference.sha256}] ${part.presentation}`;
    }
    case "table": {
      if (part.columns.length === 0) return "[table: missing columns]";
      const rows = part.rows ?? [];
      const lines = [
        part.columns.join("\t"),
        ...rows.map((row) => row.join("\t")),
      ];
      if (part.presentation) lines.unshift(part.presentation);
      if (part.total_rows != null && part.total_rows > rows.length) {
        const uri = part.reference
          ? `artifact://${part.reference.sha256}`
          : null;
        lines.push(
          uri
            ? `[showing ${rows.length}/${part.total_rows} rows; full table: ${uri}]`
            : `[showing ${rows.length}/${part.total_rows} rows]`,
        );
      }
      return lines.join("\n");
    }
    case "json":
      return `${part.preview}\n[full JSON: artifact://${part.reference.sha256}]`;
    default: {
      const raw: unknown = part;
      const kind =
        typeof raw === "object" && raw !== null && "kind" in raw
          ? raw.kind
          : undefined;
      return `[unknown part kind: ${JSON.stringify(kind)}]`;
    }
  }
}

/** Ordered readable blocks for one typed tool output; single variants yield one block. */
export function toolOutputParts(output: ToolOutput): string[] {
  switch (output.type) {
    case "text":
      return [output.presentation || output.text];
    case "attachment":
      return [`[${output.mime}]`];
    case "empty":
      return output.presentation ? [output.presentation] : [];
    case "composite":
      return output.parts.map(toolPartText).filter((block) => block !== "");
    default: {
      const raw: unknown = output;
      const type =
        typeof raw === "object" && raw !== null && "type" in raw
          ? raw.type
          : undefined;
      return [`[unknown output type: ${JSON.stringify(type)}]`];
    }
  }
}

/** Explicit sha256-hex check (TUI `_artifact_ref_uri` parity); never a regex. */
function isArtifactDigest(value: unknown): value is string {
  if (typeof value !== "string" || value.length !== 64) return false;
  for (let index = 0; index < value.length; index++) {
    const code = value.charCodeAt(index);
    const hex = (code >= 48 && code <= 57) || (code >= 97 && code <= 102);
    if (!hex) return false;
  }
  return true;
}

/**
 * Coerce one whole value to an artifact reference: a declared
 * `ArtifactReference` shape or a whole `artifact://` URI. Free-form text is
 * never substring-scanned: a string only matches when the entire value is the
 * URI, and an object only when it carries the exact reference shape.
 */
function asReference(
  value: unknown,
): { sha256: string; reference?: ArtifactReference } | null {
  if (typeof value === "string") {
    if (!value.startsWith("artifact://")) return null;
    const sha256 = value.slice("artifact://".length);
    return isArtifactDigest(sha256) ? { sha256 } : null;
  }
  if (value !== null && typeof value === "object") {
    const record = value as Record<string, unknown>;
    if (
      isArtifactDigest(record.sha256) &&
      Number.isSafeInteger(record.bytes) &&
      (record.bytes as number) >= 0 &&
      typeof record.media_type === "string"
    ) {
      return {
        sha256: record.sha256,
        reference: value as ArtifactReference,
      };
    }
  }
  return null;
}

export function artifactSources(result: ToolResult): ArtifactSource[] {
  const sources: ArtifactSource[] = [];
  const seen = new Set<string>();
  const add = (label: string, value: unknown): boolean => {
    const found = asReference(value);
    if (!found) return false;
    if (!seen.has(found.sha256)) {
      sources.push({
        label,
        sha256: found.sha256,
        reference: found.reference,
      });
      seen.add(found.sha256);
    }
    return true;
  };
  add("Original output source", result.output.bounds?.reference);
  if (result.output.type === "composite") {
    result.output.parts.forEach((part, index) => {
      add(`Part ${index} (${part.kind})`, part.reference);
    });
  }
  // Body-carried references: real tool bodies carry ArtifactReference payloads
  // and whole artifact:// URIs outside the declared output envelope (shell_exec
  // stdout/stderr, read_tool/resource_pages/web_fetch/user_shell `artifact`,
  // mcp result_artifact/artifacts, review/subagents output_uri, workflows
  // result_uri; the e2e artifact scene covers body.* explicitly). Only whole
  // well-formed reference values are accepted here; free-form text is never
  // substring-scanned. This walk stays until the backend projects every body
  // reference into declared output/result_reference fields.
  const visit = (value: unknown, path: string) => {
    if (add(path, value)) return;
    if (Array.isArray(value))
      value.forEach((child, index) => visit(child, `${path}[${index}]`));
    else if (value !== null && typeof value === "object")
      Object.entries(value).forEach(([name, child]) =>
        visit(child, `${path}.${name}`),
      );
  };
  visit(result.body, "Result body");
  add("Complete structured result JSON", result.result_reference);
  return sources;
}
