from __future__ import annotations

from typing import cast

from ..hook.presets import ResolvedHookPresetSnapshot, hook_preset_snapshot_from_payload
from .config import RuntimeAgentConfig
from .contracts import RuntimeHookPresetSnapshot


def hook_preset_refs_for_agent(agent: RuntimeAgentConfig | None) -> tuple[str, ...]:
    if agent is None:
        return ()
    if agent.hook_refs:
        return agent.hook_refs
    return agent.manifest_hook_refs


def resolved_hook_preset_snapshot_from_session_metadata(
    metadata: dict[str, object],
) -> ResolvedHookPresetSnapshot | None:
    raw_snapshot = metadata.get("resolved_hook_presets")
    if isinstance(raw_snapshot, dict):
        return hook_preset_snapshot_from_payload(cast(dict[object, object], raw_snapshot))
    raw_runtime_config = metadata.get("runtime_config")
    if not isinstance(raw_runtime_config, dict):
        return None
    runtime_config_payload = cast(dict[object, object], raw_runtime_config)
    nested_snapshot = runtime_config_payload.get("resolved_hook_presets")
    if not isinstance(nested_snapshot, dict):
        return None
    return hook_preset_snapshot_from_payload(cast(dict[object, object], nested_snapshot))


def hook_preset_event_payload_from_session_metadata(
    metadata: dict[str, object],
) -> dict[str, object] | None:
    snapshot = resolved_hook_preset_snapshot_from_session_metadata(metadata)
    if snapshot is None or not snapshot.presets:
        return None
    kinds = [preset["kind"] for preset in snapshot.presets]
    return {
        "refs": list(snapshot.refs),
        "kinds": kinds,
        "source": "builtin",
        "count": len(snapshot.presets),
    }


def debug_hook_preset_snapshot(
    metadata: dict[str, object],
) -> RuntimeHookPresetSnapshot | None:
    payload = hook_preset_event_payload_from_session_metadata(metadata)
    if payload is None:
        return None
    return RuntimeHookPresetSnapshot(
        refs=tuple(cast(list[str], payload["refs"])),
        kinds=tuple(cast(list[str], payload["kinds"])),
        source=cast(str, payload["source"]),
        count=cast(int, payload["count"]),
    )
