# Testing Policy

VoidCode uses Python and frontend tests to defend core contracts, plugin boundaries, execution lifecycles, and user-facing behavior. Python tests run with `pytest`; frontend component tests use Vitest. `mise` orchestrates project checks.

## Test Suite Structure

Python coverage is organized around the core and its plugin seam:

- **Unit tests (`tests/unit/`)**:
  - `tests/unit/core/`: core behavior — activation, storage schema and append semantics, runtime lifecycle, process leases, pre/post-tool gates, lifecycle hooks, read observations and model projections, overlays, request windows, delegation bounds, nested tools, history rendering, transcript contracts, generation edges, bundled-source boot, and CLI/TUI seams (`test_cli_arguments.py`, `test_tui_stream_cancel.py`).
  - `tests/unit/plugins/`: bundled plugin behavior asserted through each plugin's declared tools and commands — files, exec, web, providers (catalog, dispatch, reasoning/auth/streaming contracts, panels, lifecycle, usage), todo, skills, review, delegation, agent profiles, btw, cron, compaction, hooks, user_shell, and goal. `ast_grep` has dedicated semantic and native-result tests. `mcp` has dedicated configuration/schema tests and real SDK stdio integration coverage for discovery, calls, shutdown, startup failure and core admission. `lsp` is covered by `tests/unit/plugins/test_lsp_managed.py` and `tests/integration/test_lsp_managed.py`.
  - `tests/unit/sdk/`: the shared contracts — command/tool/view models, strict codec behavior, history page bounds, and the import-direction guard that keeps the SDK free of implementation dependencies.
  - `tests/unit/test_core_tui.py`: the TUI's view widgets plus the app loop over stub clients — on-demand views, command results, decision gates, and reconnect. `tests/unit/core/test_tui_stream_cancel.py` proves quit/switch cancels a real idle HTTP follow stream with generation isolation. Real-terminal PTY acceptance (composer, overlays, scrollback, remote path) remains the manual run below.
- **Integration tests (`tests/integration/`)**: multi-component workflows over a real core and real storage:
  - `tests/integration/test_core_activation.py`, `test_core_governance.py`, `test_core_transport.py`, `test_core_vertical.py`, `test_core_first_boot.py`, `test_core_static.py`, plus facts (`test_sdk_facts.py`, `test_wakeup_facts.py`), transcript (`test_transcript.py`), optional queries (`test_optional_workspace_queries.py`), and the tally example (`test_tally_example.py`).
  - `tests/integration/test_*_plugin.py`: one end-to-end suite per product plugin (btw, compaction, cron, delegation, goal, hooks, plan, review, skills, todo, user_shell, mcp, lsp).
- **Frontend (`frontend/`)**: Vitest component tests for the core app and its eight view renderers (`frontend/src/core/views.test.tsx`). See `frontend/README.md` for the frontend workflow.

## Core Testing Principles

1. **Assert Observable Behavior**:
   Prefer durable events and state, tool results, command results, rendered views, bounds, and error behavior over snapshots of implementation details. Do not port assertions on legacy wire payload shapes, legacy database rows, or paint sequences.

2. **Keep Fixtures Behavioral**:
   A collaborator fixture should drive a meaningful consumer path; its call count or forwarded arguments alone do not establish coverage.

3. **Drive Providers Through a Loopback**:
   `tests/provider_loopback.py` runs a real OpenAI-compatible HTTP server on `127.0.0.1` with canned replies and records every request body, header set, and response. Core and plugin suites point the real `providers` plugin (provider name `endpoint`) at that endpoint: no provider credentials are needed, and the exercised path is the genuine wire adapter rather than a stub. Tests using the loopback are the default for provider behavior. The loopback must stay wire-faithful on the streaming path: when a request asks with `stream_options.include_usage`, the scripted `usage` block is forwarded as a terminal usage-only SSE chunk (`choices: []`), verbatim — a scripted `usage=None` sends no chunk so unknown usage stays unknown, and a reported `0` stays a `0`, never a default. Service fakes must match the current SDK constructors they mirror; a stale kwarg (as the cron `schema_version` fixture drift was) fails every test using the fixture at setup.

4. **Exercise Plugins Through Their Public Surface**:
   Plugin tests admit the plugin and invoke its declared tools/commands, so the same registration, argument schema, effect classification, and service surface a user gets is what the test covers.

