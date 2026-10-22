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

from voidcode.capabilities.files.read_tool import ReadResultBody
from voidcode.capabilities.files.write_tool import WriteResultBody
from voidcode.plugins.files import create_plugin as _orig_create_plugin
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.provider import ProviderAbortSignal
from voidcode.sdk.tools import ConfirmedStop, ReadObservation, ToolCall, ToolFailure, ToolSuccess


class _DummyServices:
    def spill_artifact(self, data: bytes | str, *, media_type: str = "text/plain; charset=utf-8"):  # type: ignore[no-untyped-def]
        raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        return SimpleNamespace(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw), media_type=media_type)


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
        abort_signal=abort_signal,
        read_paths=read_paths,
        read_lines=read_lines or {},
        read_whole_files=read_whole_files,
        read_hash=read_hash,
    )


def test_files_tools_declare_their_effect_tiers() -> None:
    definitions = {tool.definition.name: tool.definition for tool in create_plugin({}).tools}

    assert set(definitions) == {"read", "write", "edit", "apply_workspace_edit", "glob", "grep"}
    assert {effect.value for effect in definitions["read"].effects} == {"read"}
    assert {effect.value for effect in definitions["write"].effects} == {"write"}
    assert {effect.value for effect in definitions["edit"].effects} == {"write"}
    assert {effect.value for effect in definitions["glob"].effects} == {"read"}
    assert {effect.value for effect in definitions["grep"].effects} == {"read"}


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

    The core sets read_hash=None once two versions of the path were observed,
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


