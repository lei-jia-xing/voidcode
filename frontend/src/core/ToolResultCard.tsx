import { useEffect, useMemo, useRef, useState } from "react";
import type { CoreClient } from "../lib/core/client";
import type {
  ArtifactPage,
  ArtifactReference,
  Icon,
  ToolExecution,
  TranscriptTool,
} from "../lib/core/types";
import { artifactSources, toolOutputParts } from "./toolOutput";
import type { ArtifactSource } from "./toolOutput";

/** Tool birth icon from the recorded transcript value; absent means generic. */
function toolIconText(icon: Icon | null | undefined, generic: string): string {
  const pick = icon?.glyph;
  return typeof pick === "string" ? pick : generic;
}

/** Execution cue key for stopped/unknown-effects tools, else null. */
function toolExecutionCueKey(
  execution: ToolExecution | null | undefined,
): string | null {
  if (!execution) return null;
  if (!execution.execution_stopped) return "core.toolStoppedUnconfirmed";
  if (execution.side_effect_state === "unknown")
    return "core.toolEffectsUnknown";
  return null;
}

function decodeArtifactText(
  data: Uint8Array,
  carry: Uint8Array,
  eof: boolean,
): { text: string; carry: Uint8Array } {
  let bytes = data;
  if (carry.length) {
    bytes = new Uint8Array(carry.length + data.length);
    bytes.set(carry);
    bytes.set(data, carry.length);
  }
  let end = bytes.length;
  if (!eof && end) {
    let start = end - 1;
    while (start >= 0 && (bytes[start] & 0xc0) === 0x80) start--;
    const lead = bytes[start];
    const length =
      lead >= 0xc2 && lead <= 0xdf
        ? 2
        : lead >= 0xe0 && lead <= 0xef
          ? 3
          : lead >= 0xf0 && lead <= 0xf4
            ? 4
            : 1;
    if (start >= 0 && end - start < length) end = start;
  }
  return {
    text: new TextDecoder("utf-8", { fatal: true }).decode(
      bytes.subarray(0, end),
    ),
    carry: bytes.slice(end),
  };
}

