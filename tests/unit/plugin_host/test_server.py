"""Unit tests for the PluginHostServer protocol loop and utilities."""

from __future__ import annotations

import io
import json
import threading

from voidcode.plugin_host.server import (
    HostAbortSignal,
    PluginHostServer,
)


def test_host_abort_signal() -> None:
    event = threading.Event()
    signal = HostAbortSignal(event)
    assert not signal.cancelled
    assert signal.reason is None

    signal.set_cancelled(True, reason="user abort")
    assert signal.cancelled
    assert signal.reason == "user abort"
    assert event.is_set()

    signal.set_cancelled(False)
    assert not signal.cancelled
    assert signal.reason is None
    assert not event.is_set()


def test_server_handles_frame_too_large() -> None:
    in_buf = io.BytesIO(b" " * (1024 * 1024 + 10) + b"\n")
    out_buf = io.BytesIO()

    server = PluginHostServer(in_stream=in_buf, out_stream=out_buf, standalone=False)
    server.run()

    out_buf.seek(0)
    lines = [json.loads(line.decode("utf-8")) for line in out_buf if line.strip()]
    assert len(lines) == 1
    assert lines[0]["ok"] is False
    assert lines[0]["error"]["kind"] == "frame_too_large"


def test_server_handles_invalid_json() -> None:
    in_buf = io.BytesIO(b"not a json\n")
    out_buf = io.BytesIO()

    server = PluginHostServer(in_stream=in_buf, out_stream=out_buf, standalone=False)
    server.run()

    out_buf.seek(0)
    lines = [json.loads(line.decode("utf-8")) for line in out_buf if line.strip()]
    assert len(lines) == 1
    assert lines[0]["ok"] is False
    assert lines[0]["error"]["kind"] == "invalid_json"


def test_server_handles_non_object_json() -> None:
    in_buf = io.BytesIO(b"[1, 2, 3]\n")
    out_buf = io.BytesIO()

    server = PluginHostServer(in_stream=in_buf, out_stream=out_buf, standalone=False)
    server.run()

    out_buf.seek(0)
    lines = [json.loads(line.decode("utf-8")) for line in out_buf if line.strip()]
    assert len(lines) == 1
    assert lines[0]["ok"] is False
    assert lines[0]["error"]["kind"] == "invalid_envelope"


def test_server_handles_unknown_method() -> None:
    req = json.dumps({"protocol_version": 1, "id": "1", "method": "nonexistent", "params": {}}).encode("utf-8")
    in_buf = io.BytesIO(req + b"\n")
    out_buf = io.BytesIO()

    server = PluginHostServer(in_stream=in_buf, out_stream=out_buf, standalone=False)
    server.run()

    out_buf.seek(0)
    lines = [json.loads(line.decode("utf-8")) for line in out_buf if line.strip()]
    assert len(lines) == 1
    assert lines[0]["ok"] is False
    assert lines[0]["error"]["kind"] == "unknown_method"


def test_server_rejects_unknown_envelope_fields() -> None:
    req = json.dumps({"protocol_version": 1, "id": "1", "method": "init", "params": {}, "rogue_key": 42}).encode("utf-8")
    in_buf = io.BytesIO(req + b"\n")
    out_buf = io.BytesIO()

    server = PluginHostServer(in_stream=in_buf, out_stream=out_buf, standalone=False)
    server.run()

    out_buf.seek(0)
    lines = [json.loads(line.decode("utf-8")) for line in out_buf if line.strip()]
    assert len(lines) == 1
    assert lines[0]["ok"] is False
    assert lines[0]["error"]["kind"] == "invalid_envelope"


def test_server_rejects_bool_protocol_version() -> None:
    req = json.dumps({"protocol_version": True, "id": "1", "method": "close", "params": {}}).encode("utf-8")
    in_buf = io.BytesIO(req + b"\n")
    out_buf = io.BytesIO()

    server = PluginHostServer(in_stream=in_buf, out_stream=out_buf, standalone=False)
    server.run()

    out_buf.seek(0)
    lines = [json.loads(line.decode("utf-8")) for line in out_buf if line.strip()]
    assert len(lines) == 1
    assert lines[0]["ok"] is False
    assert lines[0]["error"]["kind"] == "invalid_envelope"


def test_server_rejects_invalid_nested_params() -> None:
    req = json.dumps({"protocol_version": 1, "id": "1", "method": "invoke", "params": {"unexpected": 123}}).encode("utf-8")
    in_buf = io.BytesIO(req + b"\n")
    out_buf = io.BytesIO()

    server = PluginHostServer(in_stream=in_buf, out_stream=out_buf, standalone=False)
    server.run()

    out_buf.seek(0)
    lines = [json.loads(line.decode("utf-8")) for line in out_buf if line.strip()]
    assert len(lines) == 1
    assert lines[0]["ok"] is False
    assert lines[0]["error"]["kind"] == "invalid_params"


def test_bad_per_call_params_do_not_kill_the_server_loop() -> None:
    """A malformed per-call request answers invalid_params and the loop stays alive."""
    bad = json.dumps({"protocol_version": 1, "id": "1", "method": "contribute_context", "params": {"session_id": "s"}}).encode("utf-8")
    good = json.dumps({"protocol_version": 1, "id": "2", "method": "cancel", "params": {"target_id": "none"}}).encode("utf-8")
    in_buf = io.BytesIO(bad + b"\n" + good + b"\n")
    out_buf = io.BytesIO()

    PluginHostServer(in_stream=in_buf, out_stream=out_buf, standalone=False).run()

    out_buf.seek(0)
    lines = [json.loads(line.decode("utf-8")) for line in out_buf if line.strip()]
    assert [line["id"] for line in lines] == ["1", "2"]
    assert lines[0]["ok"] is False
    assert lines[0]["error"]["kind"] == "invalid_params"
    assert lines[1]["ok"] is True


def test_context_and_observer_arity_is_admitted_strictly() -> None:
    """Frozen service API: context takes (ctx, services), observer takes (fact, ctx, services)."""
    from voidcode.plugin_host.server import _check_arity

    def context(ctx, services):  # noqa: ARG001
        return None

    def observer(fact, ctx, services):  # noqa: ARG001
        return None

    _check_arity(context, 2, "context")
    _check_arity(observer, 3, "observer")

    def legacy_context(ctx):  # noqa: ARG001
        return None

    import pytest

    with pytest.raises(ValueError, match="must accept 2 positional arguments"):
        _check_arity(legacy_context, 2, "context")
