# Maintainability cleanup plan (2026-11-20)

Planning only. No production code, tests, configuration, generated catalogs, or TODO claims are changed by this document. All anchors and line counts were read from the current working tree on 2026-11-20; implementation must re-read them because the tree is still moving.

Scope: the residual items recorded in `docs/TODO.md` “维护性清点 → 剩余（open）” plus the TUI `_handle_stream_event` item and the test-fixture residuals. The four UTF-8 paging implementations are explicitly out of scope and must remain distinct.

Execution rule for every workstream: never run pytest with xdist. Use `-n0` explicitly. A refactor that requires changing persisted event shapes, the plugin SDK contract, append-only semantics, or a test assertion (rather than a constructor/fixture call site) is out of scope; stop and report instead.

## Landed status (2026-11-21)

All five C steps, both T steps, and all five M steps are implemented in the shared tree; this plan remains the design and acceptance record.

| Step | Landed | Current shape |
|---|---|---|
| C1 | yes | `Core._build_runtime` is the single cold-start/hot-activation runtime builder. |
| C2 | yes | `LeaseStaging` owns per-invocation scratch (`LeaseContext.staging`). |
| C3 | yes | Frozen `LeaseIdentity` carries authority; `LeaseContext` keeps only staging and lifecycle. |
| C4/C5 | yes | `core/background_execution.py` (767 lines) owns driver invoke and tool wait; `engine.py` is 6,058 lines. |
| T1/T2 | yes | `tui/stream_handlers.py` (306 lines), `terminal_app.py` 2,225 lines; the idle test uses shared `wait_for`. |
| M1 | yes | `capabilities/providers/registry.py` (122 lines) + `provider_table.json` (499 lines); the `native/provider_table.py` module is deleted. |
| M2 | yes | `cli.py` 519 lines; root options use a standard argparse pre-parser; `_normalized_argv` is deleted. |
| M3 | yes | `core/artifacts.py` 213 lines; media sidecar temp content is fsynced before publish. |
| M4/M5 | yes | Shared `loopback_server`/`wait_for`/`register_core` cover the listed suites; remaining local server loops are recorded in `docs/TODO.md`. |

Final serial verification (2026-11-21): `uv run python -m pytest -q` = **2470 passed in 25:05**; `ruff check .`, `ruff format --check .`, `ty check src`, and the four frontend gates are green. Exact counts and the post-format targeted rerun are recorded in `docs/CHANGELOG.md`.

---

## 1. Risk-ranked inventory

Execution order is defined in §3, not by this table. Table order is descending risk.

