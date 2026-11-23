"""Unit tests for the files workspace plugin."""

from __future__ import annotations

import hashlib
import inspect
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from voidcode.capabilities.files.read_tool import ReadHashBody, ReadResultBody
from voidcode.capabilities.files.write_tool import WriteResultBody
from voidcode.capabilities.resource_pages import MAX_RESOURCE_PAGE_BYTES
from voidcode.plugins.files import create_plugin as _orig_create_plugin
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.provider import ProviderAbortSignal
from voidcode.sdk.services import ArtifactReference
from voidcode.sdk.tools import TOOL_OUTPUT_BYTES, ConfirmedStop, ReadObservation, ToolCall, ToolFailure, ToolSuccess


class _DummyServices:
    def spill_artifact(self, data: bytes | str, *, media_type: str = "text/plain; charset=utf-8"):  # type: ignore[no-untyped-def]
        raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        return ArtifactReference(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw), media_type=media_type)

    def spill_artifact_stream(self, chunks, *, media_type="application/octet-stream"):
        return self.spill_artifact(b"".join(chunks), media_type=media_type)


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
) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        session_id="session-test",
        run_id="run-test",
        invocation_id="inv-test",
        revision_id="rev-test",
        actor="model",
        abort_signal=abort_signal,
    )


def test_files_tools_declare_their_effect_tiers() -> None:
    definitions = {tool.definition.name: tool.definition for tool in create_plugin({}).tools}

    assert set(definitions) == {"read", "write", "edit", "glob", "grep"}
    assert {effect.value for effect in definitions["read"].effects} == {"read"}
    assert {effect.value for effect in definitions["write"].effects} == {"write"}
    assert {effect.value for effect in definitions["edit"].effects} == {"write"}
    assert definitions["edit"].path_argument_keys == ("path", "edits[].path")
    assert {effect.value for effect in definitions["glob"].effects} == {"read"}
    assert {effect.value for effect in definitions["grep"].effects} == {"read"}


def test_read_tool_reads_utf8_single_pass_with_offset_and_limit(tmp_path: Path) -> None:
    plugin = create_plugin({})
    read_tool = next(t for t in plugin.tools if t.definition.name == "read")
    content = "line1\nline2\nline3\nline4\n"
    (tmp_path / "test.txt").write_text(content, encoding="utf-8")

    result = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "test.txt", "offset": 2, "limit": 2}),
        context=_context(tmp_path),
    )
    assert isinstance(result, ToolSuccess)
    assert "line2\nline3\n" in result.output.text  # type: ignore[union-attr]
    assert "line1\n" not in result.output.text  # type: ignore[union-attr]
    assert "SHA-256" not in result.output.text  # type: ignore[union-attr]
    body = result.body
    assert isinstance(body, ReadResultBody)
    assert body.content_hash is None
    assert body.line_count is None
    assert body.next_offset == 4
    assert body.whole_file is False
    assert result.coverage == ()


def test_read_tool_offset_page_reaching_eof_has_no_whole_file_hash(tmp_path: Path) -> None:
    plugin = create_plugin({})
    read_tool = next(t for t in plugin.tools if t.definition.name == "read")
    (tmp_path / "tail.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "tail.txt", "offset": 2, "limit": 10}),
        context=_context(tmp_path),
    )
    assert isinstance(result, ToolSuccess)
    assert isinstance(result.body, ReadResultBody)
    assert result.body.next_offset is None
    assert result.body.content_hash is None
    assert result.body.line_count == 3
    assert result.body.whole_file is False
    assert result.coverage == ()
    assert "SHA-256" not in result.output.text  # type: ignore[union-attr]


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
    assert result.body.content_hash is None
    assert result.body.line_count is None
    assert result.coverage == ()
    # Ensure the page content stays inside the model projection budget.
    assert len(result.output.text.encode("utf-8")) <= TOOL_OUTPUT_BYTES  # type: ignore[union-attr]
    # Check lines are contiguous starting from data-row-00000
    assert "data-row-00000" in result.output.text  # type: ignore[union-attr]


def test_read_tool_near_budget_whole_file_is_not_projection_clipped(tmp_path: Path) -> None:
    plugin = create_plugin({})
    read_tool = next(t for t in plugin.tools if t.definition.name == "read")
    line = "x" * 1170 + "\n"
    content = line * 40
    assert len(content.encode("utf-8")) <= MAX_RESOURCE_PAGE_BYTES
    (tmp_path / "near-budget.txt").write_text(content, encoding="utf-8")

    result = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "near-budget.txt"}),
        context=_context(tmp_path),
    )
    assert isinstance(result, ToolSuccess)
    assert isinstance(result.body, ReadResultBody)
    assert result.body.whole_file is True
    assert result.body.content_hash == hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert len(result.output.text.encode("utf-8")) <= TOOL_OUTPUT_BYTES  # type: ignore[union-attr]
    assert "SHA-256" in result.output.text  # type: ignore[union-attr]

    over_budget = line * 41
    assert len(over_budget.encode("utf-8")) > MAX_RESOURCE_PAGE_BYTES
    (tmp_path / "over-budget.txt").write_text(over_budget, encoding="utf-8")
    partial = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "over-budget.txt"}),
        context=_context(tmp_path),
    )
    assert isinstance(partial, ToolSuccess)
    assert isinstance(partial.body, ReadResultBody)
    assert partial.body.whole_file is False
    assert partial.body.content_hash is None
    assert partial.body.next_offset == 41
    assert len(partial.output.text.encode("utf-8")) <= TOOL_OUTPUT_BYTES  # type: ignore[union-attr]


def test_read_tool_hash_only_streams_full_file_hash_without_a_page(tmp_path: Path) -> None:
    plugin = create_plugin({})
    read_tool = next(t for t in plugin.tools if t.definition.name == "read")
    content = "alpha\nbeta\n"
    (tmp_path / "hash.txt").write_text(content, encoding="utf-8")
    expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    result = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "hash.txt", "hash_only": True}),
        context=_context(tmp_path),
    )
    assert isinstance(result, ToolSuccess)
    assert isinstance(result.body, ReadHashBody)
    assert result.body.sha256 == expected_hash
    assert result.body.bytes == len(content.encode("utf-8"))
    assert expected_hash in result.output.text  # type: ignore[union-attr]
    assert "alpha" not in result.output.text  # type: ignore[union-attr]
    assert result.coverage == ()

    hash_only_conflict = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "hash.txt", "hash_only": True, "offset": 1}),
        context=_context(tmp_path),
    )
    assert isinstance(hash_only_conflict, ToolFailure)
    assert hash_only_conflict.diagnostics is not None
    assert hash_only_conflict.diagnostics.details["reason"] == "hash_only_with_page_arguments"
    assert "hash_only cannot be combined" in hash_only_conflict.error

    # A partial page cannot supply expectedHash; hash_only is the explicit way to get it.
    partial = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "hash.txt", "offset": 2, "limit": 1}),
        context=_context(tmp_path),
    )
    assert isinstance(partial, ToolSuccess)
    assert partial.body.content_hash is None  # type: ignore[union-attr]
    assert expected_hash not in partial.output.text  # type: ignore[union-attr]


