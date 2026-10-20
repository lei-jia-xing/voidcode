# Plugin Authoring Contract (P6)

How to write, pin, trust, run, and diagnose an ordinary third-party plugin.
The SDK source is authoritative for every signature and schema named here;
this document states the stable contract and where to look, not a second copy
of the code.

- Manifest schema + validation: `src/voidcode/sdk/manifest.py`
  (`parse_plugin_manifest`, `PluginManifest`, `PluginFileIdentity`,
  `PluginToolDeclaration`, `PluginCommandDeclaration`,
  `PluginCapabilityRequest`)
- Registration + declaration bridging: `src/voidcode/sdk/plugin.py`
  (`Plugin`, `tool_definition`, `command_definition`)
- Tool/command/services/views contracts: `src/voidcode/sdk/tools.py`,
  `src/voidcode/sdk/commands.py`, `src/voidcode/sdk/services.py`,
  `src/voidcode/sdk/views.py`, `src/voidcode/sdk/invocation.py`
- Admission/pinning/trust/activation: `src/voidcode/kernel/activation.py`,
  `src/voidcode/kernel/bootstrap.py`, `docs/contracts/hot-activation.md`
- Runnable reference: `examples/tally-counter/` (one model tool + one user
  command + one view over shared session state)

## 1. Manifest and source identity

A plugin is a directory with `voidcode-plugin.json` plus the exact source
files the manifest pins. Required keys: `plugin_id`
(`[a-z][a-z0-9_-]*`), `version`, `entrypoint` (`module:callable`),
`files`; the parser rejects unknown fields at every level, so a typo is a
loud error, never a silent default.

Identity rules (enforced by `admit_plugin`, never negotiable):

- Every `*.py` under the source root MUST be declared in `files`; undeclared
  Python is refused. Declared non-Python resources are allowed and
  hash-pinned the same way.
- Every declared path MUST resolve inside the source root and be a regular
  file; escapes are refused.
- The entrypoint module MUST be one of the declared files
  (`mod.py` or `mod/__init__.py`).
- Admission verifies hashes, then copies the pinned bytes into the
  content-addressed artifact `<state>/artifacts/<manifest-sha256>`. The
  plugin later runs from that artifact, never from the live source tree.
- An edited source is a NEW identity, never an in-place mutation: repin and
  restart/reload to adopt it.
- Admission never imports the candidate. Import happens later, inside a fresh
  plugin-host process, from the verified artifact.

Pin identities with the canonical generator — never hand-edit `files`:

```bash
uv run python scripts/generate_plugin_identities.py --plugin-dir path/to/plugin
uv run python scripts/generate_plugin_identities.py --plugin-dir path/to/plugin --check
```

`--plugin-dir` is repeatable; the no-argument form sweeps the bundled set and
is owned by the kernel's own workflow (pre-commit hook
`plugin-identities`). `--check` is nonmutating (exit 1 + rerun hint on
stale). A not-yet-pinned author manifest (`"files": []`) pins cleanly: the
generator computes the final `files`, validates the whole manifest, then
writes. The runnable example is Python-only; the helper discovers `*.py`
automatically and repins already-declared non-Python resources. Removing a
declared non-Python resource is explicit and one plugin at a time: delete the
asset, then `... --plugin-dir path/to/plugin --drop-resource name` (repeatable
for several). A deleted-but-not-dropped resource still fails plain generation
by default — the drop is what forgets it.

> TODO(p6): new (not-yet-declared) non-Python resources have no discovery
> path yet — adding one still needs a supported workflow, not a hand-edited
> array.

Registration must match the manifest exactly: the host refuses duplicate or
missing tool/command/provider names, any definition that differs from the
declaration-derived one, reserved names (`kernel_reload`, the core command
names, `core.*`/`kernel.*`/`voidcode.*` command prefixes), and cross-plugin
name collisions. Deriving definitions from the admitted manifest at
`create_plugin` time (`tool_definition(decl)` / `command_definition(decl)`,
the pattern `exec`, `lsp`, and `session_info` use) makes drift impossible.

