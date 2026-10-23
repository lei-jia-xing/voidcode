"""AST search and replacement semantic declarations."""

from __future__ import annotations

from voidcode.plugins.ast_grep.ast_grep_tool import AstGrepTool


def test_search_and_preview_are_not_classified_as_edit_attempts():
    from voidcode.sdk.tools import ToolSemantic

    definition = AstGrepTool.definition
    assert definition.semantics_for({"mode": "search"}) == frozenset()
    assert definition.semantics_for({"mode": "preview", "apply": True}) == frozenset()
    assert definition.semantics_for({}) == frozenset()
    # Even an invalid apply flag is a failed edit attempt, rather than a preview.
    assert definition.semantics_for({"mode": "replace", "apply": False}) == frozenset({ToolSemantic.RESOURCE_EDIT})
    assert definition.semantics_for({"mode": "replace", "apply": True}) == frozenset({ToolSemantic.RESOURCE_EDIT})
