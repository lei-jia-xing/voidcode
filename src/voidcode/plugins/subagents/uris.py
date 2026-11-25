"""Stable resource URI spellings for one delegated child."""

from __future__ import annotations


def _artifact_uri(sha256: str) -> str:
    return f"artifact://{sha256}"


def _transcript_uri(task_id: str) -> str:
    return f"transcript://{task_id}"


def _agent_uri(task_id: str) -> str:
    return f"agent://{task_id}"