| # | Residual | Current evidence (file:line, current tree) | Real maintainability problem | Minimal safe seam | Risk |
|---|---|---|---|---|---|
| 1 | Background execution and tool-wait live in `Core` | `core/engine.py:137-200` `_BackgroundContext` (64 lines); `:3416-3485` `drive_background_action` (70); `:3487-3610` `_invoke_background_action` (124); `:3612-3635` `_commit_tool_wait_events` (24); `:3638-3660` `_terminal_background_tool_request_ids` (23); `:3785-3823` `_reconcile_lost_background_tool_waits` (39); `:3825-3859` `_drain_pending_background_tool_waits` (35); `:3865-3948` `_await_background_tool` (84); `:3950-4080` `_background_tool_outcome` (131). About 530 method lines plus the context object. `core/background_actions.py` already owns claim/live/abort state but not execution. | The atomic commit retry, lease open/revoke, cancellation settlement, deferred-result adoption, and full `store.events(session_id)` scans are all inside `Core`. A change to one branch of the wait state machine requires reading a 6,700-line class, and the outcome decoder cannot be tested without a real Core. This is semantic coupling, not just size. | New `core/background_execution.py` with an explicit `BackgroundExecutionHost` protocol; move `_BackgroundContext`, drive/invoke, and the wait/reconcile cluster behind it. `BackgroundActionRegistry` keeps claim/live ownership; `_drain_deferred_requests` stays in Core and is called through the host. | **High** (commit/lease/cancel concurrency) |
| 2 | `LeaseContext` is a mutable god object | `core/plugins.py:147-191` (45 lines, 31 fields). 18 identity fields, 9 staging fields, lifecycle (`revoked`, `abort_signal`, `cancel_subscription`, `opened_thread_id`). 242 `lease.*` accesses in `src/voidcode/core`; staged lists are appended directly from `services.py`. | Identity, lifecycle, and staged work have one owner-less mutable surface. Any service can mutate `owner`/`readonly` after opening, append staged work after a freeze, or read `revoked` inconsistently. There is no type-level statement that staging is per-invocation scratch and identity is immutable. | Compose `LeaseIdentity` (frozen, 18 fields) + `LeaseStaging` (9 fields, including `op_counter`/`staged_suspend`) into a small `LeaseContext` holding only staging + lifecycle. No property aliases or `__getattr__` forwarding. | **High churn**, low semantic risk |
| 3 | `_handle_stream_event` is one 157-line method | `tui/terminal_app.py:572-728` (157 lines); bridge at `tui/event_pump.py:196-205`. | Session/cursor guards, transcript settlement, active-tool lookup/replacement, tool intent/progress, provider deltas, command receipts, activation, gates, run notices, and observer errors share one if-chain. A change to tool lifecycle can silently alter committed scrollback or cursor advancement. Per-event tests must go through the whole method. | New `tui/stream_handlers.py`: the session/committed-sequence guard stays at the top; each event family becomes a ≤45-line handler taking `(app, event, rows)`. `event_pump` and `test_core_tui` call the module directly; delete `CoreTuiApp._handle_stream_event`. | **Medium** (UI behavior; no persistence) |
| 4 | CLI hand-rolled argv scan | `cli.py:52-70` command/option sets; `:73-103` `_normalized_argv` (31 lines); sole call at `:263`. | The scanner duplicates argparse’s knowledge of which options take values. A new value option omitted from `_COMMAND_SCAN_VALUE_OPTIONS` makes its value eligible to be read as the subcommand; a session/prompt value equal to a command name is an intentional edge case already covered by tests. Every CLI option change must be mirrored in two places. | Two-stage standard argparse: a root-only pre-parser with only `--workspace`/`--state-dir` using `parse_known_args`, and the main parser with `default=argparse.SUPPRESS` for those same options so help still lists them. Delete `_normalized_argv` and its scan-only constants. | **Medium** (parser edge cases) |
| 5 | `config.resolve_providers` imports the native provider table | `config.py:215-248`; lazy import at `:217`; table module `capabilities/providers/native/provider_table.py` (120 lines) and `provider_table.json` (499 lines); 13 importers across `capabilities/providers` + the catalog generator. | The product configuration layer reaches into the native wire-adapter package for rows that are product data (id, label, wire, default base URL, credential env vars, catalog keys). Provider metadata ownership is hidden inside `native/`, so config resolution depends on adapter package layout and future wires can drag config into native imports. | Move the row dataclass/validation/data to `capabilities/providers/registry.py` + `capabilities/providers/provider_table.json`; update every importer, delete `native/provider_table.py`, and import the neutral registry at `config.py` module scope. Do not move the table into `sdk/` or the core. | **Medium-low** (broad import churn, no behavior) |
| 6 | Artifact `.media` sidecar is not fsynced | `core/artifacts.py:103-128` `finish`; `:130-141` `_write_media_type` (12 lines); `:143-154` `_sync_directory` (12 lines); read at `:180-206`. | The artifact bytes are fsynced and hard-linked, but the sidecar file’s **content** is never fsynced. `open("x")` publishes the sidecar name before its data is durable, so a crash can leave a referenced artifact with missing or empty media type. The final directory fsync only makes names durable, not the sidecar data. | Keep content-addressing and the sidecar architecture. Write the media claim to a dot-prefixed temp file, `flush`+`fsync` it, hard-link it to `<digest>.media` (first claim wins), unlink the temp, then keep the single directory fsync before `finish` returns. | **Low-medium** (crash durability, small surface) |
| 7 | Test-fixture duplication | 21 unit `_server` copies (~12 lines each); 7 special registry files; `tests/unit/tui/test_idle_render.py:47-53` local `_wait_for` (7 lines) with calls at `:64,82,85`. Shared entries already exist: `tests/fixtures/core_factory.py:26` `loopback_server`, `:39` `register_core`, `:45` `discard_core`; `tests/fixtures/polling.py:11` `wait_for`. | The 21 servers are byte-equivalent except one accepts `replies=None`; a shutdown/join fix must be repeated 21 times. Seven local `_CREATED` registries duplicate teardown and differ in whether an explicitly closed Core is discarded or left for idempotent close. The idle helper has no `what` description and silently uses a 5s budget. | Use the shared loopback context manager (extend it to `replies: list \| None = None`), `register_core`/`discard_core`, and the shared `wait_for(..., what=...)`. No new fixture abstraction beyond those existing files. | **Low** |
| 8 | `PluginRuntime` is constructed twice with the same 23-keyword host surface | `core/engine.py:671-709` (39 lines) and `:3089-3127` (39 lines); 23 keyword-only arguments after `sources` (17 callback closures + store/artifact/sandbox/spool paths). `bootstrap.py:514-519` is a different six-line dry-run runtime. | Cold start and hot activation assemble the same object independently. Adding a callback in one site only makes cold and reloaded generations behave differently; the failure appears only after `core.reload`. | One `Core._build_runtime(sources, *, revision_id, sandbox_backend)` factory used by both sites. Do not introduce a service locator or change `PluginRuntime`’s explicit callback contract. Leave the bootstrap dry-run construction alone. | **Low** |

The four paging implementations named in `docs/TODO.md` are not an inventory item: `capabilities/resource_pages.py:119` (replacement decoder, streaming display), `capabilities/exec/process_reader.py:152` (strict + hex cursor), `core/coverage.py:87` (strict prefix, model projection), `tui/artifact_viewer.py:523` (strict + state-cache viewport). Their error policies are the consumer contract.

---

## 2. Three independent workstreams and exact file ownership

The workstreams are disjoint by file. They may run concurrently in one shared tree; each commit must contain only its own paths. Do not “help” by editing another workstream’s file; send the owner a blocker instead.

### Workstream C — core runtime shape

Owns:

- `src/voidcode/core/engine.py`
- `src/voidcode/core/background_actions.py`
- new `src/voidcode/core/background_execution.py`
- `src/voidcode/core/plugins.py`
- `src/voidcode/core/services.py`
- Core-owned unit tests: `tests/unit/core/test_background_actions.py`, `test_background_tool_wait.py`, `test_background_shutdown.py`, `test_facts.py`, `test_process_lease.py`, `test_transcript_contract.py`, `test_child_capture_errors.py`, `test_payload_bounds.py`, `test_services_s3.py`; core-owned integration test: `tests/integration/test_sandbox_backend.py`; and any new `tests/unit/core/test_background_execution.py` / `test_lease_identity.py`.

Must NOT touch:

- `src/voidcode/tui/**`, `tests/unit/tui/**`, `tests/unit/test_core_tui.py`
- `src/voidcode/config.py`, `cli.py`, `bootstrap.py`
- `src/voidcode/capabilities/**`, `scripts/**`
- `src/voidcode/core/artifacts.py`
- `tests/fixtures/**`
- M-owned tests: `tests/unit/core/test_activation.py`, `test_artifacts.py`, `test_collaboration.py`, `test_overlays.py`, `test_provider_passthrough.py`, `test_ask.py`, `test_child_structured.py`, `test_delegation_bounds.py`, `test_ephemeral_steering.py`, `test_followup_image_refs.py`, `test_followup_queue.py`, `test_lifecycle_hooks.py`, `test_model_query_structured.py`, `test_nested_tools.py`, `test_pre_tool_gate.py`, `test_provider_streaming.py`, `test_request_window.py`, `test_session_concurrency.py`, `test_session_reads.py`
- `tests/integration/**` (narrow exception: `tests/integration/test_sandbox_backend.py` for lease-constructor adaptation only), `docs/**`

