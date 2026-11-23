"""Provider registry: canonical definitions, default hosts, wires, and credential metadata."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, TypeIs

type ProviderWire = Literal["openai-completions", "anthropic-messages", "google-generative-ai"]


_WIRES: Final[tuple[ProviderWire, ...]] = ("openai-completions", "anthropic-messages", "google-generative-ai")

_TABLE_PATH: Final[Path] = Path(__file__).resolve().parent / "provider_table.json"


def is_provider_wire(value: object) -> TypeIs[ProviderWire]:
    """Whether a wire token names one shipped provider wire."""
    return value in _WIRES


@dataclass(frozen=True, slots=True)
class ProviderTableRow:
    """One built-in provider.

    ``env_vars`` are the environment variables that configure it, credential
    first: an OpenAI-compatible vendor reads the rest as fallbacks, the generic
    ``endpoint`` provider takes its base URL from the second, and every other
    provider takes only the first. ``models_dev_keys`` are the upstream
    models.dev keys the model-catalog generator reads; an empty tuple means the
    provider has no generated catalog entry.

    """

    id: str
    label: str
    wire: ProviderWire
    default_base_url: str
    env_vars: tuple[str, ...]
    models_dev_keys: tuple[str, ...]


def _string_tuple(value: object, *, field: str, provider_id: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"provider table entry {provider_id!r} field {field!r} must be a list of non-empty strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ValueError(f"provider table entry {provider_id!r} field {field!r} must be a list of non-empty strings")
        result.append(item)
    return tuple(result)


def _row(raw: object) -> ProviderTableRow:
    if not isinstance(raw, dict):
        raise ValueError("provider table entries must be objects")
    provider_id = raw.get("id")
    label = raw.get("label")
    wire = raw.get("wire")
    default_base_url = raw.get("default_base_url")
    if not isinstance(provider_id, str) or not provider_id:
        raise ValueError("provider table entry is missing a non-empty 'id'")
    if not isinstance(label, str) or not label:
        raise ValueError(f"provider table entry {provider_id!r} is missing a non-empty 'label'")
    if not is_provider_wire(wire):
        raise ValueError(f"provider table entry {provider_id!r} has an unknown wire: {wire!r}")
    if not isinstance(default_base_url, str) or not default_base_url:
        raise ValueError(f"provider table entry {provider_id!r} is missing a non-empty 'default_base_url'")
    return ProviderTableRow(
        id=provider_id,
        label=label,
        wire=wire,
        default_base_url=default_base_url,
        env_vars=_string_tuple(raw.get("env_vars", []), field="env_vars", provider_id=provider_id),
        models_dev_keys=_string_tuple(raw.get("models_dev_keys", []), field="models_dev_keys", provider_id=provider_id),
    )


def _rows_from_payload(payload: object) -> tuple[ProviderTableRow, ...]:
    if not isinstance(payload, dict):
        raise ValueError("provider_table.json must hold a 'rows' list")
    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list):
        raise ValueError("provider_table.json must hold a 'rows' list")
    rows = tuple(_row(raw) for raw in raw_rows)
    seen: set[str] = set()
    for row in rows:
        if row.id in seen:
            raise ValueError(f"provider_table.json declares {row.id!r} twice")
        seen.add(row.id)
    return rows


PROVIDER_TABLE: Final[tuple[ProviderTableRow, ...]] = _rows_from_payload(json.loads(_TABLE_PATH.read_text(encoding="utf-8")))

PROVIDER_TABLE_BY_ID: Final[Mapping[str, ProviderTableRow]] = {row.id: row for row in PROVIDER_TABLE}


def require_provider_id(provider_id: str, *, source: str) -> str:
    """``provider_id`` when the table declares it, else a loud import-time failure.

    The thinking rules address
    providers by id; a typo there would otherwise become a table nothing ever
    consults, so the citation is checked against the one provider table instead.
    """
    if provider_id not in PROVIDER_TABLE_BY_ID:
        raise ValueError(f"{source} names an unknown provider {provider_id!r} (not in provider_table.json)")
    return provider_id


__all__ = [
    "PROVIDER_TABLE",
    "PROVIDER_TABLE_BY_ID",
    "ProviderTableRow",
    "ProviderWire",
    "is_provider_wire",
    "require_provider_id",
]
