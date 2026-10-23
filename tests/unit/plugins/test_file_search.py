"""Shared search tools validate once and preserve real file-search behavior."""

from pathlib import Path

import pytest

from voidcode.capabilities.files.glob_tool import GlobTool
from voidcode.capabilities.files.grep_tool import GrepTool
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.tools import ToolCall


def invoke(tool, workspace, arguments):
    context = ToolContext(workspace=workspace, session_id="session", run_id="run", invocation_id="inv", revision_id="rev", actor="model")
    return tool.invoke(ToolCall(tool.definition.name, arguments), context=context, services=None)


@pytest.mark.parametrize("tool", [GlobTool(), GrepTool()])
@pytest.mark.parametrize(
    "extra",
    [
        {"limit": 5001},
        {"limit": 0},
        {"limit": True},
        {"limit": "5"},
        {"path": 1},
        {"unknown": True},
        {"respect_gitignore": "false"},
    ],
)
def test_search_rejects_invalid_arguments(tmp_path: Path, tool, extra):
    with pytest.raises(ValueError):
        invoke(tool, tmp_path, {"pattern": "example", **extra})


def test_glob_defaults_to_workspace_and_returns_nested_matches(tmp_path: Path):
    (tmp_path / "root.txt").write_text("root")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "nested.txt").write_text("nested")
    result = invoke(GlobTool(), tmp_path, {"pattern": "**/*.txt"})
    assert result.status == "ok"
    payload = result.body.as_payload()
    assert set(payload["matches"]) == {"root.txt", "src/nested.txt"}
    assert payload["path"] == "."
    assert payload["truncated"] is False


@pytest.mark.parametrize("external", [False, True])
def test_glob_requires_directory_search_path(tmp_path: Path, external: bool):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = (tmp_path if external else workspace) / "file.txt"
    target.write_text("text")
    with pytest.raises(ValueError, match="path must be a directory"):
        invoke(GlobTool(), workspace, {"pattern": "*", "path": str(target)})


def test_glob_external_directory_returns_absolute_matches(tmp_path: Path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    target = external / "file.txt"
    target.write_text("text")
    result = invoke(GlobTool(), workspace, {"pattern": "*", "path": str(external)})
    assert result.status == "ok"
    assert result.body.as_payload()["matches"] == [str(target)]


def test_grep_default_path_and_immutable_filters_work(tmp_path: Path):
    (tmp_path / "root.txt").write_text("needle\n")
    (tmp_path / "other.py").write_text("needle\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "skip.txt").write_text("needle\n")
    result = invoke(GrepTool(), tmp_path, {"pattern": "needle", "include": ["*.txt"], "exclude": ["src/*"]})
    assert result.status == "ok"
    payload = result.body.as_payload()
    assert [match["file"] for match in payload["matches"]] == ["root.txt"]
    assert payload["path"] == "."
    assert "path" not in GrepTool.definition.input_schema["required"]


@pytest.mark.parametrize("tool,pattern", [(GlobTool(), "*.txt"), (GrepTool(), "needle")])
def test_search_limit_reports_an_incomplete_page(tmp_path: Path, tool, pattern):
    for name in ("first.txt", "second.txt"):
        (tmp_path / name).write_text("needle\n")
    result = invoke(tool, tmp_path, {"pattern": pattern, "limit": 1})
    assert result.status == "ok"
    assert result.output.bounds.truncated is True
    assert result.output.bounds.partial is True
