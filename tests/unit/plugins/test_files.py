"""Unit tests for the files workspace plugin."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import voidcode.plugins.files
from voidcode.plugins.files import create_plugin as _orig_create_plugin
from voidcode.plugins.files.read_tool import ReadResultBody
from voidcode.plugins.files.repair import ToolDiagnosticError
from voidcode.plugins.files.write_tool import WriteResultBody
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.manifest import parse_plugin_manifest
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.provider import ProviderAbortSignal
from voidcode.sdk.tools import ConfirmedStop, ReadObservation, ToolCall, ToolFailure, ToolSuccess


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
            # ponytail: read/write take the SDK services param; edit/glob/grep
            # predate it (P4PluginTypes is adding it). Probe, don't guess.
            if "services" in inspect.signature(self._inner.invoke).parameters:
                return self._inner.invoke(call, context=context, services=services)
            if services is not _SERVICES:
                raise TypeError(f"{type(self._inner).__name__}.invoke() takes no services argument")
            return self._inner.invoke(call, context=context)

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
    read_paths: frozenset[str] = frozenset(),
    read_lines: dict[tuple[str, str], frozenset[int]] | None = None,
    read_whole_files: frozenset[tuple[str, str]] = frozenset(),
    read_hash: str | None = None,
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
        read_paths=read_paths,
        read_lines=read_lines or {},
        read_whole_files=read_whole_files,
        read_hash=read_hash,
    )


def test_files_tool_declarations_require_workspace_capabilities() -> None:
    """The kernel grants capabilities per declaration; the tools do not self-grant."""
    manifest = parse_plugin_manifest(json.loads((Path(voidcode.plugins.files.__file__).parent / "voidcode-plugin.json").read_text(encoding="utf-8")))
    declared = {decl.name: decl for decl in manifest.tools}

    assert declared["read"].capabilities == ("workspace.read",)
    assert declared["write"].capabilities == ("workspace.write",)
    assert declared["edit"].capabilities == ("workspace.write",)
    assert declared["glob"].capabilities == ("workspace.read",)
    assert declared["grep"].capabilities == ("workspace.read",)


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
    assert body.content_hash == expected_hash
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
    assert len(result.coverage) == 1
    (observation,) = result.coverage
    assert isinstance(observation, ReadObservation)
    assert observation.path == "full.txt"
    assert observation.content_hash == result.body.content_hash
    assert observation.whole_file is True
    assert observation.line_count == len(content.splitlines())

    # Empty file
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")
    empty_result = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "empty.txt"}),
        context=_context(tmp_path),
    )
    assert isinstance(empty_result, ToolSuccess)
    assert isinstance(empty_result.body, ReadResultBody)
    assert empty_result.body.whole_file is True
    assert empty_result.body.content_hash == hashlib.sha256(b"").hexdigest()
    assert len(empty_result.coverage) == 1
    (empty_observation,) = empty_result.coverage
    assert empty_observation.whole_file is True
    assert empty_observation.line_count == 0


def test_read_tool_bounds_contiguous_prefix_and_truncation(tmp_path: Path) -> None:
    plugin = create_plugin({})
    read_tool = next(t for t in plugin.tools if t.definition.name == "read")

    # Generate file with 3000 lines and ask for 1000 of them
    lines = [f"data-row-{i:05d}\n" for i in range(3000)]
    (tmp_path / "big.txt").write_text("".join(lines), encoding="utf-8")

    result = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "big.txt", "offset": 1, "limit": 1000}),
        context=_context(tmp_path),
    )
    assert isinstance(result, ToolSuccess)
    assert result.output.bounds.truncated is True  # type: ignore[union-attr]
    assert isinstance(result.body, ReadResultBody)
    assert result.body.whole_file is False
    assert result.body.next_offset == 1001
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
    assert result.body.content_hash == hashlib.sha256(huge_line.encode("utf-8")).hexdigest()


def test_read_tool_honors_cancellation_during_a_long_view(tmp_path: Path) -> None:
    plugin = create_plugin({})
    read_tool = next(t for t in plugin.tools if t.definition.name == "read")
    database = tmp_path / "cancelled.db"
    connection = sqlite3.connect(database)
    connection.execute("create table t (value integer)")
    connection.executemany("insert into t values (?)", [(index,) for index in range(50)])
    connection.commit()
    connection.close()

    with pytest.raises(ValueError, match="cancelled"):
        read_tool.invoke(
            ToolCall(tool_name="read", arguments={"path": "cancelled.db", "view": "sqlite"}),
            context=_context(tmp_path, abort_signal=DummyAbortSignal(cancelled=True)),
        )


def test_read_tool_renders_binary_file_as_attachment(tmp_path: Path) -> None:
    plugin = create_plugin({})
    read_tool = next(t for t in plugin.tools if t.definition.name == "read")
    (tmp_path / "binary.bin").write_bytes(b"\x80\xff\x00\x01\x02\x03\xfe")

    result = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "binary.bin"}),
        context=_context(tmp_path),
    )

    assert isinstance(result, ToolSuccess)
    assert "Binary file binary.bin" in result.output.text  # type: ignore[union-attr]
    assert result.coverage == ()


def test_read_tool_raster_attachment_grants_no_coverage(tmp_path: Path) -> None:
    """An inline image read shows pixels, not file lines: no coverage."""
    plugin = create_plugin({})
    read_tool = next(t for t in plugin.tools if t.definition.name == "read")
    (tmp_path / "pixel.png").write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
            "0000000c4944415478da63606060000000040001e221bc330000000049454e44ae426082"
        )
    )

    result = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "pixel.png"}),
        context=_context(tmp_path),
    )

    assert isinstance(result, ToolSuccess)
    assert result.coverage == ()


def test_read_tool_allows_external_paths_with_absolute_display(tmp_path: Path) -> None:
    """External reads stay allowed (the runtime's permission tier governs them)."""
    plugin = create_plugin({})
    read_tool = next(t for t in plugin.tools if t.definition.name == "read")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    traversal = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "../outside.txt"}),
        context=_context(workspace),
    )
    assert isinstance(traversal, ToolSuccess)
    assert "secret" in traversal.output.text  # type: ignore[union-attr]
    assert str(outside.resolve()) in traversal.output.text  # type: ignore[union-attr]

    absolute = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": str(outside.resolve())}),
        context=_context(workspace),
    )
    assert isinstance(absolute, ToolSuccess)
    assert "secret" in absolute.output.text  # type: ignore[union-attr]

    symlink = workspace / "escaped_link.txt"
    os.symlink(outside, symlink)
    through_link = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "escaped_link.txt"}),
        context=_context(workspace),
    )
    assert isinstance(through_link, ToolSuccess)
    assert "secret" in through_link.output.text  # type: ignore[union-attr]


