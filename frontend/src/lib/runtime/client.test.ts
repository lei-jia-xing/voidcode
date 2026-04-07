import { afterEach, describe, expect, it, vi } from 'vitest';
import { RuntimeClient } from './client';

function makeStreamResponse(body: string): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(body));
      controller.close();
    },
  });

  return new Response(stream, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream; charset=utf-8' },
  });
}

describe('RuntimeClient', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('parses ordered SSE runtime chunks from the live transport format', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      makeStreamResponse(
        'data: {"kind":"event","session":{"session":{"id":"session-123"},"status":"running","turn":1,"metadata":{"workspace":"/tmp/workspace"}},"event":{"session_id":"session-123","sequence":1,"event_type":"runtime.request_received","source":"runtime","payload":{"prompt":"read README.md"}},"output":null}\n\n' +
          'data: {"kind":"output","session":{"session":{"id":"session-123"},"status":"completed","turn":1,"metadata":{"workspace":"/tmp/workspace"}},"event":null,"output":"done"}\n\n'
      )
    );
    vi.stubGlobal('fetch', fetchMock);

    const chunks = [];
    for await (const chunk of RuntimeClient.runStream({ prompt: 'read README.md', session_id: 'session-123' })) {
      chunks.push(chunk);
    }

    expect(fetchMock).toHaveBeenCalledWith('/api/runtime/run/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt: 'read README.md', session_id: 'session-123' }),
    });
    expect(chunks).toEqual([
      {
        kind: 'event',
        session: {
          session: { id: 'session-123' },
          status: 'running',
          turn: 1,
          metadata: { workspace: '/tmp/workspace' },
        },
        event: {
          session_id: 'session-123',
          sequence: 1,
          event_type: 'runtime.request_received',
          source: 'runtime',
          payload: { prompt: 'read README.md' },
        },
        output: null,
      },
      {
        kind: 'output',
        session: {
          session: { id: 'session-123' },
          status: 'completed',
          turn: 1,
          metadata: { workspace: '/tmp/workspace' },
        },
        event: null,
        output: 'done',
      },
    ]);
  });
});