5. **Run Deterministically**:
   No test may reach the outside world by accident. Standard runs require no network and no external binaries.

## Offline Guards (`tests/conftest.py`)

`tests/conftest.py` installs autouse fixtures that keep the suite deterministic:

- **`_deny_live_sockets`**: fails immediately on any real non-loopback socket connect (including `ssl.SSLSocket`). Loopback stays open, because integration tests run real HTTP servers there; AF_UNIX sockets stay open too. Plugin code runs in the core process, so this guard covers the test and core processes alike; a plugin's own network use is the declared `network` effect, exercised against loopback endpoints.
- **`_isolated_xdg_runtime_dirs`**: gives each test its own `XDG_STATE_HOME`, `XDG_CACHE_HOME`, `XDG_DATA_HOME`, and `LOCALAPPDATA`, so core state never leaks between tests or into the developer's directories.
- **`allow_live_network`**: the explicit opt-out. Only a test whose subject *is* the live network may request it.

Two former guards — live model discovery and the doctor's external `<tool> --version` probes — were deleted together with those subjects; provider coverage now runs offline through the loopback and declared configuration instead.

## Fixtures

- `tests/fixtures/reload_plugin.py` and `tests/fixtures/s5_plugin.py` are small external plugins used to prove admission, activation, state validation, deactivation, and service surfaces without shipping product code.

## Running Tests

```bash
# Full gate: lint, typecheck, Python tests, frontend checks
mise run check

# Same hooks the commit gate runs
uv run pre-commit run --all-files

# Python suite
mise run test
mise run test:coverage

# Frontend component tests
mise run frontend:test
```

Or run `pytest` directly:

```bash
# The whole suite with slow cases reported
uv run python -X utf8 -m pytest -n auto --dist worksteal --durations=20

# One tier
uv run python -X utf8 -m pytest tests/unit/core/
uv run python -X utf8 -m pytest tests/integration/test_core_transport.py

# One file
uv run python -X utf8 -m pytest tests/unit/plugins/test_files.py
```

## Manual & Acceptance Verification

- **CLI acceptance**: with a workspace that has a valid `voidcode.json` and a reachable provider, run `uv run python -m voidcode run "read README.md"`; then inspect with `... sessions` and `... show <session_id> --events`.
- **TUI acceptance**: `uv run python -m voidcode` (bare) in a real terminal exercises the inline composer, overlays, and commit-once scrollback; the same binary with `--server http://127.0.0.1:<port> --token <token>` exercises the remote client path. The automated form is a stdlib PTY smoke (real TUI binary, genuine providers loopback, external smoke plugin): it drives explicit views, form submit with exact-read prefill, choices dispatch, command confirmation allow, native approval allow with file effects, a second turn, quit/relaunch with no duplicate replay — locally (embedded client) and remotely (serve + WebSocket RPC). Both modes must show all markers plus `smoke_out.txt == "pty-hello"`, `smoke_confirmed.txt == "confirmed"`, and matching durable approval/confirmation decided pairs in SQLite.
- **Transport acceptance**: `uv run python -m voidcode serve --port 8000`, then `curl -H "Authorization: Bearer <token>" http://127.0.0.1:8000/api/health` and open `http://127.0.0.1:8000/` for the served web client (when a frontend bundle is built).

RPC migration acceptance lives in `tests/integration/test_core_rpc.py`: real
WebSocket authentication and schema, retired HTTP route refusal, strict errors
and batches, concurrent cancellation, disconnect without mutation replay, and
bounded subscriptions with acknowledged cursors. `test_session_client_conformance.py`
drives the same SDK behavior locally and remotely. Browser reconnect acceptance
is `frontend/e2e/rpc-reconnect.spec.ts`; `frontend/src/lib/core/client.test.ts`
covers lost replies and malformed messages through a JSON-RPC peer.


## Known-red baseline @ `f16de0d1`

33 unit failures pre-exist the join/headroom slices and reproduce identically
at HEAD `3cd6afa1`. Established by running the full unit suite on both sides
with the same command (`python -m pytest tests/unit -q -p no:cacheprovider
--tb=no -rf`): tree 33 failed / 1807 passed, HEAD 33 failed / 1790 passed,
identical 33/33 failure IDs; the +17 passed delta is this slice's new tests
(2 join + 15 headroom). The two timing-sensitive `test_tui_stream_cancel`
failures were additionally re-verified solo at HEAD. Session ids inside
`unknown session` signatures are random per run; only the shape is stable.
Do not fix here; re-baseline both sides when the list changes.