function ArtifactViewer({
  getClient,
  source,
  revisionId,
  coreStatus,
}: {
  getClient: () => CoreClient;
  source: ArtifactSource;
  revisionId: string;
  coreStatus: string;
}) {
  const client = getClient();
  const [initialRevision] = useState(revisionId);
  const [offsets, setOffsets] = useState([0]);
  const [index, setIndex] = useState(0);
  const [reload, setReload] = useState(0);
  const [pageResult, setPageResult] = useState<{
    client: CoreClient;
    sha256: string;
    offset: number;
    reload: number;
    page: ArtifactPage | null;
    text: string | null;
    error: string | null;
  } | null>(null);
  const [reference, setReference] = useState<ArtifactReference | null>(
    source.reference ?? null,
  );
  const metadata = useRef<ArtifactReference | null>(source.reference ?? null);
  const decoderStates = useRef(
    new Map<number, Uint8Array>([[0, new Uint8Array()]]),
  );
  const firstPage = useRef<ArtifactPage | null>(null);
  const [firstPageReady, setFirstPageReady] = useState(false);
  const [imageUrl, setImageUrl] = useState<string | null>(null);
  const [imageError, setImageError] = useState<string | null>(null);
  const [imageProgress, setImageProgress] = useState<string>("");
  const [imageDimensions, setImageDimensions] = useState<string>("");
  const offset = offsets[index];
  const mediaType = reference?.media_type ?? "application/octet-stream";
  const isImage = mediaType.startsWith("image/");
  const currentPageResult =
    pageResult?.client === client &&
    pageResult.sha256 === source.sha256 &&
    pageResult.offset === offset &&
    pageResult.reload === reload
      ? pageResult
      : null;
  const page = currentPageResult?.page ?? null;
  const text = currentPageResult?.text ?? null;
  const error = currentPageResult?.error ?? null;
  const loading = currentPageResult === null;

  useEffect(() => {
    let cancelled = false;
    client
      .readArtifact(source.sha256, { offset })
      .then((next) => {
        if (cancelled) return;
        if (offset === 0) {
          firstPage.current = next;
          setFirstPageReady(true);
        }
        if (
          metadata.current &&
          (metadata.current.bytes !== next.artifact.bytes ||
            metadata.current.media_type !== next.artifact.media_type)
        )
          throw new Error("Immutable artifact metadata changed between pages");
        metadata.current = next.artifact;
        setReference(next.artifact);
        const media = next.artifact.media_type.split(";", 1)[0];
        let decodedText: string | null = null;
        let decodeError: string | null = null;
        if (
          media.startsWith("text/") ||
          media === "application/json" ||
          media.endsWith("+json") ||
          media === "application/xml"
        ) {
          const carry = decoderStates.current.get(offset);
          if (!carry)
            throw new Error("UTF-8 page boundary state is unavailable");
          try {
            const decoded = decodeArtifactText(next.data, carry, next.eof);
            decoderStates.current.set(
              offset + next.data.byteLength,
              decoded.carry,
            );
            decodedText = decoded.text;
          } catch (failure) {
            decodeError = `Source bytes are not valid UTF-8: ${String(failure)}. Raw bytes are shown in hex.`;
          }
        }
        setPageResult({
          client,
          sha256: source.sha256,
          offset,
          reload,
          page: next,
          text: decodedText,
          error: decodeError,
        });
      })
      .catch((failure: unknown) => {
        if (!cancelled) {
          setPageResult({
            client,
            sha256: source.sha256,
            offset,
            reload,
            page: null,
            text: null,
            error: String(failure),
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [client, source.sha256, offset, reload]);

  useEffect(() => {
    if (!isImage || !firstPageReady) return;
    let cancelled = false;
    let objectUrl: string | null = null;
    void (async () => {
      const parts: ArrayBuffer[] = [];
      let cursor = 0;
      let reference: ArtifactReference | null = null;
      while (!cancelled) {
        const next =
          cursor === 0 && firstPage.current
            ? firstPage.current
            : await client.readArtifact(source.sha256, { offset: cursor });
        if (cancelled) return;
        if (
          reference &&
          (next.artifact.bytes !== reference.bytes ||
            next.artifact.media_type !== reference.media_type)
        )
          throw new Error("Immutable image metadata changed between pages");
        reference = next.artifact;
        parts.push(next.data.buffer as ArrayBuffer);
        cursor += next.data.byteLength;
        setImageProgress(
          `Image pages: bytes [0, ${cursor}) / ${reference.bytes} · EOF ${next.eof ? "yes" : "no"}`,
        );
        if (next.eof) {
          const blob = new Blob(parts, { type: reference.media_type });
          const digest = Array.from(
            new Uint8Array(
              await crypto.subtle.digest("SHA-256", await blob.arrayBuffer()),
            ),
            (byte) => byte.toString(16).padStart(2, "0"),
          ).join("");
          if (digest !== source.sha256)
            throw new Error(
              "Image bytes do not match the immutable SHA-256 identity",
            );
          // Chromium can fire img.onload with geometry but transparent pixels for
          // a broken PNG. Force an actual raster decode before claiming a view.
          const decoded = await createImageBitmap(blob);
          decoded.close();
          if (cancelled) return;
          objectUrl = URL.createObjectURL(blob);
          setImageUrl(objectUrl);
          return;
        }
      }
    })().catch((failure: unknown) => {
      if (!cancelled) setImageError(String(failure));
    });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [client, source.sha256, isImage, firstPageReady]);

  const nextPage = () => {
    if (!page || page.eof || !page.data.byteLength) return;
    const nextOffset = page.offset + page.data.byteLength;
    setOffsets((previous) => [...previous.slice(0, index + 1), nextOffset]);
    setIndex(index + 1);
  };
  const hex =
    page && text === null && !isImage
      ? Array.from({ length: Math.ceil(page.data.length / 16) }, (_, row) => {
          const start = row * 16;
          return `${(page.offset + start).toString(16).padStart(8, "0")}  ${Array.from(page.data.subarray(start, start + 16), (byte) => byte.toString(16).padStart(2, "0")).join(" ")}`;
        }).join("\n")
      : null;
  return (
    <section
      aria-label="Artifact viewer"
      className="mt-2 border-t border-neutral-700 p-2 text-neutral-200"
    >
      <div className="select-all break-all">
        URI: artifact://{source.sha256}
      </div>
      <div>Media type: {mediaType}</div>
      <div>Core status: {coreStatus}</div>
      {initialRevision !== revisionId && (
        <div role="status">
          Revision changed to {revisionId}; this immutable workspace source is
          retained.
        </div>
      )}
      <div data-testid="artifact-range">
        {page
          ? `Page ${index + 1} · bytes [${page.offset}, ${page.offset + page.data.byteLength}) / ${page.artifact.bytes} · EOF ${page.eof ? "yes" : "no"}`
          : `Page ${index + 1} · requested offset ${offset}${loading ? " · loading…" : " · not loaded"}`}
      </div>
      <div className="flex gap-3 my-1">
        <button
          type="button"
          disabled={loading || index === 0}
          onClick={() => setIndex(index - 1)}
        >
          Back
        </button>
        <button
          type="button"
          disabled={loading || !page || page.eof}
          onClick={nextPage}
        >
          Next
        </button>
        <button
          type="button"
          disabled={loading}
          onClick={() => setReload((value) => value + 1)}
        >
          Reload page
        </button>
      </div>
      {error && (
        <div role="alert" className="text-red-400">
          Artifact page load/decoding error: {error}
        </div>
      )}
      {page && !isImage && (
        <pre
          data-testid="artifact-page-content"
          className="whitespace-pre-wrap break-words max-h-96 overflow-auto"
        >
          {text ?? hex}
        </pre>
      )}
      {isImage && (
        <div>
          <div>{imageProgress || "Fetching immutable image pages…"}</div>
          {imageError && (
            <div role="alert" className="text-red-400">
              Image load/decode failed: {imageError}
            </div>
          )}
          {imageUrl && !imageError && (
            <img
              src={imageUrl}
              alt={`${source.label} artifact`}
              className="max-w-full max-h-[70vh] object-contain"
              onLoad={(event) =>
                setImageDimensions(
                  `${event.currentTarget.naturalWidth}×${event.currentTarget.naturalHeight}`,
                )
              }
              onError={() =>
                setImageError("Browser rejected the original image bytes")
              }
            />
          )}
          {imageDimensions && <div>Decoded image: {imageDimensions}</div>}
        </div>
      )}
    </section>
  );
}

export function ToolResultCard({
  evSequence,
  tool,
  getClient,
  sessionId,
  revisionId,
  coreStatus,
  t,
}: {
  evSequence: number;
  tool: TranscriptTool;
  getClient: () => CoreClient;
  sessionId: string;
  revisionId: string;
  coreStatus: string;
  t: (key: string) => string;
}) {
  const [expanded, setExpanded] = useState(false);
  const [sourceIndex, setSourceIndex] = useState(0);
  const result = tool.result;
  const sources = useMemo(() => artifactSources(result), [result]);
  const source = sources[sourceIndex];
  const toolName =
    tool.call.tool_name ?? result.tool_name ?? t("core.unknownLabel");
  const blocks = useMemo(() => toolOutputParts(result.output), [result.output]);
  const isComposite = result.output.type === "composite";
  const text = blocks[0] ?? "";
  const failed = tool.interrupted === true || result.status === "error";
  const cueKey = toolExecutionCueKey(result.execution);
  return (
    <div
      data-testid={`event-${evSequence}`}
      className={`text-xs font-mono px-3 py-1.5 rounded border max-w-2xl ${failed ? "text-red-400 bg-red-950/20 border-red-900/50" : "text-emerald-400 bg-emerald-950/20 border-emerald-900/50"}`}
    >
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div>
          {failed ? "✖" : "✔"} {toolIconText(tool.icon, "▷")}{" "}
          {t("core.toolLabel")} [{toolName}]:{" "}
          {!isComposite && <>{failed ? (result.error ?? text) : text}</>}
          {cueKey && <span className="text-neutral-400"> ({t(cueKey)})</span>}
          {result.output.bounds?.truncated && (
            <span
              data-testid={`event-${evSequence}-truncated-tag`}
              className="text-amber-400"
            >
              {" "}
              [selected inline content; source available separately]
            </span>
          )}
        </div>
        {sources.length > 0 && (
          <button
            type="button"
            data-testid={`event-${evSequence}-toggle-output`}
            onClick={() => setExpanded(!expanded)}
            className="underline text-cyan-400"
          >
            {expanded ? "Hide artifacts" : "View artifacts"}
          </button>
        )}
      </div>
      {isComposite && (
        <div
          data-testid={`event-${evSequence}-parts`}
          className="whitespace-pre-wrap mt-1 flex flex-col gap-1"
        >
          {blocks.map((block, index) => (
            <div key={index}>{block}</div>
          ))}
          {failed && <div>{result.error ?? "Interrupted"}</div>}
        </div>
      )}
      {expanded && source && (
        <div data-testid={`event-${evSequence}-full-output`}>
          <label>
            Artifact source{" "}
            <select
              aria-label="Artifact source"
              value={sourceIndex}
              onChange={(event) => setSourceIndex(Number(event.target.value))}
              className="bg-neutral-900"
            >
              {sources.map((item, index) => (
                <option key={item.sha256} value={index}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <ArtifactViewer
            key={`${sessionId}:${source.sha256}`}
            getClient={getClient}
            source={source}
            revisionId={revisionId}
            coreStatus={coreStatus}
          />
        </div>
      )}
    </div>
  );
}