def test_single_edit_after_reread_binds_seen_lines_to_current_bytes(tmp_path: Path) -> None:
    """The single oldString path follows the same cycle: the guard binds to bytes on disk.

    Nothing passes ``current_hash`` here; the shared guard hashes the candidate itself, so the
    core's ambiguous ``read_hash`` (None once two versions were observed) cannot refuse a
    second edit whose current version the model just read.
    """
    plugin = create_plugin({})
    edit_tool = next(t for t in plugin.tools if t.definition.name == "edit")
    target = tmp_path / "single.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    v1_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    resolved = str(target.resolve())

    first = edit_tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={"path": "single.txt", "oldString": "beta", "newString": "BETA", "expectedHash": v1_hash},
        ),
        context=_context(
            tmp_path,
            read_paths=frozenset({resolved}),
            read_lines={(resolved, v1_hash): frozenset({1, 2})},
            read_whole_files=frozenset({(resolved, v1_hash)}),
            read_hash=v1_hash,
        ),
    )
    assert isinstance(first, ToolSuccess)

    v2_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    assert v2_hash != v1_hash
    second = edit_tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={"path": "single.txt", "oldString": "BETA", "newString": "beta2", "expectedHash": v2_hash},
        ),
        context=_context(
            tmp_path,
            read_paths=frozenset({resolved}),
            read_lines={(resolved, v1_hash): frozenset({1, 2}), (resolved, v2_hash): frozenset({1, 2})},
            read_whole_files=frozenset({(resolved, v1_hash), (resolved, v2_hash)}),
            read_hash=None,
        ),
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
    result = edit_tool.invoke(
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
    assert isinstance(result, ToolFailure)
    assert "never revealed by read" in result.error
    assert result.diagnostics is not None
    assert result.diagnostics.details["reason"] == "unseen_range"
    assert result.diagnostics.guidance is not None and "Use read on the target path" in result.diagnostics.guidance
    assert target.read_text(encoding="utf-8") == v2


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


class _UriServices:
    """The narrow service surface the `voidcode://` branches consume, scripted per test."""

    def __init__(
        self,
        *,
        definitions: dict | None = None,
        events: tuple = (),
        child_events: tuple = (),
        refuse_child: Exception | None = None,
        rule_answer: dict | None = None,
        rule_unanswered: bool = False,
    ) -> None:
        from voidcode.sdk.services import QueryUnavailable

        self._definitions = definitions or {}
        self._events = events
        self._child_events = child_events
        self._refuse_child = refuse_child
        self._rule_answer = rule_answer
        self._rule_unanswered = rule_unanswered
        self._missing = QueryUnavailable
        self.child_session_ids: list = []

    def tool_definition(self, name: str):  # type: ignore[no-untyped-def]
        return self._definitions.get(name)

    def read_events(self, *, after_sequence: int = 0, limit: int = 100, include_payload: bool = True):  # type: ignore[no-untyped-def]
        assert include_payload is True
        return tuple(event for event in self._events if event.sequence > after_sequence)[:limit]

    def read_child_events(self, child_session_id: str, *, after_sequence: int = 0, limit: int = 100, include_payload: bool = True):  # type: ignore[no-untyped-def]
        assert include_payload is True
        self.child_session_ids.append(child_session_id)
        if self._refuse_child is not None:
            raise self._refuse_child
        return self._child_events[:limit]

    def query(self, name: str, arguments: dict | None = None):  # type: ignore[no-untyped-def]
        assert name == "workspace.rule_page"
        if self._rule_unanswered:
            raise self._missing(f"no admitted plugin answers the query {name!r}")
        return self._rule_answer


def test_read_tool_documentation_renders_visible_definition_and_refuses_hidden(tmp_path: Path) -> None:
    from voidcode.sdk.tools import ToolDefinition, ToolEffect

    plugin = create_plugin({})
    read_tool = next(t for t in plugin.tools if t.definition.name == "read")
    definition = ToolDefinition(
        name="read",
        description="Read a file.",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        effects=frozenset({ToolEffect.READ}),
    )
    visible = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "voidcode://tool/read"}),
        context=_context(tmp_path),
        services=cast(Any, _UriServices(definitions={"read": definition})),
    )
    assert isinstance(visible, ToolSuccess)
    assert "Read a file." in visible.output.text  # type: ignore[union-attr]
    assert visible.body.as_payload()["tool_name"] == "read"  # type: ignore[union-attr]

    hidden = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "voidcode://tool/write"}),
        context=_context(tmp_path),
        services=cast(Any, _UriServices(definitions={"read": definition})),
    )
    assert isinstance(hidden, ToolFailure)
    assert "profile scope/read-only stance hides it" in hidden.error
    assert hidden.output.text == hidden.error  # type: ignore[union-attr]


