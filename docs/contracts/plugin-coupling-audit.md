# Plugin coupling audit

This inventory distinguishes dependencies on public contracts from dependencies on
core event layouts or concrete tool implementations. A new bus would change
delivery, but would not remove either kind of implementation dependency. Continue
using typed queries for optional data, durable facts for observations, and hooks
for decisions before execution.

## Findings

| Consumer | Dependency | Decision |
| --- | --- | --- |
| `todo/todo_plugin.py` | Parsed `kernel.user_message` headers to reset reminder cycles. | Removed: consume `RunRequestedFact`; only a fresh request resets the budget. Resumes and automatic continuations retain it. |
| `cron/cron_plugin.py` | Parsed `kernel.wakeup_fired` and its nested payload. | Removed: consume `WakeupFiredFact`, checking lease owner and the tracked current wakeup identity. |
| `compaction/compaction_plugin.py`, `compaction/projection.py` | Reconstructed provider messages and complete compaction boundaries from private events. | Removed: consume certified context history; the core owns matching and boundary validation. |
| `compaction/projection.py` | Extracted resource references from argument names and recognized delegation by concrete tool names. | Removed: reported resource observations/changes and actual child records supply those facts. |
| `session_info/panels.py` | Parsed provider intents, messages and outcomes for context and exports. | Removed: context/export/copy use the public typed transcript; usage uses its dedicated service. Status reads only diagnostic event headers. |
| `capabilities/providers/panels.py` | Read provider intents to identify the latest dispatched provider/model. | Removed: consume typed transcript dispatches, including those still in flight. |
| `review/review_plugin.py`, `delegation/profiles/builtin.py` | Curated tool-name allowlists define child authority and workflow scope. | Keep explicit scope restrictions. Capability-based discovery must preserve authorized subsets; selecting every read tool could widen authority. |
| `delegation/delegation_plugin.py` | Reads bounded child history through a service and delegates rendering to the SDK. | Legitimate diagnostic contract usage; consolidate rendering with the future transcript projection rather than duplicating event parsing in plugins. |

Existing typed optional queries, tool effects, tool semantics, resource observations
and child facts are the intended collaboration seams. A plugin identifying its
own tools or events is not a dependency on another plugin. Protocol fields such as
an LSP action named `edit` are not tool-name coupling either.

## Remaining policy decisions

The implementation dependencies listed above are removed from todo, cron,
compaction, session information and provider inspection. Their public fact and
transcript seams are guarded against private event parsing by SDK direction tests.

Child tool allowlists remain explicit authority policy. Evaluate capability
selectors separately, preserving the authorized subset when replacement tools
are admitted. Child diagnostic history still uses the shared SDK renderer; moving
it to typed child transcripts can be a separate change without widening access.

The [public transcript and certified context history](./transcript.md) cover
session inspection and compaction. [Wakeup facts](./facts.md) cover scheduler
observation and recovery. No additional bus is required for these consumers.