def test_read_tool_invalid_path_is_typed(tmp_path: Path) -> None:
    plugin = create_plugin({})
    read_tool = next(t for t in plugin.tools if t.definition.name == "read")
    result = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "bad\x00path"}),
        context=_context(tmp_path),
    )
    assert isinstance(result, ToolFailure)
    assert result.diagnostics is not None and result.diagnostics.details["reason"] == "invalid_path"


def test_read_tool_partial_page_skips_unseen_invalid_utf8(tmp_path: Path) -> None:
    plugin = create_plugin({})
    read_tool = next(t for t in plugin.tools if t.definition.name == "read")
    (tmp_path / "mixed.txt").write_bytes(b"valid first line\n\xff\xfe invalid later line\n")

    result = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "mixed.txt", "limit": 1}),
        context=_context(tmp_path),
    )
    assert isinstance(result, ToolSuccess)
    assert "valid first line" in result.output.text  # type: ignore[union-attr]
    assert "invalid later" not in result.output.text  # type: ignore[union-attr]
    assert isinstance(result.body, ReadResultBody)
    assert result.body.content_hash is None
    assert result.body.line_count is None
    assert result.body.next_offset == 2
    assert result.coverage == ()
    assert "SHA-256" not in result.output.text  # type: ignore[union-attr]


def test_read_tool_whole_file_page_still_rejects_invalid_utf8(tmp_path: Path) -> None:
    plugin = create_plugin({})
    read_tool = next(t for t in plugin.tools if t.definition.name == "read")
    (tmp_path / "invalid.txt").write_bytes(b"valid first line\n\xff\xfe invalid later line\n")

    result = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "invalid.txt"}),
        context=_context(tmp_path),
    )
    assert isinstance(result, ToolFailure)
    assert result.diagnostics is not None
    assert result.diagnostics.details["reason"] == "unsupported_encoding"
    assert "only supports UTF-8" in result.error


def test_read_tool_skips_overlong_line_before_offset_without_artifact(tmp_path: Path) -> None:
    plugin = create_plugin({})
    read_tool = next(t for t in plugin.tools if t.definition.name == "read")
    (tmp_path / "skipped-huge.txt").write_text("x" * (200_000) + "\nvisible\n", encoding="utf-8")

    result = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "skipped-huge.txt", "offset": 2, "limit": 1}),
        context=_context(tmp_path),
    )
    assert isinstance(result, ToolSuccess)
    assert isinstance(result.body, ReadResultBody)
    assert "visible" in result.output.text  # type: ignore[union-attr]
    assert "line truncated" not in result.output.text  # type: ignore[union-attr]
    assert result.body.content_hash is None
    assert result.body.line_count == 2
    assert result.coverage == ()
    assert result.output.bounds.reference is None  # type: ignore[union-attr]


def test_read_tool_detects_file_change_between_page_and_artifact_pass(tmp_path: Path) -> None:
    target = tmp_path / "changed.txt"
    original = b"x" * (200_000) + b"\n"

    class MutatingServices(_DummyServices):
        def spill_artifact_stream(self, chunks, *, media_type="application/octet-stream"):
            target.write_bytes(b"y" + b"x" * (len(original) - 2) + b"\n")
            return super().spill_artifact_stream(chunks, media_type=media_type)

    plugin = create_plugin({})
    read_tool = next(t for t in plugin.tools if t.definition.name == "read")
    target.write_bytes(original)
    result = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "changed.txt"}),
        context=_context(tmp_path),
        services=cast(Any, MutatingServices()),
    )
    assert isinstance(result, ToolFailure)
    assert result.diagnostics is not None
    assert result.diagnostics.details["reason"] == "file_changed_during_recovery"
    assert "file changed while preparing long-line recovery" in result.error


def test_read_tool_rejects_recovery_artifact_that_does_not_match_the_read(tmp_path: Path) -> None:
    class CorruptServices(_DummyServices):
        def spill_artifact_stream(self, chunks, *, media_type="application/octet-stream"):
            reference = super().spill_artifact_stream(chunks, media_type=media_type)
            return ArtifactReference(sha256="0" * 64, bytes=reference.bytes, media_type=reference.media_type)

    plugin = create_plugin({})
    read_tool = next(t for t in plugin.tools if t.definition.name == "read")
    (tmp_path / "huge.txt").write_text("x" * (3 * 1024 * 1024) + "\n", encoding="utf-8")

    result = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "huge.txt"}),
        context=_context(tmp_path),
        services=cast(Any, CorruptServices()),
    )
    assert isinstance(result, ToolFailure)
    assert result.diagnostics is not None
    assert result.diagnostics.details["reason"] == "file_changed_during_recovery"
    assert "file changed while preparing long-line recovery" in result.error


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
    assert len(result.output.text.encode("utf-8")) <= TOOL_OUTPUT_BYTES
    assert result.output.bounds.truncated is True
    assert result.body.whole_file is False
    assert result.body.content_hash is None
    assert result.body.line_count == 2
    assert result.coverage == ()
    assert "line truncated" in result.output.text  # type: ignore[union-attr]
    assert "second line" in result.output.text  # type: ignore[union-attr]
    assert "artifact://" in result.output.text  # type: ignore[union-attr]
    reference = result.output.bounds.reference  # type: ignore[union-attr]
    assert isinstance(reference, ArtifactReference)
    assert reference.bytes == len(huge_line.encode("utf-8"))


def test_read_tool_clipped_page_with_more_lines_reports_known_line_count(tmp_path: Path) -> None:
    plugin = create_plugin({})
    read_tool = next(t for t in plugin.tools if t.definition.name == "read")
    content = ("x" * (3 * 1024 * 1024) + "\n") + "".join(f"line-{index:04d}\n" for index in range(3000))
    (tmp_path / "huge-many.txt").write_text(content, encoding="utf-8")

    result = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "huge-many.txt", "limit": 1000}),
        context=_context(tmp_path),
    )
    assert isinstance(result, ToolSuccess)
    assert isinstance(result.body, ReadResultBody)
    assert result.body.content_hash is None
    assert result.body.line_count == 3001
    assert result.body.next_offset == 1001
    assert result.body.truncated is True
    assert result.coverage == ()
    assert result.output.bounds.reference is not None  # type: ignore[union-attr]


