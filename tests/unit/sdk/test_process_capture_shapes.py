"""Raw stream capture shapes: exactly-one-of, counts, pair order, and round-trips."""

from __future__ import annotations

import pytest

from tests.fixtures.fake_services import FakeServices
from voidcode.sdk.services import ArtifactReference, ProcessResult, RawStreamCapture

REF = ArtifactReference(sha256="ab" * 32, bytes=4, media_type="application/octet-stream")


def test_capture_truncated_and_payload() -> None:
    capture = RawStreamCapture(stream="stdout", bytes_captured=4, bytes_dropped=0, artifact=REF)
    assert capture.truncated is False
    assert capture.as_payload() == {
        "stream": "stdout",
        "bytes_captured": 4,
        "bytes_dropped": 0,
        "truncated": False,
        "artifact": {"sha256": "ab" * 32, "bytes": 4, "media_type": "application/octet-stream"},
        "capture_error": None,
    }
    dropped = RawStreamCapture(stream="stderr", bytes_captured=0, bytes_dropped=9, artifact=None, capture_error="disk full")
    assert dropped.truncated is True
    assert dropped.as_payload()["capture_error"] == "disk full"


def test_capture_round_trip() -> None:
    capture = RawStreamCapture(stream="stderr", bytes_captured=10, bytes_dropped=3, artifact=REF)
    assert RawStreamCapture.from_payload(capture.as_payload()) == capture
    failed = RawStreamCapture(stream="stdout", bytes_captured=0, bytes_dropped=5, artifact=None, capture_error="append refused")
    assert RawStreamCapture.from_payload(failed.as_payload()) == failed


def test_capture_requires_exactly_one_of_artifact_or_error() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        RawStreamCapture(stream="stdout", bytes_captured=1, bytes_dropped=0, artifact=REF, capture_error="x")
    with pytest.raises(ValueError, match="exactly one"):
        RawStreamCapture(stream="stdout", bytes_captured=1, bytes_dropped=0, artifact=None)


@pytest.mark.parametrize("counts", [(-1, 0), (0, -1), (1.0, 0), (0, "1"), (True, 0)])
def test_capture_rejects_bad_counts(counts: tuple[object, object]) -> None:
    with pytest.raises(ValueError, match="bytes_"):
        RawStreamCapture(stream="stdout", bytes_captured=counts[0], bytes_dropped=counts[1], artifact=REF)  # type: ignore[arg-type]


def test_capture_rejects_bad_stream_artifact_and_error() -> None:
    with pytest.raises(ValueError, match="stdout"):
        RawStreamCapture(stream="stdin", bytes_captured=0, bytes_dropped=0, artifact=REF)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="artifact"):
        RawStreamCapture(stream="stdout", bytes_captured=0, bytes_dropped=0, artifact="ref")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="capture_error"):
        RawStreamCapture(stream="stdout", bytes_captured=0, bytes_dropped=0, artifact=None, capture_error="")


def test_capture_from_payload_is_strict() -> None:
    good = RawStreamCapture(stream="stdout", bytes_captured=4, bytes_dropped=0, artifact=REF).as_payload()
    with pytest.raises(ValueError, match="unknown"):
        RawStreamCapture.from_payload({**good, "extra": 1})
    with pytest.raises(ValueError, match="missing"):
        RawStreamCapture.from_payload({"stream": "stdout"})
    with pytest.raises(ValueError, match="truncated"):
        RawStreamCapture.from_payload({**good, "truncated": True})
    with pytest.raises(ValueError, match="object"):
        RawStreamCapture.from_payload([])  # type: ignore[arg-type]


def test_process_result_raw_pair_rules() -> None:
    out = RawStreamCapture(stream="stdout", bytes_captured=4, bytes_dropped=0, artifact=REF)
    err = RawStreamCapture(stream="stderr", bytes_captured=0, bytes_dropped=0, artifact=REF)
    assert ProcessResult(exit_code=0, stdout="", stderr="", duration_ms=3).raw == ()
    paired = ProcessResult(exit_code=0, stdout="", stderr="", duration_ms=3, raw=[out, err])
    assert paired.raw == (out, err)
    with pytest.raises(ValueError, match="stdout/stderr pair"):
        ProcessResult(exit_code=0, stdout="", stderr="", duration_ms=3, raw=(out,))
    with pytest.raises(ValueError, match="stdout/stderr pair"):
        ProcessResult(exit_code=0, stdout="", stderr="", duration_ms=3, raw=(out, err, out))
    with pytest.raises(ValueError, match="order"):
        ProcessResult(exit_code=0, stdout="", stderr="", duration_ms=3, raw=(err, out))
    with pytest.raises(ValueError, match="order"):
        ProcessResult(exit_code=0, stdout="", stderr="", duration_ms=3, raw=(out, out))
    with pytest.raises(ValueError, match="RawStreamCapture"):
        ProcessResult(exit_code=0, stdout="", stderr="", duration_ms=3, raw=("x",))  # type: ignore[arg-type]


def test_fake_process_accepts_raw_params() -> None:
    fake = FakeServices()
    result = fake.run_process(["true"], raw_capture=True, raw_media_type="image/png")
    assert isinstance(result, ProcessResult)
    assert fake.run_query_process(["true"], raw_capture=True).raw == ()
