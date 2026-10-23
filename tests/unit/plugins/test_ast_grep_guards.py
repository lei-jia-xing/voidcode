"""Minimal guard regression: ast_grep replace honors current-hash line coverage.

Same shape as the files read→edit→read→edit fix: after two versions of a path
were observed (context read_hash is None), the second read's lines still
authorize a replace, while unseen lines still fail closed.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from voidcode.capabilities.files.guards import enforce_seen_lines
from voidcode.capabilities.files.repair import ToolDiagnosticError
from voidcode.plugins.ast_grep.ast_grep_tool import AstGrepTool
from voidcode.sdk.invocation import ToolContext


def _context(workspace: Path, *, resolved: str, v1: str, v2: str) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        session_id="session-1",
        run_id="run-1",
        invocation_id="inv-1",
        revision_id="rev-1",
        actor="model",
        read_paths=frozenset({resolved}),
        read_lines={(resolved, v1): frozenset({1, 2}), (resolved, v2): frozenset({1, 2})},
        read_whole_files=frozenset({(resolved, v1), (resolved, v2)}),
        read_hash=None,
    )


def test_replace_after_reread_succeeds_with_ambiguous_read_hash(tmp_path: Path) -> None:
    target = tmp_path / "cycle.txt"
    v2 = "alpha\nbeta2\n"
    target.write_text(v2, encoding="utf-8")
    v1_hash = hashlib.sha256(b"alpha\nbeta\n").hexdigest()
    v2_hash = hashlib.sha256(v2.encode("utf-8")).hexdigest()
    resolved = str(target.resolve())

    enforce_seen_lines(
        context=_context(tmp_path, resolved=resolved, v1=v1_hash, v2=v2_hash),
        tool_name="ast_grep",
        raw_path="cycle.txt",
        candidate=target,
        display_path="cycle.txt",
        start_line=1,
        end_line=2,
    )


def test_unseen_lines_still_fail_closed(tmp_path: Path) -> None:
    target = tmp_path / "cycle.txt"
    v2 = "alpha\nbeta2\n"
    target.write_text(v2, encoding="utf-8")
    v1_hash = hashlib.sha256(b"alpha\nbeta\n").hexdigest()
    v2_hash = hashlib.sha256(v2.encode("utf-8")).hexdigest()
    resolved = str(target.resolve())
    context = ToolContext(
        workspace=tmp_path,
        session_id="session-1",
        run_id="run-1",
        invocation_id="inv-1",
        revision_id="rev-1",
        actor="model",
        read_paths=frozenset({resolved}),
        read_lines={(resolved, v1_hash): frozenset({1, 2}), (resolved, v2_hash): frozenset({1})},
        read_whole_files=frozenset({(resolved, v1_hash)}),
        read_hash=None,
    )

    try:
        enforce_seen_lines(
            context=context,
            tool_name="ast_grep",
            raw_path="cycle.txt",
            candidate=target,
            display_path="cycle.txt",
            start_line=2,
            end_line=2,
        )
    except ToolDiagnosticError as exc:
        assert exc.error_details.get("reason") == "unseen_range"
    else:
        raise AssertionError("line 2 was never revealed at the current hash; must fail closed")


def test_search_and_preview_are_not_classified_as_edit_attempts():
    from voidcode.sdk.tools import ToolSemantic

    definition = AstGrepTool.definition
    assert definition.semantics_for({"mode": "search"}) == frozenset()
    assert definition.semantics_for({"mode": "preview", "apply": True}) == frozenset()
    assert definition.semantics_for({}) == frozenset()
    # Even an invalid apply flag is a failed edit attempt, rather than a preview.
    assert definition.semantics_for({"mode": "replace", "apply": False}) == frozenset({ToolSemantic.RESOURCE_EDIT})
    assert definition.semantics_for({"mode": "replace", "apply": True}) == frozenset({ToolSemantic.RESOURCE_EDIT})
