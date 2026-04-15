# Agent Tooling Adoption Plan

**Date:** 2026-04-15
**Status:** Draft
**Author:** Synthesized from oh-my-openagent patterns
**Repo:** `/home/hunter/Workspace/voidcode`

---

## Overview

This plan adopts three high-value tooling ideas from oh-my-openagent into voidcode's existing agent workflow. The goal is to make edits more robust, give the agent self-awareness about its tool readiness, and eventually enable structural code transformation.

Voidcode already has solid text-based edit machinery in `src/voidcode/tools/edit.py` (9 replacers including `BlockAnchorReplacer`, `IndentationFlexibleReplacer`). It also has `formatter_presets` in `src/voidcode/hook/config.py` and a `ShellExecTool` that can run formatters. The missing pieces are:

1. An AST-aware structural search tool (ast-grep)
2. A closed edit loop that re-reads files after formatting
3. A doctor/check capability that verifies external tools are available

This plan also records one strategic constraint that should guide future capability work: **MCP remains useful, but it should not be the default way to introduce core agent behavior.** For `voidcode`, the preferred order is:

1. **Native runtime-managed tool** for local, frequent, product-defining behavior
2. **Skill + CLI/native tool composition** for reusable workflows and optional higher-level behaviors
3. **MCP** only for external, separately owned, auth-heavy, or user-pluggable integrations

That means MCP is not treated as dead, but it is deliberately de-emphasized as the default substrate. In practice:

- file/search/edit/refactor/build/test/git flows should prefer native tools or CLI-backed skills
- Context7-like or Exa-like capabilities can remain useful as optional external context providers
- runtime-managed MCP should stay config-gated and boundary-first unless a real extension ecosystem justifies deeper investment

---

## Why `docs/plans/`

Plans live under `docs/plans/` because this work is an implementation plan for the repository, not a product roadmap change and not a superpowers/plugin-specific artifact. The `docs/plans/` namespace keeps execution plans adjacent to but separate from `docs/architecture.md` and `docs/roadmap.md`. Future repo-level execution plans should go here; architecture changes still belong in `docs/`.

---

## Phase 0 (P0): Foundation

### P0.1: ast-grep Structural Search Tool

**What:** A new `AstGrepTool` that wraps the ast-grep CLI and returns structured match results.

**Why:** The existing `GrepTool` does literal text search, and `CodeSearchTool` does web search. Neither understands syntax structure. ast-grep fills that gap for structural find-and-replace without requiring VoidCode to build its own parser layer.

**File to create:** `src/voidcode/tools/ast_grep.py`

**Implementation sketch:**

```python
# src/voidcode/tools/ast_grep.py
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import ClassVar

from .contracts import ToolCall, ToolDefinition, ToolResult


class AstGrepTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="ast_grep",
        description=(
            "AST-aware structural search and replace using ast-grep. "
            "Supports pattern matching across Python, TypeScript, Go, Rust, and other supported languages."
        ),
        input_schema={
            "pattern": {"type": "string", "description": "ast-grep pattern (e.g. 'console.log($MSG)')"},
            "path": {"type": "string", "description": "Path to search (file or directory)"},
            "lang": {"type": "string", "description": "Language (python, ts, js, go, rust, etc.)"},
            "replace": {"type": "string", "description": "Optional replacement pattern"},
            "limit": {"type": "integer", "description": "Max results (default 50)"},
        },
        read_only=True,  # read_only until replace is used
    )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        pattern = call.arguments.get("pattern")
        if not isinstance(pattern, str):
            raise ValueError("ast_grep requires a string pattern argument")

        path_arg = call.arguments.get("path", ".")
        lang = call.arguments.get("lang", "python")
        replace = call.arguments.get("replace")
        limit = call.arguments.get("limit", 50)

        cmd = ["ast-grep", "scan", "--json", f"--limit={limit}"]
        if lang:
            cmd.extend(["--lang", lang])
        if replace:
            cmd.extend(["--replace", replace])
            # Switch to read_only=False when replace is used
        cmd.extend([pattern, path_arg])

        try:
            completed = subprocess.run(
                cmd,
                cwd=workspace.resolve(),
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(tool_name=self.definition.name, status="error",
                              error="ast_grep timed out after 30s")
        except OSError as exc:
            return ToolResult(tool_name=self.definition.name, status="error",
                              error=f"ast_grep not found or failed: {exc}")

        # ast-grep outputs newline-delimited JSON
        matches = []
        for line in completed.stdout.strip().splitlines():
            if line:
                try:
                    matches.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=f"Found {len(matches)} matches",
            data={"matches": matches, "pattern": pattern, "lang": lang},
        )
```