def test_read_transcript_returns_readable_pages_with_a_cursor_and_reports_core_refusal(tmp_path: Path) -> None:
    from voidcode.sdk.services import EventView

    plugin = create_plugin({})
    read_tool = next(t for t in plugin.tools if t.definition.name == "read")
    prompt = "PROMPT-START " + "x" * 50_000 + " PROMPT-END"
    views = (
        EventView(
            sequence=1,
            event_type="kernel.user_message",
            run_id="r",
            revision_id="v",
            source="user",
            actor="user",
            payload={"content": prompt},
        ),
        EventView(
            sequence=2,
            event_type="plugin.todo.changed",
            run_id="r",
            revision_id="v",
            source="plugin.todo",
            actor="plugin",
            payload={"op": "view", "note": "TOOL-PAYLOAD"},
        ),
    )
    own = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "voidcode://transcript/session-test"}),
        context=_context(tmp_path),
        services=cast(Any, _UriServices(events=views)),
    )
    assert isinstance(own, ToolSuccess)
    first = own.body.as_payload()  # type: ignore[union-attr]
    assert first["type"] == "transcript" and first["session_id"] == "session-test"
    assert first["truncated"] is True and first["next_offset"] > 0
    assert "PROMPT-START" in own.output.text  # type: ignore[union-attr]
    assert "PROMPT-END" not in own.output.text  # type: ignore[union-attr]
    assert "More history: read(path=" in own.output.text  # type: ignore[union-attr]

    next_uri = f"voidcode://transcript/session-test?after_sequence={first['next_after_sequence']}&offset={first['next_offset']}"
    rest = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": next_uri}),
        context=_context(tmp_path),
        services=cast(Any, _UriServices(events=views)),
    )
    assert isinstance(rest, ToolSuccess)
    assert "PROMPT-END" in rest.output.text  # type: ignore[union-attr]
    assert "TOOL-PAYLOAD" in rest.output.text  # type: ignore[union-attr]
    assert rest.body.as_payload()["truncated"] is False  # type: ignore[union-attr]

    child_views = (
        EventView(
            sequence=1,
            event_type="kernel.user_message",
            run_id="r",
            revision_id="v",
            source="user",
            actor="user",
            payload={"content": "CHILD-PROMPT"},
        ),
    )
    child_services = _UriServices(child_events=child_views)
    child = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "voidcode://transcript/child-test"}),
        context=_context(tmp_path),
        services=cast(Any, child_services),
    )
    assert isinstance(child, ToolSuccess)
    assert "CHILD-PROMPT" in child.output.text  # type: ignore[union-attr]
    assert child_services.child_session_ids == ["child-test"]

    sibling = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "voidcode://transcript/sibling"}),
        context=_context(tmp_path),
        services=cast(Any, _UriServices(refuse_child=PermissionError("child events are readable only for this session's own descendants"))),
    )
    assert isinstance(sibling, ToolFailure)
    assert "transcript not accessible for session sibling" in json.dumps(sibling.error)
    assert "own descendants" in json.dumps(sibling.error)


def test_read_rule_reports_unavailable_unknown_and_malformed(tmp_path: Path) -> None:
    plugin = create_plugin({})
    read_tool = next(t for t in plugin.tools if t.definition.name == "read")

    disabled = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "voidcode://rule/notes"}),
        context=_context(tmp_path),
        services=cast(Any, _UriServices(rule_unanswered=True)),
    )
    assert isinstance(disabled, ToolFailure)
    assert "rule catalog is unavailable" in disabled.error

    unknown = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "voidcode://rule/notes"}),
        context=_context(tmp_path),
        services=cast(Any, _UriServices(rule_answer=None)),
    )
    assert isinstance(unknown, ToolFailure)
    assert "unknown rule in this workspace" in unknown.error

    malformed = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "voidcode://rule/notes"}),
        context=_context(tmp_path),
        services=cast(Any, _UriServices(rule_answer={"rule": "notes"})),
    )
    assert isinstance(malformed, ToolFailure)
    assert "provider shape violated" in malformed.error

    page = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "voidcode://rule/notes"}),
        context=_context(tmp_path),
        services=cast(
            Any,
            _UriServices(rule_answer={"rule": "notes", "content_hash": "abc", "text": "Read me", "next_offset": None}),
        ),
    )
    assert isinstance(page, ToolSuccess)
    assert "Read me" in page.output.text  # type: ignore[union-attr]


class _VersionServices:
    """Scripted `workspace.document_version` answers for `apply_workspace_edit`."""

    def __init__(self, answer: dict | None = None, *, unanswered: bool = False) -> None:
        from voidcode.sdk.services import QueryUnavailable

        self._answer = answer
        self._unanswered = unanswered
        self._missing = QueryUnavailable

    def query(self, name: str, arguments: dict | None = None):  # type: ignore[no-untyped-def]
        assert name == "workspace.document_version"
        if self._unanswered:
            raise self._missing(f"no admitted plugin answers the query {name!r}")
        return self._answer


def _apply_context(workspace: Path, target: Path, content_hash: str):  # type: ignore[no-untyped-def]
    resolved = str(target.resolve())
    return _context(
        workspace,
        read_paths=frozenset({resolved}),
        read_lines={(resolved, content_hash): frozenset({1, 2})},
        read_whole_files=frozenset({(resolved, content_hash)}),
        read_hash=content_hash,
    )


