"""Unit tests for the files workspace plugin."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, cast

from voidcode.plugins.files.files_plugin import ReadResultBody, WriteResultBody
from voidcode.plugins.files.files_plugin import create_plugin as _orig_create_plugin
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.provider import ProviderAbortSignal
from voidcode.sdk.tools import ConfirmedStop, ToolCall, ToolFailure, ToolSuccess


class _DummyServices:
    pass


_SERVICES = cast(Any, _DummyServices())


def create_plugin(config):
    p = _orig_create_plugin(config)

    class WrappedTool:
        def __init__(self, inner):
            self._inner = inner
            self.definition = inner.definition

        def invoke(self, call, *, context, services=_SERVICES):
            return self._inner.invoke(call, context=context, services=services)

    return Plugin(
        tools=tuple(WrappedTool(t) for t in p.tools),
        commands=p.commands,
        providers=p.providers,
        views=p.views,
        context=p.context,
        observer=p.observer,
        close=p.close,
    )


class DummyAbortSignal:
    def __init__(self, cancelled: bool = False, reason: str | None = None) -> None:
        self._cancelled = cancelled
        self._reason = reason

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def reason(self) -> str | None:
        return self._reason

    def set_cancelled(self, value: bool, *, reason: str | None = None) -> None:
        self._cancelled = value
        self._reason = reason


def _context(
    workspace: Path,
    *,
    capabilities: frozenset[str] = frozenset({"workspace.read", "workspace.write"}),
    abort_signal: ProviderAbortSignal | None = None,
) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        session_id="session-test",
        run_id="run-test",
        invocation_id="inv-test",
        revision_id="rev-test",
        actor="model",
        capabilities=capabilities,
        abort_signal=abort_signal,
    )


def test_read_tool_requires_capability(tmp_path: Path) -> None:
    plugin = create_plugin({})
    read_tool = next(t for t in plugin.tools if t.definition.name == "read")
    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")

    result = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "hello.txt"}),
        context=_context(tmp_path, capabilities=frozenset()),
    )
    assert isinstance(result, ToolFailure)
    assert "Missing required capability: workspace.read" in result.error


def test_read_tool_reads_utf8_single_pass_with_offset_and_limit(tmp_path: Path) -> None:
    plugin = create_plugin({})
    read_tool = next(t for t in plugin.tools if t.definition.name == "read")
    content = "line1\nline2\nline3\nline4\n"
    (tmp_path / "test.txt").write_text(content, encoding="utf-8")
    expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    result = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "test.txt", "offset": 2, "limit": 2}),
        context=_context(tmp_path),
    )
    assert isinstance(result, ToolSuccess)
    assert "line2\nline3\n" in result.output.text  # type: ignore[union-attr]
    assert "line1\n" not in result.output.text  # type: ignore[union-attr]
    assert expected_hash in result.output.text  # type: ignore[union-attr]
    body = result.body
    assert isinstance(body, ReadResultBody)
    assert body.path == "test.txt"
    assert body.sha256 == expected_hash
    assert body.whole_file is False


def test_read_tool_whole_file_observation_and_empty_file(tmp_path: Path) -> None:
    plugin = create_plugin({})
    read_tool = next(t for t in plugin.tools if t.definition.name == "read")

    # Full read
    content = "first\nsecond\n"
    (tmp_path / "full.txt").write_text(content, encoding="utf-8")
    result = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "full.txt", "offset": 1, "limit": 100}),
        context=_context(tmp_path),
    )
    assert isinstance(result, ToolSuccess)
    assert isinstance(result.body, ReadResultBody)
    assert result.body.whole_file is True

    # Empty file
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")
    empty_result = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "empty.txt"}),
        context=_context(tmp_path),
    )
    assert isinstance(empty_result, ToolSuccess)
    assert isinstance(empty_result.body, ReadResultBody)
    assert empty_result.body.whole_file is True
    assert empty_result.body.sha256 == hashlib.sha256(b"").hexdigest()


def test_read_tool_bounds_contiguous_prefix_and_truncation(tmp_path: Path) -> None:
    plugin = create_plugin({})
    read_tool = next(t for t in plugin.tools if t.definition.name == "read")

    # Generate file with 1500 lines
    lines = [f"data-row-{i:05d}\n" for i in range(1500)]
    (tmp_path / "big.txt").write_text("".join(lines), encoding="utf-8")

    result = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "big.txt", "offset": 1, "limit": 2000}),
        context=_context(tmp_path),
    )
    assert isinstance(result, ToolSuccess)
    assert result.output.bounds.truncated is True  # type: ignore[union-attr]
    assert isinstance(result.body, ReadResultBody)
    assert result.body.whole_file is False
    # Ensure total text output strictly does not exceed 50 KiB
    assert len(result.output.text.encode("utf-8")) <= 50 * 1024  # type: ignore[union-attr]
    # Check lines are contiguous starting from data-row-00000
    assert "data-row-00000" in result.output.text  # type: ignore[union-attr]


def test_read_tool_handles_huge_lines_without_unbounded_allocation(tmp_path: Path) -> None:
    plugin = create_plugin({})
    read_tool = next(t for t in plugin.tools if t.definition.name == "read")

    huge_line = "x" * (3 * 1024 * 1024) + "\nsecond line\n"
    (tmp_path / "huge.txt").write_text(huge_line, encoding="utf-8")

    result = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "huge.txt", "offset": 1, "limit": 10}),
        context=_context(tmp_path),
    )
    assert isinstance(result, ToolSuccess)
    assert len(result.output.text.encode("utf-8")) <= 50 * 1024
    assert result.output.bounds.truncated is True
    assert result.body.whole_file is False
    assert result.body.sha256 == hashlib.sha256(huge_line.encode("utf-8")).hexdigest()


def test_read_tool_honors_cancellation_during_read(tmp_path: Path) -> None:
    plugin = create_plugin({})
    read_tool = next(t for t in plugin.tools if t.definition.name == "read")
    (tmp_path / "sample.txt").write_text("content", encoding="utf-8")
    abort = DummyAbortSignal(cancelled=True)

    result = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "sample.txt"}),
        context=_context(tmp_path, abort_signal=abort),
    )
    assert isinstance(result, ToolFailure)
    assert "cancelled" in result.error
    assert isinstance(result.execution, ConfirmedStop)


def test_read_tool_rejects_binary_file(tmp_path: Path) -> None:
    plugin = create_plugin({})
    read_tool = next(t for t in plugin.tools if t.definition.name == "read")
    (tmp_path / "binary.bin").write_bytes(b"\x80\xff\x00\x01\x02\x03\xfe")

    result = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "binary.bin"}),
        context=_context(tmp_path),
    )
    assert isinstance(result, ToolFailure)
    assert "Invalid UTF-8" in result.error


def test_read_tool_confinement_and_symlink_escape(tmp_path: Path) -> None:
    plugin = create_plugin({})
    read_tool = next(t for t in plugin.tools if t.definition.name == "read")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    # Path traversal
    res1 = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "../outside.txt"}),
        context=_context(workspace),
    )
    assert isinstance(res1, ToolFailure)
    assert "escapes workspace" in res1.error

    # Absolute escape
    res2 = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": str(outside.resolve())}),
        context=_context(workspace),
    )
    assert isinstance(res2, ToolFailure)
    assert "escapes workspace" in res2.error

    # Symlink escape
    symlink = workspace / "escaped_link.txt"
    os.symlink(outside, symlink)
    res3 = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "escaped_link.txt"}),
        context=_context(workspace),
    )
    assert isinstance(res3, ToolFailure)
    assert "escapes workspace" in res3.error


def test_write_tool_requires_capability(tmp_path: Path) -> None:
    plugin = create_plugin({})
    write_tool = next(t for t in plugin.tools if t.definition.name == "write")

    result = write_tool.invoke(
        ToolCall(tool_name="write", arguments={"path": "new.txt", "content": "hello"}),
        context=_context(tmp_path, capabilities=frozenset({"workspace.read"})),
    )
    assert isinstance(result, ToolFailure)
    assert "Missing required capability: workspace.write" in result.error
    assert not (tmp_path / "new.txt").exists()


def test_write_tool_creates_new_file_without_expected_hash(tmp_path: Path) -> None:
    plugin = create_plugin({})
    write_tool = next(t for t in plugin.tools if t.definition.name == "write")
    content = "brand new content"

    result = write_tool.invoke(
        ToolCall(tool_name="write", arguments={"path": "sub/dir/new.txt", "content": content}),
        context=_context(tmp_path),
    )
    assert isinstance(result, ToolSuccess)
    created = tmp_path / "sub" / "dir" / "new.txt"
    assert created.exists()
    assert created.read_text(encoding="utf-8") == content
    expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    body = result.body
    assert isinstance(body, WriteResultBody)
    assert body.path == "sub/dir/new.txt"
    assert body.sha256 == expected_hash
    assert body.bytes == len(content.encode("utf-8"))


def test_write_tool_requires_and_enforces_expected_hash_for_existing(tmp_path: Path) -> None:
    plugin = create_plugin({})
    write_tool = next(t for t in plugin.tools if t.definition.name == "write")
    target = tmp_path / "exist.txt"
    original = "original content"
    target.write_text(original, encoding="utf-8")
    original_hash = hashlib.sha256(original.encode("utf-8")).hexdigest()

    # Missing expectedHash
    res1 = write_tool.invoke(
        ToolCall(tool_name="write", arguments={"path": "exist.txt", "content": "update"}),
        context=_context(tmp_path),
    )
    assert isinstance(res1, ToolFailure)
    assert "requires an expectedHash" in res1.error
    assert target.read_text(encoding="utf-8") == original

    # Mismatched expectedHash (stale overwrite protection)
    res2 = write_tool.invoke(
        ToolCall(tool_name="write", arguments={"path": "exist.txt", "content": "update", "expectedHash": "000000"}),
        context=_context(tmp_path),
    )
    assert isinstance(res2, ToolFailure)
    assert "Stale write rejected" in res2.error
    assert target.read_text(encoding="utf-8") == original

    # Correct expectedHash succeeds and preserves mode
    os.chmod(target, 0o600)
    new_content = "updated content"
    res3 = write_tool.invoke(
        ToolCall(tool_name="write", arguments={"path": "exist.txt", "content": new_content, "expectedHash": original_hash}),
        context=_context(tmp_path),
    )
    assert isinstance(res3, ToolSuccess)
    assert target.read_text(encoding="utf-8") == new_content
    assert (target.stat().st_mode & 0o777) == 0o600


def test_write_tool_rejects_oversized_content(tmp_path: Path) -> None:
    plugin = create_plugin({})
    write_tool = next(t for t in plugin.tools if t.definition.name == "write")
    oversized = "a" * (65 * 1024)

    result = write_tool.invoke(
        ToolCall(tool_name="write", arguments={"path": "toolarge.txt", "content": oversized}),
        context=_context(tmp_path),
    )
    assert isinstance(result, ToolFailure)
    assert "Content exceeds size limit" in result.error
    assert not (tmp_path / "toolarge.txt").exists()


def test_write_tool_rejects_symlink_and_directory(tmp_path: Path) -> None:
    plugin = create_plugin({})
    write_tool = next(t for t in plugin.tools if t.definition.name == "write")

    # Directory write
    dir_target = tmp_path / "mydir"
    dir_target.mkdir()
    res1 = write_tool.invoke(
        ToolCall(tool_name="write", arguments={"path": "mydir", "content": "data"}),
        context=_context(tmp_path),
    )
    assert isinstance(res1, ToolFailure)
    assert "Cannot write to directory" in res1.error

    # Symlink write
    real_file = tmp_path / "real.txt"
    real_file.write_text("real", encoding="utf-8")
    link = tmp_path / "link.txt"
    os.symlink(real_file, link)
    res2 = write_tool.invoke(
        ToolCall(tool_name="write", arguments={"path": "link.txt", "content": "data", "expectedHash": "dummy"}),
        context=_context(tmp_path),
    )
    assert isinstance(res2, ToolFailure)
    assert "Cannot write to symlink" in res2.error


def test_write_tool_honors_cancellation_before_side_effects(tmp_path: Path) -> None:
    plugin = create_plugin({})
    write_tool = next(t for t in plugin.tools if t.definition.name == "write")
    abort = DummyAbortSignal(cancelled=True, reason="User cancelled")

    target = tmp_path / "will_not_exist.txt"
    result = write_tool.invoke(
        ToolCall(tool_name="write", arguments={"path": "will_not_exist.txt", "content": "nope"}),
        context=_context(tmp_path, abort_signal=abort),
    )
    assert isinstance(result, ToolFailure)
    assert "cancelled before side effect" in result.error
    assert isinstance(result.execution, ConfirmedStop)
    assert not target.exists()
