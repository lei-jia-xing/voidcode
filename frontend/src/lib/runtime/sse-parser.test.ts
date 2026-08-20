import { afterEach, describe, expect, it, vi } from "vitest";

import { SseFrameParser, parseSseDataPayload } from "./sse-parser";

describe("SseFrameParser frame splitting", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("splits frames on blank lines and joins multi-line data fields", () => {
    const parser = new SseFrameParser();
    const frames = parser.push(
      'data: {"kind":"event","session":null,"event":null,"output":"one"}\n\n' +
        'data: {"a":1}\ndata: {"b":2}\n\n',
    );
    expect(frames).toEqual([
      '{"kind":"event","session":null,"event":null,"output":"one"}',
      '{"a":1}\n{"b":2}',
    ]);
    expect(parser.flush()).toEqual([]);
  });

  it("strips the single leading space after the data: prefix", () => {
    const parser = new SseFrameParser();
    const frames = parser.push('data:{"a":1}\n\ndata: {"b":2}\n\n');
    expect(frames).toEqual(['{"a":1}', '{"b":2}']);
  });

  it("ignores comment and other non-data lines", () => {
    const parser = new SseFrameParser();
    const frames = parser.push(
      ': ignored\nevent: message\nid: 42\ndata: {"a":1}\n\n',
    );
    expect(frames).toEqual(['{"a":1}']);
  });

  it("accepts CRLF frame terminators and strips trailing carriage returns", () => {
    const parser = new SseFrameParser();
    const frames = parser.push(': ignored\r\ndata: {"a":1}\r\n\r\n');
    expect(frames).toEqual(['{"a":1}']);
  });

  it("accumulates fragmented chunks across push calls", () => {
    const parser = new SseFrameParser();
    expect(parser.push('data: {"a":')).toEqual([]);
    expect(parser.push('1}\n\ndata: {"b":2}')).toEqual(['{"a":1}']);
    expect(parser.flush()).toEqual(['{"b":2}']);
  });

  it("flushes a trailing data payload without a terminating blank line", () => {
    const parser = new SseFrameParser();
    expect(parser.push('data: {"a":1}\n\n')).toEqual(['{"a":1}']);
    expect(parser.push('data: {"b":2}')).toEqual([]);
    expect(parser.flush()).toEqual(['{"b":2}']);
  });

  it("does not emit empty frames for blank lines with no buffered data", () => {
    const parser = new SseFrameParser();
    expect(parser.push("\n\n")).toEqual([]);
    expect(parser.flush()).toEqual([]);
  });
});

describe("parseSseDataPayload chunk conversion", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("parses and validates a valid event chunk, stamping received_at", () => {
    const chunk = parseSseDataPayload(
      '{"kind":"event","session":null,"event":{"sequence":1},"output":null}',
    );
    expect(chunk?.kind).toBe("event");
    expect(chunk?.event?.sequence).toBe(1);
    expect(typeof chunk?.event?.received_at).toBe("number");
  });

  it("leaves output chunks un-stamped and returns them intact", () => {
    const chunk = parseSseDataPayload(
      '{"kind":"output","session":null,"event":null,"output":"done"}',
    );
    expect(chunk?.output).toBe("done");
    expect(chunk?.event).toBeNull();
  });

  it("returns null for malformed JSON instead of throwing", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    expect(parseSseDataPayload("{bad json}")).toBeNull();
    expect(warn).toHaveBeenCalled();
  });

  it("returns null for structurally invalid chunks instead of throwing", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    expect(
      parseSseDataPayload(
        '{"kind":"bogus","session":null,"event":null,"output":null}',
      ),
    ).toBeNull();
    expect(warn).toHaveBeenCalled();
  });
});