### Workstream T — TUI stream decomposition

Owns:

- `src/voidcode/tui/terminal_app.py`
- `src/voidcode/tui/event_pump.py`
- new `src/voidcode/tui/stream_handlers.py`
- `tests/unit/tui/**`
- `tests/unit/test_core_tui.py`

Must NOT touch:

- `src/voidcode/core/**`, `src/voidcode/config.py`, `cli.py`, `bootstrap.py`, `src/voidcode/capabilities/**`
- `tests/fixtures/**`, `tests/unit/core/**`, `tests/unit/plugins/**`
- `tests/integration/**`, `docs/**`
- Other TUI modules (`events.py`, `rendering.py`, `views.py`, `term.py`, `composer.py`, `operations.py`, `catalog.py`) unless this plan explicitly amends ownership. Read them, do not edit them.

### Workstream M — product/config/mechanical cleanup

Owns:

- `src/voidcode/config.py`
- `src/voidcode/cli.py`
- new `src/voidcode/capabilities/providers/registry.py`
- moved `src/voidcode/capabilities/providers/provider_table.json`
- `src/voidcode/capabilities/providers/__init__.py`, `dispatch.py`, `panels.py`
- `src/voidcode/capabilities/providers/native/anthropic_native.py`, `config.py`, `naming.py`, `openai_native.py`, `opencode.py`, `opencode_go.py`, `openrouter.py`, `provider_config.py`, `thinking_rules.py`
- delete `src/voidcode/capabilities/providers/native/provider_table.py`
- `scripts/generate_model_catalog.py`
- `src/voidcode/core/artifacts.py`
- `tests/fixtures/core_factory.py`
- M-owned tests: `tests/unit/test_config.py`, `tests/unit/core/test_activation.py`, `test_artifacts.py`, `test_collaboration.py`, `test_overlays.py`, `test_provider_passthrough.py`, `test_ask.py`, `test_child_structured.py`, `test_delegation_bounds.py`, `test_ephemeral_steering.py`, `test_followup_image_refs.py`, `test_followup_queue.py`, `test_lifecycle_hooks.py`, `test_model_query_structured.py`, `test_nested_tools.py`, `test_pre_tool_gate.py`, `test_provider_streaming.py`, `test_request_window.py`, `test_session_concurrency.py`, `test_session_reads.py`, `tests/unit/core/test_cli_arguments.py`, `tests/unit/plugins/test_provider_errors.py`, `test_providers_data.py`, `test_providers_panels.py`, `tests/integration/test_workflows_restart.py`, `test_goal_plugin.py`, `test_plan_plugin.py`, `test_todo_plugin.py`

Must NOT touch:

- `src/voidcode/core/engine.py`, `plugins.py`, `services.py`, `background_actions.py`, new `background_execution.py`
- `src/voidcode/tui/**`, `tests/unit/tui/**`, `tests/unit/test_core_tui.py`
- Core-owned tests listed in C, including `tests/integration/test_sandbox_backend.py`
- `src/voidcode/bootstrap.py`
- `docs/**` (the final docs/status commit is the integration owner’s)
- `src/voidcode/capabilities/providers/native/model_catalog_data.json` (generated; never hand-edit)

---

## 3. Workstream steps

Every step is behavior-preserving. Every commit is independently revertible. Run the listed targeted command after the step; do not run the full suite mid-workstream.

### Workstream C

#### C1. One `PluginRuntime` builder (low risk, first)

Steps:

1. Add `Core._build_runtime(self, sources: tuple[PluginSource, ...], *, revision_id: str, sandbox_backend: SandboxBackend | None) -> PluginRuntime` near `build_candidate_runtime`.
2. It passes `workspace=self.workspace`, `revision_id=revision_id`, `store=self.store`, all 17 callback closures, `artifacts_dir=self._state_dir / "artifacts"`, `process_spool_dir=self._process_spool_dir()`, and the supplied sandbox backend.
3. Replace `engine.py:671-709` and `:3091-3127` with calls. Cold start passes its existing `sandbox_backend`; candidate passes `self._activation.candidate_sandbox_backend(admission)`.
4. Do not touch `bootstrap.adopt_generation` (`bootstrap.py:514-519`): it is a no-callback, no-store dry run.
5. Remove any now-unused imports in `engine.py` only if ruff proves them unused; do not delete `PluginRuntime` import (the return type uses it).

Target: `engine.py` 6,697 → about 6,658 (−39). One runtime call site remains in `engine.py`.

Acceptance (serial):

```bash
uv run python -X utf8 -m pytest -n0 \
  tests/unit/core/test_activation.py \
  tests/unit/core/test_host_lifecycle.py \
  tests/unit/core/test_facts.py \
  tests/unit/core/test_payload_bounds.py -q
```

Rollback/stop: revert the commit. Stop if cold and candidate construction need genuinely different callbacks; that would be a real generation difference and must be designed, not hidden in a factory.

Commit: `refactor(core): build plugin runtimes through one host factory`.

#### C2. Extract `LeaseStaging` (medium churn, mechanical)

Steps:

1. In `plugins.py`, add `LeaseStaging` with the nine staging fields from `LeaseContext:170-182`: `state`, `events`, `continuations`, `cancellations`, `observer_receipts`, `budget`, `observations`, `suspend`, `op_counter`. Keep the same container types and initialization semantics; `staged_suspend` is still uncommittable by construction.
2. Change `LeaseContext` to hold `staging: LeaseStaging = field(default_factory=LeaseStaging)`; remove the nine flat fields.
3. Update all reads/writes:
   - `plugins.py`: `_staged_changes`, invoke/suspend/freeze, observer aggregation, close/revoke (only staging-independent state stays).
   - `services.py`: every `lease.staged_*` / `lease.op_counter` becomes `lease.staging.*`; do not add staging methods yet (this step is mechanical, not a new API).
   - `engine.py`: any `lease.staged_*` read (background invoke/observer paths) becomes `lease.staging.*`.
4. Update direct test constructors only if they pass staged fields; most construct no staging and need no change.
5. Keep `LeaseContext.internal_restricted` on `LeaseContext` for this step; identity is still flat.

