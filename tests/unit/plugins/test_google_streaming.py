"""Google stream ownership and cancellation use a retained SDK-shaped stream."""

from types import SimpleNamespace

import pytest
from google.genai import types

from voidcode.capabilities.providers.native.google_native import GoogleGenAIProvider
from voidcode.sdk.provider import ProviderExecutionError, ProviderTurnRequest


class Stream:
    def __init__(self, *, terminal=True):
        self.chunks = iter(
            [
                types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(parts=[types.Part(text="first")]))]),
                *([types.GenerateContentResponse(candidates=[types.Candidate(finish_reason="STOP")])] if terminal else []),
            ]
        )
        self.closed = 0
        self.pulled = 0

    def __iter__(self):
        return self

    def __next__(self):
        self.pulled += 1
        return next(self.chunks)

    def close(self):
        self.closed += 1


def provider_for(stream):
    return GoogleGenAIProvider(client=SimpleNamespace(models=SimpleNamespace(generate_content_stream=lambda **kwargs: stream)))


def test_google_stream_closes_after_success():
    raw = Stream()
    events = list(provider_for(raw).stream_turn(ProviderTurnRequest(messages=(), model_name="test")))
    assert events[0].text == "first"
    assert events[-1].done_reason == "stop"
    assert raw.closed == 1


def test_google_stream_closes_when_consumer_stops_early():
    raw = Stream()
    events = provider_for(raw).stream_turn(ProviderTurnRequest(messages=(), model_name="test"))
    assert next(events).text == "first"
    events.close()
    assert raw.closed == 1


def test_google_stream_closes_when_terminal_event_is_missing():
    raw = Stream(terminal=False)
    with pytest.raises(ProviderExecutionError) as caught:
        list(provider_for(raw).stream_turn(ProviderTurnRequest(messages=(), model_name="test")))
    assert caught.value.details["reason"] == "missing_terminal_event"
    assert raw.closed == 1


@pytest.mark.parametrize("before_start", [False, True])
def test_google_stream_cancellation_does_not_report_success(before_start):
    raw = Stream()
    abort = SimpleNamespace(cancelled=before_start)
    events = provider_for(raw).stream_turn(ProviderTurnRequest(messages=(), model_name="test", abort_signal=abort))
    if not before_start:
        assert next(events).text == "first"
        abort.cancelled = True
    error = next(events)
    assert error.kind == "error" and error.error_kind == "cancelled"
    assert raw.closed == (0 if before_start else 1)
    assert next(events).done_reason == "cancelled"
    events.close()
    if before_start:
        assert raw.pulled == 0


@pytest.mark.parametrize(("seconds", "milliseconds"), [(None, 300000), (0.1, 100), (0.0001, 1)])
def test_owned_google_client_always_has_a_finite_sdk_read_timeout(monkeypatch, seconds, milliseconds):
    from voidcode.capabilities.providers.native import google_native
    from voidcode.capabilities.providers.native.config import GoogleProviderConfig

    captured = {}

    def client(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(google_native.genai, "Client", client)
    provider = GoogleGenAIProvider(config=GoogleProviderConfig(timeout_seconds=seconds))
    try:
        provider._client(provider_name="google", model_name="test")
        assert captured["http_options"].timeout == milliseconds
    finally:
        provider.close()
