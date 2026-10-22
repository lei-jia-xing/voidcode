# Public committed transcript

`PluginServices.read_transcript(after_sequence=0, limit=100)` exposes typed
conversation records for the calling session. The core decodes storage events;
consumers do not depend on `kernel.*` names or nested payload layouts. This uses
the existing SQLite history and adds no bus, dependency or persistence table.

`TranscriptEntry` preserves source sequence, run, revision and source identity.
Its `content` is one of:

- `ProviderMessage`: user input, or a durable continuation/wakeup with its declared
  user/system role. User-role text is verbatim; system messages preserve owner
  attribution and the structured payload rendered as text.
- `ProviderTurnResult`: assistant output, reasoning, tool calls and reported usage.
- `TranscriptTool`: the original call, typed result and interruption flag. Tool
  names and argument shapes are data, not a consumer's classification mechanism.
- `TranscriptDispatch`: the dispatched provider/model and measured `RequestWindow`.
  It is available before a response completes; absence of a response does not hide
  the dispatch or fabricate usage.
- `TranscriptRun`: completed, failed or interrupted run status.

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

## Current consumers and next boundary

Session information uses the transcript for context, export and copy. Status reads
only event headers for diagnostic event counts; usage uses the usage service.

Compaction has not migrated yet. Before doing so, extend this contract with
certified native-batch boundaries, dispatch/result correlation and resource/child
observations. A display transcript must not be mistaken for proof that a prefix
is safe to compact. The core remains the authority that validates projections.