def test_read_tool_page_boundary_probe_reports_next_offset(tmp_path: Path) -> None:
    from voidcode.capabilities.files.read_tool import _READ_CHUNK_BYTES

    plugin = create_plugin({})
    read_tool = next(t for t in plugin.tools if t.definition.name == "read")
    prefix = "".join("a" * 80 + "\n" for _ in range(99))
    boundary_line = "b" * (_READ_CHUNK_BYTES - len(prefix) - 1) + "\n"
    content = prefix + boundary_line + "next\n"
    assert len((prefix + boundary_line).encode("utf-8")) == _READ_CHUNK_BYTES
    (tmp_path / "boundary.txt").write_text(content, encoding="utf-8")

    result = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "boundary.txt", "limit": 100}),
        context=_context(tmp_path),
    )
    assert isinstance(result, ToolSuccess)
    assert isinstance(result.body, ReadResultBody)
    assert result.body.content_hash is None
    assert result.body.line_count is None
    assert result.body.next_offset == 101
    assert result.body.truncated is True
    assert len(result.body.lines) == 100


def test_read_tool_honors_cancellation_before_reading(tmp_path: Path) -> None:
    plugin = create_plugin({})
    read_tool = next(t for t in plugin.tools if t.definition.name == "read")
    result = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "cancelled.txt"}),
        context=_context(tmp_path, abort_signal=DummyAbortSignal(cancelled=True)),
    )
    assert isinstance(result, ToolFailure)
    assert "cancelled" in result.error
    assert result.execution == ConfirmedStop(cancellation_signalled=True)


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
    """A reference-backed raster read shows pixels, not file lines: no coverage."""
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
    from voidcode.sdk.resources import ResourcePage
    from voidcode.sdk.tools import TextOutput

    plugin = create_plugin({})
    read_tool = next(t for t in plugin.tools if t.definition.name == "read")

    def read_resource(request):  # type: ignore[no-untyped-def]
        assert request.uri.startswith("artifact://")
        return ResourcePage(output=TextOutput("uri line one\nuri line two"), body={"path": request.uri, "type": "artifact"})

    result = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "artifact://" + "a" * 64}),
        context=_context(tmp_path),
        services=cast(Any, SimpleNamespace(read_resource=read_resource)),
    )
    assert isinstance(result, ToolSuccess)
    assert "uri line one" in result.output.text  # type: ignore[union-attr]
    assert result.coverage == ()


def test_edit_batch_accepts_successive_content_hashes_without_read_history(tmp_path: Path) -> None:
    plugin = create_plugin({})
    edit_tool = next(t for t in plugin.tools if t.definition.name == "edit")
    target = tmp_path / "cycle.txt"
    v1 = "alpha\nbeta\n"
    target.write_text(v1, encoding="utf-8")
    v1_hash = hashlib.sha256(v1.encode("utf-8")).hexdigest()

    context_v1 = _context(tmp_path)
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
    context_v2 = _context(tmp_path)
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


def test_single_edit_accepts_successive_content_hashes_without_read_history(tmp_path: Path) -> None:
    plugin = create_plugin({})
    edit_tool = next(t for t in plugin.tools if t.definition.name == "edit")
    target = tmp_path / "single.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    v1_hash = hashlib.sha256(target.read_bytes()).hexdigest()

    first = edit_tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={"path": "single.txt", "oldString": "beta", "newString": "BETA", "expectedHash": v1_hash},
        ),
        context=_context(tmp_path),
    )
    assert isinstance(first, ToolSuccess)

    v2_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    assert v2_hash != v1_hash
    second = edit_tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={"path": "single.txt", "oldString": "BETA", "newString": "beta2", "expectedHash": v2_hash},
        ),
        context=_context(tmp_path),
    )
    assert isinstance(second, ToolSuccess)
    assert target.read_text(encoding="utf-8") == "alpha\nbeta2\n"


def test_edit_batch_without_read_history_uses_explicit_hash(tmp_path: Path) -> None:
    plugin = create_plugin({})
    edit_tool = next(t for t in plugin.tools if t.definition.name == "edit")
    target = tmp_path / "stale.txt"
    v1 = "alpha\nbeta\ngamma\n"
    target.write_text(v1, encoding="utf-8")
    v2 = "alpha\nbeta\nGAMMA\n"
    v2_hash = hashlib.sha256(v2.encode("utf-8")).hexdigest()
    target.write_text(v2, encoding="utf-8")

    context = _context(tmp_path)
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
    assert isinstance(result, ToolSuccess), result
    assert target.read_text(encoding="utf-8") == "alpha\nbeta\ngamma2\n"


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
    assert "cancelled before committing file" in result.error
    assert isinstance(result.execution, ConfirmedStop)
    assert not target.exists()


class _UriServices:
    """Scripted ``read_resource`` dispatcher backed by the real default readers."""

    def __init__(
        self,
        *,
        definitions: dict | None = None,
        events: tuple = (),
        child_events: tuple = (),
        refuse_child: Exception | None = None,
    ) -> None:
        from voidcode.capabilities.files.resource_readers import create_resource_readers

        self._definitions = definitions or {}
        self._events = events
        self._child_events = child_events
        self._refuse_child = refuse_child
        self.child_session_ids: list = []
        self._readers = {reader.scheme: reader.handler for reader in create_resource_readers()}

    def tool_definition(self, name: str):  # type: ignore[no-untyped-def]
        return self._definitions.get(name)

    def read_session_history(self, session_id: str, *, after_sequence: int = 0, offset: int = 0, limit: int = 100):  # type: ignore[no-untyped-def]
        self.child_session_ids.append(session_id)
        if self._refuse_child is not None:
            raise self._refuse_child
        from voidcode.core.history_renderer import render_history_page

        events = self._events if session_id == "session-test" else self._child_events
        return render_history_page(events, after_sequence=after_sequence, offset=offset)

    def read_resource(self, request):  # type: ignore[no-untyped-def]
        from voidcode.sdk.resources import ResourceReadCaller

        handler = self._readers.get(request.scheme)
        assert handler is not None
        caller = ResourceReadCaller(session_id="session-test", workspace=Path("."), revision_id="rev-test", run_id="run-test")
        return handler(request, caller, self)


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
        ToolCall(tool_name="read", arguments={"path": "tool://read"}),
        context=_context(tmp_path),
        services=cast(Any, _UriServices(definitions={"read": definition})),
    )
    assert isinstance(visible, ToolSuccess)
    assert "Read a file." in visible.output.text  # type: ignore[union-attr]
    assert visible.body.as_payload()["tool_name"] == "read"  # type: ignore[union-attr]

    hidden = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "tool://write"}),
        context=_context(tmp_path),
        services=cast(Any, _UriServices(definitions={"read": definition})),
    )
    assert isinstance(hidden, ToolFailure)
    assert "no such resource" in hidden.error
    assert hidden.output.text == hidden.error  # type: ignore[union-attr]


