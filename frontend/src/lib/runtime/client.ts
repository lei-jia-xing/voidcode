import {
  RuntimeRequest,
  StoredSessionSummary,
  RuntimeResponse,
  RuntimeStreamChunk,
  ApprovalDecision,
  RuntimeSettings,
  RuntimeSettingsUpdate,
} from './types';

export class RuntimeClient {
  static async listSessions(): Promise<StoredSessionSummary[]> {
    const res = await fetch(`/api/sessions`);
    if (!res.ok) throw new Error(`Failed to list sessions: ${res.statusText}`);
    return res.json();
  }

  static async getSessionReplay(sessionId: string): Promise<RuntimeResponse> {
    const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`);
    if (!res.ok) throw new Error(`Failed to replay session: ${res.statusText}`);
    return res.json();
  }

  static async resolveApproval(
    sessionId: string,
    requestId: string,
    decision: ApprovalDecision
  ): Promise<RuntimeResponse> {
    const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/approval`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ request_id: requestId, decision })
    });

    if (!res.ok) throw new Error(`Failed to resolve approval: ${res.statusText}`);
    return res.json();
  }

  static async getSettings(): Promise<RuntimeSettings> {
    const res = await fetch(`/api/settings`);
    if (!res.ok) throw new Error(`Failed to load settings: ${res.statusText}`);
    return res.json();
  }

  static async updateSettings(settings: RuntimeSettingsUpdate): Promise<RuntimeSettings> {
    const res = await fetch(`/api/settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(settings)
    });
    if (!res.ok) throw new Error(`Failed to save settings: ${res.statusText}`);
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
    let dataLines: string[] = [];

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      let eolIndex = buffer.indexOf('\n');
      while (eolIndex >= 0) {
        const line = buffer.slice(0, eolIndex);
        buffer = buffer.slice(eolIndex + 1);
        const trimmedLine = line.replace(/\r$/, '');

        if (trimmedLine === '') {
          // Empty line indicates end of an SSE event
          if (dataLines.length > 0) {
            try {
              const chunk = JSON.parse(dataLines.join('\n')) as RuntimeStreamChunk;
              yield chunk;
            } catch (e) {
              console.warn('Failed to parse SSE data chunk:', dataLines.join('\n'), e);
            }
            dataLines = [];
          }
        } else if (trimmedLine.startsWith('data:')) {
          const data = trimmedLine.slice(5).replace(/^ /, '');
          dataLines.push(data);
        }

        eolIndex = buffer.indexOf('\n');
      }
    }

    // Process any remaining buffered data after stream closes
    if (dataLines.length > 0) {
      try {
        const chunk = JSON.parse(dataLines.join('\n')) as RuntimeStreamChunk;
        yield chunk;
      } catch (e) {
        console.warn('Failed to parse trailing SSE data chunk:', dataLines.join('\n'), e);
      }
    }
  }
}
