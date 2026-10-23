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
| `delegation/profiles/` | Profile roles duplicated tool authority using concrete name lists. | Removed: profiles supply prompt/stance; children inherit parent scope, optionally narrowed explicitly by the caller. |
| `review/review_plugin.py` | Fixed inspection-tool names restrict the standalone review workflow. | Still present; review policy needs a separate decision. Delegation simplification does not silently widen this workflow. |
| `delegation/delegation_plugin.py` | Reads bounded child history through a service and delegates rendering to the SDK. | Legitimate diagnostic contract usage; consolidate rendering with the future transcript projection rather than duplicating event parsing in plugins. |

Existing typed optional queries, tool effects, tool semantics, resource observations
and child facts are the intended collaboration seams. A plugin identifying its
own tools or events is not a dependency on another plugin. Protocol fields such as
an LSP action named `edit` are not tool-name coupling either.

## Coverage and remaining work

This is a coupling audit, not a completed per-plugin quality or security review.
A structural scan covers all 20 bundled plugin packages for implementation-layer
imports, raw event reads, concrete tool lookups and child-spawn call sites. The
SDK direction checks enforce import boundaries. The findings above received
focused changes and behavioral tests; absence of a search hit is not proof that a
plugin's lifecycle, failure modes or external-process behavior has been audited.

Remaining concrete findings:

- Standalone review still selects a fixed set of inspection tool names.
- Delegation diagnostic history still reads raw child events and uses the shared
  SDK renderer. It does not parse peer state, but a typed child transcript could
  remove that storage-vocabulary dependency.
- Some plugins identify their own tools/owners by constants (for example todo).
  Those are not peer dependencies, but aliasing the admitted plugin id has not
  been comprehensively checked; cron now uses the lease's actual owner.

Explicit child scope validation remains core authority policy. Delegation profiles
no longer own tool lists, perform discovery, report missing tools or conflict with
caller-supplied scopes. The core's inherited scope and read-only stance are retained.

The public facts/transcript consumers listed above are guarded against private
event parsing. [Transcript/context history](./transcript.md) and
[wakeup facts](./facts.md) cover their data needs without adding a bus. Full
per-plugin lifecycle and failure-path reviews remain separate work.
