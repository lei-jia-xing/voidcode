"""Contract tests for ast_grep search/preview and single-file replace."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from voidcode.plugins.ast_grep.ast_grep_tool import AstGrepReplaceTool, AstGrepTool
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import ArtifactReference, PluginServices, ProcessResult
from voidcode.sdk.tools import TOOL_OUTPUT_BYTES, ConfirmedStop, ToolCall, ToolFailure, ToolResult, ToolSuccess

BINARY = shutil.which("ast-grep")


class NativeResults(PluginServices):
    def __init__(self, result: ProcessResult, target: Path) -> None:
        self.result = result
        self.apply_result: ProcessResult | None = None
        self.apply_writes: str | None = None
        self.preview_mutates_to: str | None = None
        self.target = target
        self.commands: list[Sequence[str]] = []
        self.query_commands: list[Sequence[str]] = []
        self.run_kwargs: list[dict[str, object]] = []
        self.query_kwargs: list[dict[str, object]] = []
        self.artifacts: list[str] = []
        self.outline_language = "Python"

    def _outline(self, argv: Sequence[str]) -> ProcessResult | None:
        if "outline" not in argv:
            return None
        payload = [{"path": self.target.name, "language": self.outline_language, "items": []}]
        return ProcessResult(0, json.dumps(payload) + "\n", "", 1)

    def run_process(self, argv: Sequence[str], **kwargs: object) -> ProcessResult:
        self.commands.append(argv)
        self.run_kwargs.append(kwargs)
        outline = self._outline(argv)
        if outline is not None:
            return outline
        if "-U" in argv:
            if self.apply_result is not None:
                if self.apply_writes is not None:
                    self.target.write_text(self.apply_writes, encoding="utf-8")
                return self.apply_result
            self.target.write_text("bar()\n", encoding="utf-8")
            return ProcessResult(0, "Applied 1 changes\n", "", 1)
        if self.preview_mutates_to is not None:
            self.target.write_text(self.preview_mutates_to, encoding="utf-8")
        return self.result

    def run_query_process(self, argv: Sequence[str], **kwargs: object) -> ProcessResult:
        self.query_commands.append(argv)
        self.query_kwargs.append(kwargs)
        outline = self._outline(argv)
        return outline if outline is not None else self.result

    def spill_artifact(self, data: bytes | str, *, media_type: str = "text/plain; charset=utf-8") -> ArtifactReference:
        assert isinstance(data, str)
        self.artifacts.append(data)
        raw = data.encode()
        return ArtifactReference(hashlib.sha256(raw).hexdigest(), len(raw), media_type)


class ReadOnlyLeaseServices(NativeResults):
    """Simulates a read-only lease where run_process is refused but run_query_process is permitted."""

    def run_process(self, argv: Sequence[str], **kwargs: object) -> ProcessResult:
        raise PermissionError("this lease may not run processes: read-only and hook leases do not touch the machine")


class RealAstGrepServices(PluginServices):
    """Real ast-grep binary through a plain subprocess, with artifact spill recorded in memory."""

    def __init__(self) -> None:
        self.commands: list[Sequence[str]] = []
        self.artifacts: list[str] = []

    def _run(self, argv: Sequence[str], **kwargs: object) -> ProcessResult:
        self.commands.append(argv)
        started = time.monotonic()
        cwd = kwargs.get("cwd")
        timeout = kwargs.get("timeout_seconds")
        completed = subprocess.run(
            list(argv),
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=float(timeout) if isinstance(timeout, (int, float)) else None,
            check=False,
        )
        return ProcessResult(
            completed.returncode,
            completed.stdout,
            completed.stderr,
            int((time.monotonic() - started) * 1000),
        )

    def run_process(self, argv: Sequence[str], **kwargs: object) -> ProcessResult:
        return self._run(argv, **kwargs)

    def run_query_process(self, argv: Sequence[str], **kwargs: object) -> ProcessResult:
        return self._run(argv, **kwargs)

    def spill_artifact(self, data: bytes | str, *, media_type: str = "text/plain; charset=utf-8") -> ArtifactReference:
        text = data if isinstance(data, str) else data.decode("utf-8", errors="replace")
        self.artifacts.append(text)
        raw = text.encode()
        return ArtifactReference(hashlib.sha256(raw).hexdigest(), len(raw), media_type)


def _context(tmp_path: Path) -> ToolContext:
    return ToolContext(
        workspace=tmp_path,
        session_id="session",
        run_id="run",
        invocation_id="invocation",
        revision_id="revision",
        actor="model",
    )


def _invoke_search(tmp_path: Path, services: PluginServices, *, mode: str = "search", **extra: object) -> ToolResult:
    arguments = {"mode": mode, "path": "sample.py", "pattern": "foo()", "lang": "python", **extra}
    return AstGrepTool().invoke(
        ToolCall(tool_name="ast_grep", arguments=arguments, tool_call_id="ast-call"),
        context=_context(tmp_path),
        services=services,
    )


def _invoke_replace(tmp_path: Path, services: PluginServices, *, target: Path | None = None, **extra: object) -> ToolResult:
    chosen = target or Path(tmp_path / "sample.py")
    path = chosen.name
    raw = chosen.read_bytes() if chosen.is_file() else b""
    arguments = {
        "path": path,
        "pattern": "foo()",
        "rewrite": "bar()",
        "apply": True,
        "lang": "python",
        "expectedHash": hashlib.sha256(raw).hexdigest(),
        **extra,
    }
    return AstGrepReplaceTool().invoke(
        ToolCall(tool_name="ast_grep_replace", arguments=arguments, tool_call_id="ast-call"),
        context=_context(tmp_path),
        services=services,
    )


def _services(tmp_path: Path, result: ProcessResult | None = None) -> NativeResults:
    target = tmp_path / "sample.py"
    target.write_text("foo()\n", encoding="utf-8")
    match = {
        "file": target.name,
        "text": "foo()",
        "replacement": "bar()",
        "range": {"start": {"line": 0, "column": 0}, "end": {"line": 0, "column": 5}},
    }
    return NativeResults(result or ProcessResult(0, json.dumps(match) + "\n", "", 1), target)


def _failure(result: ToolResult) -> ToolFailure:
    assert isinstance(result, ToolFailure), result
    return result


def _success(result: ToolResult) -> ToolSuccess:
    assert isinstance(result, ToolSuccess), result
    return result


def _detail(result: ToolFailure, key: str) -> object:
    assert result.diagnostics is not None
    return result.diagnostics.details.get(key)


def _payload(result: ToolSuccess) -> dict[str, object]:
    assert result.body is not None
    return result.body.as_payload()


# ── search / preview ─────────────────────────────────────────────────────────


def test_search_returns_matches_and_full_artifact(tmp_path: Path) -> None:
    services = _services(tmp_path)
    result = _success(_invoke_search(tmp_path, services))
    assert "Found 1 AST match" in result.output.text
    assert result.output.bounds.reference is not None
    payload = _payload(result)
    assert payload["match_count"] == 1
    assert payload["matches_truncated"] is False
    assert services.artifacts == [services.result.stdout]
    assert len(services.query_commands) == 1
    assert services.commands == []


def test_zero_matches_is_success_without_an_artifact(tmp_path: Path) -> None:
    services = _services(tmp_path, ProcessResult(1, "", "", 1))
    result = _success(_invoke_search(tmp_path, services))
    assert "Found 0 AST match" in result.output.text
    assert services.artifacts == []
    assert result.output.bounds.reference is None


def test_directory_search_and_preview_require_lang(tmp_path: Path) -> None:
    services = _services(tmp_path)
    for mode in ("search", "preview"):
        result = _failure(_invoke_search(tmp_path, services, mode=mode, path=".", lang=None, rewrite="bar()"))
        assert result.diagnostics is not None
        assert result.diagnostics.kind == "tool_input_mismatch"
        assert _detail(result, "reason") == "invalid_arguments"
        assert "requires lang" in result.error
    assert services.query_commands == []


def test_file_target_infers_language_before_search(tmp_path: Path) -> None:
    services = _services(tmp_path)
    result = _success(_invoke_search(tmp_path, services, lang=None))
    assert "Found 1 AST match" in result.output.text
    assert tuple(services.query_commands[0][:2]) == ("ast-grep", "outline")
    assert tuple(services.query_commands[1][:2]) == ("ast-grep", "run")
    assert "--lang" in services.query_commands[1]
    assert "Python" in services.query_commands[1]
    assert _payload(result)["lang"] == "Python"


def test_uninferable_language_is_a_typed_error(tmp_path: Path) -> None:
    services = _services(tmp_path)
    services.outline_language = ""
    result = _failure(_invoke_search(tmp_path, services, lang=None))
    assert result.diagnostics is not None
    assert result.diagnostics.kind == "tool_input_mismatch"
    assert "pass lang" in result.error


def test_malformed_pattern_warning_is_visible_with_zero_matches(tmp_path: Path) -> None:
    stderr = (
        "Warning: Pattern contains an ERROR node and may cause unexpected results.\n"
        "Help: ast-grep parsed the pattern but it matched nothing in this run.\n"
    )
    services = _services(tmp_path, ProcessResult(0, "", stderr, 1))
    for mode in ("search", "preview"):
        result = _success(_invoke_search(tmp_path, services, mode=mode, rewrite="bar()"))
        assert "Warnings:" in result.output.text
        assert "Pattern contains an ERROR node" in result.output.text
        assert _payload(result)["warnings"] == [
            "Warning: Pattern contains an ERROR node and may cause unexpected results.",
            "Help: ast-grep parsed the pattern but it matched nothing in this run.",
        ]
        assert "0 AST" in result.output.text
    assert services.artifacts == []


def test_cli_argument_errors_are_classified_by_exit_code_not_stderr_text(tmp_path: Path) -> None:
    services = _services(tmp_path, ProcessResult(2, "", "unrecognized invocation shape\n", 1))
    result = _failure(_invoke_search(tmp_path, services))
    assert result.diagnostics is not None
    assert result.diagnostics.kind == "tool_input_mismatch"
    assert _detail(result, "reason") == "invalid_arguments"
    assert "rejected the search arguments" in result.error


def test_success_warnings_are_visible_in_output_and_body(tmp_path: Path) -> None:
    services = _services(tmp_path, ProcessResult(0, services_stdout(tmp_path), "Warning: check this\n", 1))
    result = _success(_invoke_search(tmp_path, services))
    assert "Warnings:" in result.output.text
    assert "Warning: check this" in result.output.text
    assert _payload(result)["warnings"] == ["Warning: check this"]


def services_stdout(tmp_path: Path) -> str:
    match = {
        "file": "sample.py",
        "text": "foo()",
        "range": {"start": {"line": 0, "column": 0}, "end": {"line": 0, "column": 5}},
    }
    return json.dumps(match) + "\n"


def test_search_flags_match_grep_defaults_and_include_exclude(tmp_path: Path) -> None:
    services = _services(tmp_path, ProcessResult(1, "", "", 1))
    _success(_invoke_search(tmp_path, services, path=".", include=["*.py"], exclude=["vendor/*"]))
    command = list(services.query_commands[-1])
    assert "--no-ignore" in command and "hidden" in command and "vcs" in command
    assert "--globs" in command
    assert "*.py" not in command and "!vendor/*" not in command
    assert "!**/node_modules/**" in command

    _success(_invoke_search(tmp_path, services, path=".", include_hidden=False, respect_gitignore=True))
    command = list(services.query_commands[-1])
    assert "hidden" not in command and "vcs" not in command
    assert "exclude" in command and "global" in command and "parent" in command


def test_search_filters_match_grep_semantics(tmp_path: Path) -> None:
    matches = [
        {
            "file": file,
            "text": "foo()",
            "range": {"start": {"line": 0, "column": 0}, "end": {"line": 0, "column": 5}},
        }
        for file in ("sample.py", "vendor/x.py", "node_modules/y.py", ".hidden/h.py")
    ]
    native = "\n".join(json.dumps(match) for match in matches)
    services = _services(tmp_path, ProcessResult(0, native, "", 1))
    result = _success(
        _invoke_search(
            tmp_path,
            services,
            path=".",
            include=["*.py"],
            exclude=["vendor/*"],
            include_hidden=False,
        )
    )
    payload = _payload(result)
    assert [match["file"] for match in payload["matches"]] == ["sample.py"]  # type: ignore[index]
    assert payload["match_count"] == 1
    assert "node_modules/y.py" not in services.artifacts[0]
    assert ".hidden/h.py" not in services.artifacts[0]


def test_search_explicit_default_ignored_root_is_still_searched(tmp_path: Path) -> None:
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    match = {
        "file": "vendor/x.py",
        "text": "foo()",
        "range": {"start": {"line": 0, "column": 0}, "end": {"line": 0, "column": 5}},
    }
    services = _services(tmp_path, ProcessResult(0, json.dumps(match) + "\n", "", 1))
    result = _success(_invoke_search(tmp_path, services, path="vendor"))
    assert _payload(result)["match_count"] == 1


def test_search_include_paths_use_workspace_relative_spelling(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    match = {
        "file": "src/keep.py",
        "text": "foo()",
        "range": {"start": {"line": 0, "column": 0}, "end": {"line": 0, "column": 5}},
    }
    services = _services(tmp_path, ProcessResult(0, json.dumps(match) + "\n", "", 1))
    result = _success(_invoke_search(tmp_path, services, path="src", include=["src/keep.py"]))
    assert _payload(result)["match_count"] == 1


def test_search_file_target_applies_include_exclude_without_globs(tmp_path: Path) -> None:
    services = _services(tmp_path)
    result = _success(_invoke_search(tmp_path, services, include=["*.js"]))
    assert "Found 0 AST match" in result.output.text
    assert services.query_commands == []
    result = _success(_invoke_search(tmp_path, services, exclude=["sample.py"]))
    assert "Found 0 AST match" in result.output.text
    assert services.query_commands == []


def test_search_limit_truncates_body_but_keeps_full_artifact(tmp_path: Path) -> None:
    matches = [
        {
            "file": "sample.py",
            "text": "foo()",
            "range": {"start": {"line": index, "column": 0}, "end": {"line": index, "column": 5}},
        }
        for index in range(5)
    ]
    native = "\n".join(json.dumps(match) for match in matches)
    services = _services(tmp_path, ProcessResult(0, native, "", 1))
    result = _success(_invoke_search(tmp_path, services, limit=2))
    payload = _payload(result)
    assert payload["match_count"] == 5
    assert len(payload["matches"]) == 2  # type: ignore[arg-type]
    assert payload["matches_truncated"] is True
    assert len(services.artifacts[0].splitlines()) == 5


def test_search_projection_stays_within_the_output_budget(tmp_path: Path) -> None:
    matches = [
        {
            "file": "sample.py",
            "text": "foo() " + ("x" * 500),
            "range": {"start": {"line": index, "column": 0}, "end": {"line": index, "column": 5}},
        }
        for index in range(300)
    ]
    native = "\n".join(json.dumps(match) for match in matches)
    services = _services(tmp_path, ProcessResult(0, native, "", 1))
    result = _success(_invoke_search(tmp_path, services))
    payload = _payload(result)
    assert payload["match_count"] == 300
    assert payload["matches_truncated"] is True
    assert len(result.output.text.encode("utf-8")) <= TOOL_OUTPUT_BYTES
    assert len(json.dumps(payload, separators=(",", ":")).encode("utf-8")) <= TOOL_OUTPUT_BYTES


@pytest.mark.parametrize("flag, label", [("timed_out", "timed out"), ("cancelled", "cancelled"), ("truncated", "capture limit")])
def test_incomplete_process_is_typed_partial_with_artifact(tmp_path: Path, flag: str, label: str) -> None:
    partial = json.dumps(
        {
            "file": "sample.py",
            "text": "foo()",
            "range": {"start": {"line": 0, "column": 0}, "end": {"line": 0, "column": 5}},
        }
    )
    services = _services(tmp_path, ProcessResult(None, partial + "\n", "", 1, **{flag: True}))
    result = _failure(_invoke_search(tmp_path, services))
    assert result.diagnostics is not None
    assert label in result.error
    if flag == "cancelled":
        assert result.execution == ConfirmedStop(cancellation_signalled=True)
        assert result.diagnostics.kind == "tool_cancelled"
    if flag == "timed_out":
        assert result.timeout_seconds == 30
    if flag == "truncated":
        assert _detail(result, "reason") == "process_capture_failed"
    assert result.output.bounds.reference is not None
    assert result.output.bounds.partial is True
    assert services.artifacts == [partial + "\n"]


def test_output_truncation_never_claims_complete_matches(tmp_path: Path) -> None:
    services = _services(tmp_path, ProcessResult(0, '{"file": "sample.py"}\npartial', "", 1, truncated=True))
    result = _failure(_invoke_search(tmp_path, services))
    assert result.diagnostics is not None
    assert result.diagnostics.kind == "tool_execution_error"
    assert "partial" in result.error
    assert result.output.bounds.reference is not None


def test_search_and_preview_run_under_readonly_lease(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("foo()\n", encoding="utf-8")
    services = ReadOnlyLeaseServices(ProcessResult(0, services_stdout(tmp_path), "", 1), target)
    assert isinstance(_invoke_search(tmp_path, services), ToolSuccess)
    assert isinstance(_invoke_search(tmp_path, services, mode="preview", rewrite="bar()"), ToolSuccess)
    assert len(services.query_commands) == 2
    assert services.commands == []


def test_missing_binary_is_a_typed_environment_error(tmp_path: Path) -> None:
    class MissingBinary(NativeResults):
        def run_query_process(self, argv: Sequence[str], **kwargs: object) -> ProcessResult:
            raise ValueError("failed to start process: [Errno 2] No such file or directory: 'ast-grep'")

    target = tmp_path / "sample.py"
    target.write_text("foo()\n", encoding="utf-8")
    services = MissingBinary(ProcessResult(0, "", "", 1), target)
    result = _failure(_invoke_search(tmp_path, services))
    assert result.diagnostics is not None
    assert result.diagnostics.kind == "tool_environment_error"
    assert _detail(result, "reason") == "process_start_failed"
    assert "Install ast-grep" in (result.diagnostics.guidance or "")


@pytest.mark.parametrize(
    "extra",
    [
        {"mode": "replace"},
        {"mode": "other"},
        {"pattern": " "},
        {"unknown": True},
        {"include": [" "]},
        {"limit": 0},
        {"limit": 5001},
    ],
)
def test_search_invalid_arguments_are_typed_validation_failures(tmp_path: Path, extra: dict[str, object]) -> None:
    services = _services(tmp_path)
    result = _failure(_invoke_search(tmp_path, services, **extra))
    assert result.diagnostics is not None
    assert result.diagnostics.kind == "tool_input_validation"
    assert services.query_commands == []
    assert services.commands == []


# ── replace ──────────────────────────────────────────────────────────────────


def test_replace_requires_a_valid_expected_hash(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("foo()\n", encoding="utf-8")
    context = _context(tmp_path)
    for missing in ({}, {"expectedHash": "xyz"}):
        arguments: dict[str, object] = {"path": "sample.py", "pattern": "foo()", "rewrite": "bar()", "apply": True, **missing}
        result = AstGrepReplaceTool().invoke(
            ToolCall(tool_name="ast_grep_replace", arguments=arguments, tool_call_id="ast-call"),
            context=context,
            services=NativeResults(ProcessResult(0, "", "", 1), target),
        )
        failure = _failure(result)
        assert failure.diagnostics is not None
        assert failure.diagnostics.kind == "tool_input_validation"
        assert "expectedHash" in failure.error


def test_replace_rejects_directory_and_external_and_symlink_targets(tmp_path: Path) -> None:
    services = _services(tmp_path)
    directory = _failure(_invoke_replace(tmp_path, services, target=tmp_path, path="."))
    assert directory.diagnostics is not None
    assert _detail(directory, "reason") == "not_a_regular_file"

    outside = tmp_path.parent / "outside-ast-grep.py"
    outside.write_text("foo()\n", encoding="utf-8")
    external = _failure(_invoke_replace(tmp_path, services, target=outside, path=str(outside)))
    assert external.diagnostics is not None
    assert _detail(external, "reason") == "outside_workspace"
    assert outside.read_text(encoding="utf-8") == "foo()\n"

    link = tmp_path / "link.py"
    link.symlink_to(services.target)
    linked = _failure(_invoke_replace(tmp_path, services, target=services.target, path="link.py"))
    assert linked.diagnostics is not None
    assert _detail(linked, "reason") == "symlink_target"
    assert services.commands == []


def test_replace_success_reports_hash_bound_change_and_diff(tmp_path: Path) -> None:
    services = _services(tmp_path)
    source_hash = hashlib.sha256(services.target.read_bytes()).hexdigest()
    result = _success(_invoke_replace(tmp_path, services))
    assert services.target.read_text(encoding="utf-8") == "bar()\n"
    payload = _payload(result)
    assert payload["source_hash"] == source_hash
    assert payload["final_hash"] == hashlib.sha256(services.target.read_bytes()).hexdigest()
    assert payload["byte_count"] == len(b"bar()\n")
    assert "diff" in payload
    assert len(result.changes) == 1
    assert result.changes[0].path == "sample.py"
    assert result.changes[0].content_hash == payload["final_hash"]
    preview, apply = services.commands
    assert "-U" in apply and apply.index("--") < apply.index("sample.py")
    assert "--json=stream" in preview


def test_replace_rejects_stale_expected_hash_before_running(tmp_path: Path) -> None:
    services = _services(tmp_path)
    result = _failure(_invoke_replace(tmp_path, services, expectedHash="0" * 64))
    assert result.diagnostics is not None
    assert result.diagnostics.kind == "stale_edit"
    assert _detail(result, "reason") == "content_hash_mismatch"
    assert services.commands == []
    assert services.target.read_text(encoding="utf-8") == "foo()\n"


def test_replace_rechecks_the_file_after_preview(tmp_path: Path) -> None:
    services = _services(tmp_path)
    services.preview_mutates_to = "foo()\n# concurrent\n"
    result = _failure(_invoke_replace(tmp_path, services))
    assert result.diagnostics is not None
    assert result.diagnostics.kind == "stale_edit"
    assert len(services.commands) == 1
    assert services.target.read_text(encoding="utf-8") == "foo()\n# concurrent\n"


def test_replace_rejects_a_preview_match_outside_the_single_target(tmp_path: Path) -> None:
    match = {
        "file": "other.py",
        "text": "foo()",
        "replacement": "bar()",
        "range": {"start": {"line": 0, "column": 0}, "end": {"line": 0, "column": 5}},
    }
    services = _services(tmp_path, ProcessResult(0, json.dumps(match) + "\n", "", 1))
    result = _failure(_invoke_replace(tmp_path, services))
    assert result.diagnostics is not None
    assert result.diagnostics.kind == "tool_execution_error"
    assert "escaped the single-file target" in result.error
    assert len(services.commands) == 1


def test_replace_no_matches_is_a_noop_without_artifact(tmp_path: Path) -> None:
    services = _services(tmp_path, ProcessResult(1, "", "", 1))
    result = _success(_invoke_replace(tmp_path, services))
    payload = _payload(result)
    assert payload["applied"] is False
    assert payload["replacement_count"] == 0
    assert services.artifacts == []
    assert result.changes == ()
    assert len(services.commands) == 1


def test_replace_apply_timeout_reports_possible_partial_prefix(tmp_path: Path) -> None:
    services = _services(tmp_path)
    services.apply_writes = "bar()\n"
    services.apply_result = ProcessResult(None, "", "", 1, timed_out=True)
    result = _failure(_invoke_replace(tmp_path, services))
    assert result.diagnostics is not None
    assert result.diagnostics.kind == "tool_execution_error"
    assert result.timeout_seconds == 30
    assert "may have been partially rewritten" in result.error
    assert len(result.changes) == 1
    assert services.artifacts == []
    assert result.changes[0].content_hash == hashlib.sha256(services.target.read_bytes()).hexdigest()
    assert services.target.read_text(encoding="utf-8") == "bar()\n"


def test_replace_apply_cancel_reports_confirmed_stop_and_change(tmp_path: Path) -> None:
    services = _services(tmp_path)
    services.apply_writes = "bar()\n"
    services.apply_result = ProcessResult(None, "", "", 1, cancelled=True)
    result = _failure(_invoke_replace(tmp_path, services))
    assert result.execution == ConfirmedStop(cancellation_signalled=True)
    assert result.diagnostics is not None
    assert result.diagnostics.kind == "tool_cancelled"
    assert len(result.changes) == 1


def test_replace_apply_without_a_real_change_is_a_noop_failure(tmp_path: Path) -> None:
    services = _services(tmp_path)
    services.apply_writes = "foo()\n"
    services.apply_result = ProcessResult(0, "Applied 1 changes\n", "Warning: arbitrary exit-0 notice\n", 1)
    result = _failure(_invoke_replace(tmp_path, services))
    assert result.diagnostics is not None
    assert result.diagnostics.kind == "no_op"
    assert "unchanged" in result.error
    assert "Warning: arbitrary exit-0 notice" in result.error
    assert result.diagnostics.details["warnings"] == ("Warning: arbitrary exit-0 notice",)


def test_replace_large_file_omits_diff_but_reports_stats(tmp_path: Path) -> None:
    target = tmp_path / "large.py"
    target.write_text("foo()\n" + ("# pad\n" * 20_000), encoding="utf-8")
    match = {
        "file": "large.py",
        "text": "foo()",
        "replacement": "bar()",
        "range": {"start": {"line": 0, "column": 0}, "end": {"line": 0, "column": 5}},
    }
    services = NativeResults(ProcessResult(0, json.dumps(match) + "\n", "", 1), target)
    services.apply_writes = "bar()\n"
    services.apply_result = ProcessResult(0, "Applied 1 changes\n", "", 1)
    result = _success(_invoke_replace(tmp_path, services, target=target))
    payload = _payload(result)
    assert "diff" not in payload
    assert "64 KiB" in str(payload["diff_omitted"])
    assert payload["byte_count"] == len(b"bar()\n")


def test_replace_runs_under_a_readonly_lease_as_a_typed_failure(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("foo()\n", encoding="utf-8")
    services = ReadOnlyLeaseServices(ProcessResult(0, "", "", 1), target)
    result = _failure(_invoke_replace(tmp_path, services))
    assert result.diagnostics is not None
    assert result.diagnostics.kind == "tool_environment_error"
    assert target.read_text(encoding="utf-8") == "foo()\n"


# ── real binary ──────────────────────────────────────────────────────────────

real_binary = pytest.mark.skipif(BINARY is None, reason="ast-grep binary is not installed")


@real_binary
def test_real_binary_search_infers_language_and_honors_visibility_flags(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("foo()\n", encoding="utf-8")
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / "h.py").write_text("foo()\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("foo()\n", encoding="utf-8")

    services = RealAstGrepServices()
    inferred = _success(_invoke_search(tmp_path, services, path="a.py", lang=None))
    assert _payload(inferred)["lang"] == "Python"
    assert "Found 1 AST match" in inferred.output.text

    services = RealAstGrepServices()
    all_files = _success(_invoke_search(tmp_path, services, path=".", lang="python"))
    files = {match["file"] for match in _payload(all_files)["matches"]}  # type: ignore[index]
    assert files == {"a.py", ".hidden/h.py", "ignored.py"}

    services = RealAstGrepServices()
    hidden_off = _success(_invoke_search(tmp_path, services, path=".", lang="python", include_hidden=False))
    files = {match["file"] for match in _payload(hidden_off)["matches"]}  # type: ignore[index]
    assert ".hidden/h.py" not in files

    services = RealAstGrepServices()
    ignored = _success(_invoke_search(tmp_path, services, path=".", lang="python", respect_gitignore=True))
    files = {match["file"] for match in _payload(ignored)["matches"]}  # type: ignore[index]
    assert "ignored.py" not in files

    services = RealAstGrepServices()
    ignored_with_filter = _success(_invoke_search(tmp_path, services, path=".", lang="python", include=["*.py"], respect_gitignore=True))
    files = {match["file"] for match in _payload(ignored_with_filter)["matches"]}  # type: ignore[index]
    assert "ignored.py" not in files


@real_binary
def test_real_binary_malformed_pattern_is_a_visible_warning(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("foo()\n", encoding="utf-8")
    services = RealAstGrepServices()
    result = _success(_invoke_search(tmp_path, services, path="a.py", lang="python", pattern="foo("))
    assert "Found 0 AST match" in result.output.text
    assert "Warnings:" in result.output.text
    warnings = _payload(result)["warnings"]
    assert isinstance(warnings, list)
    assert any("Pattern contains an ERROR node" in str(line) for line in warnings)


@real_binary
def test_real_binary_replace_is_confined_and_reports_the_new_hash(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("foo()\n", encoding="utf-8")
    services = RealAstGrepServices()
    result = _success(_invoke_replace(tmp_path, services, target=target, lang=None))
    assert target.read_text(encoding="utf-8") == "bar()\n"
    payload = _payload(result)
    assert payload["final_hash"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert result.changes[0].content_hash == payload["final_hash"]
    assert payload["lang"] == "Python"


@real_binary
def test_real_binary_accepts_a_flag_shaped_path_after_the_separator(tmp_path: Path) -> None:
    target = tmp_path / "-U.py"
    target.write_text("foo()\n", encoding="utf-8")
    services = RealAstGrepServices()
    result = _success(_invoke_search(tmp_path, services, path="-U.py", lang="python"))
    assert "Found 1 AST match" in result.output.text
    assert target.read_text(encoding="utf-8") == "foo()\n"


@real_binary
def test_real_binary_include_and_exclude_filters(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("foo()\n", encoding="utf-8")
    (tmp_path / "keep.py").write_text("foo()\n", encoding="utf-8")
    (tmp_path / "skip.js").write_text("foo();\n", encoding="utf-8")
    services = RealAstGrepServices()
    result = _success(_invoke_search(tmp_path, services, path=".", lang="python", include=["*.py"], exclude=["a.py"]))
    files = {match["file"] for match in _payload(result)["matches"]}  # type: ignore[index]
    assert files == {"keep.py"}
