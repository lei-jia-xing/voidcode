# Plugin Authoring Contract

How to write, configure, run, and diagnose an ordinary third-party plugin.
The SDK source is authoritative for every signature and schema named here;
this document states the stable contract and where to look, not a second copy
of the code.

- Registration: `src/voidcode/sdk/plugin.py` (`Plugin`, `StateSchema`)
- Tool/command/services/views contracts: `src/voidcode/sdk/tools.py`,
  `src/voidcode/sdk/commands.py`, `src/voidcode/sdk/services.py`,
  `src/voidcode/sdk/views.py`, `src/voidcode/sdk/invocation.py`
- Admission/activation: `src/voidcode/core/activation.py`,
  `src/voidcode/bootstrap.py`, `docs/contracts/hot-activation.md`
- Runnable reference: `examples/tally-counter/` (one model tool + one user
  command + one view over shared session state)

## 1. Package layout and factory

A plugin is a Python package directory whose `__init__.py` exports
`create_plugin(config) -> Plugin`. The runtime loads it as `{id, root, config}`:
no manifest, no entrypoint string, no hash pinning, no trust flag. Plugins
are trusted code equal to the application — not sandboxed. Bundled plugins load
by default; anything else loads because the user configured it.

The returned `Plugin` is the single truth about what the plugin registers:
tools, commands, providers, views, queries, context, observer (plus
`observed_facts`), lifecycle callbacks, pre/post-tool hooks (plus match
lists), state schema, migration/deactivation transforms, and `close`.

The runtime refuses duplicate or missing tool/command/provider names, reserved
names (`core_reload`, the core command names, `core.*`/
`voidcode.*` command prefixes), and cross-plugin name collisions. Define
`ToolDefinition`/`CommandDefinition` values in the same module as their
handlers, so drift is impossible by construction.

## 2. Entry point and SDK-only rule

`create_plugin(config)` returns a `Plugin`. `config` is the plugin's own
configuration mapping plus one reserved key, `providers`: the core injects
the same resolved providers map (credentials, base URLs, extras) into every
factory. A plugin must accept and ignore `providers` unless it is one that
registers providers; it must never copy that map into state, events, views,
error strings, or digests. Plugins import only the SDK plus their own
third-party dependencies; anything core-shaped goes through
`PluginServices`. No private core import exists — bundled plugins use the
same public seam, so an author hits no ceiling they do not also hit.

## 3. Tools: effects, approval, read-only, schema, errors

Each tool declares `effects` (`read`, `write`, `execute`, `network`,
`spawn`, `session`), an `input_schema`, `replay_policy`,
`path_argument_keys`, and optionally `icon` (`Icon(glyph)` from
`voidcode.sdk.icons`). The icon is the tool's own display identity: `glyph`
is the mark clients render (plain Unicode text, NerdFont PUA allowed; empty
means "render nothing"). Omit `icon` and clients show their generic tool mark.
The core snapshots the birth icon into the tool intent/result events
and `TranscriptTool.icon`; history keeps that value across reloads and
removals, never re-resolving from the live catalog. Icons never enter
provider function schemas. The core validates the model's
arguments against the registered schema before dispatch; a schema failure is
`"tool arguments failed the admitted JSON schema"`, never a handler call.

- **Approval**: `READ` and `SESSION` never need approval in any mode.
  `WRITE`/`EXECUTE`/`SPAWN` wait under `ask`; `write` auto-allows
  `READ`+`WRITE`-only tools; `yolo` auto-allows all.
- **Read-only**: refuses `WRITE` and `EXECUTE` outright (stronger than
  `yolo`, checked first). `READ` and `SESSION` stay available;
  `SPAWN`-only stays available because a restricted owner may only spawn a
  child that inherits the restriction. A tool whose lease declares
  `SESSION` keeps staging state/events/budget/continuations under a
  read-only lease; other leases lose that staging.
- **Replay**: `safe` only for pure reads; anything mutating is `never`
  (default). A crashed-then-recovered mutation is never re-executed.