def test_read_transcript_returns_readable_pages_with_a_cursor_and_reports_core_refusal(tmp_path: Path) -> None:
    from voidcode.sdk.services import EventView

    plugin = create_plugin({})
    read_tool = next(t for t in plugin.tools if t.definition.name == "read")
    prompt = "PROMPT-START " + "x" * 50_000 + " PROMPT-END"
    views = (
        EventView(
            sequence=1,
            event_type="core.user_message",
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
        ToolCall(tool_name="read", arguments={"path": "transcript://session-test"}),
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

    next_uri = f"transcript://session-test?after_sequence={first['next_after_sequence']}&offset={first['next_offset']}"
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
            event_type="core.user_message",
            run_id="r",
            revision_id="v",
            source="user",
            actor="user",
            payload={"content": "CHILD-PROMPT"},
        ),
    )
    child_services = _UriServices(child_events=child_views)
    child = read_tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "transcript://child-test"}),
        context=_context(tmp_path),
        services=cast(Any, child_services),
    )
    assert isinstance(child, ToolSuccess)
    assert "CHILD-PROMPT" in child.output.text  # type: ignore[union-attr]
    assert child_services.child_session_ids == ["child-test"]

    with pytest.raises(PermissionError, match="own descendants"):
        read_tool.invoke(
            ToolCall(tool_name="read", arguments={"path": "transcript://sibling"}),
            context=_context(tmp_path),
            services=cast(Any, _UriServices(refuse_child=PermissionError("child events are readable only for this session's own descendants"))),
        )


class _VersionServices:
    """Scripted `workspace.document_version` answers for range edits."""

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
    return _context(
        workspace,
    )


def _range_tool():  # type: ignore[no-untyped-def]
    return next(t for t in create_plugin({}).tools if t.definition.name == "edit")


def test_range_explicit_utf8_coordinates_win_over_negotiated_utf16(tmp_path: Path) -> None:
    """A caller may address a non-BMP line in UTF-8 units while LSP negotiated UTF-16.

    "𝄞" is one UTF-32 char, two UTF-16 units, four UTF-8 bytes: replacing the note that
    follows it needs character 6 in UTF-8 coordinates but character 4 in UTF-16 ones.
    """
    tool = _range_tool()
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
            tool_name="edit",
            arguments={
                "mode": "range",
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
                        "version": 3,
                    }
                ],
            },
        ),
        context=_apply_context(tmp_path, target, content_hash),
        services=cast(Any, _VersionServices(document)),
    )
    assert isinstance(result, ToolSuccess), result
    assert target.read_text(encoding="utf-8") == "𝄞aB\ncd\n"
    assert f"SHA-256 content hash (music.txt): {hashlib.sha256(target.read_bytes()).hexdigest()}" in result.output.text  # type: ignore[union-attr]


def test_range_omitted_encoding_uses_negotiated_default(tmp_path: Path) -> None:
    """Without `positionEncoding`, the opened document's negotiated encoding applies."""
    tool = _range_tool()
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
            tool_name="edit",
            arguments={
                "mode": "range",
                "edits": [
                    {
                        "path": "music.txt",
                        "startLine": 1,
                        "startCharacter": 4,
                        "endLine": 1,
                        "endCharacter": 5,
                        "newText": "B",
                        "expectedHash": content_hash,
                        "version": 3,
                    }
                ],
            },
        ),
        context=_apply_context(tmp_path, target, content_hash),
        services=cast(Any, _VersionServices(document)),
    )
    assert isinstance(result, ToolSuccess), result
    assert target.read_text(encoding="utf-8") == "𝄞aB\ncd\n"


def test_range_matched_version_and_hash_only_succeed_with_correct_bytes(tmp_path: Path) -> None:
    tool = _range_tool()
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
            tool_name="edit",
            arguments={
                "mode": "range",
                "edits": [
                    {
                        "path": "target.txt",
                        "startLine": 1,
                        "startCharacter": 1,
                        "endLine": 1,
                        "endCharacter": 6,
                        "newText": "omega",
                        "expectedHash": first_hash,
                        "version": 1,
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
            tool_name="edit",
            arguments={
                "mode": "range",
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


def test_range_accepts_successive_content_hashes_without_read_history(tmp_path: Path) -> None:
    tool = _range_tool()
    target = tmp_path / "uni.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    first_hash = hashlib.sha256(target.read_bytes()).hexdigest()

    first = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={
                "mode": "range",
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
    both_versions = _context(tmp_path)
    second = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={
                "mode": "range",
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


def test_range_edits_multiple_files_and_reports_each_new_hash(tmp_path: Path) -> None:
    tool = _range_tool()
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("alpha\n", encoding="utf-8")
    second.write_text("beta\n", encoding="utf-8")
    first_hash = hashlib.sha256(first.read_bytes()).hexdigest()
    second_hash = hashlib.sha256(second.read_bytes()).hexdigest()

    result = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={
                "mode": "range",
                "edits": [
                    {
                        "path": "first.txt",
                        "startLine": 1,
                        "startCharacter": 1,
                        "endLine": 1,
                        "endCharacter": 6,
                        "newText": "ALPHA",
                        "expectedHash": first_hash,
                    },
                    {
                        "path": "second.txt",
                        "startLine": 1,
                        "startCharacter": 1,
                        "endLine": 1,
                        "endCharacter": 5,
                        "newText": "BETA",
                        "expectedHash": second_hash,
                    },
                ],
            },
        ),
        context=_context(tmp_path),
        services=cast(Any, _VersionServices(unanswered=True)),
    )
    assert isinstance(result, ToolSuccess), result
    assert first.read_text(encoding="utf-8") == "ALPHA\n"
    assert second.read_text(encoding="utf-8") == "BETA\n"
    assert hashlib.sha256(first.read_bytes()).hexdigest() in result.output.text  # type: ignore[union-attr]
    assert hashlib.sha256(second.read_bytes()).hexdigest() in result.output.text  # type: ignore[union-attr]


def test_range_applies_multiple_non_overlapping_edits_to_one_file(tmp_path: Path) -> None:
    tool = _range_tool()
    target = tmp_path / "multi.txt"
    target.write_text("alpha beta gamma\n", encoding="utf-8")
    original_hash = hashlib.sha256(target.read_bytes()).hexdigest()

    result = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={
                "mode": "range",
                "edits": [
                    {
                        "path": "multi.txt",
                        "startLine": 1,
                        "startCharacter": 1,
                        "endLine": 1,
                        "endCharacter": 6,
                        "newText": "ALPHA",
                        "expectedHash": original_hash,
                    },
                    {
                        "path": "multi.txt",
                        "startLine": 1,
                        "startCharacter": 12,
                        "endLine": 1,
                        "endCharacter": 17,
                        "newText": "GAMMA",
                        "expectedHash": original_hash,
                    },
                ],
            },
        ),
        context=_context(tmp_path),
        services=cast(Any, _VersionServices(unanswered=True)),
    )
    assert isinstance(result, ToolSuccess), result
    assert target.read_text(encoding="utf-8") == "ALPHA beta GAMMA\n"


def test_range_rejects_overlapping_edits_before_writing(tmp_path: Path) -> None:
    tool = _range_tool()
    target = tmp_path / "overlap.txt"
    target.write_text("alpha beta\n", encoding="utf-8")
    original_hash = hashlib.sha256(target.read_bytes()).hexdigest()

    result = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={
                "mode": "range",
                "edits": [
                    {
                        "path": "overlap.txt",
                        "startLine": 1,
                        "startCharacter": 1,
                        "endLine": 1,
                        "endCharacter": 7,
                        "newText": "ALPHA ",
                        "expectedHash": original_hash,
                    },
                    {
                        "path": "overlap.txt",
                        "startLine": 1,
                        "startCharacter": 6,
                        "endLine": 1,
                        "endCharacter": 11,
                        "newText": "BETA",
                        "expectedHash": original_hash,
                    },
                ],
            },
        ),
        context=_context(tmp_path),
        services=cast(Any, _VersionServices(unanswered=True)),
    )
    assert isinstance(result, ToolFailure)
    assert result.diagnostics is not None and result.diagnostics.details["reason"] == "overlapping_edits"
    assert target.read_text(encoding="utf-8") == "alpha beta\n"