def test_apply_explicit_utf8_coordinates_win_over_negotiated_utf16(tmp_path: Path) -> None:
    """A caller may address a non-BMP line in UTF-8 units while LSP negotiated UTF-16.

    "𝄞" is one UTF-32 char, two UTF-16 units, four UTF-8 bytes: replacing the note that
    follows it needs character 6 in UTF-8 coordinates but character 4 in UTF-16 ones.
    """
    plugin = create_plugin({})
    tool = next(t for t in plugin.tools if t.definition.name == "apply_workspace_edit")
    target = tmp_path / "music.txt"
    target.write_text("𝄞ab\ncd\n", encoding="utf-8")
    content_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    document = {
        "path": str(target.resolve()),
        "version": 3,
        "content_hash": content_hash,
        "position_encoding": "utf-16",
    }
    result = tool.invoke(
        ToolCall(
            tool_name="apply_workspace_edit",
            arguments={
                "positionEncoding": "utf-8",
                "edits": [
                    {
                        "path": "music.txt",
                        "startLine": 1,
                        "startCharacter": 6,
                        "endLine": 1,
                        "endCharacter": 7,
                        "newText": "B",
                        "expectedHash": content_hash,
                        "expectedVersion": 3,
                    }
                ],
            },
        ),
        context=_apply_context(tmp_path, target, content_hash),
        services=cast(Any, _VersionServices(document)),
    )
    assert isinstance(result, ToolSuccess), result
    assert target.read_text(encoding="utf-8") == "𝄞aB\ncd\n"


def test_apply_omitted_encoding_uses_negotiated_default(tmp_path: Path) -> None:
    """Without `positionEncoding`, the opened document's negotiated encoding applies."""
    plugin = create_plugin({})
    tool = next(t for t in plugin.tools if t.definition.name == "apply_workspace_edit")
    target = tmp_path / "music.txt"
    target.write_text("𝄞ab\ncd\n", encoding="utf-8")
    content_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    document = {
        "path": str(target.resolve()),
        "version": 3,
        "content_hash": content_hash,
        "position_encoding": "utf-16",
    }
    result = tool.invoke(
        ToolCall(
            tool_name="apply_workspace_edit",
            arguments={
                "edits": [
                    {
                        "path": "music.txt",
                        "startLine": 1,
                        "startCharacter": 4,
                        "endLine": 1,
                        "endCharacter": 5,
                        "newText": "B",
                        "expectedHash": content_hash,
                        "expectedVersion": 3,
                    }
                ],
            },
        ),
        context=_apply_context(tmp_path, target, content_hash),
        services=cast(Any, _VersionServices(document)),
    )
    assert isinstance(result, ToolSuccess), result
    assert target.read_text(encoding="utf-8") == "𝄞aB\ncd\n"


def test_apply_matched_version_and_hash_only_succeed_with_correct_bytes(tmp_path: Path) -> None:
    plugin = create_plugin({})
    tool = next(t for t in plugin.tools if t.definition.name == "apply_workspace_edit")
    target = tmp_path / "target.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    first_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    document = {
        "path": str(target.resolve()),
        "version": 1,
        "content_hash": first_hash,
        "position_encoding": "utf-16",
    }
    matched = tool.invoke(
        ToolCall(
            tool_name="apply_workspace_edit",
            arguments={
                "edits": [
                    {
                        "path": "target.txt",
                        "startLine": 1,
                        "startCharacter": 1,
                        "endLine": 1,
                        "endCharacter": 6,
                        "newText": "omega",
                        "expectedHash": first_hash,
                        "expectedVersion": 1,
                    }
                ],
            },
        ),
        context=_apply_context(tmp_path, target, first_hash),
        services=cast(Any, _VersionServices(document)),
    )
    assert isinstance(matched, ToolSuccess), matched
    assert target.read_text(encoding="utf-8") == "omega\nbeta\n"

    second_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    hash_only = tool.invoke(
        ToolCall(
            tool_name="apply_workspace_edit",
            arguments={
                "edits": [
                    {
                        "path": "target.txt",
                        "startLine": 2,
                        "startCharacter": 1,
                        "endLine": 2,
                        "endCharacter": 5,
                        "newText": "BETA",
                        "expectedHash": second_hash,
                    }
                ],
            },
        ),
        context=_apply_context(tmp_path, target, second_hash),
        services=cast(Any, _VersionServices(unanswered=True)),
    )
    assert isinstance(hash_only, ToolSuccess), hash_only
    assert target.read_text(encoding="utf-8") == "omega\nBETA\n"


