import { createParser, type EventSourceParser } from "eventsource-parser";

import { RuntimeStreamChunk } from "./types";

/**
 * Incremental SSE frame parser. Accepts decoded text chunks and splits them
 * into complete `data:` payloads.
 *
 * This is the single implementation both `RuntimeClient.runStream` and
 * `RuntimeClient.sessionEvents` stream through. It is a thin adapter over
 * `eventsource-parser`'s `createParser`, which owns the tolerant wire contract
 * the runStream path has always relied on:
 *  - multi-line `data:` fields are joined with `\n`;
 *  - comment (`: ...`) and other unknown lines are ignored;
 *  - a single leading space after `data:` is stripped, matching SSE semantics;
 *  - trailing carriage returns are stripped (CRLF frames are accepted);
 *  - only frames terminated by a blank line are complete.
 *
 * End-of-stream differs from the SSE spec: the runStream path also needs the
 * trailing payload of a stream that closes without a terminating blank line.
 * Measured with a throwaway probe against `eventsource-parser@4.1.1`:
 * `feed('data: {"a":1}')` emits nothing, and neither `reset()` nor
 * `reset({ consume: true })` emits it afterwards — the library drops every
 * event whose terminating blank line never arrives. `flush()` therefore feeds a
 * synthetic blank line, which completes a buffered partial line and terminates
 * the pending event block without emitting anything when no `data:` field is
 * pending (empty blocks never dispatch).
 *
 * `reset()` is deliberately not used: there is no reconnect path here, and
 * reconnecting a stream is not the same operation as re-running an agent run.
 * Parse errors are ignored (no `onError` callback), matching the previous
 * hand-rolled parser, which skipped unparsable lines silently.
 */
export class SseFrameParser {
  private readonly parser: EventSourceParser;
  private frames: string[] = [];

  constructor() {
    this.parser = createParser({
      onEvent: (event) => {
        this.frames.push(event.data);
      },
    });
  }

  /** Feed decoded text; returns any complete `data:` payloads delimited by blank lines. */
  push(input: string): string[] {
    this.frames = [];
    this.parser.feed(input);
    return this.frames;
  }

  /** Finalize the stream; flushes any trailing payload without a blank line. */
  flush(): string[] {
    this.frames = [];
    this.parser.feed("\n\n");
    return this.frames;
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