def test_range_preflight_refuses_external_path_before_any_commit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    internal = workspace / "keep.txt"
    internal.write_text("alpha\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    tool = _range_tool()
    result = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={
                "mode": "range",
                "edits": [
                    {
                        "path": "keep.txt",
                        "startLine": 1,
                        "startCharacter": 1,
                        "endLine": 1,
                        "endCharacter": 6,
                        "newText": "ALPHA",
                        "expectedHash": hashlib.sha256(internal.read_bytes()).hexdigest(),
                    },
                    {
                        "path": str(outside),
                        "startLine": 1,
                        "startCharacter": 1,
                        "endLine": 1,
                        "endCharacter": 2,
                        "newText": "S",
                        "expectedHash": hashlib.sha256(outside.read_bytes()).hexdigest(),
                    },
                ],
            },
        ),
        context=_context(workspace),
        services=cast(Any, _VersionServices(unanswered=True)),
    )
    assert isinstance(result, ToolFailure)
    assert result.diagnostics is not None and result.diagnostics.details["reason"] == "outside_workspace"
    assert result.changes == ()
    assert internal.read_text(encoding="utf-8") == "alpha\n"
    assert outside.read_text(encoding="utf-8") == "secret\n"


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


@pytest.mark.parametrize("tool_name", ["glob", "grep"])
def test_searches_do_not_prematurely_truncate_on_output_bytes(tmp_path: Path, tool_name: str) -> None:
    tool = next(t for t in create_plugin({}).tools if t.definition.name == tool_name)
    if tool_name == "grep":
        long_line = "needle " + ("x" * 1000)
        lines = [f"{long_line} line {i}" for i in range(80)]
        (tmp_path / "large.txt").write_text("\n".join(lines), encoding="utf-8")
        result = tool.invoke(
            ToolCall(tool_name="grep", arguments={"pattern": "needle", "limit": 80}),
            context=_context(tmp_path),
        )
        assert isinstance(result, ToolSuccess)
        assert len(result.body.matches) == 80  # type: ignore[union-attr]
        assert result.body.truncated is False  # type: ignore[union-attr]
        assert "[TRUNCATED]" not in result.output.text  # type: ignore[union-attr]
    else:
        nested_dir = tmp_path / ("d1_" + "a" * 100) / ("d2_" + "b" * 100) / ("d3_" + "c" * 100) / ("d4_" + "d" * 100)
        nested_dir.mkdir(parents=True)
        for i in range(100):
            (nested_dir / f"file_{i:03d}.txt").write_text("data")
        result = tool.invoke(
            ToolCall(tool_name="glob", arguments={"pattern": "**/*.txt", "limit": 100}),
            context=_context(tmp_path),
        )
        assert isinstance(result, ToolSuccess)
        assert len(result.body.matches) == 100  # type: ignore[union-attr]
        assert result.body.truncated is False  # type: ignore[union-attr]


@pytest.mark.parametrize("operation", ["single", "batch"])
@pytest.mark.parametrize("stale", [False, True])
def test_edit_without_read_history_keeps_hash_checks(tmp_path: Path, operation: str, stale: bool) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    digest = "0" * 64 if stale else hashlib.sha256(target.read_bytes()).hexdigest()
    if operation == "single":
        arguments = {"path": target.name, "oldString": "beta", "newString": "BETA", "expectedHash": digest}
    elif operation == "batch":
        arguments = {
            "path": target.name,
            "edits": [{"oldString": "beta", "newString": "B"}, {"oldString": "B", "newString": "BETA"}],
            "expectedHash": digest,
        }
    tool = next(t for t in create_plugin({}).tools if t.definition.name == "edit")
    result = tool.invoke(ToolCall(tool_name="edit", arguments=arguments), context=_context(tmp_path))
    if stale:
        assert isinstance(result, ToolFailure), result
        assert result.diagnostics is not None
        assert result.diagnostics.details["reason"] == "content_hash_mismatch"
        assert target.read_text() == "alpha\nbeta\n"
    else:
        assert isinstance(result, ToolSuccess), result
        assert target.read_text() == "alpha\nBETA\n"


def test_batch_failure_keeps_file_unchanged_without_read_history(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    tool = next(t for t in create_plugin({}).tools if t.definition.name == "edit")
    result = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={
                "path": target.name,
                "expectedHash": hashlib.sha256(target.read_bytes()).hexdigest(),
                "edits": [{"oldString": "beta", "newString": "BETA"}, {"oldString": "missing", "newString": "value"}],
            },
        ),
        context=_context(tmp_path),
    )
    assert isinstance(result, ToolFailure)
    assert target.read_text() == "alpha\nbeta\n"


@pytest.mark.parametrize("failure", ["chmod", "replace", "interrupt"])
def test_write_failure_preserves_original_and_removes_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    from voidcode.capabilities.files import write_tool

    target = tmp_path / "script.sh"
    target.write_text("original\n", encoding="utf-8")
    target.chmod(0o755)
    expected_hash = hashlib.sha256(target.read_bytes()).hexdigest()

    def fail(*args: object, **kwargs: object) -> None:
        if failure == "interrupt":
            raise KeyboardInterrupt
        raise OSError("injected write failure")

    monkeypatch.setattr(write_tool.os, {"chmod": "chmod", "replace": "replace", "interrupt": "fsync"}[failure], fail)
    tool = next(t for t in create_plugin({}).tools if t.definition.name == "write")
    call = ToolCall(tool_name="write", arguments={"path": target.name, "content": "updated\n", "expectedHash": expected_hash})
    if failure == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            tool.invoke(call, context=_context(tmp_path))
    else:
        result = tool.invoke(call, context=_context(tmp_path))
        assert isinstance(result, ToolFailure)
        assert "injected write failure" in result.error
    assert target.read_text() == "original\n"
    assert target.stat().st_mode & 0o777 == 0o755
    assert list(tmp_path.glob(".voidcode_write_*")) == []