**Registration:** Add to `src/voidcode/runtime/tool_provider.py` following the same optional-tool pattern used for `_ApplyPatchTool`:

```python
try:
    from ..tools.ast_grep import AstGrepTool as _AstGrepTool
except ImportError:
    _AstGrepTool = None
```

Then in `BuiltinToolProvider.provide_tools()`, append `AstGrepTool()` if `_AstGrepTool is not None`.

**Test file to create:** `tests/unit/test_ast_grep_tool.py`

```python
# tests/unit/test_ast_grep_tool.py
from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.tools import ToolCall
from voidcode.tools.ast_grep import AstGrepTool


def test_ast_grep_returns_error_when_not_installed(tmp_path: Path) -> None:
    tool = AstGrepTool()
    result = tool.invoke(
        ToolCall(tool_name="ast_grep", arguments={"pattern": "foo($X)", "path": "."}),
        workspace=tmp_path,
    )
    # Should gracefully report ast-grep not available
    assert result.status == "error"
    assert "ast-grep" in result.error or "not found" in result.error.lower()


def test_ast_grep_rejects_empty_pattern(tmp_path: Path) -> None:
    tool = AstGrepTool()
    with pytest.raises(ValueError, match="pattern"):
        tool.invoke(
            ToolCall(tool_name="ast_grep", arguments={"pattern": "", "path": "."}),
            workspace=tmp_path,
        )
```

**Verification command:**

```bash
uv run pytest tests/unit/test_ast_grep_tool.py -v
mise run typecheck  # should pass without errors
```

**Acceptance criteria:**
- `AstGrepTool` class is importable from `voidcode.tools`
- Tool is registered in `ToolRegistry.with_defaults()` when ast-grep is installed
- Tool gracefully degrades when ast-grep CLI is not present (status=error, not crash)
- Unit tests cover: missing ast-grep, empty pattern rejection, workspace boundary

---

### P0.2: Formatter-Aware Edit Closure

**What:** After a successful edit, the system optionally runs the configured formatter for the file type and re-reads the file to confirm the formatter did not invalidate the edit. This closes the edit loop.

**Why:** `src/voidcode/tools/edit.py` writes the file after `_replace()`. `src/voidcode/hook/config.py` already has `formatter_presets` with commands for python (ruff format), typescript (prettier), rust (rustfmt), and others. The missing piece is calling the formatter after edit and validating the result.

**File to modify:** `src/voidcode/tools/edit.py`

**Insertion point:** At the end of `EditTool.invoke()`, after `candidate.write_bytes(new_content.encode("utf-8"))` (line 485) and before the `return ToolResult(...)`.

**Implementation sketch:**

Add a helper in `edit.py`:

```python
def _run_formatter_for_path(path: Path, hooks_config: RuntimeHooksConfig | None) -> str | None:
    """Run formatter if configured for this file type. Returns error message or None."""
    if hooks_config is None:
        return None

    suffix = path.suffix.lstrip(".")
    preset = hooks_config.formatter_presets.get(suffix)
    if preset is None:
        return None

    try:
        subprocess.run(
            list(preset.command) + [str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        return f"formatter warning: {exc.stderr.strip()}"
    except OSError as exc:
        return f"formatter warning: formatter command not found"
    return None
```

Then in `EditTool.invoke()`, after writing the file, add:

```python
# Optionally run formatter and re-read to confirm edit survived formatting
formatter_warning = _run_formatter_for_path(candidate, hooks_config=None)  # pass hooks from context
if formatter_warning:
    # Append warning to output but do not fail the edit
    output = f"{output} ({formatter_warning})"
```

**Important constraint:** The `EditTool.invoke()` signature currently does not receive `hooks_config`. The recommended approach is to keep the runtime/tool contract unchanged and have `EditTool` read formatter preset information from workspace runtime config (`.voidcode.json`) through a small cached helper that mirrors existing config parsing rules closely enough for formatter preset lookup.

Do **not** thread `hooks_config` through every tool invocation just for this feature; that would widen the runtime/tool coupling more than necessary for the first iteration.

**Test file to modify:** `tests/unit/test_edit_tool.py`

Add tests:

```python
def test_edit_tool_runs_formatter_and_re_reads(tmp_path: Path) -> None:
    # Create a .voidcode.json with a formatter preset
    config = {"hooks": {"formatter_presets": {"txt": {"command": ["tee"]}}}}
    (tmp_path / ".voidcode.json").write_text(json.dumps(config))

    file_path = tmp_path / "test.txt"
    file_path.write_text("hello world", encoding="utf-8")

    tool = EditTool()
    result = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={"path": "test.txt", "oldString": "world", "newString": "voidcode"},
        ),
        workspace=tmp_path,
    )
    assert result.status == "ok"
    # tee writes to stdout, so file should still have content
    assert file_path.read_text(encoding="utf-8") == "hello voidcode"
```

**Verification command:**

```bash
uv run pytest tests/unit/test_edit_tool.py -v
mise run lint
```

**Acceptance criteria:**
- Edit tool result includes formatter warning in output field (not error) if formatter fails
- Edit succeeds even if formatter is missing or fails (graceful degradation)
- Re-read after formatter is implemented and file content is validated
- Unit tests cover formatter-is-missing and formatter-fails scenarios

---

### P0.3: Doctor / Capability Check Tool

**What:** A new `DoctorTool` that checks readiness of all external tools: ast-grep, LSP servers, MCP servers, and formatters. Returns a structured report of what is available and what is not.

**Why:** The agent should be able to self-diagnose before attempting operations. Currently there is no single tool that aggregates capability discovery. LSP and MCP managers exist in `VoidCodeRuntime` but are not exposed as a self-service tool.

**File to create:** `src/voidcode/tools/doctor.py`

```python
# src/voidcode/tools/doctor.py
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import ClassVar

from .contracts import ToolCall, ToolDefinition, ToolResult


class DoctorTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="doctor",
        description=(
            "Check readiness of external tools and capabilities. "
            "Reports availability of ast-grep, formatters, LSP servers, and MCP servers."
        ),
        input_schema={},
        read_only=True,
    )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        checks = []

        # Check ast-grep
        try:
            result = subprocess.run(
                ["ast-grep", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            checks.append({"tool": "ast-grep", "status": "ok", "version": result.stdout.strip()})
        except (OSError, subprocess.TimeoutExpired):
            checks.append({"tool": "ast-grep", "status": "missing"})

        # Check key formatters
        formatters = ["ruff", "prettier", "rustfmt", "gofmt", "black", "prettier"]
        for fmt in formatters:
            if shutil.which(fmt):
                checks.append({"tool": fmt, "status": "ok"})
            else:
                checks.append({"tool": fmt, "status": "missing"})

        # Check formatter presets in .voidcode.json
        voidcode_json = workspace / ".voidcode.json"
        if voidcode_json.exists():
            try:
                import json
                config = json.loads(voidcode_json.read_text())
                presets = config.get("hooks", {}).get("formatter_presets", {})
                for name, preset in presets.items():
                    cmd = preset.get("command", [])
                    if cmd and shutil.which(cmd[0]):
                        checks.append({"tool": f"formatter:{name}", "status": "ok"})
                    else:
                        checks.append({"tool": f"formatter:{name}", "status": "missing"})
            except (json.JSONDecodeError, OSError):
                pass

        missing = [c for c in checks if c["status"] == "missing"]
        all_ok = len(missing) == 0

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content="All tools available" if all_ok else f"{len(missing)} tools missing",
            data={"checks": checks, "all_ok": all_ok},
        )
```