- `tests/unit/core/test_artifacts.py` (6):
  - `test_spill_writes_the_bytes_under_their_digest_before_the_result_commits` — KeyError: 'sha256'
  - `test_pages_are_bounded_and_report_their_own_geometry` — KeyError: 'data'
  - `test_spilling_the_same_bytes_again_is_free` — KeyError: 'sha256'
  - `test_the_media_type_survives_the_reference` — KeyError: 'media_type'
  - `test_empty_payloads_are_stored_and_read_back` — KeyError: 'sha256'
  - `test_a_read_only_lease_may_spill_because_the_store_is_not_the_machine` — KeyError: 'sha256'
- `tests/unit/core/test_background_actions.py` (5):
  - `test_keyed_spawn_returns_one_child_and_refuses_a_differing_spec` — KeyError: 'unknown session: <id>'
  - `test_full_budget_still_refetches_a_keyed_child` — KeyError: 'unknown session: <id>'
  - `test_crash_between_child_row_and_parent_fact_stays_recoverable` — KeyError: 'unknown session: <id>'
  - `test_cancel_before_first_run_is_never_started_late` — KeyError: 'unknown session: <id>'
  - `test_concurrent_same_key_spawns_start_exactly_one_initial_run` — assert set() == {None}
- `tests/unit/core/test_background_processes.py` (6, all KeyError: 'handle'):
  - `test_a_supervised_process_outlives_the_call_and_lists_itself` — KeyError: 'handle'
  - `test_pages_read_the_log_by_offset_and_report_liveness` — KeyError: 'handle'
  - `test_written_stdin_reaches_the_process` — KeyError: 'handle'
  - `test_a_live_id_cannot_be_started_twice_and_cancel_kills_the_group` — KeyError: 'handle'
  - `test_another_session_cannot_see_or_stop_a_supervised_process` — KeyError: 'handle'
  - `test_core_shutdown_stops_supervised_processes_by_group` — KeyError: 'handle'
- `tests/unit/core/test_media_projection.py` (3, all KeyError: 'result_reference' at :320):
  - `test_real_core_native_and_untrusted_deferred_images_reach_anthropic_sdk_wire[False-1]` — KeyError: 'result_reference'
  - `test_real_core_native_and_untrusted_deferred_images_reach_anthropic_sdk_wire[True-1]` — KeyError: 'result_reference'
  - `test_real_core_native_and_untrusted_deferred_images_reach_anthropic_sdk_wire[False-10]` — KeyError: 'result_reference'
- `tests/unit/core/test_p2a_features.py` (1):
  - `test_p2a_durable_continuation_executes_real_governed_run_and_recovers` — assert 'failed' == 'completed'
- `tests/unit/core/test_process_lease.py` (4):
  - `test_run_process_returns_stdout_stderr_exit_code_and_stdin` — KeyError: 'exit_code'
  - `test_run_process_bounds_each_stream_and_reports_truncation` — KeyError: 'stdout'
  - `test_run_process_timeout_kills_the_whole_group` — KeyError: 'timed_out'
  - `test_read_only_lease_refuses_to_run_a_process` — KeyError: 'refused'
- `tests/unit/core/test_session_reads.py` (3):
  - `test_session_info_reports_one_running_session_and_its_chain_budget` — assert None == 16
  - `test_child_event_reads_are_bounded_cursor_scoped_and_descendant_checked` — s5_probe body read is None, not a Mapping
  - `test_post_tool_hook_reads_the_session_row_under_a_hook_lease` — assert None == 16
- `tests/unit/core/test_tui_stream_cancel.py` (2, both Event.wait(5) timeout):
  - `test_quit_cancels_an_idle_follow_stream_before_joining` — Event.wait(5) never set
  - `test_session_switch_survives_a_late_old_stream_attach` — Event.wait(5) never set
- `tests/unit/plugins/test_agent_profiles.py` (1):
  - `test_subagents_plugin_surface` — surface lists extra 'wait' tool
- `tests/unit/test_bundled_sources.py` (1):
  - `test_bundle_dispatches_one_tool_per_tool_plugin` — 'bundle-ok' missing from shell_exec text
- `tests/unit/test_core_tui.py` (1):
  - `test_model_turn_updates_footer_context` — latest_input_tokens None, expected 5200
