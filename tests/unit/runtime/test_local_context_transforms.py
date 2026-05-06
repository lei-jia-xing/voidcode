from __future__ import annotations

import json
from pathlib import Path

import pytest

from voidcode.runtime.context_transforms import RuntimeContextTransformRequest
from voidcode.runtime.local_context_transforms import (
    discover_local_context_transform_registry,
    merge_runtime_context_transform_registries,
)


def test_discover_local_context_transform_registry_loads_project_manifest(tmp_path: Path) -> None:
    manifest_dir = tmp_path / ".voidcode" / "context-transforms"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "custom.json").write_text(
        json.dumps(
            {
                "id": "project_custom",
                "description": "Project custom guidance",
                "content": "Custom transform guidance.",
                "priority": 250,
                "enabled": True,
            }
        ),
        encoding="utf-8",
    )

    registry = discover_local_context_transform_registry(tmp_path)
    result = registry.build_result(
        RuntimeContextTransformRequest(
            workspace=tmp_path,
            tool_results=(),
            hook_preset_context="",
        )
    )

    assert registry.provider_ids() == ("project_custom",)
    assert result.injections[0].metadata["source"] == "project_custom"
    assert result.injections[0].metadata["manifest"] == str(manifest_dir / "custom.json")
    assert result.traces[0].priority == 250


def test_discover_local_context_transform_registry_rejects_builtin_id_collision(
    tmp_path: Path,
) -> None:
    manifest_dir = tmp_path / ".voidcode" / "context-transforms"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "collision.json").write_text(
        json.dumps(
            {
                "id": "runtime_file_rules",
                "description": "Collision",
                "content": "Bad",
                "priority": 250,
                "enabled": True,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="uses builtin id 'runtime_file_rules'"):
        _ = discover_local_context_transform_registry(tmp_path)


def test_merge_runtime_context_transform_registries_appends_custom_providers(
    tmp_path: Path,
) -> None:
    manifest_dir = tmp_path / ".voidcode" / "context-transforms"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "custom.json").write_text(
        json.dumps(
            {
                "id": "project_custom",
                "description": "Project custom guidance",
                "content": "Custom transform guidance.",
                "priority": 250,
                "enabled": True,
            }
        ),
        encoding="utf-8",
    )

    builtins = discover_local_context_transform_registry(tmp_path)
    merged = merge_runtime_context_transform_registries(builtins, builtins)
    assert merged.provider_ids() == ("project_custom", "project_custom")
