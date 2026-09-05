from __future__ import annotations

from .acp import AcpAdapterState
from .contracts import CapabilityStatusSnapshot


def project_acp_status(acp_state: AcpAdapterState) -> CapabilityStatusSnapshot:
    """Project ACP adapter state into the runtime capability status contract."""
    acp_status = (
        "unconfigured"
        if acp_state.mode != "managed" or not acp_state.configuration.configured_enabled
        else "failed"
        if acp_state.status == "failed"
        else "running"
        if acp_state.available and acp_state.status == "connected"
        else "stopped"
    )
    details: dict[str, object] = {
        "mode": acp_state.mode,
        "configured": acp_state.configured,
        "configured_enabled": acp_state.configuration.configured_enabled,
        "available": acp_state.available,
        "status": acp_state.status,
    }
    if acp_state.last_request_type is not None:
        details["last_request_type"] = acp_state.last_request_type
    if acp_state.last_request_id is not None:
        details["last_request_id"] = acp_state.last_request_id
    if acp_state.last_event_type is not None:
        details["last_event_type"] = acp_state.last_event_type
    if acp_state.last_delegation is not None:
        details["last_delegation"] = acp_state.last_delegation.as_payload()
    return CapabilityStatusSnapshot(
        state=acp_status,
        error=acp_state.last_error,
        details=details,
    )


__all__ = ["project_acp_status"]