Target: no production line-count reduction; `plugins.py` grows by roughly the moved field block (+10-15). The point is one explicit staging owner.

Acceptance (serial):

```bash
uv run python -X utf8 -m pytest -n0 \
  tests/unit/core/test_facts.py \
  tests/unit/core/test_process_lease.py \
  tests/unit/core/test_transcript_contract.py \
  tests/unit/core/test_child_capture_errors.py \
  tests/integration/test_sandbox_backend.py \
  tests/unit/core/test_services_s3.py \
  tests/unit/core/test_payload_bounds.py -q
```

Rollback/stop: revert the commit. Stop if any code mutates staging through a path that does not have a lease (then the split exposed a real ownership bug, report it; do not add a global staging buffer).

Commit: `refactor(core): give each lease an explicit staging buffer`.

#### C3. Extract frozen `LeaseIdentity` (highest churn in workstream C)

Steps:

1. Add `@dataclass(frozen=True, slots=True) class LeaseIdentity` with the 18 fields from `LeaseContext:148-169`: `token`, `owner`, `session_id`, `workspace`, `run_id`, `revision_id`, `invocation_id`, `readonly`, `root_action_id`, `initial_root_budget`, `origin_visible_tools`, `origin_readonly`, `background_action_id`, `actor`, `hook`, `internal_work`, `invocation_depth`, `tool_callback`.
2. Move `internal_restricted` to `LeaseIdentity` (it depends only on `readonly` + `internal_work`).
3. `LeaseContext` becomes: `identity`, `staging`, `revoked`, `abort_signal`, `cancel_subscription`, `opened_thread_id`. No identity fields and no aliases.
4. Update `_open_lease` to construct `LeaseIdentity(...)` and pass it. Update `_staged_changes`, `admission_guard`, `_revoke_lease`, `cancel_event_for`, `_mirror_lease_cancel`, and `store_window_projection` to `lease.identity.*`.
5. Update every `lease.owner`/`session_id`/`run_id`/`token`/... access in `plugins.py`, `services.py`, `engine.py` to `lease.identity.*`. Prefer a local `identity = lease.identity` at the top of long service methods to avoid repeated chains; do not add forwarding properties.
6. Update the direct constructors in `tests/integration/test_sandbox_backend.py`, `tests/unit/core/test_child_capture_errors.py`, `test_facts.py`, `test_process_lease.py`, `test_transcript_contract.py` to build `LeaseIdentity` + `LeaseContext`. This is constructor adaptation, not new assertions.
7. Do not change any service permission, cancellation, or staging behavior.

Target: `plugins.py` 1,894 → about 1,915-1,935; `services.py` line-neutral; `engine.py` unchanged except access names. No property shims.

Acceptance (serial):

```bash
uv run python -X utf8 -m pytest -n0 \
  tests/unit/core/test_facts.py \
  tests/unit/core/test_process_lease.py \
  tests/unit/core/test_transcript_contract.py \
  tests/unit/core/test_child_capture_errors.py \
  tests/integration/test_sandbox_backend.py \
  tests/unit/core/test_services_s3.py \
  tests/unit/core/test_payload_bounds.py -q
```

Rollback/stop: split into a separate commit and revert if typing/tests fail. Stop if any code assigns an identity field after construction (that is a real design decision; report it, do not add a mutable identity escape hatch). Stop if the diff requires changing behavior or assertions beyond constructors.

Commit: `refactor(core): separate immutable lease identity from lifecycle`.

#### C4. Move background tool-wait out of `Core` (medium-high risk)

Steps:

1. Create `src/voidcode/core/background_execution.py` with an explicit `BackgroundExecutionHost` protocol. Initial protocol members: `store`, `workspace`, `closed`, `ensure_open()`, `session()`, `admits_fresh_work()`, `cancel_background_tool_requests()`, `drain_deferred_requests()`, `artifact_page()`.
2. Keep `_BackgroundContext` (64 lines) in `engine.py` for this step. Its `wait_tool()` delegates to `self._core._background_execution.await_tool(self, receipt)`; do not move the context before the executor owns its `invoke()` path.
3. Move `_background_tool_outcome` (131), `_terminal_background_tool_request_ids` (23), `_commit_tool_wait_events` (24), `_await_background_tool` (84), `_reconcile_lost_background_tool_waits` (39), and `_drain_pending_background_tool_waits` (35) into `BackgroundExecution`.
4. `Core` constructs `self._background_execution = BackgroundExecution(self)` after `self._background`; the existing call sites at `engine.py:1647,1845,1905,1968` call `self._background_execution.drain_pending(...)`; the restart reconcile at `engine.py:6446` calls `.reconcile_lost(...)`.
5. Keep `_drain_deferred_requests` in `Core` (it is foreground deferred dispatch and uses `_drive`); expose it as a host method.
6. Keep `_terminal_background_tool_request_ids` and `_commit_tool_wait_events` behavior bit-for-bit, including the sequence-conflict retry and `_stamp_fact_owners` call.
7. Do not move `BackgroundActionRegistry`; claim/live/abort state stays where it is.

Target: `engine.py` drops about 300-320 lines; new `background_execution.py` about 330-360 lines. `engine.py` about 6,340 before C5.

Acceptance (serial):

```bash
uv run python -X utf8 -m pytest -n0 \
  tests/unit/core/test_background_actions.py \
  tests/unit/core/test_background_tool_wait.py \
  tests/unit/core/test_background_shutdown.py \
  tests/unit/core/test_collaboration.py -q
```

Rollback/stop: revert C4 only. Stop if the host protocol needs more than the listed 9 methods or if moving any wait branch requires changing which event commits atomically. In that case leave the wait cluster in Core and record the exact branch; do not add a partial adapter that calls back into a removed method.

Commit: `refactor(core): move background tool waits into a domain collaborator`.

#### C5. Move background driver invoke out of `Core` (highest risk, last)

Steps:

1. Extend `BackgroundExecutionHost` with the minimum execution surface: `dispatch(label)`, `check_session()`, `visible_tool_names_for_session()`, `session_read_only()`, `notify_commit()`, `owner_cancel_claimed_work()`, and `snapshot`.
2. Move `_BackgroundContext` (64), `drive_background_action` (70), and `_invoke_background_action` (124) into `BackgroundExecution`. The context now holds the executor; `invoke()` and `wait_tool()` keep the same driver-facing shape.
3. `BackgroundActionRegistry` continues to call `host.drive_background_action(...)`; `Core.drive_background_action` becomes a one-line delegation required by the existing host protocol (not a compatibility alias).
4. Keep the atomic `commit_staged` retry loop, sequence-conflict handling, lease revoke in `except BaseException`, `notify_tool_waiters`, owner-cancel settlement, and deferred drain exactly as they are.
5. The new module may keep the existing `runtime._open_lease` / `_staged_changes` / `_revoke_lease` calls with the current `# noqa: SLF001` comments; do not widen `PluginRuntime`’s public API for this move.
6. Remove the moved methods from `Core`; no duplicate implementation remains.

Target: `engine.py` 6,697 → about 6,090-6,130; `background_execution.py` about 590-620 lines total. If C5 stops, C4 alone still leaves `engine.py` near 6,340, and the residual must explicitly say “invoke/context remain” rather than claiming completion.

Acceptance (serial):

```bash
uv run python -X utf8 -m pytest -n0 \
  tests/unit/core/test_background_actions.py \
  tests/unit/core/test_background_tool_wait.py \
  tests/unit/core/test_background_shutdown.py \
  tests/unit/core/test_collaboration.py \
  tests/integration/test_core_governance.py -q
```

Rollback/stop: revert C5 only, keep C4. Stop if `drive_background_action` cannot be delegated without changing the `BackgroundActionHost` contract, or if the commit/abort ordering changes. A test assertion change is a stop signal, not a refactor task.

Commit: `refactor(core): move background driver execution out of Core`.

---

### Workstream T

#### T1. Split stream event handling by event family (medium risk)

Steps:

1. Create `src/voidcode/tui/stream_handlers.py` with `handle_stream_event(app: CoreTuiApp, event: StreamEvent, rows: list[str]) -> None`.
2. Keep the first guards in the top-level function exactly as today:
   - reject `event.session_id != app.session_id` without advancing the cursor;
   - reject `event.sequence <= committed`;
   - advance `app._session_cursors[app.session_id]`;
   - compute `payload` and `entry`.
3. Extract these handlers, each ≤45 lines:
   - `_transcript_entry` — `ProviderTurnResult` settlement, `TranscriptTool` active-tool identity replacement, `_record_tool_artifact`, `_content_rows`;
   - `_tool_intent` — decode call, icon, `ActiveToolProgress` construction;
   - `_tool_progress` — identity/generation guard + `append_chunk`;
   - `_tool_terminal` — active-tool pop + artifact recording fallback;
   - `_provider_progress` — text/reasoning delta buffers;
   - `_command_completed` — `CommandResult.from_payload` + receipt rows;
   - `_activation_committed` — overlay revision drop, gate change, catalog load, health refresh, queued confirmation;
   - `_gate_change` / `_run_notice` / `_observer_error`.
4. Top-level dispatch uses the existing `EventType` branches but each branch calls one handler; target ≤35 lines.
5. Delete `CoreTuiApp._handle_stream_event` (`terminal_app.py:572-728`). Update `event_pump.py:196-205` (both stream-head and stream-event paths) to call the module. Update `tests/unit/test_core_tui.py` call sites to call `stream_handlers.handle_stream_event(app, event, rows)`.
6. Do not change event order, cursor advancement, active-tool identity tuples, or scrollback commit points.
7. Add `tests/unit/tui/test_stream_handlers.py` only for behavior not already covered: a wrong-session event does not advance the cursor; an already-committed sequence does not render; a tool-intent → progress → completed sequence commits exactly once. Reuse the existing `CoreTuiApp` test harness.

Target: `terminal_app.py` 2,437 → ≤2,320; new module about 280-320 lines; top-level ≤35, no handler >45.

Acceptance (serial):

```bash
uv run python -X utf8 -m pytest -n0 tests/unit/tui tests/unit/test_core_tui.py -q
```

Then the PTY-facing TUI suites once for the workstream:

```bash
uv run python -X utf8 -m pytest -n0 \
  tests/integration/test_tui_monitor.py \
  tests/integration/test_tui_command_browsers.py -q
```

Rollback/stop: revert T1 if any TUI unit test changes behavior. Stop if a handler needs new app state or if the split changes when rows are appended/committed; that is a behavior change and must be designed separately.

Commit: `refactor(tui): split stream event handling by event family`.

#### T2. Use the shared polling helper in the idle test (low risk)

Steps:

1. Import `wait_for` from `tests.fixtures.polling`.
2. Delete `test_idle_render.py:47-53` `_wait_for`.
3. Replace the three calls with explicit descriptions:
   - `wait_for(lambda: len(terminal.frames) >= 1, what="the initial idle paint")`
   - `wait_for(lambda: len(terminal.frames) >= settled + 1, what="one frame after WakeEvent")`
4. Keep `time.sleep(0.4)`: the test intentionally observes silence; it is not a condition wait and must not become a busy helper.

Target: helper removed; test behavior unchanged.

Acceptance (serial):

```bash
uv run python -X utf8 -m pytest -n0 tests/unit/tui/test_idle_render.py -q
```

Rollback/stop: revert T2. Stop if the shared helper’s 20s default changes the test’s intent; pass an explicit timeout if needed, but keep a `what` description.

Commit: `test(tui): use shared condition waits in the idle render test`.

---

### Workstream M

#### M1. Neutral provider registry (medium-low risk)

Steps:

1. Create `src/voidcode/capabilities/providers/registry.py` containing the current `ProviderWire`, `ProviderTableRow`, `_row`, `_rows_from_payload`, `PROVIDER_TABLE`, `PROVIDER_TABLE_BY_ID`, and `require_provider_id` from `native/provider_table.py`.
2. Move `native/provider_table.json` → `capabilities/providers/provider_table.json`. The registry loads it with a path relative to its own module and `json.loads` (do not add a generic resource-loader abstraction; `native/_resources.py` remains for native-only data).
3. Update importers:
   - `capabilities/providers/dispatch.py:42`
   - `capabilities/providers/panels.py:19`
   - `native/anthropic_native.py:61`, `config.py:15`, `naming.py:33`, `openai_native.py:60`, `opencode.py:19`, `opencode_go.py:16`, `openrouter.py:10`, `provider_config.py:11`, `thinking_rules.py:19`
   - `config.py:217` → module-scope `from .capabilities.providers.registry import ...`; delete the function-local import
   - `scripts/generate_model_catalog.py:24`
4. Delete `native/provider_table.py`. Update module docstrings/error strings that still say the table lives in `native/`; keep the validation messages’ `provider_table.json` wording.
5. Update test imports: `tests/unit/core/test_activation.py`, `tests/unit/plugins/test_provider_errors.py` (2 sites), `test_providers_data.py`, `test_providers_panels.py`, `tests/unit/test_config.py`.
6. Do not edit `native/model_catalog_data.json` or `native/thinking_rules.json`; the catalog generator still emits to `native/`.

Target: one data module at the provider capability root; `config.py` no longer mentions `native`; no behavior change.

Acceptance (serial):

```bash
uv run python -X utf8 -m pytest -n0 \
  tests/unit/test_config.py \
  tests/unit/plugins/test_providers_data.py \
  tests/unit/plugins/test_providers_panels.py \
  tests/unit/plugins/test_provider_errors.py \
  tests/unit/core/test_activation.py \
  tests/unit/core/test_provider_passthrough.py -q
```

Rollback/stop: revert M1. Stop if moving the JSON breaks wheel/sdist packaging or creates an import cycle; do not keep a re-export shim at `native/provider_table.py`. If packaging needs a hatch data rule, that is an explicit change to `pyproject.toml` and must be called out in the commit.

Commit: `refactor(config): move provider rows to a neutral registry`.

#### M2. Replace hand-rolled argv normalization (medium risk)

Steps:

1. Add a root-only parser factory, e.g. `_root_parser()`, with `add_help=False` and only `--workspace`/`--state-dir` (`type=Path`, `default=None`).
2. In `main`, before the main parser:
   - `root_args, remaining = _root_parser().parse_known_args(argv if argv is not None else sys.argv[1:])`
3. Keep the existing main parser’s `--workspace`/`--state-dir` entries for help text, but set `default=argparse.SUPPRESS` so they do not overwrite the pre-parsed values.
4. After `parser.parse_args(remaining)`, resolve:
   - `workspace = root_args.workspace if root_args.workspace is not None else getattr(args, "workspace", None)`
   - `state_dir` likewise; default to `Path.cwd()` for workspace as today.
5. Delete `_normalized_argv`, `_COMMANDS`, `_ROOT_VALUE_OPTIONS`, and `_COMMAND_SCAN_VALUE_OPTIONS` if no other reader remains. Update the call at `:263`.
6. Preserve these tested semantics:
   - root options before or after the command;
   - a workspace value literally named `run` is not the command;
   - bare-client `--session-id` before a command still parses and is rejected by the existing bare-client check;
   - an unknown command still exits 2;
   - `--workspace=…` inline form works.
7. Add edge cases to `tests/unit/core/test_cli_arguments.py`: inline `--workspace=…`, root options after a command with a value equal to a command name, and `--`-terminated prompt unchanged.

Target: `cli.py` 557 → ≤535; no hand scan; standard argparse owns option/value knowledge.

Acceptance (serial):

```bash
uv run python -X utf8 -m pytest -n0 \
  tests/unit/core/test_cli_arguments.py \
  tests/unit/core/test_provider_passthrough.py -q
```

Rollback/stop: revert M2. Stop if an existing documented argv shape cannot be expressed without a manual scan; do not reintroduce a scanner under another name. Report the exact shape.

Commit: `refactor(cli): let argparse own root option placement`.

#### M3. Make the artifact media sidecar durable (low-medium risk)

Steps:

1. In `_write_media_type`, write to `self.root / f".media-{uuid.uuid4().hex}"` with mode `0o600`, `flush()`, `os.fsync(handle.fileno())`, close.
2. `os.link(temp, sidecar)` to preserve first-claim-wins (`FileExistsError` = another claim stands), then `temp.unlink(missing_ok=True)` in a `finally`.
3. Leave `_sync_directory()` as the single directory fsync after the sidecar link; do not add a second directory fsync per artifact.
4. Do not move media types into SQLite, do not change `ArtifactReference`, and do not change `artifact://` identity.
5. Add a unit test that monkeypatches `os.fsync` and records the file descriptors/paths, asserting the sidecar content is fsynced before `finish()` returns; keep the existing media-type round-trip test.

Target: `artifacts.py` 206 → about 220-225; no API change.

Acceptance (serial):

```bash
uv run python -X utf8 -m pytest -n0 tests/unit/core/test_artifacts.py -q
```

Rollback/stop: revert M3. Stop if the platform’s hard-link semantics cannot support the temp→sidecar publish; do not silently fall back to a non-atomic write.

Commit: `fix(core): fsync artifact media sidecars before publication`.

#### M4. Shared loopback server fixture (low risk)

Steps:

1. Extend `tests/fixtures/core_factory.py:26` `loopback_server` to accept `replies: list[dict[str, object]] | None = None` and construct `OpenAILoopbackServer(replies or [])`.
2. In the 18 M-owned test files, delete the local `_server` contextmanager and import/use `loopback_server`. Remove now-unused `threading`/`contextmanager` imports only when ruff confirms.
3. The 3 core-owned files (`test_background_actions.py`, `test_background_tool_wait.py`, `test_process_lease.py`) are C’s responsibility; do not edit them. TUI files are T’s responsibility.
4. If a file uses a genuinely different server lifecycle, leave it local and record it; do not add a second shared abstraction.

