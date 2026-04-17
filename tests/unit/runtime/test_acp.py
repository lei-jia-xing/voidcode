from __future__ import annotations

from importlib import import_module
from typing import Any

import pytest

from voidcode.acp import AcpConfigState, AcpRequestEnvelope, AcpResponseEnvelope
from voidcode.runtime.config import RuntimeAcpConfig


def _load_acp_symbols() -> tuple[Any, ...]:
    module: Any = import_module("voidcode.runtime.acp")
    return (
        module.DisabledAcpAdapter,
        module.ManagedAcpAdapter,
        module.build_acp_adapter,
    )


DisabledAcpAdapter: Any
ManagedAcpAdapter: Any
build_acp_adapter: Any
(
    DisabledAcpAdapter,
    ManagedAcpAdapter,
    build_acp_adapter,
) = _load_acp_symbols()


def test_acp_config_state_defaults_to_disabled() -> None:
    state = AcpConfigState.from_enabled(None)

    assert state.configured_enabled is False


def test_acp_config_state_wraps_runtime_acp_config() -> None:
    state = AcpConfigState.from_enabled(RuntimeAcpConfig(enabled=True).enabled)

    assert state.configured_enabled is True


def test_disabled_acp_adapter_reports_disabled_unavailable_state() -> None:
    adapter = DisabledAcpAdapter(RuntimeAcpConfig(enabled=True))

    state = adapter.current_state()

    assert adapter.configuration.configured_enabled is True
    assert state.mode == "disabled"
    assert state.configuration is adapter.configuration
    assert state.configured is True
    assert state.available is False
    assert state.status == "disconnected"


def test_disabled_acp_adapter_rejects_connect_and_request() -> None:
    adapter = DisabledAcpAdapter(RuntimeAcpConfig(enabled=True))

    with pytest.raises(ValueError, match="disabled"):
        _ = adapter.connect()
    with pytest.raises(ValueError, match="disabled"):
        _ = adapter.request(AcpRequestEnvelope(request_type="ping"))
    assert adapter.disconnect() == ()
    assert adapter.drain_events() == ()


def test_build_acp_adapter_returns_managed_adapter_when_enabled() -> None:
    adapter = build_acp_adapter(RuntimeAcpConfig(enabled=True))

    assert isinstance(adapter, ManagedAcpAdapter)
    assert adapter.current_state().mode == "managed"
    assert adapter.current_state().status == "disconnected"


def test_managed_acp_adapter_connects_and_disconnects() -> None:
    adapter = ManagedAcpAdapter(RuntimeAcpConfig(enabled=True))

    connect_events = adapter.connect()
    assert [event.event_type for event in connect_events] == ["runtime.acp_connected"]
    assert adapter.current_state().status == "connected"
    assert adapter.current_state().available is True

    response = adapter.request(AcpRequestEnvelope(request_type="ping", payload={"x": 1}))
    assert response.status == "ok"
    assert response.payload == {"request_type": "ping", "accepted": True, "x": 1}

    disconnect_events = adapter.disconnect()
    assert [event.event_type for event in disconnect_events] == ["runtime.acp_disconnected"]
    assert adapter.current_state().status == "disconnected"
    assert adapter.current_state().available is False


def test_managed_acp_adapter_request_before_connect_returns_error_without_failing_adapter() -> None:
    adapter = ManagedAcpAdapter(RuntimeAcpConfig(enabled=True))

    response = adapter.request(AcpRequestEnvelope(request_type="ping"))

    assert response == AcpResponseEnvelope(
        status="error",
        error="ACP adapter is not connected",
        payload={"request_type": "ping"},
    )
    state = adapter.current_state()
    assert state.status == "disconnected"
    assert state.available is False
    assert state.last_error is None
    assert adapter.drain_events() == ()

    connect_events = adapter.connect()
    assert [event.event_type for event in connect_events] == ["runtime.acp_connected"]
    assert adapter.current_state().status == "connected"


def test_managed_acp_adapter_exposes_explicit_failure_hook() -> None:
    adapter = ManagedAcpAdapter(RuntimeAcpConfig(enabled=True))

    events = adapter.fail("boom")

    assert [event.event_type for event in events] == ["runtime.acp_failed"]
    assert adapter.current_state().status == "failed"
    assert adapter.current_state().last_error == "boom"