## 2. Entry point and SDK-only rule

`create_plugin(config)` returns a `Plugin` (tools, commands, providers,
views, queries, callbacks). Plugins import only the SDK plus their own
third-party dependencies; anything kernel-shaped goes through
`PluginServices`. No private kernel import exists — bundled plugins use the
same public seam, so an author hits no ceiling they do not also hit.

## 3. Tools: effects, approval, read-only, schema, errors

Each tool declares `effects` (`read`, `write`, `execute`, `network`,
`spawn`, `session`), an `input_schema`, `replay_policy`, `capabilities`,
`path_argument_keys`, and `supports_parallel`. The kernel validates the
model's arguments against the admitted schema before dispatch
(`engine._denial`); a schema failure is
`"tool arguments failed the admitted JSON schema"`, never a handler call.

- **Approval** (`engine._needs_approval`): `READ` and `SESSION` never need
  approval in any mode. `WRITE`/`EXECUTE`/`SPAWN` wait under `ask`;
  `write` auto-allows `READ`+`WRITE`-only tools; `yolo` auto-allows all.
- **Read-only**: refuses `WRITE` and `EXECUTE` outright (stronger than
  `yolo`, checked first). `READ` and `SESSION` stay available;
  `SPAWN`-only stays available because a restricted owner may only spawn a
  child that inherits the restriction. A tool whose lease declares
  `SESSION` (`internal_work`) keeps staging state/events/budget/
  continuations under a read-only lease; other leases lose that staging
  (`LeaseContext.internal_restricted`, `host.py`).
- **Replay**: `safe` only for pure reads; anything mutating is `never`
  (default). A crashed-then-recovered mutation is never re-executed.
- **Input errors**: return `ToolFailure(tool_name, error, output)` with a
  plain message — never raise for bad input, never invent a placeholder
  success. Use the typed SDK results (`ToolSuccess`/`ToolFailure` with
  `TextOutput`/`EmptyOutput`/`AttachmentOutput`); bodies cross the host
  boundary as opaque JSON (`sdk/codec.py`).
- **Parallelism**: `supports_parallel` opts into native-batch parallel
  dispatch; default `False` is sequential and safe.

## 4. Commands: user authority, revision binding, busy gate

Commands declare `argument_schema`, `effects`, `capabilities`, `busy_gate`,
`client_capabilities`, `direct_user`, and optional `client_syntax`.
Reaching every client the same way (`/<name>` in TUI/web, `voidcode invoke`
from CLI), with no client-side command table.

- `expected_revision` is REQUIRED and must equal the catalog revision the
  client displayed; a mismatch is a visible stale error with no side
  effect. An omitted revision is an error, never "current".