- **Input errors**: return `ToolFailure(tool_name, error, output)` with a
  plain message — never raise for bad input, never invent a placeholder
  success. Use the typed SDK results (`ToolSuccess`/`ToolFailure` with
  `TextOutput`/`EmptyOutput`/`AttachmentOutput`); results cross only the
  storage/HTTP boundary as strict JSON (`sdk/codec.py`), never a plugin RPC.

The model receives `TextOutput.text` as the authoritative selected page, not
another copy of its `ToolBody`. The complete structured result is recoverable
through the core-owned `result_reference`; the core publishes it before omitting
even a small machine body. A recovery `OutputBounds.reference` does not replace
selected text or reset its page cursor. For structured-only model data use
`EmptyOutput` plus the body. For intentionally out-of-line textual content use
`EmptyOutput` with a typed textual `ArtifactReference` and a separate summary in `presentation`.
Use streaming artifact publication for chunked sources; see [artifacts](artifacts.md).
The core pages explicitly referenced text, JSON, XML, and JavaScript bytes under
the existing model allocation; it does not relabel the stored media type.

The same budget distinction applies to child joins: only a committed user or
owner cancellation is `cancelled`; interrupted results retain their actual or
unknown stop cause. `ChildOutcome` currently has no typed `BudgetExhaustion`
field, so joined children cannot yet report its dimension/source/counters.


## 4. Commands: revision binding, busy gate

Commands declare `argument_schema`, `effects`, `busy_gate`,
and optional `client_syntax` and `default_shortcut`.
Clients render namespace dots as spaces (`/goal set`); CLI/RPC invocation and persisted facts retain the canonical ID (`goal.set`). A slash `client_syntax` chooses one preferred public path, with dots normalized the same way. There is no client-side command table or dotted slash alias.

- `expected_revision` is REQUIRED and must equal the catalog revision the
  client displayed; a mismatch is a visible stale error with no side
  effect. An omitted revision is an error, never "current".
- `busy_gate: true` refuses while the session is busy.
- Unknown command names raise `Unknown command`; schema failures return a
  `CommandResult(status="error")` naming the schema violation.
- `default_shortcut`, such as `alt+a`, is declarative catalog metadata, not a
  keyboard callback. The TUI binds a uniquely claimed, non-reserved chord;
  conflicts bind nobody and name every claimant. Web clients expose action
  buttons without taking browser shortcuts. Hidden commands can expose this
  entry without adding a public slash command.

All commands follow the same effects/read-only rules: there is no command
trust classification and no read-only exemption for particular commands.
`actor="client"` is the ordinary command provenance; `actor="user"` appears
only on an accepted confirmation's re-execution, as explicit-decision provenance.

## 5. State: scopes, CAS versioning, schemas, migration

`get_state(scope)` returns `(payload | None, version)` for `session`
(default) or `workspace` scope; `replace_state(payload,
expected_version, scope)` stages an optimistic replacement and returns the
new local version. Exactly one future version per lease: a second write in
the same lease must target the version the first published, and a version
that no longer matches the store is a `state CAS conflict`. Staged state
commits atomically with the invocation's events and receipts.

Conventions that keep state debuggable:

- Validate strictly on every read: refuse unknown keys, require every
  field, check `schema_version` and value types (see `TallyState`).
  Missing state (`None`) is the legitimate first-run case — construct a
  default, never fabricate a version.
- Declare `state=StateSchema(version, schema)` (default version 1, no
  schema) when the shape matters. The core validates
  unchanged-version copies against it at activation; a row above the
  declared version is a downgrade and refuses.
- Deactivation is pure: `prepare_deactivate(payload, scope, scope_key, tip)`
  (arity 4) runs in the current runtime for owners the candidate drops. A
  missing transform fails the deactivation visibly. See
  `docs/contracts/hot-activation.md` for the full transaction.