def test_apply_read_edit_read_edit_cycle_binds_to_current_version(tmp_path: Path) -> None:
    """The second edit of a read→edit→read→edit cycle must not refuse on stale binding.

    After two versions of the path were observed the core's `read_hash` is None; the
    seen-lines guard binds to the verified expectedHash of this edit instead, so a fresh
    full read of the current content authorizes the second write.
    """
    plugin = create_plugin({})
    tool = next(t for t in plugin.tools if t.definition.name == "apply_workspace_edit")
    target = tmp_path / "uni.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    first_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    resolved = str(target.resolve())

    first = tool.invoke(
        ToolCall(
            tool_name="apply_workspace_edit",
            arguments={
                "edits": [
                    {
                        "path": "uni.py",
                        "startLine": 1,
                        "startCharacter": 1,
                        "endLine": 1,
                        "endCharacter": 10,
                        "newText": "VALUE = 2\n",
                        "expectedHash": first_hash,
                    }
                ],
            },
        ),
        context=_apply_context(tmp_path, target, first_hash),
        services=cast(Any, _VersionServices(unanswered=True)),
    )
    assert isinstance(first, ToolSuccess), first

    second_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    assert second_hash != first_hash
    both_versions = _context(
        tmp_path,
        read_paths=frozenset({resolved}),
        read_lines={(resolved, first_hash): frozenset({1}), (resolved, second_hash): frozenset({1})},
        read_whole_files=frozenset({(resolved, first_hash), (resolved, second_hash)}),
        read_hash=None,
    )
    second = tool.invoke(
        ToolCall(
            tool_name="apply_workspace_edit",
            arguments={
                "edits": [
                    {
                        "path": "uni.py",
                        "startLine": 1,
                        "startCharacter": 1,
                        "endLine": 1,
                        "endCharacter": 10,
                        "newText": "VALUE = 3\n",
                        "expectedHash": second_hash,
                    }
                ],
            },
        ),
        context=both_versions,
        services=cast(Any, _VersionServices(unanswered=True)),
    )
    assert isinstance(second, ToolSuccess), second
    assert target.read_text(encoding="utf-8") == "VALUE = 3\n\n\n"


@pytest.mark.parametrize("tool_name", ["glob", "grep"])
def test_searches_respect_gitignore_double_star_and_negation(tmp_path: Path, tool_name: str) -> None:
    (tmp_path / ".gitignore").write_text("a/**/b.txt\n*.log\n!keep.log\n", encoding="utf-8")
    for name in ("a/b.txt", "a/x/b.txt", "drop.log", "keep.log", "visible.txt"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("needle\n", encoding="utf-8")
    tool = next(t for t in create_plugin({}).tools if t.definition.name == tool_name)
    pattern = "**/*" if tool_name == "glob" else "needle"
    result = tool.invoke(
        ToolCall(tool_name=tool_name, arguments={"pattern": pattern, "path": ".", "respect_gitignore": True}),
        context=_context(tmp_path),
    )
    assert isinstance(result, ToolSuccess)
    assert "keep.log" in result.output.text
    assert "visible.txt" in result.output.text
    assert "b.txt" not in result.output.text
    assert "drop.log" not in result.output.text
