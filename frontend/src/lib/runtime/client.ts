import {
  RuntimeRequest,
  StoredSessionSummary,
  RuntimeResponse,
  RuntimeStreamChunk
} from './types';

export class RuntimeClient {
  static async listSessions(): Promise<StoredSessionSummary[]> {
    const res = await fetch(`/api/sessions`);
    if (!res.ok) throw new Error(`Failed to list sessions: ${res.statusText}`);
    return res.json();
  }

  static async getSessionReplay(sessionId: string): Promise<RuntimeResponse> {
    const res = await fetch(`/api/sessions/${sessionId}`);
    if (!res.ok) throw new Error(`Failed to replay session: ${res.statusText}`);
    return res.json();
  }

  static async *runStream(request: RuntimeRequest): AsyncGenerator<RuntimeStreamChunk, void, unknown> {
    const res = await fetch(`/api/runtime/run/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request)
    });

    if (!res.ok) throw new Error(`Stream request failed: ${res.statusText}`);
    if (!res.body) throw new Error('No response body for stream');

    const reader = res.body.getReader();
    const decoder = new TextDecoder();

    let buffer = '';
    let dataBuffer = '';

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      let eolIndex;
      while ((eolIndex = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, eolIndex);
        buffer = buffer.slice(eolIndex + 1);
        const trimmedLine = line.replace(/\r$/, '');

        if (trimmedLine === '') {
          // Empty line indicates end of an SSE event
          if (dataBuffer) {
            try {
              const chunk = JSON.parse(dataBuffer) as RuntimeStreamChunk;
              yield chunk;
            } catch (e) {
              console.warn('Failed to parse SSE data chunk:', dataBuffer, e);
            }
            dataBuffer = '';
          }
        } else if (trimmedLine.startsWith('data:')) {
          const data = trimmedLine.slice(5).trimStart();
          dataBuffer += data;
        }
      }
    }

    // Process any remaining buffered data after stream closes
    if (dataBuffer) {
      try {
        const chunk = JSON.parse(dataBuffer) as RuntimeStreamChunk;
        yield chunk;
      } catch (e) {
        console.warn('Failed to parse trailing SSE data chunk:', dataBuffer, e);
      }
    }
  }
}