- Observers declare `observed_facts` (`usage`, `invocation_finished`,
  `run_finished`); the registration must provide an observer callback
  exactly when that tuple is non-empty.
- Hook leases observe only (`state_get`, event reads, usage, budget,
  `tool_definition`, artifact reads): staging anything is refused loudly.

## 6. Configuration, revisions, activation

- External sources are configured per plugin id in `voidcode.json`
  (`plugins.<id>.path`, resolved relative to the workspace). Bundled ids
  need no entry: they are enabled with an empty configuration by default.
- Plugin `config` is validated by the plugin itself in `create_plugin`;
  there is no `config_schema` file. A plugin's own config must never own
  the reserved `providers` key.
- The admitted generation is the revision: an opaque UUID names the recorded
  config/composition, with the private snapshot digest remaining for config
  integrity. A restart imports the *current* source with the *recorded*
  configuration.
- Activation re-admits the live configuration and commits one new revision
  transactionally; clients refetch catalog + views on
  `core.activation_committed`. Two paths (`docs/contracts/hot-activation.md`):
  the user command `core.reload` (idle sessions only, refused under `read_only`
  and while busy, no automatic continuation) and the native model tool
  `core_reload` (stages the request mid-run; the switch commits at the
  native-batch boundary and the same session continues automatically with no
  prompt resend).
- To remove a plugin, disable it via `core.reload` on the old running
  core first so its real `prepare_deactivate` runs, then adopt the new
  generation; a disabled owner's historic state rows may remain.

## 7. SDK service scopes and contracts

