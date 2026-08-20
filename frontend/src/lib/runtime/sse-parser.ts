import { RuntimeStreamChunk } from "./types";

/**
 * Incremental SSE frame parser. Accepts decoded text chunks and splits them
 * into complete `data:` payloads.
 *
 * This is the single implementation both `RuntimeClient.runStream` and
 * `RuntimeClient.sessionEvents` stream through. It encodes the tolerant wire
 * contract the runStream path has always relied on:
 *  - multi-line `data:` fields are joined with `\n`;
 *  - comment (`: ...`) and other unknown lines are ignored;
 *  - a single leading space after `data:` is stripped, matching SSE semantics;
 *  - trailing carriage returns are stripped (CRLF frames are accepted);
 *  - a trailing payload without a terminating blank line is flushed on close.
 */
export class SseFrameParser {
  private buffer = "";
  private dataLines: string[] = [];

  /** Feed decoded text; returns any complete `data:` payloads delimited by blank lines. */
  push(input: string): string[] {
    this.buffer += input;
    const frames: string[] = [];
    let eolIndex = this.buffer.indexOf("\n");
    while (eolIndex >= 0) {
      const line = this.buffer.slice(0, eolIndex);
      this.buffer = this.buffer.slice(eolIndex + 1);
      const trimmedLine = line.replace(/\r$/, "");
      if (trimmedLine === "") {
        // Empty line indicates the end of an SSE event.
        if (this.dataLines.length > 0) {
          frames.push(this.dataLines.join("\n"));
          this.dataLines = [];
        }
      } else if (trimmedLine.startsWith("data:")) {
        this.dataLines.push(trimmedLine.slice(5).replace(/^ /, ""));
      }
      eolIndex = this.buffer.indexOf("\n");
    }
    return frames;
  }

  /** Finalize the stream; flushes any trailing payload without a blank line. */
  flush(): string[] {
    const frames: string[] = [];
    if (this.buffer.length > 0) {
      const trimmedLine = this.buffer.replace(/\r$/, "");
      if (trimmedLine.startsWith("data:")) {
        this.dataLines.push(trimmedLine.slice(5).replace(/^ /, ""));
      }
      this.buffer = "";
    }
    if (this.dataLines.length > 0) {
      frames.push(this.dataLines.join("\n"));
      this.dataLines = [];
    }
    return frames;
  }
}

/**
 * Convert a raw SSE `data:` payload into a validated RuntimeStreamChunk.
 *
 * Tolerant by design: malformed payloads are logged and skipped rather than
 * aborting the stream, so a single bad event never kills a long-lived follow
 * or run stream.
 */
export function parseSseDataPayload(data: string): RuntimeStreamChunk | null {
  try {
    const chunk = parseRuntimeStreamChunk(JSON.parse(data));
    if (chunk.event) chunk.event.received_at = Date.now();
    return chunk;
  } catch (error) {
    console.warn("Failed to parse SSE data chunk:", data, error);
    return null;
  }
}

function parseRuntimeStreamChunk(value: unknown): RuntimeStreamChunk {
  if (!value || typeof value !== "object") {
    throw new Error("runtime stream chunk must be an object");
  }
  const chunk = value as Partial<RuntimeStreamChunk>;
  if (
    chunk.kind !== "session" &&
    chunk.kind !== "event" &&
    chunk.kind !== "output"
  ) {
    throw new Error("runtime stream chunk has invalid kind");
  }
  if (
    chunk.session !== null &&
    (chunk.session === undefined || typeof chunk.session !== "object")
  ) {
    throw new Error("runtime stream chunk has invalid session");
  }
  if (chunk.event !== null && chunk.event !== undefined) {
    if (
      typeof chunk.event !== "object" ||
      typeof chunk.event.sequence !== "number"
    ) {
      throw new Error("runtime stream event has invalid sequence");
    }
  }
  if (
    chunk.output !== null &&
    chunk.output !== undefined &&
    typeof chunk.output !== "string"
  ) {
    throw new Error("runtime stream output must be a string or null");
  }
  return chunk as RuntimeStreamChunk;
}
