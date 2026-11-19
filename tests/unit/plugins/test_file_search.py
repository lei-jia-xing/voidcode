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
    assert "[TRUNCATED]" not in result.output.text


@pytest.mark.parametrize("failure", ["walk", "read"])
def test_grep_reports_io_failure_instead_of_empty_search(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    from voidcode.capabilities.files.errors import ToolDiagnosticError

    target = tmp_path / "input.txt"
    target.write_text("needle\n", encoding="utf-8")
    if failure == "walk":

        def failed_walk(self, *, on_error, **kwargs):
            on_error(PermissionError("unreadable directory"))
            return iter(())

        monkeypatch.setattr(Path, "walk", failed_walk)
    else:
        original_open = Path.open

        def failed_open(self, *args, **kwargs):
            if self == target:
                raise PermissionError("unreadable file")
            return original_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", failed_open)
    with pytest.raises(ToolDiagnosticError, match="grep search failed") as caught:
        invoke(GrepTool(), tmp_path, {"pattern": "needle"})
    assert caught.value.error_details["reason"] == "search_io_error"


@pytest.mark.parametrize("pattern,expected", [("*.py", {"a.py"}), ("**/*.py", {"a.py", "sub/b.py"}), ("sub/*.py", {"sub/b.py"})])
def test_glob_standard_path_patterns_and_ignored_directories(tmp_path: Path, pattern: str, expected: set[str]) -> None:
    for name in ["a.py", "sub/b.py", "node_modules/ignored.py", ".git/ignored.py"]:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("content", encoding="utf-8")
    result = invoke(GlobTool(), tmp_path, {"pattern": pattern})
    assert set(result.body.matches) == expected


def test_glob_reports_directory_scan_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from voidcode.sdk.tools import ToolFailure

    def failed_walk(self, *, on_error, **kwargs):
        on_error(PermissionError("unreadable directory"))
        return iter(())

    monkeypatch.setattr(Path, "walk", failed_walk)
    result = invoke(GlobTool(), tmp_path, {"pattern": "**/*.py"})
    assert isinstance(result, ToolFailure)
    assert "unreadable directory" in result.error
    assert result.output.bounds.partial is True


@pytest.mark.parametrize("pattern", ["../*.py", "/tmp/*.py"])
def test_glob_requires_patterns_relative_to_search_directory(tmp_path: Path, pattern: str) -> None:
    with pytest.raises(ValueError, match="must be relative"):
        invoke(GlobTool(), tmp_path, {"pattern": pattern})


def test_grep_and_glob_do_not_prematurely_truncate_on_output_bytes(tmp_path: Path) -> None:
    long_line = "needle " + ("x" * 1000)
    lines = [f"{long_line} line {i}" for i in range(80)]
    (tmp_path / "large.txt").write_text("\n".join(lines), encoding="utf-8")

    grep_result = invoke(GrepTool(), tmp_path, {"pattern": "needle", "limit": 80})
    assert grep_result.status == "ok"
    assert len(grep_result.body.matches) == 80
    assert grep_result.output.bounds.truncated is False
    assert grep_result.body.truncated is False
    assert "[TRUNCATED]" not in grep_result.output.text

    glob_dir = tmp_path / "glob_dir"
    glob_dir.mkdir()
    nested_dir = glob_dir / ("d1_" + "a" * 100) / ("d2_" + "b" * 100) / ("d3_" + "c" * 100) / ("d4_" + "d" * 100)
    nested_dir.mkdir(parents=True)
    for i in range(100):
        (nested_dir / f"file_{i:03d}.txt").write_text("data")

    glob_result = invoke(GlobTool(), glob_dir, {"pattern": "**/*.txt", "limit": 100})
    assert glob_result.status == "ok"
    assert len(glob_result.body.matches) == 100
    assert glob_result.output.bounds.truncated is False
    assert glob_result.body.truncated is False

    # When limit is smaller than total results, both truncate at limit:
    grep_limited = invoke(GrepTool(), tmp_path, {"pattern": "needle", "limit": 79})
    assert grep_limited.status == "ok"
    assert len(grep_limited.body.matches) == 79
    assert grep_limited.output.bounds.truncated is True
    assert grep_limited.body.truncated is True
    assert "[TRUNCATED]" not in grep_limited.output.text

    glob_limited = invoke(GlobTool(), glob_dir, {"pattern": "**/*.txt", "limit": 99})
    assert glob_limited.status == "ok"
    assert len(glob_limited.body.matches) == 99
    assert glob_limited.output.bounds.truncated is True
    assert glob_limited.body.truncated is True
    assert "[TRUNCATED]" not in glob_limited.output.text