M-owned files with `_server` copies:
`test_activation.py`, `test_ask.py`, `test_child_structured.py`, `test_collaboration.py`, `test_delegation_bounds.py`, `test_ephemeral_steering.py`, `test_followup_image_refs.py`, `test_followup_queue.py`, `test_lifecycle_hooks.py`, `test_model_query_structured.py`, `test_nested_tools.py`, `test_overlays.py`, `test_pre_tool_gate.py`, `test_provider_passthrough.py`, `test_provider_streaming.py`, `test_request_window.py`, `test_session_concurrency.py`, `test_session_reads.py`.

Target: one server implementation, 21 call sites.

Acceptance (serial):

```bash
uv run python -X utf8 -m pytest -n0 \
  tests/unit/core/test_activation.py \
  tests/unit/core/test_collaboration.py \
  tests/unit/core/test_overlays.py \
  tests/unit/core/test_provider_passthrough.py \
  tests/unit/core/test_session_reads.py -q
```

Then a broader M-owned core pass before final:

```bash
uv run python -X utf8 -m pytest -n0 tests/unit/core -q
```

Rollback/stop: revert M4. Stop if a test depends on a distinct shutdown order; keep that one local and document why.

Commit: `test: share the loopback provider server across core suites`.

#### M5. Shared core registry (low risk)

Steps:

1. In the six M-owned special-registry files, remove the local `_CREATED`/`_CREATED_CORES` lists and local autouse teardown.
2. Replace `open_core(...)` + list append with `register_core(open_core(...))`.
3. Replace explicit `core.close(); list.remove(core)` with `discard_core(core)` **after** the existing close (or let `discard_core` plus the test-local close remain; the shared teardown closes anything still registered).
4. Rely on `tests/conftest.py`’s autouse `_close_core_factory` → `close_cores`; do not create a second registry.
5. Leave `test_background_actions.py` to C. If an explicit close is needed mid-test, call `core.close()` then `discard_core(core)`; `Core.close()` is idempotent, so forgetting a discard is not a leak.
6. Integration files: `tests/integration/test_workflows_restart.py`, `test_goal_plugin.py`, `test_plan_plugin.py`, `test_todo_plugin.py`.
7. Unit files: `tests/unit/core/test_overlays.py`, `test_collaboration.py`.

Target: one core registry/teardown; seven local blocks disappear.

Acceptance (serial):

```bash
uv run python -X utf8 -m pytest -n0 \
  tests/unit/core/test_overlays.py \
  tests/unit/core/test_collaboration.py \
  tests/integration/test_workflows_restart.py \
  tests/integration/test_goal_plugin.py \
  tests/integration/test_plan_plugin.py \
  tests/integration/test_todo_plugin.py -q
```

Rollback/stop: revert M5. Stop if a test intentionally keeps a Core alive across tests or depends on teardown ordering not expressible through the shared fixture; register it explicitly with `register_core` and document the reason.

Commit: `test: use the shared core registry in the remaining suites`.

---

## 4. Explicitly leave alone

| Item | Why |
|---|---|
| Four UTF-8 paging implementations | Intentionally distinct error policies: replacement for streaming display (`capabilities/resource_pages.py:119`), strict+hex cursor for byte-addressed logs (`capabilities/exec/process_reader.py:152`), strict prefix for model projection (`core/coverage.py:87`), strict+state-cache for the TUI viewport (`tui/artifact_viewer.py:523`). A shared helper would change at least one visible behavior. |
| `BackgroundActionRegistry` claim loop | `core/background_actions.py:64` already owns claim/live/abort/wakeup state; its 1s idle wake and 30s park deadline are existing recovery bounds. Do not merge it into the foreground drain or remove the wake without a separate design. |
| `bootstrap.adopt_generation` dry-run `PluginRuntime` | `bootstrap.py:514-519` is a no-callback, no-store probe with different lifecycle and validation needs. Forcing it through `Core._build_runtime` would create a fake Core or optional callback soup. |
| `PluginRuntime`’s explicit 23-keyword constructor | The builder removes duplicate assembly, not the explicit host contract. Do not replace it with a service locator, `**kwargs`, or a god `RuntimeCallbacks` bag; the explicit signature is what makes the host surface auditable. |
| `_drain_deferred_requests` / `_drive` / per-turn event rebuild | Foreground governed dispatch and model-context rebuild are not background cleanup. Moving them would widen the background host into the whole engine. |
| Artifact `.media` sidecar architecture | Content-addressed bytes must remain readable without a SQLite connection; `PluginRuntime` and tests use `ArtifactStore` standalone. Do not introduce an artifact metadata table or schema migration for this cleanup. Only fix the missing fsync/atomic publish. |
| `_Execution` full event pull per turn | The per-turn rebuild is required by the model projection contract; it is not the same as the background full-scan residual. Do not “optimize” it here. |
| `services.py` ingress `isinstance` checks | Remaining checks are real ingress or semantic shape checks, not duplicate `ChildOutcome` parsing. Leave them. |
| TUI `/tui` namespace + eleven local commands; composer’s own width engine | Accepted prior decisions. The TUI workstream only touches stream handling and the idle test. |
| TUI `time.sleep(0.4)` in `test_idle_render` | Intentional silence observation proving no idle 30fps loop; only the condition wait helper is replaced. |
| `snapshot`/`payload` double dict and `config_from_snapshot`’s five-section check | Credential-boundary design: private snapshot vs masked payload. Not part of this cleanup. |
| Generated `native/model_catalog_data.json` and `native/thinking_rules.json` | Generated/static native data; only provider rows move. Do not hand-edit generated catalogs. |
| `tests/fixtures/polling.py` API | Existing `wait_for(predicate, *, what, timeout, interval)` is the single helper. TUI and core consume it; no new polling helper. |

---

## 5. Commit boundaries

Each bullet is one commit, scoped to the listed paths. Do not stage other agents’ files. Do not use `--no-verify`.

