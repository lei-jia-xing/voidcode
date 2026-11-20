# Public committed transcript

`PluginServices.read_transcript(after_sequence=0, limit=100)` exposes typed
conversation records for the calling session. The core decodes storage events;
consumers do not depend on `core.*` names or nested payload layouts. This uses
the existing SQLite history and adds no bus, dependency or persistence table.

`TranscriptEntry` preserves source sequence, run, revision and source identity.
Provider outcomes preserve their original `dispatch_sequence`. Its `content` is one of:

- `ProviderMessage`: user input, or a durable continuation/wakeup with its declared
  user/system role. User-role text is verbatim; system messages preserve owner
  attribution and the structured payload rendered as text.
- `ProviderTurnResult`: assistant output, reasoning, tool calls and reported usage.
- `TranscriptTool`: the original call, typed result, interruption flag, committed
  batch sequence and whether it is a deferred invocation. Tool names and argument
  shapes are data, not a consumer's classification mechanism.
- `TranscriptDispatch`: the dispatched provider/model and measured `RequestWindow`.
  It is available before a response completes; absence of a response does not hide
  the dispatch or fabricate usage.
- `TranscriptRun`: completed, failed or interrupted run status.
- `TranscriptInterrupted`: a provider dispatch with no usable result; its usage
  remains unknown.
- `TranscriptChild`: an actual child-session identity, independent of the tool
  name or mechanism that spawned it.

This is committed history, not a reconstruction of the current model window.
Compaction does not remove entries, and in-progress/unanswered calls remain
observable. Unrecognized bookkeeping and plugin events produce no transcript
entry. Malformed recognized records fail visibly instead of disappearing.

## Paging and authority

The limit is 1..200 **source events**, not messages. A page reports `entries`,
`next_sequence`, `has_more` and `source_events`. Advance to `next_sequence` while
`has_more` is true, including when `entries` is empty. Source cursors retain their
meaning after restart and do not require an unchanged tool catalog.

Pages use a soft 64 KiB source-payload budget and stop before a subsequent record
would exceed it. An oversized first record is returned whole on its own. No
message, reasoning block or tool output is clipped by this service. Consumers
must bound their own total scans and rendered output. The session export retains
its existing output cap; copy fails explicitly when its scan cap prevents it from
identifying the last response.

The service exposes only the lease's session, is available to read-only/hook
leases, and rejects revoked or closed leases. It accepts no session-id override.

## Certified context history

`PluginServices.read_context_history(limit=600)` returns `ContextHistory(records,
reached_end)`: a prefix starting at the calling session's first source event,
limited to 1..2000 source records. Unlike display paging, prefix certification
must inspect calls before the requested boundary. This service deliberately has
no arbitrary start cursor. The limit counts whole records, not bytes; a large
record is never silently omitted.

Each `ContextRecord` contains its source sequence, canonical payload byte count,
optional typed transcript entry, and `safe_boundary`. Bookkeeping has no transcript
entry but still contributes bytes and source records. The safety flag applies to
the **whole prefix** through that sequence. Every native call must have its result
inside that prefix, matched by batch sequence, call id and tool identity. Duplicate
or mismatched results cannot make a prefix safe just by equalizing counts.
Deferred results do not answer native calls and do not unbalance a closed batch.

The core uses the same validator and byte measurement when accepting
`project_window`; certification is advisory data, never authority to bypass
validation. A plugin can choose a boundary but cannot forge one.

Compaction now consumes this contract rather than reconstructing private events.
Its retention and summary policy remain plugin-owned. File references come from
the result's read observations and resource changes (including the old path of a
move); delegated work comes from actual child records. Displaying arbitrary tool
arguments does not establish a resource observation or a delegation fact.

Session information uses the paged transcript for context, export and copy. Status
reads only event headers for diagnostic event counts; usage uses the usage service.