**Registration:** Same optional pattern in `src/voidcode/runtime/tool_provider.py`.

**Test file to create:** `tests/unit/test_doctor_tool.py`

**Verification command:**

```bash
uv run pytest tests/unit/test_doctor_tool.py -v
voidcode run "check capabilities" --workspace .
```

**Acceptance criteria:**
- `DoctorTool` returns a list of checks with status for each external tool
- Tool is read_only (no side effects)
- Graceful handling when tools are missing
- Output clearly indicates what is available vs. missing

---

## Phase 1 (P1): Enhanced Editing

### P1.1: Lightweight Refactor Workflow

**What:** Combine `AstGrepTool` (from P0.1) with `EditTool` or `MultiEditTool` to enable a read-then-edit refactor workflow. A new `RefactorTool` exposes a `pattern` + `rewrite` flow that uses ast-grep under the hood but presents results in voidcode's tool result format.

**File to create:** `src/voidcode/tools/refactor.py`

**Implementation:** This is a composite tool that:
1. Calls `AstGrepTool` with a pattern and `replace` argument
2. If ast-grep is not available, returns an error with instructions
3. If ast-grep is available, returns structured match data and applies the rewrite

**Why not just use ast-grep directly?** This tool can provide voidcode-native error handling, diff generation, and multi-file rewrite coordination through the existing `multi_edit` pattern.

**Test file to create:** `tests/unit/test_refactor_tool.py`

---

### P1.2: Richer Edit Mismatch Diagnostics

**What:** When `edit.py` fails to find `oldString`, surface more diagnostic information: nearby code snippets, suggestion from `BlockAnchorReplacer` if it almost matched, and the list of available replacers that were tried.

**File to modify:** `src/voidcode/tools/edit.py`

**Insertion point:** The `ValueError` at line 305: `raise ValueError("Could not find oldString in the file using replacers.")`

**Implementation sketch:**

```python
# At line 305 in _replace()
# Build diagnostic message
diagnostic_lines = [f"Could not find oldString in the file. Tried {len(replacers)} replacers:"]
for replacer in replacers:
    diagnostic_lines.append(f"  - {replacer.__name__}")

# Try BlockAnchorReplacer even if it didn't match, to give a hint
if "BlockAnchorReplacer" not in [r.__name__ for r in replacers]:
    block_hints = BlockAnchorReplacer.find(content, old_string)
    if block_hints:
        diagnostic_lines.append(f"Hint: BlockAnchorReplacer found {len(block_hints)} near-matches")

raise ValueError("\n".join(diagnostic_lines))
```

**Verification:**

```bash
uv run pytest tests/unit/test_edit_tool.py -v
# Run a test where oldString has a slight mismatch and confirm diagnostic output
```

**Acceptance criteria:**
- Error message names all replacers that were tried
- Near-matches from `BlockAnchorReplacer` are suggested when available
- Error message is human-readable and actionable

---

## Phase 2 (P2): Advanced Editing

### P2.1: Hashline / Hash-Anchored Editing

**What:** A new `HashAnchorEditTool` that uses content hashes (or line-number + content-hash) to anchor edit targets, making edits robust to insertion/deletion above the target line.

**Why:** Current replacers use text content to locate edits. If the agent adds lines above a target region, line numbers shift and subsequent edits can misfire. A hash-anchored approach computes a stable hash of the target region and uses that as an anchor.

**Implementation approach:** This is speculative at this stage. Do not implement until P0 and P1 are validated. The approach would be:

1. For a given `oldString`, compute a rolling hash of each line or line range
2. Store: `(start_line, content_hash, end_line)` as an anchor
3. On apply, find the anchor region by hash, then apply the text replacement within that anchored region
4. This is similar to how `BlockAnchorReplacer` works but uses cryptographic hashing instead of fuzzy string matching

**File to create (future):** `src/voidcode/tools/hash_anchor_edit.py`

