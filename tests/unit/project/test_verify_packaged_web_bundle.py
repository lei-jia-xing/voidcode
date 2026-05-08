from __future__ import annotations

import importlib.util
import io
from pathlib import Path

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "scripts"
    / "verify_packaged_web_bundle.py"
)
_SPEC = importlib.util.spec_from_file_location("verify_packaged_web_bundle", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
verify_packaged_web_bundle = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(verify_packaged_web_bundle)


class _SilentStdout(io.TextIOBase):
    def fileno(self) -> int:
        return 0

    def readline(self, size: int = -1) -> str:
        _ = size
        raise AssertionError("readline should not be called when selector reports no data")


def test_wait_for_url_from_stream_times_out_for_silent_alive_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    time_values = iter([0.0, 0.0, 0.2, 0.4, 0.6, 0.6])

    def fake_monotonic() -> float:
        return next(time_values)

    monkeypatch.setattr(verify_packaged_web_bundle.time, "monotonic", fake_monotonic)

    class _Selector:
        def __enter__(self) -> _Selector:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            _ = (exc_type, exc, tb)
            return None

        def register(self, fileobj: object, events: object) -> None:
            _ = (fileobj, events)

        def select(self, timeout: float | None = None) -> list[tuple[object, object]]:
            _ = timeout
            return []

    monkeypatch.setattr(verify_packaged_web_bundle.selectors, "DefaultSelector", _Selector)

    stdout = _SilentStdout()
    with pytest.raises(SystemExit, match="timed out waiting for packaged launcher URL"):
        verify_packaged_web_bundle._wait_for_url_from_stream(
            stdout=stdout,
            poll=lambda: None,
            timeout_seconds=0.5,
        )


def test_wait_for_url_from_stream_surfaces_launcher_output_on_early_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify_packaged_web_bundle.time, "monotonic", lambda: 0.0)

    class _ReadableStdout(io.StringIO):
        def fileno(self) -> int:
            return 0

    class _Selector:
        def __enter__(self) -> _Selector:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            _ = (exc_type, exc, tb)
            return None

        def register(self, fileobj: object, events: object) -> None:
            _ = (fileobj, events)

        def select(self, timeout: float | None = None) -> list[tuple[object, object]]:
            _ = timeout
            return [(object(), object())]

    monkeypatch.setattr(verify_packaged_web_bundle.selectors, "DefaultSelector", _Selector)

    stdout = _ReadableStdout("starting up\n")
    poll_results = iter([1])

    with pytest.raises(SystemExit, match="launcher output:\nstarting up"):
        verify_packaged_web_bundle._wait_for_url_from_stream(
            stdout=stdout,
            poll=lambda: next(poll_results),
            timeout_seconds=30.0,
        )
