/**
 * Canonical types for the VoidCode core client, derived from authoritative backend OpenAPI schemas.
 */

import type { components } from "./generated/api";

export type Schemas = components["schemas"];

export type CommandDescriptor = Schemas["CommandDescriptor"];
export type CommandResult = Schemas["CommandResult"];
export type HealthBody = Schemas["HealthBody"];
export type SessionRecord = Schemas["SessionRecord"];
export type CoreResult = Schemas["CoreResult"];
export type SessionUsageReport = Schemas["SessionUsageReport"];
export type StoredEventBody = Schemas["StoredEventBody"];
export type ShowBody = Schemas["ShowBody"];
export type PluginViewDescriptor = Schemas["PluginViewDescriptor"];
export type PluginViewData = Schemas["PluginViewData"];

export type NoticeViewData = Schemas["NoticeViewData"];
export type MarkdownViewData = Schemas["MarkdownViewData"];
export type FieldsViewData = Schemas["FieldsViewData"];
export type FieldRow = Schemas["FieldRow"];
export type ChoicesViewData = Schemas["ChoicesViewData"];
export type ChoiceOption = Schemas["ChoiceOption"];
export type ConfirmationViewData = Schemas["ConfirmationViewData"];
export type FormViewData = Schemas["FormViewData"];
export type FormField = Schemas["FormField"];
export type DiffViewData = Schemas["DiffViewData"];
export type StatusViewData = Schemas["StatusViewData"];

export interface SseEventFrame {
  kind?: "event" | "error" | "server_restart";
  session_id?: string;
  /**
   * Restart watermark: the last sequence THAT stream delivered. Corroboration
   * only — resume from the client's own applied cursor, never jump forward.
   */
  cursor?: number;
  event?: {
    session_id: string;
    sequence: number;
    type: string;
    run_id: string;
    revision_id: string;
    source: string;
    actor: string;
    payload: Record<string, unknown>;
    transcript?: Record<string, unknown> | null;
  };
  session?: SessionRecord;
  events?: Array<{
    session_id: string;
    sequence: number;
    type: string;
    run_id: string;
    revision_id: string;
    source: string;
    actor: string;
    payload: Record<string, unknown>;
    transcript?: Record<string, unknown> | null;
  }>;
  error?: string;
}
