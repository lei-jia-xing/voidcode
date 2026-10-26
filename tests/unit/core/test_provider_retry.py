"""Runtime-owned provider retry: approved failures replay, everything else fails fast."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from voidcode.core.plugins import PluginRuntime, PluginSource
from voidcode.core.retry import MAX_ATTEMPTS
from voidcode.sdk.provider import (
    ProviderAbortSignal,
    ProviderErrorKind,
    ProviderExecutionError,
    ProviderMessage,
    ProviderTurnRequest,
)


class _Abort(ProviderAbortSignal):
    def __init__(self) -> None:
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def reason(self) -> str | None:
        return None

    def set_cancelled(self, value: bool, *, reason: str | None = None) -> None:
        self._cancelled = value


_PROVIDER_MODULE = """
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.provider import ProviderTurnResult


class FlakyProvider:
    name = "flaky"

    def __init__(self) -> None:
        self.script = []
        self.calls = []

    def propose_turn(self, request):
        self.calls.append(request.attempt)
        if self.script:
            raise self.script.pop(0)
        return ProviderTurnResult(output="recovered", tool_calls=())


def create_plugin(config=None):
    return Plugin(providers=(FlakyProvider(),))
"""


def _request(*, abort: ProviderAbortSignal | None = None) -> ProviderTurnRequest:
    return ProviderTurnRequest(
        messages=(ProviderMessage("user", "hi"),),
        provider_name="flaky",
        model_name="m",
        abort_signal=abort,
    )


def _failure(kind: ProviderErrorKind, *, retryable: bool | None, retry_after: float | None = None, message: str = "boom") -> ProviderExecutionError:
    return ProviderExecutionError(
        kind=kind,
        provider_name="flaky",
        model_name="m",
        message=message,
        retryable=retryable,
        retry_after=retry_after,
    )


def test_provider_retry_replays_approved_failures_and_fails_fast_otherwise(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    source.mkdir(parents=True, exist_ok=True)
    (source / "__init__.py").write_text(_PROVIDER_MODULE, encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with PluginRuntime((PluginSource(root=source, plugin_id="flaky-fixture", config={}),), workspace=workspace, revision_id="rev-retry") as runtime:
        flaky = runtime._providers["flaky"][0]  # noqa: SLF001 — the fixture exposes its script

        # Approved failures replay: an explicit adapter opt-in, then a rate limit
        # the adapter left undecided for the runtime to decide.
        flaky.script = [
            _failure("transient_failure", retryable=True),
            _failure("rate_limit", retryable=None, retry_after=0.0),
        ]
        assert runtime.propose_turn(_request(), provider="flaky").output == "recovered"
        assert flaky.calls == [1, 2, 3]

        # A vetoed failure fails fast on its first and only call.
        flaky.calls.clear()
        flaky.script = [_failure("invalid_model", retryable=False)]
        with pytest.raises(ProviderExecutionError, match="boom"):
            runtime.propose_turn(_request(), provider="flaky")
        assert flaky.calls == [1]

        # Endless failures stop at the attempt bound, reraising the last failure.
        flaky.calls.clear()
        flaky.script = [_failure("transient_failure", retryable=True, retry_after=0.0, message=f"boom-{i}") for i in range(MAX_ATTEMPTS + 5)]
        with pytest.raises(ProviderExecutionError, match=f"boom-{MAX_ATTEMPTS - 1}"):
            runtime.propose_turn(_request(), provider="flaky")
        assert flaky.calls == list(range(1, MAX_ATTEMPTS + 1))

        # Cancellation during backoff aborts the wait instead of stalling the run.
        flaky.calls.clear()
        flaky.script = [_failure("transient_failure", retryable=True, retry_after=30.0)]
        abort = _Abort()
        threading.Timer(0.05, lambda: abort.set_cancelled(True)).start()
        with pytest.raises(ProviderExecutionError) as excinfo:
            runtime.propose_turn(_request(abort=abort), provider="flaky")
        assert excinfo.value.kind == "cancelled"
        assert flaky.calls == [1]
