import { EventEnvelope } from './types';

export interface DerivedTask {
  id: string;
  titleKey: string;
  titleValues?: Record<string, string>;
  type: 'request' | 'tool' | 'approval' | 'response' | 'unknown';
  status: 'pending' | 'in_progress' | 'completed' | 'failed' | 'waiting';
  sequence: number;
  events: EventEnvelope[];
}

export function deriveTasksFromEvents(events: EventEnvelope[]): DerivedTask[] {
  const tasks: DerivedTask[] = [];
  let currentToolTask: DerivedTask | null = null;
  let currentRequest: DerivedTask | null = null;

  for (const event of events) {
    if (event.event_type === 'runtime.request_received') {
      if (currentToolTask && currentToolTask.status === 'in_progress') {
        currentToolTask.status = 'completed';
      }
      currentToolTask = null;

      if (currentRequest && currentRequest.status === 'in_progress') {
        currentRequest.status = 'completed';
      }

      const prompt = typeof event.payload?.prompt === 'string' ? event.payload.prompt : 'Unknown Request';
      currentRequest = {
        id: `req-${event.sequence}`,
        titleKey: 'task.request',
        titleValues: { prompt },
        type: 'request',
        status: 'in_progress',
        sequence: event.sequence,
        events: [event]
      };
      tasks.push(currentRequest);

    } else if (event.event_type === 'graph.tool_request_created') {
      if (currentToolTask && currentToolTask.status === 'in_progress') {
        currentToolTask.status = 'completed';
      }
      const toolName = typeof event.payload?.tool === 'string' ? event.payload.tool : 'unknown';
      currentToolTask = {
        id: `tool-${event.sequence}`,
        titleKey: 'task.tool',
        titleValues: { tool: toolName },
        type: 'tool',
        status: 'in_progress',
        sequence: event.sequence,
        events: [event]
      };
      tasks.push(currentToolTask);

    } else if (event.event_type === 'runtime.tool_completed') {
      if (currentToolTask) {
        currentToolTask.status = 'completed';
        currentToolTask.events.push(event);
        currentToolTask = null;
      } else {
        tasks.push({
          id: `tool-done-${event.sequence}`,
          titleKey: 'task.unknown',
          titleValues: { type: 'Tool Completed (Orphaned)' },
          type: 'tool',
          status: 'completed',
          sequence: event.sequence,
          events: [event]
        });
      }

    } else if (event.event_type === 'graph.response_ready') {
      if (currentToolTask && currentToolTask.status === 'in_progress') {
        currentToolTask.status = 'completed';
      }
      currentToolTask = null;
      if (currentRequest && currentRequest.status === 'in_progress') {
        currentRequest.status = 'completed';
      }
      tasks.push({
        id: `resp-${event.sequence}`,
        titleKey: 'task.response',
        type: 'response',
        status: 'completed',
        sequence: event.sequence,
        events: [event]
      });

    } else if (event.event_type === 'runtime.permission_resolved' || event.event_type === 'runtime.approval_requested' || event.event_type === 'runtime.approval_resolved') {
      if (currentToolTask) {
        currentToolTask.events.push(event);
        const decision = event.payload?.decision;
        if (decision === 'deny') {
          currentToolTask.status = 'failed';
        } else if (decision === 'ask') {
          currentToolTask.status = 'waiting';
        }
      } else {
        const toolName = typeof event.payload?.tool === 'string' ? event.payload.tool : 'unknown';
        tasks.push({
          id: `perm-${event.sequence}`,
          titleKey: 'task.permission',
          titleValues: { tool: toolName },
          type: 'approval',
          status: 'completed',
          sequence: event.sequence,
          events: [event]
        });
      }

    } else {
      // Any other intermediate event (like tool_lookup_succeeded)
      if (currentToolTask) {
        currentToolTask.events.push(event);
      } else {
        tasks.push({
          id: `evt-${event.sequence}`,
          titleKey: 'task.unknown',
          titleValues: { type: event.event_type },
          type: 'unknown',
          status: 'completed',
          sequence: event.sequence,
          events: [event]
        });
      }
    }
  }

  return tasks;
}

export function deriveActivitiesFromEvents(events: EventEnvelope[]) {
  return events.map(event => {
    let payloadStr = '';
    try {
      payloadStr = event.payload ? JSON.stringify(event.payload) : '';
    } catch {
      payloadStr = '{...}';
    }

    return {
      id: `act-${event.sequence}`,
      type: 'log' as const,
      message: event.event_type,
      source: event.source,
      timestamp: '',
      sequence: event.sequence,
      payloadStr
    };
  });
}