1. `refactor(core): build plugin runtimes through one host factory` — C1 (`core/engine.py`).
2. `refactor(core): give each lease an explicit staging buffer` — C2 (`core/plugins.py`, `core/services.py`, `core/engine.py` staging reads, direct test constructors).
3. `refactor(core): separate immutable lease identity from lifecycle` — C3 (same plus identity test constructors).
4. `refactor(core): move background tool waits into a domain collaborator` — C4 (`core/engine.py`, `core/background_execution.py`, core background tests).
5. `refactor(core): move background driver execution out of Core` — C5 (same; only if C4 landed cleanly).
6. `refactor(tui): split stream event handling by event family` — T1 (`tui/terminal_app.py`, `tui/event_pump.py`, `tui/stream_handlers.py`, `tests/unit/tui/**`, `tests/unit/test_core_tui.py`).
7. `test(tui): use shared condition waits in the idle render test` — T2 (`tests/unit/tui/test_idle_render.py`).
8. `refactor(config): move provider rows to a neutral registry` — M1 (provider registry/data + importers + provider tests + `config.py`).
9. `refactor(cli): let argparse own root option placement` — M2 (`cli.py`, `tests/unit/core/test_cli_arguments.py`).
10. `fix(core): fsync artifact media sidecars before publication` — M3 (`core/artifacts.py`, `tests/unit/core/test_artifacts.py`).
11. `test: share the loopback provider server across core suites` — M4 (`tests/fixtures/core_factory.py`, M-owned test files).
12. `test: use the shared core registry in the remaining suites` — M5 (six registry files).
13. `docs: record maintainability cleanup and deferred residuals` — after all code commits: update `docs/TODO.md` “剩余（open）”, `src/voidcode/core/TODO.md` residual list, `src/voidcode/tui/TODO.md` `_handle_stream_event` line, and `docs/CHANGELOG.md` if any user-visible behavior changed (CLI parsing/durability only; internal refactors need no changelog entry).

If C5 stops, commit 4 still lands and commit 13 must keep the background-invoke residual open. If C4 stops, neither C4 nor C5 lands and the residual stays as-is; do not commit a half-extracted helper.

---

## 6. Final verification sequence (serial, no xdist)

Run after all workstreams have landed, from the repo root. The Python suite is authoritative only with `-n0`.

```bash
uv run ruff check .
uv run ruff format --check .
uv run ty check src
uv run python -X utf8 -m pytest -n0
mise run frontend:api:check
mise run frontend:check
uv run pre-commit run --all-files
```

Notes:

- The full serial suite is the contract; do not substitute `pytest -n auto` or `mise run test` without `PYTEST_ADDOPTS=-n0`. If the canonical `mise run check` is required, run it only after the serial suite and with `PYTEST_ADDOPTS=-n0` so it does not reintroduce xdist.
- Expected baseline is the current 2,461-pass serial run; new tests from T1/T2/M2/M3 may add a small number. Record the exact count in the final result; a lower count means a test was skipped or lost and must be investigated.
- `uv run pre-commit run --all-files` is the commit gate; never pass `--no-verify`.
- Frontend is not modified, but the API/typecheck gates are cheap and guard against accidental generated-file drift.

Per-workstream targeted commands are in §3; each is also serial with `-n0`. The PTY integration suites are run once by T and once in final verification if the runner is stable; a known environment flake must be reported as a rerun, never silently skipped.

---

## 7. Schedule and dependencies

| Workstream | Can start | Internal order | External dependency |
|---|---|---|---|
| C — core runtime | Immediately | C1 → C2 → C3 → C4 → C5 | None. C2/C3 touch the same files and must not run concurrently with each other. C4/C5 share `background_execution.py` and must be sequential. |
| T — TUI | Immediately | T1 → T2 | None. T1’s `tests/unit/test_core_tui.py` ownership is exclusive. |
| M — product/config/fixtures | Immediately | M1 → M2 → M3 → M4 → M5 | None for M1-M3. M4/M5 touch only M-owned tests; C/T own their own three/four cleanup files. |
| Integration | After C, T, M targeted checks | Docs commit last | Main agent runs the full serial sequence once. |

Parallel safety: C, T, and M can run in the same tree because their production files and test files are disjoint. The only shared imported files are `tests/fixtures/core_factory.py` (edited only by M) and `tests/fixtures/polling.py` (read by T/C, edited by neither). If a workstream discovers a required change outside its ownership, it must ask the owner or the main agent; it must not edit across the boundary.

First safe landing: C1 (lowest risk, reduces duplicated runtime assembly), M3 (isolated durability fix), or T2 (isolated test-helper cleanup). The highest-risk work (C5) is deliberately last and independently revertible.

---

## 8. Appendix: current anchors for re-verification

| Anchor | Current value |
|---|---|
| `core/engine.py` | 6,697 lines; `Core` at `:599` |
| `core/plugins.py` | 1,894 lines; `LeaseContext` at `:147-191`; `PluginRuntime.__init__` at `:278-389` |
| `core/services.py` | 1,498 lines |
| `tui/terminal_app.py` | 2,437 lines; `CoreTuiApp` at `:222`; `_handle_stream_event` at `:572-728` |
| `config.py` | 308 lines; `resolve_providers` at `:215-248`; native import at `:217` |
| `cli.py` | 557 lines; `_normalized_argv` at `:73-103`; call at `:263` |
| `core/artifacts.py` | 206 lines; `finish` at `:103-128`; `_write_media_type` at `:130-141`; `_sync_directory` at `:143-154` |
| Provider table | `native/provider_table.py` 120 lines; `native/provider_table.json` 499 lines |
| `tests/unit/tui/test_idle_render.py` | local helper `:47-53`; calls `:64,82,85` |
| Shared fixtures | `tests/fixtures/core_factory.py:26` server, `:39` register, `:45` discard; `tests/fixtures/polling.py:11` wait |
| Paging decision | `resource_pages.py:119`, `process_reader.py:152`, `coverage.py:87`, `artifact_viewer.py:523` |

If the tree has moved by the time implementation starts, re-run `wc -l`/grep for these symbols and update the anchors in the final docs commit. Do not infer behavior from stale line numbers; read the current function before editing.
