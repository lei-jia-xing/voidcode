# VoidCode Cleanup & Stabilization Checklist

## Overview
Tracking checklist for dead code removal, architecture violation fixes, slow test optimizations, and test backdoor elimination.

## 1. Dead Code Removal (Production)
- [x] `src/voidcode/plugins/goal/goal_plugin.py` (`is_complete`, `in_active_window`)
- [x] `src/voidcode/plugins/skills/skills_plugin.py` (`enabled_skills`, `is_enabled`)
- [x] `src/voidcode/sdk/hooks.py` (`total_image_bytes`)
- [x] `src/voidcode/sdk/invocation.py` (`require_revision`)
- [x] `src/voidcode/sdk/provider.py` (`cache_hit_rate`)
- [x] `src/voidcode/transport/http.py` (`create_core_app_with_factory`)
- [x] Deadlock resolution in `tests/integration/test_workflows_plugin.py`
- [x] Update test references matching removed symbols

## 2. Ingress & Type Checks
- [x] Verify `from pathlib import Path` in `src/voidcode/capabilities/files/_replace.py` (`ty check src` passes cleanly)

## 3. Useless & Over-Testing Cleanup
- [x] Remove `tests/unit/sdk/test_history.py` (mirror test and SDK reverse-importing Core violation)
- [x] Clean up `tests/unit/sdk/test_foreground.py:78-130` (self-testing fake mock `_FakeForegroundContext`)
- [x] Clean up `tests/unit/sdk/test_resource_caps.py:41-75` (Cartesian product parametrized tests on internal dataclass)

## 4. Slow Tests & CPU Overload Optimization
- [x] Fix 60s hard sleep in `tests/fixtures/reload_plugin.py:593` (replace with bounded soft polling loop)
- [x] Optimize hard sleeps in `tests/integration/test_tui_monitor.py` (replace fixed 0.4s-1.5s sleeps with deterministic `until`/`until_gone` conditions)

## 5. Test Backdoor Elimination
- [x] Remove `SqliteStore.pending_wakeups` from production code (`src/voidcode/core/storage/store.py`) and update test references
- [x] Remove `SqliteStore.count_root_children` from production code (`src/voidcode/core/storage/store.py`) and update test references to use `descendant_sessions`
- [x] Remove `TerminalApp._process_input` from `src/voidcode/tui/terminal_app.py` and update test references to call `_handle_input_bytes` directly

## 6. Integration Test Fixes & Flakiness Elimination
- [x] `tests/integration/test_workflows_restart.py`: Enabled `"subagents": {"enabled": True}` in config and aligned agent name to `"execute"`
- [x] `tests/integration/test_todo_plugin.py`: Aligned model projection assertion to read durable body from artifact `result_uri` per the artifact-spilling model projection contract
- [x] `tests/integration/test_artifact_tui_surface.py`: Fixed Linux PTY race condition in `KittyPty.wait_for` and `close` where early EOF/OSError caused premature timeout
- [x] `src/voidcode/core/engine.py`: Fixed race condition in `Core.spawn_child` for keyed child spawns on idle root sessions where concurrent contenders minted divergent random UUIDs instead of sharing root authorization, resolving unhandled `StoreConflict` warning in `test_concurrent_same_key_spawns_start_exactly_one_initial_run`

## 7. Verification & Quality Gates
- [x] `mise run lint` (ruff check)
- [x] `mise run format` (ruff format)
- [x] `mise run typecheck` (`uv run ty check src`)
- [x] `uv run pre-commit run --all-files` (all hooks passed)
- [x] Unit test suite execution (`uv run pytest tests/unit/`: all unit tests passed with 0 unhandled thread exception warnings)
- [x] Integration test suite execution (`uv run pytest tests/integration/`: all 206 passed)
- [x] Full test suite and gate verification (`mise run check`: all 2417 tests passed, lint passed, typecheck passed, frontend checks passed)