def test_read_tool_uri_read_grants_no_coverage(tmp_path: Path) -> None:
    """A lineage-free artifact read's lines are not workspace file versions."""
    plugin = create_plugin({})
    read_tool = next(t for t in plugin.tools if t.definition.name == "read")
    raw = b"uri line one\nuri line two\n"
    artifact_id = hashlib.sha256(raw).hexdigest()
    total = len(raw)

    def read_artifact(reference: object, *, offset: int = 0, limit: int = 65536):  # type: ignore[no-untyped-def]
        digest = getattr(reference, "sha256", reference)
        assert digest == artifact_id
        page = raw[offset : offset + limit]
        return SimpleNamespace(
            sha256=artifact_id,
            offset=offset,
            total_bytes=total,
            data=page,
            eof=offset + len(page) >= total,
            media_type="",
        )

    result = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": f"voidcode://artifact/{artifact_id}"}),
        context=_context(tmp_path),
        services=cast(Any, SimpleNamespace(read_artifact=read_artifact)),
    )
    assert isinstance(result, ToolSuccess)
    assert "uri line one" in result.output.text  # type: ignore[union-attr]
    assert result.coverage == ()


def test_edit_batch_after_reread_succeeds_with_ambiguous_read_hash(tmp_path: Path) -> None:
    """Fresh-base coverage: read/edit/read-new/edit works when read_hash is None.

    The kernel sets read_hash=None once two versions of the path were observed,
    so the batch guard binds coverage to the SHA-256 of its own verified base
    content instead of the ambiguous context field.
    """
    plugin = create_plugin({})
    edit_tool = next(t for t in plugin.tools if t.definition.name == "edit")
    target = tmp_path / "cycle.txt"
    v1 = "alpha\nbeta\n"
    target.write_text(v1, encoding="utf-8")
    v1_hash = hashlib.sha256(v1.encode("utf-8")).hexdigest()

    resolved = str(target.resolve())
    session_reads: dict[tuple[str, str], frozenset[int]] = {(resolved, v1_hash): frozenset({1, 2})}
    context_v1 = _context(
        tmp_path,
        read_paths=frozenset({resolved}),
        read_lines=dict(session_reads),
        read_whole_files=frozenset({(resolved, v1_hash)}),
        read_hash=v1_hash,
    )
    first = edit_tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={
                "path": "cycle.txt",
                "edits": [{"oldString": "beta", "newString": "BETA"}],
                "expectedHash": v1_hash,
            },
        ),
        context=context_v1,
    )
    assert isinstance(first, ToolSuccess)

    v2 = target.read_text(encoding="utf-8")
    v2_hash = hashlib.sha256(v2.encode("utf-8")).hexdigest()
    assert v2_hash != v1_hash
    session_reads[(resolved, v2_hash)] = frozenset({1, 2})
    context_v2 = _context(
        tmp_path,
        read_paths=frozenset({resolved}),
        read_lines=dict(session_reads),
        read_whole_files=frozenset({(resolved, v1_hash), (resolved, v2_hash)}),
        read_hash=None,
    )
    second = edit_tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={
                "path": "cycle.txt",
                "edits": [{"oldString": "BETA", "newString": "beta2"}],
                "expectedHash": v2_hash,
            },
        ),
        context=context_v2,
    )
    assert isinstance(second, ToolSuccess)
    assert target.read_text(encoding="utf-8") == "alpha\nbeta2\n"


