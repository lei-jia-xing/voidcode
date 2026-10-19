"""Read coverage: codec roundtrip, model projection bound, and reducer grants."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from voidcode.kernel.coverage import (
    MODEL_PROJECTION_MARKER,
    ReadCoverage,
    canonical_path,
    model_result_text,
    read_coverage,
)
from voidcode.kernel.storage.store import StoredEvent
from voidcode.sdk.codec import decode_context, decode_tool_result, encode_context, encode_tool_result
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.limits import TOOL_OUTPUT_BYTES
from voidcode.sdk.tools import OpaqueToolBody, OutputBounds, ReadObservation, TextOutput, ToolFailure, ToolResult, ToolSuccess

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
SESSION = "session-1"


def _read_success(
    path: str,
    digest: str,
    lines: tuple[int, ...],
    line_count: int,
    *,
    whole_file: bool = False,
    clipped: bool = False,
    text: str = "file body",
    bounds: OutputBounds | None = None,
) -> ToolSuccess[OpaqueToolBody]:
    observation = ReadObservation(path, digest, lines, line_count, whole_file, clipped)
    return ToolSuccess("read", TextOutput(text, bounds=bounds or OutputBounds()), None, (observation,))


def _event(
    result: ToolResult,
    sequence: int = 1,
    *,
    session_id: str = SESSION,
    event_type: str = "kernel.tool_completed",
    deferred: bool = False,
) -> StoredEvent:
    payload: dict[str, object] = {
        "batch_sequence": 1,
        "call": {"tool_name": "read"},
        "result": encode_tool_result(result),
    }
    if deferred:
        payload["deferred"] = True
        payload["request_id"] = f"req-{sequence}"
    return StoredEvent(session_id, sequence, "run-1", event_type, "tool", "model", "rev-1", payload)


def _resolved(workspace: Path, name: str) -> str:
    return str((workspace / name).resolve())


# --- context codec -------------------------------------------------------------------------


def test_context_codec_roundtrips_every_field_json_safely() -> None:
    context = ToolContext(
        workspace=Path("/workspace"),
        session_id="session-1",
        run_id="run-1",
        invocation_id="invocation-1",
        revision_id="revision-1",
        actor="agent",
        capabilities=frozenset({"read", "write"}),
        logical_turn="turn-2",
        invocation_depth=2,
        read_paths=frozenset({"/workspace/a.txt"}),
        read_lines={("/workspace/a.txt", DIGEST_A): frozenset({1, 3})},
        read_whole_files=frozenset({("/workspace/b.txt", DIGEST_B)}),
        read_hash=DIGEST_A,
        emit_tool_progress=lambda payload: None,
    )
    encoded = encode_context(context)
    assert json.loads(json.dumps(encoded)) == encoded
    assert "emit_tool_progress" not in encoded
    decoded = decode_context(json.loads(json.dumps(encoded)))
    assert decoded == context
    assert decoded.emit_tool_progress is None
    assert decoded.read_lines == {("/workspace/a.txt", DIGEST_A): frozenset({1, 3})}
    assert decoded.read_whole_files == frozenset({("/workspace/b.txt", DIGEST_B)})


def test_context_codec_refuses_unknown_keys_and_missing_identity() -> None:
    context = ToolContext(workspace=Path("/workspace"), session_id="s", run_id="r", invocation_id="i", revision_id="rev", actor="a")
    encoded = encode_context(context)
    assert encoded["invocation_depth"] == 0
    assert encoded["read_lines"] == []
    assert encoded["read_hash"] is None
    with pytest.raises(ValueError):
        decode_context({**encoded, "read_mode": "product-specific"})
    with pytest.raises(ValueError):
        decode_context({key: value for key, value in encoded.items() if key != "workspace"})


# --- result codec --------------------------------------------------------------------------


def test_tool_result_coverage_roundtrips_and_rejects_invalid_claims() -> None:
    result = _read_success("a.txt", DIGEST_A, (2, 3), 5, whole_file=True)
    decoded = decode_tool_result(encode_tool_result(result))
    assert isinstance(decoded, ToolSuccess)
    assert decoded == result
    assert decoded.coverage == result.coverage

    observations = cast("list[dict[str, object]]", encode_tool_result(result)["coverage"])

    bad_hash = encode_tool_result(result)
    cast("list[dict[str, object]]", bad_hash["coverage"])[0]["content_hash"] = "NOT-A-DIGEST"
    with pytest.raises(ValueError):
        decode_tool_result(bad_hash)

    out_of_range = encode_tool_result(result)
    cast("list[dict[str, object]]", out_of_range["coverage"])[0]["lines"] = [7]
    with pytest.raises(ValueError):
        decode_tool_result(out_of_range)

    failure = encode_tool_result(ToolFailure("read", "boom"))
    failure["coverage"] = observations
    with pytest.raises(ValueError):
        decode_tool_result(failure)


# --- model projection ----------------------------------------------------------------------


def test_model_result_text_bounds_and_marks_the_projection() -> None:
    assert model_result_text(_read_success("a.txt", DIGEST_A, (1,), 1, text="short")) == ("short", False)
    text, clipped = model_result_text(_read_success("a.txt", DIGEST_A, (1,), 1, text="x" * (TOOL_OUTPUT_BYTES + 10)))
    assert clipped is True
    assert text is not None and text.endswith(MODEL_PROJECTION_MARKER)
    assert len(text.encode("utf-8")) < TOOL_OUTPUT_BYTES + len(MODEL_PROJECTION_MARKER.encode("utf-8")) + 1
    assert model_result_text(ToolSuccess("read", TextOutput(""), OpaqueToolBody({"a": 1}))) == ('{"a": 1}\n', False)
    assert model_result_text(ToolFailure("read", "boom", TextOutput("partial"))) == ("boom\npartial", False)


# --- reducer -------------------------------------------------------------------------------


def test_same_hash_windows_union_and_name_one_version(tmp_path: Path) -> None:
    events = [
        _event(_read_success("a.txt", DIGEST_A, (1, 2), 5), 1),
        _event(_read_success("a.txt", DIGEST_A, (3,), 5), 2),
    ]
    coverage = read_coverage(events, workspace=tmp_path, session_id=SESSION, arguments={"path": "a.txt"}, path_argument_keys=("path",))
    assert coverage.read_paths == frozenset({_resolved(tmp_path, "a.txt")})
    assert coverage.read_lines == {(_resolved(tmp_path, "a.txt"), DIGEST_A): frozenset({1, 2, 3})}
    assert coverage.read_whole_files == frozenset()
    assert coverage.read_hash == DIGEST_A


def test_different_hashes_never_merge_and_leave_no_target_version(tmp_path: Path) -> None:
    path = _resolved(tmp_path, "a.txt")
    events = [
        _event(_read_success("a.txt", DIGEST_A, (1, 2), 4), 1),
        _event(_read_success("DIR/../a.txt", DIGEST_B, (3,), 4), 2),
    ]
    coverage = read_coverage(events, workspace=tmp_path, session_id=SESSION, arguments={"path": "a.txt"}, path_argument_keys=("path",))
    assert dict(coverage.read_lines) == {(path, DIGEST_A): frozenset({1, 2}), (path, DIGEST_B): frozenset({3})}
    assert coverage.read_whole_files == frozenset()
    assert coverage.read_hash is None


def test_another_sessions_results_grant_nothing(tmp_path: Path) -> None:
    events = [
        _event(_read_success("a.txt", DIGEST_A, (1,), 1, whole_file=True), 1, session_id="session-2"),
        _event(_read_success("a.txt", DIGEST_A, (1,), 1, whole_file=True), 2, session_id=SESSION),
    ]
    assert read_coverage([events[0]], workspace=tmp_path, session_id=SESSION) == ReadCoverage()
    assert read_coverage(events, workspace=tmp_path, session_id=SESSION) == read_coverage([events[1]], workspace=tmp_path, session_id=SESSION)


def test_clipped_projection_partial_bounds_and_clipped_observation_grant_nothing(tmp_path: Path) -> None:
    oversized = _read_success("a.txt", DIGEST_A, (1,), 1, whole_file=True, text="x" * (TOOL_OUTPUT_BYTES + 1))
    partial = _read_success("a.txt", DIGEST_A, (1,), 1, whole_file=True, bounds=OutputBounds(partial=True))
    truncated = _read_success("a.txt", DIGEST_A, (1,), 1, whole_file=True, bounds=OutputBounds(truncated=True))
    clipped = _read_success("a.txt", DIGEST_A, (1,), 1, whole_file=True, clipped=True)
    failure = ToolFailure("read", "boom", TextOutput("failed mid-read"))
    events = [
        # one whole-file read is denied by each result-level or observation-level defect
        _event(oversized, 1),
        _event(partial, 2),
        _event(truncated, 3),
        _event(clipped, 4),
        _event(failure, 5),
        _event(_read_success("a.txt", DIGEST_A, (1,), 1), 6, event_type="kernel.tool_interrupted"),
    ]
    assert read_coverage(events, workspace=tmp_path, session_id=SESSION) == ReadCoverage()
    # the same read grants once nothing was clipped
    clean = _read_success("a.txt", DIGEST_A, (1,), 1, whole_file=True)
    assert read_coverage([_event(clean, 7)], workspace=tmp_path, session_id=SESSION).read_whole_files == frozenset(
        {(_resolved(tmp_path, "a.txt"), DIGEST_A)}
    )


def test_whole_file_requires_one_consistent_authentic_claim(tmp_path: Path) -> None:
    path = _resolved(tmp_path, "a.txt")
    inconsistent = [
        _event(_read_success("a.txt", DIGEST_A, (1, 2, 3), 3, whole_file=True), 1),
        _event(_read_success("a.txt", DIGEST_A, (1, 2), 4), 2),
    ]
    assert read_coverage(inconsistent, workspace=tmp_path, session_id=SESSION).read_whole_files == frozenset()

    incomplete = [_event(_read_success("a.txt", DIGEST_A, (1,), 3, whole_file=True), 1)]
    assert read_coverage(incomplete, workspace=tmp_path, session_id=SESSION).read_whole_files == frozenset()

    unioned = [
        _event(_read_success("a.txt", DIGEST_A, (1, 2), 3), 1),
        _event(_read_success("a.txt", DIGEST_A, (3,), 3), 2),
    ]
    coverage = read_coverage(unioned, workspace=tmp_path, session_id=SESSION)
    assert coverage.read_whole_files == frozenset()
    assert coverage.read_lines == {(path, DIGEST_A): frozenset({1, 2, 3})}

    authentic = [_event(_read_success("a.txt", DIGEST_A, (1, 2, 3), 3, whole_file=True), 1)]
    assert read_coverage(authentic, workspace=tmp_path, session_id=SESSION).read_whole_files == frozenset({(path, DIGEST_A)})


def test_authentic_whole_file_replays_from_committed_and_deferred_events(tmp_path: Path) -> None:
    path = _resolved(tmp_path, "a.txt")
    assert read_coverage(
        [_event(_read_success("a.txt", DIGEST_A, (1, 2), 2, whole_file=True), 1)], workspace=tmp_path, session_id=SESSION
    ).read_whole_files == frozenset({(path, DIGEST_A)})
    deferred = _event(_read_success("a.txt", DIGEST_A, (1, 2), 2, whole_file=True), 2, deferred=True)
    assert read_coverage([deferred], workspace=tmp_path, session_id=SESSION).read_whole_files == frozenset({(path, DIGEST_A)})


def test_invalid_committed_claim_fails_closed(tmp_path: Path) -> None:
    payload = encode_tool_result(_read_success("a.txt", DIGEST_A, (1, 2), 2, whole_file=True))
    cast("list[dict[str, object]]", payload["coverage"])[0]["lines"] = [1, 2, 9]
    event = StoredEvent(SESSION, 1, "run-1", "kernel.tool_completed", "tool", "model", "rev-1", {"batch_sequence": 1, "result": payload})
    assert read_coverage([event], workspace=tmp_path, session_id=SESSION) == ReadCoverage()
    assert read_coverage([{"nonsense": True}], workspace=tmp_path, session_id=SESSION) == ReadCoverage()
    assert read_coverage([], workspace=tmp_path, session_id=SESSION) == ReadCoverage()


def test_explicit_result_payload_grants_like_a_committed_event(tmp_path: Path) -> None:
    payload = encode_tool_result(_read_success("a.txt", DIGEST_A, (1,), 1, whole_file=True))
    coverage = read_coverage([payload], workspace=tmp_path, session_id=SESSION)
    assert coverage.read_whole_files == frozenset({(_resolved(tmp_path, "a.txt"), DIGEST_A)})


def test_read_hash_follows_declared_target_paths_only(tmp_path: Path) -> None:
    events = [
        _event(_read_success("a.txt", DIGEST_A, (1,), 1), 1),
        _event(_read_success("b.txt", DIGEST_B, (1,), 1), 2),
    ]
    assert read_coverage(events, workspace=tmp_path, session_id=SESSION).read_hash is None
    assert (
        read_coverage(events, workspace=tmp_path, session_id=SESSION, arguments={"path": "a.txt"}, path_argument_keys=("path",)).read_hash == DIGEST_A
    )
    assert (
        read_coverage(events, workspace=tmp_path, session_id=SESSION, arguments={"file_path": "b.txt"}, path_argument_keys=("file_path",)).read_hash
        == DIGEST_B
    )
    assert read_coverage(events, workspace=tmp_path, session_id=SESSION, arguments={"path": "c.txt"}, path_argument_keys=("path",)).read_hash is None
    assert read_coverage(events, workspace=tmp_path, session_id=SESSION, arguments={"path": "a.txt"}, path_argument_keys=()).read_hash is None
    edits = {"edits": [{"path": "a.txt"}, {"path": "b.txt"}]}
    assert read_coverage(events, workspace=tmp_path, session_id=SESSION, arguments=edits, path_argument_keys=("edits[].path",)).read_hash is None
    assert (
        read_coverage(
            events, workspace=tmp_path, session_id=SESSION, arguments={"edits": [{"path": "a.txt"}]}, path_argument_keys=("edits[].path",)
        ).read_hash
        == DIGEST_A
    )
    versions = [_event(_read_success("a.txt", DIGEST_A, (1,), 1), 1), _event(_read_success("a.txt", DIGEST_B, (1,), 2), 2)]
    assert (
        read_coverage(versions, workspace=tmp_path, session_id=SESSION, arguments={"path": "a.txt"}, path_argument_keys=("path",)).read_hash is None
    )


def test_canonical_path_resolves_relative_and_symlink_free_paths(tmp_path: Path) -> None:
    assert canonical_path(tmp_path, "dir/../a.txt") == _resolved(tmp_path, "a.txt")
    assert canonical_path(tmp_path, str(tmp_path / "a.txt")) == _resolved(tmp_path, "a.txt")
