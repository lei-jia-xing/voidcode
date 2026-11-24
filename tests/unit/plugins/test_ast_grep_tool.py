"""Native AST results must be complete before a rewrite is allowed."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError

from voidcode.plugins.ast_grep.ast_grep_tool import AstGrepReplaceTool, AstGrepTool
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import ArtifactReference, PluginServices, ProcessResult
from voidcode.sdk.tools import ToolCall, ToolEffect, ToolResult, ToolSuccess


class NativeResults(PluginServices):
    def __init__(self, result: ProcessResult, target: Path) -> None:
        self.result = result
        self.target = target
        self.commands: list[Sequence[str]] = []
        self.query_commands: list[Sequence[str]] = []
        self.artifacts: list[str] = []

    def run_process(self, argv: Sequence[str], **kwargs: object) -> ProcessResult:
        self.commands.append(argv)
        if "-U" in argv:
            self.target.write_text("bar()\n", encoding="utf-8")
            return ProcessResult(0, "", "", 1)
        return self.result

    def run_query_process(self, argv: Sequence[str], **kwargs: object) -> ProcessResult:
        self.query_commands.append(argv)
        return self.result

    def spill_artifact(self, data: bytes | str, *, media_type: str = "text/plain; charset=utf-8") -> ArtifactReference:
        assert isinstance(data, str)
        self.artifacts.append(data)
        raw = data.encode()
        return ArtifactReference(hashlib.sha256(raw).hexdigest(), len(raw), media_type)


class ReadOnlyLeaseServices(NativeResults):
    """Simulates a read-only lease where run_process is refused but run_query_process is permitted."""

    def run_process(self, argv: Sequence[str], **kwargs: object) -> ProcessResult:
        raise PermissionError("this lease may not run processes: read-only and hook leases do not touch the machine")


def _invoke_replace(tmp_path: Path, services: NativeResults, **extra: object) -> ToolResult:
    target = services.target
    context = ToolContext(
        workspace=tmp_path,
        session_id="session",
        run_id="run",
        invocation_id="invocation",
        revision_id="revision",
        actor="model",
    )
    arguments = {"path": target.name, "pattern": "foo()", "rewrite": "bar()", "apply": True, **extra}
    return AstGrepReplaceTool().invoke(
        ToolCall(tool_name="ast_grep_replace", arguments=arguments, tool_call_id="ast-call"),
        context=context,
        services=services,
    )


def _invoke_query(tmp_path: Path, services: NativeResults, *, mode: str = "search", **extra: object) -> ToolResult:
    target = services.target
    context = ToolContext(
        workspace=tmp_path,
        session_id="session",
        run_id="run",
        invocation_id="invocation",
        revision_id="revision",
        actor="model",
    )
    arguments = {"mode": mode, "path": target.name, "pattern": "foo()", **extra}
    return AstGrepTool().invoke(
        ToolCall(tool_name="ast_grep", arguments=arguments, tool_call_id="ast-call"),
        context=context,
        services=services,
    )


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
    result = _invoke_replace(tmp_path, services)
    assert isinstance(result, ToolSuccess)
    assert services.target.read_text() == "bar()\n"
    assert len(services.commands) == 2
    assert len(services.query_commands) == 0
    assert [change.path for change in result.changes] == ["sample.py"]
    assert len(services.artifacts) == 1


@pytest.mark.parametrize("flag, message", [("truncated", "capture limit"), ("timed_out", "timed out"), ("cancelled", "cancelled")])
def test_incomplete_native_preview_never_applies_changes(tmp_path: Path, flag: str, message: str) -> None:
    services = _services(tmp_path, **{flag: True})
    with pytest.raises(ValueError, match=message):
        _invoke_replace(tmp_path, services)
    assert services.target.read_text() == "foo()\n"
    assert len(services.commands) == 1
    assert services.artifacts == []


@pytest.mark.parametrize("stdout", ["not JSON", "[]", "42"])
def test_malformed_native_match_is_visible_before_any_change(tmp_path: Path, stdout: str) -> None:
    services = _services(tmp_path)
    services.result = ProcessResult(0, stdout, "", 1)
    with pytest.raises(ValueError, match="JSON"):
        _invoke_replace(tmp_path, services)
    assert services.target.read_text() == "foo()\n"
    assert len(services.commands) == 1


def test_native_no_match_is_success_without_applying_changes(tmp_path: Path) -> None:
    services = _services(tmp_path)
    services.result = ProcessResult(1, "", "", 1)
    result = _invoke_query(tmp_path, services, mode="search")
    assert isinstance(result, ToolSuccess)
    assert "Found 0 AST match" in result.output.text
    assert len(services.query_commands) == 1
    assert len(services.commands) == 0


@pytest.mark.parametrize(
    "tool_cls, extra",
    [
        (AstGrepTool, {"mode": "replace"}),
        (AstGrepTool, {"mode": "other"}),
        (AstGrepTool, {"pattern": " "}),
        (AstGrepTool, {"unknown": True}),
        (AstGrepReplaceTool, {"apply": "false"}),
        (AstGrepReplaceTool, {"pattern": " "}),
        (AstGrepReplaceTool, {"unknown": True}),
    ],
)
def test_invalid_arguments_fail_before_executing_native_process(tmp_path: Path, tool_cls: type, extra: dict[str, object]) -> None:
    services = _services(tmp_path)
    target = services.target
    context = ToolContext(
        workspace=tmp_path,
        session_id="session",
        run_id="run",
        invocation_id="invocation",
        revision_id="revision",
        actor="model",
    )
    if tool_cls is AstGrepTool:
        arguments = {"mode": "search", "path": target.name, "pattern": "foo()", **extra}
    else:
        arguments = {"path": target.name, "pattern": "foo()", "rewrite": "bar()", "apply": True, **extra}
    with pytest.raises(ValidationError):
        tool_cls().invoke(
            ToolCall(tool_name="test", arguments=arguments, tool_call_id="call-1"),
            context=context,
            services=services,
        )
    assert services.commands == []
    assert services.query_commands == []


def test_replace_rejects_explicit_stale_hash_before_rewrite(tmp_path: Path) -> None:
    services = _services(tmp_path)
    with pytest.raises(ValueError, match="stale edit"):
        _invoke_replace(tmp_path, services, expectedHash="0" * 64)
    assert services.target.read_text() == "foo()\n"
    assert len(services.commands) == 1


def test_ast_grep_search_and_preview_run_under_readonly_lease(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("foo()\n", encoding="utf-8")
    match = {
        "file": target.name,
        "text": "foo()",
        "replacement": "bar()",
        "range": {"start": {"line": 0, "column": 0}, "end": {"line": 0, "column": 5}},
    }
    services = ReadOnlyLeaseServices(ProcessResult(0, json.dumps(match) + "\n", "", 1), target)

    search_res = _invoke_query(tmp_path, services, mode="search")
    assert isinstance(search_res, ToolSuccess)
    assert "Found 1 AST match" in search_res.output.text
    assert len(services.query_commands) == 1
    assert len(services.commands) == 0

    preview_res = _invoke_query(tmp_path, services, mode="preview", rewrite="bar()")
    assert isinstance(preview_res, ToolSuccess)
    assert "Previewed 1 AST replacement" in preview_res.output.text
    assert len(services.query_commands) == 2
    assert len(services.commands) == 0


def test_ast_grep_replace_has_write_effect_and_mutates_with_preflight_hash(tmp_path: Path) -> None:
    assert AstGrepReplaceTool.definition.effects == frozenset({ToolEffect.WRITE})
    assert AstGrepTool.definition.effects == frozenset({ToolEffect.READ})

    services = _services(tmp_path)
    correct_hash = hashlib.sha256(services.target.read_bytes()).hexdigest()

    res = _invoke_replace(tmp_path, services, expectedHash=correct_hash)
    assert isinstance(res, ToolSuccess)
    assert services.target.read_text() == "bar()\n"
    assert [change.path for change in res.changes] == ["sample.py"]
    assert len(services.commands) == 2


def test_ast_grep_replace_fails_under_readonly_lease(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("foo()\n", encoding="utf-8")
    services = ReadOnlyLeaseServices(ProcessResult(0, "", "", 1), target)

    with pytest.raises(PermissionError, match="read-only and hook leases do not touch the machine"):
        _invoke_replace(tmp_path, services)
    assert target.read_text() == "foo()\n"


def test_ast_grep_replace_requires_apply_true(tmp_path: Path) -> None:
    services = _services(tmp_path)
    with pytest.raises(ValueError, match="apply=True"):
        _invoke_replace(tmp_path, services, apply=False)


def test_ast_grep_replace_rejects_concurrent_mutation_after_preview(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    services = _services(tmp_path)
    real_read_bytes = Path.read_bytes
    reads = 0

    def mock_read_bytes(self: Path) -> bytes:
        nonlocal reads
        reads += 1
        data = real_read_bytes(self)
        if self == services.target and reads > 1:
            return data + b"# mutated\n"
        return data

    monkeypatch.setattr(Path, "read_bytes", mock_read_bytes)
    with pytest.raises(ValueError, match="stale edit"):
        _invoke_replace(tmp_path, services)
    assert len(services.commands) == 1


def test_ast_grep_returns_usable_long_paths_and_complete_native_matches(tmp_path: Path) -> None:
    target = tmp_path.joinpath(*("nested_component_" + str(index) for index in range(18)), "sample.py")
    target.parent.mkdir(parents=True)
    target.write_text("foo()\n" * 60, encoding="utf-8")
    relative_path = target.relative_to(tmp_path).as_posix()
    assert len(relative_path) > 256
    matches = [
        {
            "file": relative_path,
            "text": "foo()",
            "range": {"start": {"line": index, "column": 0}, "end": {"line": index, "column": 5}},
        }
        for index in range(60)
    ]
    native_jsonl = "\n".join(json.dumps(match, ensure_ascii=False) for match in matches)
    services = NativeResults(ProcessResult(0, native_jsonl, "", 1), target)

    result = _invoke_query(tmp_path, services, path=relative_path)
    assert isinstance(result, ToolSuccess)
    payload = result.body.as_payload()
    assert payload["path"] == relative_path
    assert payload["matches"][0]["file"] == relative_path
    assert (tmp_path / payload["matches"][0]["file"]).read_text(encoding="utf-8") == "foo()\n" * 60
    assert payload["match_count"] == len(matches)
    assert payload["matches_truncated"] is False
    assert result.output.bounds.partial is False
    assert result.output.bounds.reference.sha256 == hashlib.sha256(native_jsonl.encode("utf-8")).hexdigest()
    assert [json.loads(line) for line in services.artifacts[0].splitlines()] == matches

    # Oversized result packs as many matches as fit in the transport byte budget without silent fixed count caps.
    many_matches = [
        {
            "file": relative_path,
            "text": "foo()",
            "range": {"start": {"line": index, "column": 0}, "end": {"line": index, "column": 5}},
        }
        for index in range(120)
    ]
    oversized_jsonl = "\n".join(json.dumps(match, ensure_ascii=False) for match in many_matches)
    oversized_services = NativeResults(ProcessResult(0, oversized_jsonl, "", 1), target)
    oversized = _invoke_query(tmp_path, oversized_services, path=relative_path)
    assert isinstance(oversized, ToolSuccess)
    oversized_payload = oversized.body.as_payload()
    assert oversized_payload["match_count"] == len(many_matches)
    assert 60 <= len(oversized_payload["matches"]) < len(many_matches)
    assert oversized_payload["matches_truncated"] is True
    assert oversized.output.bounds.partial is True
    assert oversized.output.bounds.reference.sha256 == hashlib.sha256(oversized_jsonl.encode("utf-8")).hexdigest()
    assert [json.loads(line) for line in oversized_services.artifacts[0].splitlines()] == many_matches