- `busy_gate: true` refuses while the session is busy.
- `direct_user: true` means authentic user ingress only: the kernel sets
  `actor="user"` itself, and the command is exempt from read-only/plan
  overlays and the approval gate (the user's own action). Everything else
  is attributed to the client acting for the model. Agent/plugin paths can
  never claim user authority.
- Unknown command names raise `Unknown command`; schema failures return a
  `CommandResult(status="error")` naming the schema violation.

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
- Declare `state_schema` + `state_schema_version` (default 1) when the
  shape matters. The kernel validates migrated AND unchanged-version copies
  against it at activation (`validate_state_payload`); a row above the
  declared version is a downgrade and refuses.
- Migration is pure: `migrate_state(payload, from_version, to_version)`
  (arity 3, no services/store) runs in the candidate host for rows whose
  version increased; `prepare_deactivate(payload, scope, scope_key, tip)`
  (arity 4) runs in the current host for owners the candidate drops. A
  missing transform, or a candidate below the stored version, fails the
  activation visibly. See `docs/contracts/hot-activation.md` for the full
  transaction.
- Hook leases observe only (`state_get`, event reads, usage, budget,
  `tool_definition`, artifact reads): staging anything is refused loudly.

## 6. Trust, configuration, revisions, activation

- External sources are configured per plugin id in `voidcode.json`
  (`plugins.<id>.path`, resolved relative to the workspace) and trusted at
  startup with `--trust-plugin <id>` — trust authorizes the canonical path
  the configuration resolves to, recorded in the revision's private
  snapshot. Bundled ids are trusted by construction.
- The agent cannot self-grant trust: `kernel.reload` only trusts source
  paths already authorized in the current generation. A brand-new external
  plugin is a restart with `--trust-plugin`, never a reload.
- Plugin `config` is validated against the manifest's `config_schema` at
  admission; a `providers` key inside plugin config is refused (credentials
  belong in the root `providers` section and live only in the 0600 private
  snapshot).
- The admitted generation is the capability revision: manifest + artifact
  identities, config digests, and the pinned SDK hash. Changed source,
  config, or SDK is never admitted implicitly; a mutated SDK refuses
  restart as a controlled kernel upgrade (reconcile with the real P5
  upgrade CLI once it lands — no syntax is published here).
- Activation re-admits the live configuration and commits one new revision
  transactionally; clients refetch catalog + views on
  `kernel.activation_committed`. New paths are refused with explicit-trust
  guidance; the previous generation stays active. Two paths
  (`docs/contracts/hot-activation.md`): the user command `kernel.reload`
  (idle sessions only, refused under `read_only` and while busy, no
  automatic continuation) and the native model tool `kernel_reload`
  (stages the request mid-run; the switch commits at the native-batch
  boundary and the same session continues automatically with no prompt
  resend). The author edit → repin → reload loop depends on the model
  path mid-run and the command path when idle.
- Current CLI topology (observed, full default bundle + tally-counter):
  the first command on fresh state snapshots the tree into
  `<state>/kernel-images/<digest>` and re-executes into it; subsequent
  commands run inside the elected snapshot (`show` reads the DB directly
  with no re-exec). Proven sequence: `create-session` →
  `catalog` (37 commands incl. `tally`) → `invoke tally {"delta": 3}` →
  `invoke tally {"delta": 2}` → `views` (tally `5 (2 update(s))`) →
  durable `plugin_state` row `{total: 5, updates: 2}`, version 2.

## 7. Capabilities, services, and what plugins cannot do

- The kernel grants `workspace.read`, `workspace.write`, `network`; a
  manifest requesting more is refused, and each tool/command's required
  capabilities must be covered by the plugin grant. Capabilities gate
  dispatch — they are not an OS sandbox. An admitted plugin runs in its own
  host process, but only explicitly trusted code is admitted at all.
- `PluginServices` is the whole kernel surface: state, events,
  continuations, wakeups, delegation (`spawn_child`/`join_child`),
  window projection, overlays (authentic-user invocations only), reads,
  budgets, usage, `model_query` (one tool-free turn), processes, and
  artifacts. Callbacks receive lease-scoped services only — never arbitrary
  kernel access, never a sibling plugin's state, never an unrestricted store
  scan. Own state stays owner/scope-bound; event reads cover the bound
  session plus true descendants via explicit `read_child_events` (parent,
  sibling, and unrelated sessions are refused).
- Neutral cross-plugin reads: `services.tool_definition(name)` answers from
  the active revision's dispatch snapshot with the caller's scope and stance
  applied (`None` covers unknown/hidden equally; the host keeps no copy),
  and `services.query(name, arguments)` asks a host-local read-only question
  another plugin answers (`None` = "nothing to report"; a name nobody
  answers raises `QueryUnavailable`, which callers catch by itself when
  provider absence is expected).
  Queries have authorized read-only access to already-authorized data — no
  state mutation, writes, process starts, or other external effects
  (code-review contract, not an OS sandbox).
- Managed service processes (`spawn_service_process` + `write_process` /
  `read_process_output` / `cancel_process` / `list_processes`) join the
  host's process group and stop at host retirement/swap or owner shutdown —
  the shape for a language server or other interactive helper. Detached
  `spawn_process` jobs keep their own groups and survive a host swap.
  Poll `ToolContext.abort_signal` and cancel explicitly; never retain a
  `PluginServices` object past its invocation.
- Sessions are commit-once ledgers over append-only facts: no fork, undo,
  revert, or import. `read_only` is declaration-driven, and approval stays
  declaration-driven — nothing in a plugin bypasses either.

## 8. Executable diagnostics

Run these in order; each step's failure names the layer at fault.

1. **Pin check** — `uv run python scripts/generate_plugin_identities.py
   --plugin-dir <dir> --check`. Stale → edit + repin; the message prints
   the exact rerun command. Never hand-edit `files`.
2. **Manifest parse** — `python -c "from voidcode.sdk.manifest import
   parse_plugin_manifest; import json;
   parse_plugin_manifest(json.load(open('<dir>/voidcode-plugin.json')))"`.
   Unknown-field, entrypoint, duplicate-name, and bad-hash failures surface
   here with the field name.
3. **Catalog** — `voidcode --trust-plugin <id> catalog` (plus
   `--workspace`/`--state-dir` as needed). Admission failures print the
   exact refusal: untrusted source, undeclared Python, escaped path, hash
   mismatch, ungranted capability, config-schema failure, or manifest/source
   id disagreement.
4. **Command smoke** — `create-session`, then `invoke <sid> <cmd>
   --arguments '{...}' --expected-revision <rev from catalog>`. Stale
   revision, busy session, missing client capability, schema failure, and
   read-only denial each return their own message; handler input errors
   come back as `status: error` with the plugin's own text.
5. **Model-tool smoke (no paid call)** — point the workspace `model` at the
  loopback provider (`tests/provider_loopback.py:
  OpenAILoopbackServer` + `openai_response`) and `run` a prompt that
  drives the tool; assert durable behavior (committed state, completion
  event, second dispatch billed), not message wording
  (`tests/integration/test_tally_example.py` is the working template).
6. **Views** — `views <sid>` renders every contributed view under one
   captured revision; a failing view degrades to a `notice` naming the
   view, never a missing payload.
7. **Durable truth** — `show <sid> --events` (no config/host/revision
   needed) and the SQLite store (`plugin_state`, `kernel.tool_completed`,
   `kernel.command_completed`) confirm what actually committed.

Common refusals and their fixes:

| Message | Meaning | Fix |
|---|---|---|
| `plugin source requires explicit user trust` | `--trust-plugin <id>` missing at startup | restart with the flag |
| `plugin has undeclared Python source` | new `.py` not in `files` | repin with the generator |
| `plugin file identity changed` / `is stale` | source edited after pinning | repin; edited bytes are a new identity |
| `entrypoint is not in its declared implementation files` | entry module missing/renamed | fix entrypoint or repin |
| `requests ungranted capabilities` | manifest asks beyond the grant set | reduce requests |
| `configuration failed schema validation` | plugin config vs `config_schema` | fix `voidcode.json` entry |
| `configured plugin identity differs from its manifest` | id/path points at another plugin | fix `path` |
| `displayed catalog revision ... is stale` | catalog from an older generation | refetch catalog, retry |
| `read-only policy denies` | WRITE/EXECUTE under restriction | declare SESSION/READ or lift policy |
| `state CAS conflict` | concurrent writers / stale version | re-read, re-apply, retry |
| `state copy failed <id> state_schema` | stored state vs declared schema | migrate or fix schema |
| `downgrades are not supported` | candidate below stored version | ship a forward migration |
| `hook leases cannot ...` | hook tried to stage/start work | move the write to a tool/command/observer |
| `SDK source no longer matches` | SDK edited under an old revision | reconcile with the P5 controlled-upgrade CLI; no syntax published here |
