"""Read observation serialization, model projection bounds and resource paths."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from voidcode.core.coverage import canonical_path
from voidcode.sdk.codec import decode_tool_result, encode_tool_result
from voidcode.sdk.tools import OpaqueToolBody, OutputBounds, ReadObservation, TextOutput, ToolFailure, ToolSuccess

DIGEST_A = "a" * 64


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


# --- resource paths ------------------------------------------------------------------------


def test_canonical_path_resolves_relative_and_symlink_free_paths(tmp_path: Path) -> None:
    assert canonical_path(tmp_path, "dir/../a.txt") == _resolved(tmp_path, "a.txt")
    assert canonical_path(tmp_path, str(tmp_path / "a.txt")) == _resolved(tmp_path, "a.txt")


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


def _resolved(workspace: Path, name: str) -> str:
    return str((workspace / name).resolve())
