"""Immutable workflow-source snapshotting and trusted script loading."""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class WorkflowScript:
    name: str
    path: str
    source_sha256: str
    entry: Any


def snapshot_workflow_source(workflows: Mapping[str, str], workspace: Path, name: str) -> tuple[str, bytes, str]:
    """Read one configured script without executing it; returns (relative path, bytes, sha256)."""
    relative = workflows.get(name)
    if relative is None:
        raise ValueError(f"unknown workflow: {name!r}")
    root = workspace.resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"workflow '{name}' escapes the workspace")
    if not candidate.is_file():
        raise ValueError(f"workflow '{name}' source is missing: {relative}")
    source = candidate.read_bytes()
    return relative, source, hashlib.sha256(source).hexdigest()


def load_workflow_script(name: str, path: str, source: bytes) -> WorkflowScript:
    """Execute fixed source bytes once and return their ``async def run(ctx, task)`` entry."""
    namespace: dict[str, Any] = {"__name__": f"voidcode_workflow_{name}", "__file__": path}
    try:
        exec(compile(source, path, "exec"), namespace)  # noqa: S102 - configured trusted code only
    except Exception as exc:
        raise ValueError(f"workflow '{name}' failed to load: {exc}") from exc
    entry = namespace.get("run")
    if not inspect.iscoroutinefunction(entry):
        raise ValueError(f"workflow '{name}' must define 'async def run(ctx, task)'")
    try:
        signature = inspect.signature(entry)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"workflow '{name}' entry has an unreadable signature: {exc}") from exc
    params = list(signature.parameters.values())
    if len(params) != 2 or any(param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD) for param in params):
        raise ValueError(f"workflow '{name}' entry must take exactly (ctx, task)")
    return WorkflowScript(name=name, path=path, source_sha256=hashlib.sha256(source).hexdigest(), entry=entry)