@pytest.mark.parametrize(
    "invalid_fields",
    [{"startLine": True}, {"startCharacter": "1"}, {"version": False}, {"unexpected": "ignored-before"}],
)
def test_range_rejects_invalid_batch_before_writing(tmp_path: Path, invalid_fields: dict) -> None:
    from voidcode.capabilities.files.edit_tool import EditTool

    target = tmp_path / "target.txt"
    target.write_text("original", encoding="utf-8")
    edit = {
        "path": "target.txt",
        "startLine": 1,
        "startCharacter": 1,
        "endLine": 1,
        "endCharacter": 2,
        "newText": "X",
        "expectedHash": hashlib.sha256(target.read_bytes()).hexdigest(),
    }
    result = EditTool().invoke(
        ToolCall(tool_name="edit", arguments={"mode": "range", "edits": [edit, {**edit, **invalid_fields}]}),
        context=_context(tmp_path),
        services=cast(Any, _VersionServices(unanswered=True)),
    )
    assert isinstance(result, ToolFailure)
    assert result.diagnostics is not None and result.diagnostics.kind == "tool_input_validation"
    assert target.read_text(encoding="utf-8") == "original"


def test_range_missing_hash_rejects_frozen_arguments(tmp_path: Path) -> None:
    from voidcode.capabilities.files.edit_tool import EditTool

    result = EditTool().invoke(
        ToolCall(tool_name="edit", arguments={"mode": "range", "edits": [{"path": "target.txt"}]}),
        context=_context(tmp_path),
        services=_SERVICES,
    )
    assert isinstance(result, ToolFailure)
    assert result.diagnostics is not None and result.diagnostics.kind == "tool_input_validation"
    validation_errors = result.diagnostics.details["errors"]
    assert isinstance(validation_errors, tuple)
    assert any("expectedHash" in str(item) for item in validation_errors)


@pytest.mark.parametrize("failure", ["replace", "concurrent_change"])
def test_range_failed_commit_preserves_target_and_cleans_temporary_file(tmp_path: Path, monkeypatch, failure: str) -> None:
    from voidcode.capabilities.files import range_edit
    from voidcode.capabilities.files.edit_tool import EditTool

    target = tmp_path / "target.txt"
    original = b"original\r\n"
    target.write_bytes(original)
    target.chmod(0o750)

    original_replace = range_edit.replace_file

    def fail_replace(path, content, *, expected_content):
        if failure == "concurrent_change":
            target.write_bytes(b"concurrent writer\r\n")
        else:
            raise OSError("injected replace failure")
        return original_replace(path, content, expected_content=expected_content)

    class Services(_VersionServices):
        def query(self, name, arguments=None):
            return None

    monkeypatch.setattr(range_edit, "replace_file", fail_replace)
    result = EditTool().invoke(
        ToolCall(
            tool_name="edit",
            arguments={
                "mode": "range",
                "edits": [
                    {
                        "path": "target.txt",
                        "startLine": 1,
                        "startCharacter": 1,
                        "endLine": 1,
                        "endCharacter": 2,
                        "newText": "X",
                        "expectedHash": hashlib.sha256(original).hexdigest(),
                    }
                ],
            },
        ),
        context=_context(tmp_path),
        services=cast(Any, Services()),
    )
    assert isinstance(result, ToolFailure)
    assert "uncommitted remaining" in result.error
    assert result.body.paths == ()
    assert result.changes == ()
    assert target.read_bytes() == (original if failure == "replace" else b"concurrent writer\r\n")
    assert target.stat().st_mode & 0o777 == 0o750
    assert sorted(p.name for p in tmp_path.iterdir()) == ["target.txt"]


def test_range_reports_applied_prefix_when_a_later_file_fails(tmp_path: Path, monkeypatch) -> None:
    from voidcode.capabilities.files import range_edit
    from voidcode.capabilities.files.edit_tool import EditTool

    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("alpha\n", encoding="utf-8")
    second.write_text("beta\n", encoding="utf-8")
    original_replace = range_edit.replace_file

    def fail_second(path, content, *, expected_content):
        if path == second:
            raise OSError("injected second-file failure")
        return original_replace(path, content, expected_content=expected_content)

    monkeypatch.setattr(range_edit, "replace_file", fail_second)
    result = EditTool().invoke(
        ToolCall(
            tool_name="edit",
            arguments={
                "mode": "range",
                "edits": [
                    {
                        "path": "first.txt",
                        "startLine": 1,
                        "startCharacter": 1,
                        "endLine": 1,
                        "endCharacter": 6,
                        "newText": "ALPHA",
                        "expectedHash": hashlib.sha256(first.read_bytes()).hexdigest(),
                    },
                    {
                        "path": "second.txt",
                        "startLine": 1,
                        "startCharacter": 1,
                        "endLine": 1,
                        "endCharacter": 5,
                        "newText": "BETA",
                        "expectedHash": hashlib.sha256(second.read_bytes()).hexdigest(),
                    },
                ],
            },
        ),
        context=_context(tmp_path),
        services=cast(Any, _VersionServices(unanswered=True)),
    )
    assert isinstance(result, ToolFailure)
    assert result.body.paths == ("first.txt",)
    assert [change.path for change in result.changes] == ["first.txt"]
    assert first.read_text(encoding="utf-8") == "ALPHA\n"
    assert second.read_text(encoding="utf-8") == "beta\n"
    assert "Applied prefix: ['first.txt SHA-256 " in result.error
    assert hashlib.sha256(first.read_bytes()).hexdigest() in result.error


def test_range_preserves_executable_mode_and_crlf(tmp_path: Path) -> None:
    from voidcode.capabilities.files.edit_tool import EditTool

    target = tmp_path / "target.txt"
    target.write_bytes(b"original\r\n")
    target.chmod(0o750)
    result = EditTool().invoke(
        ToolCall(
            tool_name="edit",
            arguments={
                "mode": "range",
                "edits": [
                    {
                        "path": "target.txt",
                        "startLine": 1,
                        "startCharacter": 1,
                        "endLine": 1,
                        "endCharacter": 2,
                        "newText": "X\n",
                        "expectedHash": hashlib.sha256(target.read_bytes()).hexdigest(),
                    }
                ],
            },
        ),
        context=_context(tmp_path),
        services=cast(Any, _VersionServices(unanswered=True)),
    )
    assert isinstance(result, ToolSuccess)
    assert target.read_bytes() == b"X\r\nriginal\r\n"
    assert target.stat().st_mode & 0o777 == 0o750
    assert sorted(p.name for p in tmp_path.iterdir()) == ["target.txt"]
    assert f"SHA-256 content hash (target.txt): {hashlib.sha256(b'X\r\nriginal\r\n').hexdigest()}" in result.output.text  # type: ignore[union-attr]


