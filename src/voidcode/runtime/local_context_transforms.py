from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast, final

from .context_transforms import (
    RuntimeContextTransformInjection,
    RuntimeContextTransformProvider,
    RuntimeContextTransformRegistry,
    RuntimeContextTransformRequest,
    RuntimeContextTransformResult,
    RuntimeContextTransformTrace,
)

LOCAL_CONTEXT_TRANSFORM_DEFAULT_PATH = ".voidcode/context-transforms"
LOCAL_CONTEXT_TRANSFORM_MANIFEST_SUFFIX = ".json"


@dataclass(frozen=True, slots=True)
class LocalContextTransformManifest:
    provider_id: str
    description: str
    content: str
    priority: int
    manifest_path: Path


def discover_local_context_transform_registry(workspace: Path) -> RuntimeContextTransformRegistry:
    workspace_root = workspace.resolve()
    root = (workspace_root / LOCAL_CONTEXT_TRANSFORM_DEFAULT_PATH).resolve()
    try:
        root.relative_to(workspace_root)
    except ValueError as exc:
        raise ValueError("local context transforms path must stay inside the workspace") from exc
    if not root.exists():
        return RuntimeContextTransformRegistry()
    if not root.is_dir():
        raise ValueError("local context transforms path is not a directory")

    providers: list[RuntimeContextTransformProvider] = []
    seen_ids: set[str] = set()
    builtin_ids = {"hook_preset_guidance", "runtime_file_rules"}
    for path in sorted(root.glob(f"*{LOCAL_CONTEXT_TRANSFORM_MANIFEST_SUFFIX}")):
        if not path.is_file():
            continue
        manifest = _load_local_context_transform_manifest(path)
        if manifest.provider_id in builtin_ids:
            raise ValueError(
                f"local context transform manifest {path} uses builtin id '{manifest.provider_id}'"
            )
        if manifest.provider_id in seen_ids:
            raise ValueError(
                f"duplicate local context transform provider id '{manifest.provider_id}' in {path}"
            )
        seen_ids.add(manifest.provider_id)
        providers.append(LocalContextTransformProvider(manifest))
    return RuntimeContextTransformRegistry(providers=tuple(providers))


def merge_runtime_context_transform_registries(
    base: RuntimeContextTransformRegistry,
    extra: RuntimeContextTransformRegistry,
) -> RuntimeContextTransformRegistry:
    if not extra.providers:
        return base
    return RuntimeContextTransformRegistry(providers=(*base.providers, *extra.providers))


def _load_local_context_transform_manifest(path: Path) -> LocalContextTransformManifest:
    try:
        raw_payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid local context transform manifest JSON at {path}") from exc
    if not isinstance(raw_payload, dict):
        raise ValueError(f"local context transform manifest must be an object: {path}")
    payload = cast(dict[str, object], raw_payload)
    allowed_keys = {"id", "description", "content", "priority", "enabled"}
    unknown_keys = sorted(key for key in payload if key not in allowed_keys)
    if unknown_keys:
        raise ValueError(
            f"local context transform manifest {path} has unsupported field: {unknown_keys[0]}"
        )
    enabled = payload.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"local context transform manifest {path} enabled must be a boolean")
    if enabled is not True:
        raise ValueError(f"local context transform manifest {path} must be enabled to load")
    provider_id = payload.get("id")
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise ValueError(f"local context transform manifest {path} requires a non-empty id")
    description = payload.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(
            f"local context transform manifest {path} requires a non-empty description"
        )
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError(f"local context transform manifest {path} requires non-empty content")
    priority = payload.get("priority", 300)
    if not isinstance(priority, int) or isinstance(priority, bool):
        raise ValueError(f"local context transform manifest {path} priority must be an integer")
    return LocalContextTransformManifest(
        provider_id=provider_id.strip(),
        description=description.strip(),
        content=content.strip(),
        priority=priority,
        manifest_path=path,
    )


@final
class LocalContextTransformProvider:
    def __init__(self, manifest: LocalContextTransformManifest) -> None:
        self._manifest = manifest

    @property
    def provider_id(self) -> str:
        return self._manifest.provider_id

    @property
    def priority(self) -> int:
        return self._manifest.priority

    def build_result(
        self,
        request: RuntimeContextTransformRequest,
    ) -> RuntimeContextTransformResult:
        _ = request
        return RuntimeContextTransformResult(
            injections=(
                RuntimeContextTransformInjection(
                    role="system",
                    content=self._manifest.content,
                    metadata={
                        "source": self._manifest.provider_id,
                        "description": self._manifest.description,
                        "manifest": str(self._manifest.manifest_path),
                    },
                ),
            ),
            traces=(
                RuntimeContextTransformTrace(
                    provider_id=self._manifest.provider_id,
                    priority=self._manifest.priority,
                    injection_count=1,
                    sources=(self._manifest.provider_id,),
                ),
            ),
            failure_policy=request.failure_policy,
        )
