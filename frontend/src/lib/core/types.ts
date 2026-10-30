/**
 * Canonical types for the VoidCode core client, derived from authoritative backend OpenAPI schemas.
 */

import type { components } from "./generated/api";

export type Schemas = components["schemas"];

export type CommandDescriptor = Schemas["CommandDescriptor"];
export type CommandInputSnapshot = Schemas["CommandInputSnapshot"];
export type CommandResult = Schemas["CommandResult"];
export type HealthStatus = Schemas["HealthStatus"];
export type SessionRow = Schemas["SessionRow"];
export type RunResult = Schemas["RunResult"];
export type SessionUsageReport = Schemas["SessionUsageReport"];
export type StoredEventBody = Schemas["StoredEventBody"];
export type ShowBody = Schemas["ShowBody"];
export type PluginViewDescriptor = Schemas["PluginViewDescriptor"];
export type PluginViewData = Schemas["PluginViewData"];
export type ComponentStyle = Schemas["ComponentStyle"];
export type Icon = Schemas["Icon"];
export type TranscriptEntry = Schemas["TranscriptEntryWire"];
export type TranscriptContent = TranscriptEntry["content"];
export type TranscriptTool = Schemas["TranscriptToolWire"];
export type ToolCall = Schemas["ToolCallWire"];
export type ToolResult = Schemas["ToolResultWire"];
export type ToolExecution = Schemas["ExecutionWire"];
export type ToolOutput = ToolResult["output"];

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
// Session frames: the generated union is the contract. The head carries no kind,
// so TS narrows it via `'events' in frame`; the other three narrow on `kind`.
export type SessionFrame = Schemas["SessionFrameWire"];
export type StreamEvent = Schemas["StreamEventWire"];
export type HeadFrame = Schemas["HeadFrameWire"];
export type EventFrame = Schemas["EventFrameWire"];
export type ErrorFrame = Schemas["ErrorFrameWire"];
export type RestartFrame = Schemas["RestartFrameWire"];