**Risk:** Complexity is high. Only pursue if P0+P1 editing is still fragile for multi-step edit sessions.

---

## Files Summary

| File | Action | Phase |
|------|--------|-------|
| `src/voidcode/tools/ast_grep.py` | Create | P0 |
| `tests/unit/test_ast_grep_tool.py` | Create | P0 |
| `src/voidcode/tools/edit.py` | Modify (formatter closure + diagnostics) | P0 + P1 |
| `src/voidcode/tools/doctor.py` | Create | P0 |
| `tests/unit/test_doctor_tool.py` | Create | P0 |
| `src/voidcode/runtime/tool_provider.py` | Modify (register new tools) | P0 |
| `tests/unit/test_edit_tool.py` | Modify (add formatter tests) | P0 |
| `src/voidcode/tools/refactor.py` | Create | P1 |
| `tests/unit/test_refactor_tool.py` | Create | P1 |
| `src/voidcode/tools/hash_anchor_edit.py` | Create (future) | P2 |

---

## Verification Strategy

1. **P0.1 (ast-grep):** `uv run pytest tests/unit/test_ast_grep_tool.py -v`
   `mise run typecheck` passes
   `voidcode run "find all console.log in src" --workspace .` produces structured output

2. **P0.2 (formatter-aware edit):**
   `uv run pytest tests/unit/test_edit_tool.py -v` (all existing tests still pass)
   Add new test with formatter preset and verify re-read behavior
   `mise run lint`

3. **P0.3 (doctor):**
   `uv run pytest tests/unit/test_doctor_tool.py -v`
   `voidcode run "check capabilities" --workspace .` returns JSON with `all_ok` field

4. **P1.1 (refactor):** `uv run pytest tests/unit/test_refactor_tool.py -v`
   Tool appears in `ToolRegistry.with_defaults().definitions()`

5. **P1.2 (diagnostics):** Error messages from failed edits include replacer list and hints

---

## Risks to Avoid

1. **Do not claim AST capability the repo does not have.** Voidcode has no AST parsing today. The ast-grep tool wraps an external CLI; it does not add AST parsing to the codebase.

2. **Do not make formatters required.** The formatter-aware edit must be opt-in and graceful. If a formatter is missing, the edit still succeeds. Only surface a warning.

3. **Do not modify `src/voidcode/runtime/service.py` without careful review.** The `_execute_graph_loop` is the hot path. Any change here should be minimal and tested.

4. **Do not expand scope to multi-agent orchestration.** This plan is about tooling for a single agent. Keep it focused.

5. **Do not touch `docs/roadmap.md` or `docs/architecture.md`.** This plan lives in `docs/plans/` by design.

6. **Do not implement P2 hash-anchored editing until P0 and P1 are validated.** P2 is speculative. The codebase has enough text-based edit robustness that P2 may not be needed.

---

## Recommended Commit Boundaries

| Commit | Content |
|--------|---------|
| 1 | `src/voidcode/tools/ast_grep.py` + `tests/unit/test_ast_grep_tool.py` |
| 2 | `src/voidcode/tools/doctor.py` + `tests/unit/test_doctor_tool.py` |
| 3 | `src/voidcode/runtime/tool_provider.py` (register ast_grep + doctor) |
| 4 | `src/voidcode/tools/edit.py` (P0.2 formatter-aware closure) |
| 5 | `tests/unit/test_edit_tool.py` (add formatter + diagnostics tests) |
| 6 | `src/voidcode/tools/edit.py` (P1.2 richer diagnostics) |
| 7 | `src/voidcode/tools/refactor.py` + tests (P1.1) |
| 8 | `src/voidcode/tools/hash_anchor_edit.py` + tests (P2, only if needed) |

Each commit should pass `mise run check` before committing. Use conventional commit messages: `feat:`, `fix:`, `test:`.

---

## Dependencies

- ast-grep CLI must be installed (`brew install ast-grep` / `pip install ast-grep`) for P0.1 to be fully functional, but graceful degradation is required so the tool is still useful without it.
- All other work is pure Python and self-contained.