Loaded plugin Python runs with the application's full authority — not a sandbox.
`PluginServices` is the scoped API surface the core passes into each callback:
state, events, continuations, wakeups, delegation (`spawn_child`/`join_child`),
window projection, scoped restrictive overlays, reads,
budgets, usage, `model_query` (one tool-free turn), processes, and
artifacts. Callbacks receive lease-scoped services only — never arbitrary
core access, never a sibling plugin's state, never an unrestricted store
scan. Own state stays owner/scope-bound; session or descendant history reads through typed
`read_session_history` / `read_child_history` return bounded Markdown `HistoryPage` (parent,
sibling, and unrelated sessions are refused).
`read_session_history(..., page_bytes=8192)` requests a smaller page than the
default 40 KiB bound; continue with the returned sequence/UTF-8 offset cursor,
never reconstruct a cursor from trimmed display text.
- Cross-plugin descendant monitoring uses `list_descendants()` and the typed
  `DescendantNode` projection, not a concrete delegation-plugin import.
  `cancel_descendant_subtree(target, expected_run_id=...)` is a mutating,
  run-bound operation; use a durable confirmation for a user-facing cancel.
  Scope, race receipts, and explicit-null unstarted nodes are defined in
  [agent collaboration](agent-collaboration.md#subagent-monitor).
- Neutral cross-plugin reads: `services.tool_definition(name)` answers from
  the active revision's dispatch snapshot with the caller's scope and stance
  applied (`None` covers unknown/hidden equally; the runtime keeps no copy),
  and `services.query(name, arguments)` asks a runtime-local read-only question
  another plugin answers (`None` = "nothing to report"; a name nobody
  answers raises `QueryUnavailable`, which callers catch by itself when
  provider absence is expected).
  Queries follow the scoped read-only convention — no
  state mutation, writes, process starts, or other external effects
  (code-review contract, not an OS sandbox).
- Managed service processes (`spawn_service_process` + `write_process` /
  `read_process_output` / `cancel_process` / `list_processes`) run in their own
  foster-owned session group (never the core's group) and stop at runtime
  retirement/swap or owner shutdown —
  the shape for a language server or other interactive helper. Detached
  `spawn_process` jobs keep their own groups and survive a runtime swap.
  Poll `ToolContext.abort_signal` and cancel explicitly; never retain a
  `PluginServices` object past its invocation.
- Long-running owner drivers: `Plugin.background_action` registers one callback
  the core runs per claimed background action, outside any model turn. The driver
  keeps only its `BackgroundContext` (action id, immutable payload, abort event);
  each `context.invoke(callback)` runs the callback exactly once under a fresh
  owner/revision-bound lease and commits its staged work atomically, so progress
  is durable invoke by invoke. Services from one invoke must never escape into
  another. `PluginServices.invoke_tool` returns a receipt; only the driver may
  call `context.wait_tool(receipt)`, after that callback lease has returned. See
  the [typed wait contract](./background-actions.md#awaiting-a-deferred-tool)
  and [background actions](./background-actions.md) for its result, scope,
  approval, cancellation, and liveness semantics.
- Sessions are commit-once ledgers over append-only facts: no fork, undo,
  revert, or import. `read_only` and approval are SDK invocation contracts
  over declared effects — not OS isolation.

## 8. Executable diagnostics

Run these in order; each step's failure names the layer at fault.

1. **Factory smoke** — import the package and call
   `create_plugin({"providers": {}})` directly: leftover imports,
   definition errors, and strict parsers rejecting the reserved key all
   surface here with no core. Observer/`observed_facts` wiring is
   validated when the runtime loads the registration catalog.
2. **Catalog** — `voidcode catalog` (plus `--workspace`/`--state-dir` as
   needed). Admission failures print the exact refusal: missing source,
   factory error, unknown config key, reserved-name or cross-plugin
   collision.
3. **Command smoke** — `create-session`, then `invoke <sid> <cmd>
   --arguments '{...}' --expected-revision <rev from catalog>`. Stale
   revision, busy session, missing client capability, schema failure, and
   read-only denial each return their own message; handler input errors
   come back as `status: error` with the plugin's own text.
4. **Model-tool smoke (no paid call)** — point the workspace `model` at the
   loopback provider (`tests/provider_loopback.py:
   OpenAILoopbackServer` + `openai_response`) and `run` a prompt that
   drives the tool; assert durable behavior (committed state, completion
   event, second dispatch billed), not message wording
   (`tests/integration/test_tally_example.py` is the working template).
5. **Views** — `views <sid>` renders every contributed view under one
   captured revision; a failing view degrades to a `notice` naming the
   view, never a missing payload. Plugin authors keep constructing
   `PluginViewDescriptor(..., data={...})` with plain mappings; clients
   decode with the strict `from_payload` constructors
   (`NoticeViewData` … `StatusViewData`, `PluginViewDescriptor`,
   `ViewsSnapshot`, `CommandCatalog`, `CommandDescriptor`,
   `ConfirmationRequest`) or `parse_view_data(view_type,
   descriptor.data)`. Views style via an optional `style` object on the data
   mapping — `{"tone": "success", "foreground": "#22c55e",
   "background": null}` (`tone` optional, default `default`; colors
   `#RRGGBB` or one of the 16 ANSI names, no raw CSS/ANSI/markup); absent
   style renders the client's built-in default, and
   `dataclasses.asdict(StatusViewData(label="Count", value="2",
   style=ComponentStyle(foreground="#12ab34")))` is the supported author
   pattern, as in the tally example (`style` is never a top-level
   `PluginViewDescriptor` field). Dispatch semantics live in `choices_dispatch`,
   `validate_form_submission` (native numeric prefill accepted; typed strings validated strictly), and
   `is_read_only_load` (load runs only when the catalog command declares exactly the `read` effect).
6. **Durable truth** — `show <sid> --events` (no config/runtime/revision
   needed) and the SQLite store (`plugin_state`, `core.tool_completed`,
   `core.command_completed`) confirm what actually committed.

Common refusals and their fixes:

| Message | Meaning | Fix |
|---|---|---|
| `displayed catalog revision ... is stale` | catalog from an older generation | refetch catalog, retry |
| `read-only policy denies` | WRITE/EXECUTE under restriction | lift policy or fix the declared effects to match what the tool does |
| `state CAS conflict` | concurrent writers / stale version | re-read, re-apply, retry |
| `state copy failed <id> state_schema` | stored state vs declared schema | migrate or fix schema |
| `downgrades are not supported` | candidate below stored version | ship a forward migration |
| `hook leases cannot ...` | hook tried to stage/start work | move the write to a tool/command/observer |
| `declares observed_facts but provides no observer` | metadata/callback mismatch | wire both or neither |
| `invalid observed fact kind` | typo in `observed_facts` | use usage/invocation_finished/run_finished |

### Interactive command results

A successful `CommandResult` may include a `CommandPresentation(title, view_type,
data)` using the same bounded, validated view data as plugin views. Use `choices`
for selectable lists, `form` for editing, and `markdown`/`fields`/`status` for
readable details. Keep `output` as the human receipt for CLI/history consumers.
Presentations cannot carry confirmations: governed confirmation still uses
`ConfirmationRequest` and its durable request id.

TUI and web mount interactive presentations only from a direct command reply,
bound to the revision used for that invocation. Historical events retain the
presentation as a fact but must never reopen an interactive dialog on replay.
An unavailable or stale action is a visible failure, never an automatic retry
under a new revision. Rendering a form does not execute its action; submission
uses the normal command validation, effects, and confirmation path.

### Prepare command input without executing it

A command may implement the optional SDK `CommandInput` protocol:
`prepare_input(invocation, services) -> CommandPresentation`. The admitted catalog
sets `has_input` from this capability. Bare slash input requests `command.input`
and mounts the returned presentation; explicit arguments continue directly to
normal invocation. CLI/RPC `command.invoke` still requires valid arguments.

Preparation runs under a fresh read-only lease with the caller's workspace,
session, and captured revision. It cannot stage state, events, continuations,
processes, or other service mutations. It produces no command-completed event.
A form/choices presentation must target the preparing command itself. A missing
capability, malformed result, handler error, and an empty candidate set remain
distinct. Empty choices are valid, including a mounted live roster waiting for
new work; use a notice when no interaction is needed. Failures surface.
Every presentation remains bounded by the existing view-data byte limit.
`command.input` can carry partial `arguments`. Supplied keys and types are
validated against the command schema, with only its top-level `required`
removed; normal invocation still requires the complete schema.
`CommandPresentation.refresh_after_ms` (250..60000) requests advisory refresh
only while that view remains open. Refresh must preserve its captured revision,
navigation arguments, filter, stable selected id, and viewport; it must not
invoke the command, consume notices, or replace an active approval/form.

Text form fields may declare `options` as a sequence of `ChoiceOption(id, label)`.
`None` means free text; an explicit option list restricts the submitted value to
one of its ids. Required fields still require a selection, optional untouched
fields remain absent, and labels never become submitted values. TUI supports
Left/Right/Space selection and web uses a native select. The final action still
passes through the command schema, effects, busy policy, and confirmation gate.

A `CommandDefinition.text_argument` may name a declared string property. Clients
map natural slash trailing text to that property, including when the command has
additional optional fields. A tail beginning with `{` is parsed as a structured
JSON object. Admission rejects undeclared/non-string text targets. Neither this
metadata nor input preparation changes canonical command IDs or structured RPC
arguments. Examples: `/goal set Ship the command UI`, `/review Focus on races`,
and `/workflow run review`; bare forms provide guided optional arguments.

Markdown details may declare `actions` as `ViewAction(label, action, arguments)`.
Clients show these controls only for live presentations. Set `prepare_input=True`
to pass those arguments to the target command's declared input without invoking
it (for example a history cursor or selected session id). Choices may likewise
set `prepare_input=True` to route selections through preparation. Normal actions
retain the producing revision and pass through governed command invocation.

Forms may provide fixed `arguments` alongside editable fields, for identifiers
and concurrency tokens. Field names cannot shadow those arguments. Submission
merges validated fields into the fixed arguments; identifiers are not editors.
Use `slash_visible=False` for implementation helpers that should remain callable
through forms but should not appear in slash discovery or human help.