def test_range_preserves_lone_cr_and_reencodes_new_text(tmp_path: Path) -> None:
    from voidcode.capabilities.files.edit_tool import EditTool

    target = tmp_path / "cr.txt"
    original = b"one\rtwo\rthree\r"
    target.write_bytes(original)
    result = EditTool().invoke(
        ToolCall(
            tool_name="edit",
            arguments={
                "mode": "range",
                "edits": [
                    {
                        "path": "cr.txt",
                        "startLine": 2,
                        "startCharacter": 1,
                        "endLine": 2,
                        "endCharacter": 4,
                        "newText": "TWO\nX",
                        "expectedHash": hashlib.sha256(original).hexdigest(),
                    }
                ],
            },
        ),
        context=_context(tmp_path),
        services=cast(Any, _VersionServices(unanswered=True)),
    )
    assert isinstance(result, ToolSuccess), result
    assert target.read_bytes() == b"one\rTWO\rX\rthree\r"


def test_range_preserves_missing_final_newline(tmp_path: Path) -> None:
    from voidcode.capabilities.files.edit_tool import EditTool

    target = tmp_path / "no-final.txt"
    original = b"one\ntwo"
    target.write_bytes(original)
    result = EditTool().invoke(
        ToolCall(
            tool_name="edit",
            arguments={
                "mode": "range",
                "edits": [
                    {
                        "path": "no-final.txt",
                        "startLine": 2,
                        "startCharacter": 1,
                        "endLine": 2,
                        "endCharacter": 4,
                        "newText": "TWO",
                        "expectedHash": hashlib.sha256(original).hexdigest(),
                    }
                ],
            },
        ),
        context=_context(tmp_path),
        services=cast(Any, _VersionServices(unanswered=True)),
    )
    assert isinstance(result, ToolSuccess), result
    assert target.read_bytes() == b"one\nTWO"


def test_range_rejects_mixed_line_endings_without_writing(tmp_path: Path) -> None:
    from voidcode.capabilities.files.edit_tool import EditTool

    target = tmp_path / "mixed.txt"
    original = b"one\r\ntwo\n"
    target.write_bytes(original)
    result = EditTool().invoke(
        ToolCall(
            tool_name="edit",
            arguments={
                "mode": "range",
                "edits": [
                    {
                        "path": "mixed.txt",
                        "startLine": 2,
                        "startCharacter": 1,
                        "endLine": 2,
                        "endCharacter": 4,
                        "newText": "TWO",
                        "expectedHash": hashlib.sha256(original).hexdigest(),
                    }
                ],
            },
        ),
        context=_context(tmp_path),
        services=cast(Any, _VersionServices(unanswered=True)),
    )
    assert isinstance(result, ToolFailure)
    assert result.diagnostics is not None and result.diagnostics.details["reason"] == "mixed_line_endings"
    assert target.read_bytes() == original


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("failure", [None, "replace", "concurrent_change"])
def test_edit_commit_preserves_bytes_permissions_and_concurrent_changes(tmp_path: Path, monkeypatch, batch: bool, failure: str | None) -> None:
    from voidcode.capabilities.files import edit_tool

    target = tmp_path / "target.txt"
    original = b"original\r\n"
    target.write_bytes(original)
    target.chmod(0o750)
    arguments = {"path": "target.txt", "expectedHash": hashlib.sha256(original).hexdigest()}
    edit = {"oldString": "original", "newString": "changed"}
    arguments.update({"edits": [edit]} if batch else edit)
    original_replace = edit_tool.replace_file

    def fail_replace(path, content, *, expected_content):
        if failure == "concurrent_change":
            target.write_bytes(b"concurrent writer\r\n")
        else:
            raise OSError("injected replace failure")
        return original_replace(path, content, expected_content=expected_content)

    if failure is not None:
        monkeypatch.setattr(edit_tool, "replace_file", fail_replace)

    tool = edit_tool.EditTool()
    changed_hash = hashlib.sha256(b"changed\r\n").hexdigest()
    if failure is None:
        result = tool.invoke(ToolCall(tool_name="edit", arguments=arguments), context=_context(tmp_path), services=_SERVICES)
        assert isinstance(result, ToolSuccess)
        assert result.body.content_hash == changed_hash
        assert f"SHA-256 content hash: {changed_hash}" in result.output.text  # type: ignore[union-attr]
    else:
        with pytest.raises(OSError):
            tool.invoke(ToolCall(tool_name="edit", arguments=arguments), context=_context(tmp_path), services=_SERVICES)

    expected = b"changed\r\n" if failure is None else original if failure == "replace" else b"concurrent writer\r\n"
    assert target.read_bytes() == expected
    assert target.stat().st_mode & 0o777 == 0o750
    assert sorted(p.name for p in tmp_path.iterdir()) == ["target.txt"]


@pytest.mark.parametrize("batch", [False, True])
def test_edit_replace_all_preserves_single_and_batch_result_counts(tmp_path: Path, batch: bool) -> None:
    from voidcode.capabilities.files.edit_tool import EditTool

    target = tmp_path / "target.txt"
    target.write_text("old old old\n", encoding="utf-8")
    edit = {"oldString": "old", "newString": "new", "replaceAll": True}
    arguments = {"path": "target.txt", "expectedHash": hashlib.sha256(target.read_bytes()).hexdigest()}
    arguments.update({"edits": [edit]} if batch else edit)
    result = EditTool().invoke(ToolCall(tool_name="edit", arguments=arguments), context=_context(tmp_path), services=_SERVICES)
    assert isinstance(result, ToolSuccess)
    assert target.read_text(encoding="utf-8") == "new new new\n"
    assert result.body.match_count == 3


def test_edit_matches_lf_old_string_in_crlf_file_and_preserves_crlf(tmp_path: Path) -> None:
    from voidcode.capabilities.files.edit_tool import EditTool

    target = tmp_path / "crlf.txt"
    original = b"one\r\ntwo\r\n"
    target.write_bytes(original)
    result = EditTool().invoke(
        ToolCall(
            tool_name="edit",
            arguments={"path": "crlf.txt", "oldString": "two", "newString": "TWO", "expectedHash": hashlib.sha256(original).hexdigest()},
        ),
        context=_context(tmp_path),
        services=_SERVICES,
    )
    assert isinstance(result, ToolSuccess), result
    assert target.read_bytes() == b"one\r\nTWO\r\n"
    assert hashlib.sha256(target.read_bytes()).hexdigest() in result.output.text  # type: ignore[union-attr]


def test_edit_new_text_uses_the_files_crlf_ending(tmp_path: Path) -> None:
    from voidcode.capabilities.files.edit_tool import EditTool

    target = tmp_path / "crlf.txt"
    original = b"one\r\ntwo\r\n"
    target.write_bytes(original)
    result = EditTool().invoke(
        ToolCall(
            tool_name="edit",
            arguments={"path": "crlf.txt", "oldString": "two\n", "newString": "new\nline\n", "expectedHash": hashlib.sha256(original).hexdigest()},
        ),
        context=_context(tmp_path),
        services=_SERVICES,
    )
    assert isinstance(result, ToolSuccess), result
    assert target.read_bytes() == b"one\r\nnew\r\nline\r\n"


