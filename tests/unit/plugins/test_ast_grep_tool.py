"""Native AST results must be complete before a rewrite is allowed."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError

from voidcode.plugins.ast_grep.ast_grep_tool import AstGrepTool
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import ArtifactReference, PluginServices, ProcessResult
from voidcode.sdk.tools import ToolCall, ToolSuccess


class NativeResults(PluginServices):
    def __init__(self, result: ProcessResult, target: Path) -> None:
        self.result = result
        self.target = target
        self.commands: list[Sequence[str]] = []
        self.artifacts: list[str] = []

    def run_process(self, argv: Sequence[str], **kwargs: object) -> ProcessResult:
        self.commands.append(argv)
        if "-U" in argv:
            self.target.write_text("bar()\n", encoding="utf-8")
            return ProcessResult(0, "", "", 1)
        return self.result

    def spill_artifact(self, data: bytes | str, *, media_type: str = "text/plain; charset=utf-8") -> ArtifactReference:
        assert isinstance(data, str)
        self.artifacts.append(data)
        raw = data.encode()
        return ArtifactReference(hashlib.sha256(raw).hexdigest(), len(raw), media_type)


def _invoke(tmp_path: Path, services: NativeResults, *, mode: str = "replace", **extra: object):
    target = services.target
    context = ToolContext(
        workspace=tmp_path,
        session_id="session",
        run_id="run",
        invocation_id="invocation",
        revision_id="revision",
        actor="model",
    )
    arguments = {"mode": mode, "path": target.name, "pattern": "foo()", "rewrite": "bar()", "apply": True, **extra}
    return AstGrepTool().invoke(ToolCall(tool_name="ast_grep", arguments=arguments, tool_call_id="ast-call"), context=context, services=services)


def _services(tmp_path: Path, **flags: bool) -> NativeResults:
    target = tmp_path / "sample.py"
    target.write_text("foo()\n", encoding="utf-8")
    match = {
        "file": target.name,
        "text": "foo()",
        "replacement": "bar()",
        "range": {"start": {"line": 0, "column": 0}, "end": {"line": 0, "column": 5}},
    }
    return NativeResults(ProcessResult(0, json.dumps(match) + "\n", "", 1, **flags), target)


def test_replace_without_read_history_reports_actual_changes(tmp_path: Path) -> None:
    services = _services(tmp_path)
    result = _invoke(tmp_path, services)
    assert isinstance(result, ToolSuccess)
    assert services.target.read_text() == "bar()\n"
    assert len(services.commands) == 2
    assert [change.path for change in result.changes] == ["sample.py"]
    assert len(services.artifacts) == 1


@pytest.mark.parametrize("flag, message", [("truncated", "capture limit"), ("timed_out", "timed out"), ("cancelled", "cancelled")])
def test_incomplete_native_preview_never_applies_changes(tmp_path: Path, flag: str, message: str) -> None:
    services = _services(tmp_path, **{flag: True})
    with pytest.raises(ValueError, match=message):
        _invoke(tmp_path, services)
    assert services.target.read_text() == "foo()\n"
    assert len(services.commands) == 1
    assert services.artifacts == []


@pytest.mark.parametrize("stdout", ["not JSON", "[]", "42"])
def test_malformed_native_match_is_visible_before_any_change(tmp_path: Path, stdout: str) -> None:
    services = _services(tmp_path)
    services.result = ProcessResult(0, stdout, "", 1)
    with pytest.raises(ValueError, match="JSON"):
        _invoke(tmp_path, services)
    assert services.target.read_text() == "foo()\n"
    assert len(services.commands) == 1


def test_native_no_match_is_success_without_applying_changes(tmp_path: Path) -> None:
    services = _services(tmp_path)
    services.result = ProcessResult(1, "", "", 1)
    result = _invoke(tmp_path, services, mode="search")
    assert isinstance(result, ToolSuccess)
    assert "Found 0 AST match" in result.output.text
    assert len(services.commands) == 1


@pytest.mark.parametrize("extra", [{"apply": "false"}, {"pattern": " "}, {"unknown": True}, {"mode": "other"}])
def test_invalid_arguments_fail_before_executing_native_process(tmp_path: Path, extra: dict[str, object]) -> None:
    services = _services(tmp_path)
    with pytest.raises(ValidationError):
        _invoke(tmp_path, services, **extra)
    assert services.commands == []


def test_replace_rejects_explicit_stale_hash_before_rewrite(tmp_path: Path) -> None:
    services = _services(tmp_path)
    with pytest.raises(ValueError, match="stale edit"):
        _invoke(tmp_path, services, expectedHash="0" * 64)
    assert services.target.read_text() == "foo()\n"
    assert len(services.commands) == 1
