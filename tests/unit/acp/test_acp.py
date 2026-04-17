from __future__ import annotations

from typing import cast

from voidcode.acp import AcpConfigState, AcpRequestEnvelope, AcpRequestHandler, AcpResponseEnvelope


def test_acp_config_state_defaults_to_disabled() -> None:
    assert AcpConfigState() == AcpConfigState(configured_enabled=False)


def test_acp_config_state_derives_enabled_flag_without_runtime_dependency() -> None:
    assert AcpConfigState.from_enabled(None).configured_enabled is False
    assert AcpConfigState.from_enabled(False).configured_enabled is False
    assert AcpConfigState.from_enabled(True).configured_enabled is True


def test_acp_request_envelope_defaults_payload_to_empty_object() -> None:
    envelope = AcpRequestEnvelope(request_type="ping")

    assert envelope.request_type == "ping"
    assert envelope.payload == {}


def test_acp_response_envelope_supports_ok_and_error_shapes() -> None:
    ok = AcpResponseEnvelope(status="ok", payload={"accepted": True})
    error = AcpResponseEnvelope(status="error", error="boom", payload={"request_type": "ping"})

    assert ok.payload == {"accepted": True}
    assert ok.error is None
    assert error.error == "boom"
    assert error.payload == {"request_type": "ping"}


def test_acp_request_handler_protocol_matches_adapter_facing_request_contract() -> None:
    class _StubHandler:
        def request(self, envelope: AcpRequestEnvelope) -> AcpResponseEnvelope:
            return AcpResponseEnvelope(
                status="ok",
                payload={"request_type": envelope.request_type, **envelope.payload},
            )

    handler = cast(AcpRequestHandler, _StubHandler())
    response = handler.request(AcpRequestEnvelope(request_type="ping", payload={"x": 1}))

    assert response == AcpResponseEnvelope(
        status="ok",
        payload={"request_type": "ping", "x": 1},
    )