def test_edit_preserves_lone_cr_line_endings(tmp_path: Path) -> None:
    from voidcode.capabilities.files.edit_tool import EditTool

    target = tmp_path / "cr.txt"
    original = b"one\rtwo\rthree\r"
    target.write_bytes(original)
    result = EditTool().invoke(
        ToolCall(
            tool_name="edit",
            arguments={"path": "cr.txt", "oldString": "two\n", "newString": "TWO\n", "expectedHash": hashlib.sha256(original).hexdigest()},
        ),
        context=_context(tmp_path),
        services=_SERVICES,
    )
    assert isinstance(result, ToolSuccess), result
    assert target.read_bytes() == b"one\rTWO\rthree\r"


def test_edit_preserves_pure_lf_bytes(tmp_path: Path) -> None:
    from voidcode.capabilities.files.edit_tool import EditTool

    target = tmp_path / "lf.txt"
    original = b"one\ntwo\n"
    target.write_bytes(original)
    result = EditTool().invoke(
        ToolCall(
            tool_name="edit",
            arguments={"path": "lf.txt", "oldString": "two", "newString": "TWO", "expectedHash": hashlib.sha256(original).hexdigest()},
        ),
        context=_context(tmp_path),
        services=_SERVICES,
    )
    assert isinstance(result, ToolSuccess), result
    assert target.read_bytes() == b"one\nTWO\n"


def test_edit_rejects_mixed_line_endings_without_writing(tmp_path: Path) -> None:
    from voidcode.capabilities.files.edit_tool import EditTool

    target = tmp_path / "mixed.txt"
    original = b"one\r\ntwo\n"
    target.write_bytes(original)
    result = EditTool().invoke(
        ToolCall(
            tool_name="edit",
            arguments={"path": "mixed.txt", "oldString": "two", "newString": "TWO", "expectedHash": hashlib.sha256(original).hexdigest()},
        ),
        context=_context(tmp_path),
        services=_SERVICES,
    )
    assert isinstance(result, ToolFailure)
    assert result.diagnostics is not None and result.diagnostics.details["reason"] == "mixed_line_endings"
    assert target.read_bytes() == original


def test_write_rejects_external_target_with_typed_failure(tmp_path: Path) -> None:
    from voidcode.capabilities.files.write_tool import WriteTool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    result = WriteTool().invoke(
        ToolCall(tool_name="write", arguments={"path": str(outside), "content": "nope"}),
        context=_context(workspace),
        services=_SERVICES,
    )
    assert isinstance(result, ToolFailure)
    assert result.diagnostics is not None and result.diagnostics.details["reason"] == "outside_workspace"
    assert not outside.exists()


def test_write_distinguishes_invalid_path_from_outside_workspace(tmp_path: Path) -> None:
    from voidcode.capabilities.files.write_tool import WriteTool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = WriteTool().invoke(
        ToolCall(tool_name="write", arguments={"path": "bad\x00name", "content": "x"}),
        context=_context(workspace),
        services=_SERVICES,
    )
    assert isinstance(result, ToolFailure)
    assert result.diagnostics is not None
    assert result.diagnostics.details["reason"] == "invalid_path"


def test_edit_rejects_external_target_with_typed_failure(tmp_path: Path) -> None:
    from voidcode.capabilities.files.edit_tool import EditTool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    result = EditTool().invoke(
        ToolCall(
            tool_name="edit",
            arguments={
                "path": str(outside),
                "oldString": "secret",
                "newString": "changed",
                "expectedHash": hashlib.sha256(outside.read_bytes()).hexdigest(),
            },
        ),
        context=_context(workspace),
        services=_SERVICES,
    )
    assert isinstance(result, ToolFailure)
    assert result.diagnostics is not None and result.diagnostics.details["reason"] == "outside_workspace"
    assert outside.read_text(encoding="utf-8") == "secret\n"


def test_edit_schema_excludes_patch_and_strictly_requires_replacements(tmp_path: Path) -> None:
    from voidcode.capabilities.files.edit_tool import EditTool

    tool = EditTool()
    schema = tool.definition.input_schema
    defs = schema.get("$defs", {})
    assert "_PatchEdit" not in defs
    assert "patch" not in schema.get("properties", {})
    assert set(defs.keys()) == {"_SingleEdit", "_BatchEdit", "_Replacement", "_RangeEditArgs", "_RangeTextEdit"}

    result = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={"patch": "*** Begin Patch\n*** End Patch\n"},
        ),
        context=_context(tmp_path),
        services=_SERVICES,
    )
    assert isinstance(result, ToolFailure)
    assert result.diagnostics is not None and result.diagnostics.kind == "tool_input_validation"


def test_shared_primitives_path_and_diff(tmp_path: Path) -> None:
    from voidcode.capabilities.files._common import (
        encode_line_endings,
        line_ending_kinds,
        make_file_change,
        normalize_line_endings,
        resolve_workspace_path,
        summarize_diff,
        to_display_path,
    )

    sub = tmp_path / "sub"
    sub.mkdir()
    target = sub / "file.txt"
    resolved, display = resolve_workspace_path(tmp_path, "sub/file.txt")
    assert resolved == target
    assert display == "sub/file.txt"
    assert to_display_path(target, tmp_path) == "sub/file.txt"

    outside = tmp_path.parent / "outside.txt"
    resolved_out, display_out = resolve_workspace_path(tmp_path, outside)
    assert resolved_out == outside.resolve()
    assert display_out == outside.resolve().as_posix()
    assert to_display_path(outside, tmp_path) == outside.resolve().as_posix()

    with pytest.raises(ValueError, match="Path escapes workspace"):
        resolve_workspace_path(tmp_path, outside, allow_outside_workspace=False)

    diff, additions, deletions = summarize_diff(
        path="test.py",
        before="def foo():\n    return 1\n",
        after="def foo():\n    return 2\n",
    )
    assert additions == 1
    assert deletions == 1
    assert "-    return 1" in diff
    assert "+    return 2" in diff

    change = make_file_change("sub/file.txt", "update", content_hash="abc")
    assert change.path == "sub/file.txt"
    assert change.operation == "update"
    assert change.content_hash == "abc"

    assert line_ending_kinds("line1\r\nline2") == frozenset({"\r\n"})
    assert line_ending_kinds("line1\nline2") == frozenset({"\n"})
    assert line_ending_kinds("line1\rline2") == frozenset({"\r"})
    assert line_ending_kinds("line1\r\nline2\n") == frozenset({"\r\n", "\n"})
    assert normalize_line_endings("a\r\nb\rc\n") == "a\nb\nc\n"
    assert encode_line_endings("a\nb\n", "\r\n") == "a\r\nb\r\n"
    assert encode_line_endings("a\nb\n", "\r") == "a\rb\r"
    assert encode_line_endings("a\nb\n", None) == "a\nb\n"
