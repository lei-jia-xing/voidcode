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

export function artifactSources(result: ToolResult): ArtifactSource[] {
  const sources: ArtifactSource[] = [];
  const seen = new Set<string>();
  const add = (label: string, value: unknown): boolean => {
    let sha256: string | undefined;
    let reference: ArtifactReference | undefined;
    if (typeof value === "string" && value.startsWith("artifact://")) {
      sha256 = value.slice(11);
    } else if (value !== null && typeof value === "object") {
      const record = value as Record<string, unknown>;
      if (
        typeof record.sha256 === "string" &&
        Number.isSafeInteger(record.bytes) &&
        (record.bytes as number) >= 0 &&
        typeof record.media_type === "string"
      ) {
        sha256 = record.sha256;
        reference = value as ArtifactReference;
      }
    }
    if (!sha256 || !/^[0-9a-f]{64}$/.test(sha256)) return false;
    if (!seen.has(sha256)) {
      sources.push({ label, sha256, reference });
      seen.add(sha256);
    }
    return true;
  };
  add("Original output source", result.output.bounds?.reference);
  if (result.output.type === "composite") {
    result.output.parts.forEach((part, index) => {
      add(`Part ${index} (${part.kind})`, part.reference);
    });
  }
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
