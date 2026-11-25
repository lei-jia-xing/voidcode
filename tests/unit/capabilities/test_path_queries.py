"""Workspace path-completion contract and files provider behavior."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast

import pytest

from voidcode.capabilities.files import path_queries
from voidcode.capabilities.files.path_queries import (
    MAX_PATH_COMPLETION_LIMIT,
    PATH_COMPLETION,
    PathCompletionRequest,
    PathCompletionResult,
    complete_workspace_paths,
    create_path_queries,
)
from voidcode.sdk.plugin import PluginQueryCaller
from voidcode.sdk.services import QueryContractError, QueryUnavailable
from voidcode.transport.projection import core_error_status


def _paths(root: Path, request: PathCompletionRequest) -> list[str]:
    result = complete_workspace_paths(request, PluginQueryCaller(session_id="s", workspace=root))
    return [entry.path for entry in result.entries]


def test_prefix_completion_is_workspace_relative_and_deterministic(tmp_path: Path) -> None:
    (tmp_path / "src" / "nested").mkdir(parents=True)
    (tmp_path / "src" / "main.py").write_text("x", encoding="utf-8")
    (tmp_path / "storage").mkdir()
    (tmp_path / "README.md").write_text("x", encoding="utf-8")

    assert _paths(tmp_path, PathCompletionRequest(query="sr")) == ["src/"]
    assert _paths(tmp_path, PathCompletionRequest(query="")) == ["src/", "storage/", "README.md"]
    assert _paths(tmp_path, PathCompletionRequest(query="src/")) == ["src/nested/", "src/main.py"]
    assert _paths(tmp_path, PathCompletionRequest(query="src/m")) == ["src/main.py"]
    assert _paths(tmp_path, PathCompletionRequest(query="missing")) == []


def test_fuzzy_completion_prefers_tight_ordered_subsequences(tmp_path: Path) -> None:
    (tmp_path / "rm.md").write_text("x", encoding="utf-8")
    (tmp_path / "readme.md").write_text("x", encoding="utf-8")
    (tmp_path / "xreadme.md").write_text("x", encoding="utf-8")
    candidates = _paths(tmp_path, PathCompletionRequest(query="rm", mode="fuzzy"))
    assert candidates == ["rm.md", "readme.md", "xreadme.md"]


def test_recursive_fuzzy_matches_the_path_below_the_base(tmp_path: Path) -> None:
    (tmp_path / "src" / "deep").mkdir(parents=True)
    (tmp_path / "src" / "deep" / "main.py").write_text("x", encoding="utf-8")
    (tmp_path / "src" / "other.py").write_text("x", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "readme.md").write_text("x", encoding="utf-8")

    assert _paths(tmp_path, PathCompletionRequest(query="", mode="fuzzy", depth=3)) == [
        "src/",
        "docs/",
        "src/deep/",
        "src/other.py",
        "docs/readme.md",
        "src/deep/main.py",
    ]
    assert _paths(tmp_path, PathCompletionRequest(query="srm", mode="fuzzy", depth=3)) == ["src/deep/main.py", "docs/readme.md"]
    assert _paths(tmp_path, PathCompletionRequest(query="src/dm", mode="fuzzy", depth=3)) == ["src/deep/main.py"]


def test_recursive_depth_and_limit_are_bounded(tmp_path: Path) -> None:
    (tmp_path / "src" / "deep" / "deeper").mkdir(parents=True)
    (tmp_path / "src" / "deep" / "needle.py").write_text("x", encoding="utf-8")
    (tmp_path / "src" / "deeper.py").write_text("x", encoding="utf-8")

    assert _paths(tmp_path, PathCompletionRequest(query="src/", depth=1)) == ["src/deep/", "src/deeper.py"]
    recursive = _paths(tmp_path, PathCompletionRequest(query="src/", depth=3))
    assert recursive == ["src/deep/", "src/deep/deeper/", "src/deep/needle.py", "src/deeper.py"]

    result = complete_workspace_paths(PathCompletionRequest(query="src/", depth=3, limit=2), PluginQueryCaller("s", tmp_path))
    assert [entry.path for entry in result.entries] == recursive[:2]
    assert result.truncated is True


def test_ignore_rules_and_hidden_entries(tmp_path: Path) -> None:
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.js").write_text("x", encoding="utf-8")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "out.txt").write_text("x", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("x", encoding="utf-8")
    (tmp_path / ".env").write_text("x", encoding="utf-8")
    (tmp_path / "visible.txt").write_text("x", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "ignored.log").write_text("x", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("secret.txt\nnested/ignored.log\n", encoding="utf-8")

    assert _paths(tmp_path, PathCompletionRequest(query="")) == ["nested/", "visible.txt"]
    assert _paths(tmp_path, PathCompletionRequest(query="", respect_gitignore=False)) == ["nested/", "secret.txt", "visible.txt"]
    assert _paths(tmp_path, PathCompletionRequest(query=".")) == [".env", ".gitignore"]
    assert _paths(tmp_path, PathCompletionRequest(query="", include_hidden=True)) == ["nested/", ".env", ".gitignore", "visible.txt"]
    assert _paths(tmp_path, PathCompletionRequest(query="node_modules/")) == []
    assert _paths(tmp_path, PathCompletionRequest(query="build/")) == []
    assert _paths(tmp_path, PathCompletionRequest(query="secret")) == []


def test_unicode_prefix_is_casefolded(tmp_path: Path) -> None:
    (tmp_path / "Ärger.txt").write_text("x", encoding="utf-8")
    (tmp_path / "ärger.md").write_text("x", encoding="utf-8")
    (tmp_path / "src").mkdir()
    assert _paths(tmp_path, PathCompletionRequest(query="är")) == ["ärger.md", "Ärger.txt"]
    assert _paths(tmp_path, PathCompletionRequest(query="ÄR")) == ["ärger.md", "Ärger.txt"]


def test_symlinks_outside_the_workspace_are_not_candidates(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leaked.txt").write_text("x", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inside_dir = workspace / "inside"
    inside_dir.mkdir()
    (inside_dir / "kept.txt").write_text("x", encoding="utf-8")
    os.symlink(outside, workspace / "escape-dir")
    os.symlink(outside / "leaked.txt", workspace / "escape-file")
    os.symlink(inside_dir, workspace / "inside-link")
    os.symlink(workspace / "missing", workspace / "broken-link")

    assert _paths(workspace, PathCompletionRequest(query="")) == ["inside-link/", "inside/", "broken-link"]
    assert _paths(workspace, PathCompletionRequest(query="escape")) == []
    assert _paths(workspace, PathCompletionRequest(query="inside-link/")) == ["inside-link/kept.txt"]
    assert _paths(workspace, PathCompletionRequest(query="../../")) == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"query": "x" * (path_queries.MAX_PATH_COMPLETION_QUERY_CHARS + 1)},
        {"query": "a\x00b"},
        {"limit": 0},
        {"limit": MAX_PATH_COMPLETION_LIMIT + 1},
        {"depth": 0},
        {"depth": path_queries.MAX_PATH_COMPLETION_DEPTH + 1},
        {"mode": "other"},
        {"include_hidden": 1},
        {"respect_gitignore": "yes"},
    ],
)
def test_request_bounds_reject_invalid_values(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        PathCompletionRequest(**kwargs)  # type: ignore[arg-type]


def test_request_payload_is_strict() -> None:
    request = PathCompletionRequest.from_payload({"query": "src", "depth": 2, "limit": 5})
    assert request == PathCompletionRequest(query="src", depth=2, limit=5)
    for payload in (
        {"query": True},
        {"depth": True},
        {"limit": 1.5},
        {"extra": 1},
        {"mode": None},
    ):
        with pytest.raises(ValueError):
            PathCompletionRequest.from_payload(cast(dict[str, object], payload))


def test_result_bytes_are_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for index in range(10):
        (tmp_path / f"file-{index:02d}-{'x' * 40}.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(path_queries, "MAX_PATH_COMPLETION_RESULT_BYTES", 200)
    result = complete_workspace_paths(PathCompletionRequest(query="file", limit=10), PluginQueryCaller("s", tmp_path))
    assert 0 < len(result.entries) < 10
    assert result.truncated is True
    assert result.as_payload()["query"] == "file"


class _Transport:
    def __init__(self, answer: object) -> None:
        self.answer = answer

    def query(self, name: str, arguments: object) -> Any:
        assert name == PATH_COMPLETION.name
        return self.answer


def test_contract_requires_correlated_well_formed_answer() -> None:
    request = PathCompletionRequest(query="sr")
    valid: dict[str, object] = {"query": "sr", "entries": [{"path": "src/", "kind": "directory"}], "truncated": False}
    assert PATH_COMPLETION.ask(cast(Any, _Transport(valid)), request) == PathCompletionResult(
        query="sr", entries=(path_queries.PathCompletionEntry(path="src/", kind="directory"),), truncated=False
    )
    for answer in (
        None,
        [],
        {"query": "other", "entries": [], "truncated": False},
        {"query": "sr", "entries": [{"path": "/etc/passwd", "kind": "file"}], "truncated": False},
        {"query": "sr", "entries": [{"path": "src", "kind": "directory"}], "truncated": False},
        {"query": "sr", "entries": [{"path": "src/", "kind": "directory"}], "truncated": "no"},
    ):
        with pytest.raises(QueryContractError):
            PATH_COMPLETION.ask(cast(Any, _Transport(answer)), request)


def test_bound_handler_rejects_invalid_arguments_before_running(tmp_path: Path) -> None:
    bound = PATH_COMPLETION.bind(lambda request, caller: PathCompletionResult(query=request.query, entries=(), truncated=False))
    assert bound.name == PATH_COMPLETION.name
    with pytest.raises(ValueError):
        bound.handler({"query": True}, PluginQueryCaller("s", tmp_path))
    assert [query.name for query in create_path_queries()] == [PATH_COMPLETION.name]


def test_query_failure_sources_map_to_stable_transport_codes() -> None:
    assert core_error_status(QueryUnavailable("no provider")) == (404, "query_unavailable")
    assert core_error_status(QueryContractError("malformed")) == (400, "query_contract")