def test_edit_batch_with_stale_base_refuses_prior_version_lines(tmp_path: Path) -> None:
    """Stale-base coverage: lines of a prior version never authorize an edit."""
    plugin = create_plugin({})
    edit_tool = next(t for t in plugin.tools if t.definition.name == "edit")
    target = tmp_path / "stale.txt"
    v1 = "alpha\nbeta\ngamma\n"
    target.write_text(v1, encoding="utf-8")
    v1_hash = hashlib.sha256(v1.encode("utf-8")).hexdigest()
    resolved = str(target.resolve())
    # Only line 3 of v1 was ever shown; v2 (line 3 changed) was never read.
    v2 = "alpha\nbeta\nGAMMA\n"
    v2_hash = hashlib.sha256(v2.encode("utf-8")).hexdigest()
    target.write_text(v2, encoding="utf-8")

    context = _context(
        tmp_path,
        read_paths=frozenset({resolved}),
        read_lines={(resolved, v1_hash): frozenset({3})},
        read_whole_files=frozenset(),
        read_hash=None,
    )
    with pytest.raises(ToolDiagnosticError, match="never revealed by read") as exc_info:
        edit_tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={
                    "path": "stale.txt",
                    "edits": [{"oldString": "GAMMA", "newString": "gamma2"}],
                    "expectedHash": v2_hash,
                },
            ),
            context=context,
        )
    assert exc_info.value.error_details["reason"] == "unseen_range"
    assert target.read_text(encoding="utf-8") == v2


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
