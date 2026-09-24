"""The one provider table: id -> label, wire, vendor host, credentials, catalog key.

Every provider surface derives from this module instead of carrying its own
list: the registry's built-in ids and the human labels (``naming``), the
``providers.<id>`` payload fields and their vendor defaults and credential
environment variables (``config``), the Anthropic-wire host and listing-header
overrides (``provider_config``), and the model-catalog generator's upstream
source keys (``scripts/generate_model_catalog.py``).

Adding a vendor is one row in ``provider_table.json`` plus its payload field;
adding a *host* or an ``<ID>_API_KEY`` spelling is only a row.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files as _resource_files
from typing import Final, Literal, TypeIs, cast

type ProviderWire = Literal["openai-completions", "anthropic-messages", "google-generative-ai"]

type WireSource = Literal["model", "provider"]

_WIRES: Final[tuple[ProviderWire, ...]] = ("openai-completions", "anthropic-messages", "google-generative-ai")

_WIRE_SOURCES: Final[tuple[WireSource, ...]] = ("model", "provider")


def _is_wire(value: object) -> TypeIs[ProviderWire]:
    return value in _WIRES


def _is_wire_source(value: object) -> TypeIs[WireSource]:
    return value in _WIRE_SOURCES


@dataclass(frozen=True, slots=True)
class ProviderTableRow:
    """One built-in provider.

    ``env_vars`` are the environment variables that configure it, credential
    first: an OpenAI-compatible vendor reads the rest as fallbacks, the generic
    ``endpoint`` provider takes its base URL from the second, and every other
    provider takes only the first. ``models_dev_keys`` are the upstream
    models.dev keys the model-catalog generator reads; an empty tuple means the
    provider has no generated catalog entry.

    ``wire_source`` says which wire dispatch uses: ``model`` means the catalog
    row's ``api`` decides (a per-model-routing gateway), ``provider`` means the
    provider's own ``wire`` decides and the row's ``api`` is recorded upstream
    truth we do not serve yet.
    """

    id: str
    label: str
    wire: ProviderWire
    default_base_url: str
    env_vars: tuple[str, ...]
    models_dev_keys: tuple[str, ...]
    wire_source: WireSource = "model"
    notes: str = ""


def _string_tuple(value: object, *, field: str, provider_id: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"provider table entry {provider_id!r} field {field!r} must be a list of non-empty strings")
    return tuple(cast(list[str], value))


def _row(raw: object) -> ProviderTableRow:
    if not isinstance(raw, dict):
        raise ValueError("provider table entries must be objects")
    entry = cast(dict[str, object], raw)
    provider_id = entry.get("id")
    label = entry.get("label")
    wire = entry.get("wire")
    default_base_url = entry.get("default_base_url")
    if not isinstance(provider_id, str) or not provider_id:
        raise ValueError("provider table entry is missing a non-empty 'id'")
    if not isinstance(label, str) or not label:
        raise ValueError(f"provider table entry {provider_id!r} is missing a non-empty 'label'")
    if not _is_wire(wire):
        raise ValueError(f"provider table entry {provider_id!r} has an unknown wire: {wire!r}")
    if not isinstance(default_base_url, str) or not default_base_url:
        raise ValueError(f"provider table entry {provider_id!r} is missing a non-empty 'default_base_url'")
    wire_source = entry.get("wire_source", "model")
    if not _is_wire_source(wire_source):
        raise ValueError(f"provider table entry {provider_id!r} has an unknown wire_source: {wire_source!r}")
    notes = entry.get("notes")
    return ProviderTableRow(
        id=provider_id,
        label=label,
        wire=wire,
        default_base_url=default_base_url,
        env_vars=_string_tuple(entry.get("env_vars", []), field="env_vars", provider_id=provider_id),
        models_dev_keys=_string_tuple(entry.get("models_dev_keys", []), field="models_dev_keys", provider_id=provider_id),
        wire_source=wire_source,
        notes=notes if isinstance(notes, str) else "",
    )


def _load_payload() -> object:
    return json.loads(_resource_files("voidcode.provider").joinpath("provider_table.json").read_text(encoding="utf-8"))


def _rows_from_payload(payload: object) -> tuple[ProviderTableRow, ...]:
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        raise ValueError("provider_table.json must hold a 'rows' list")
    rows = tuple(_row(raw) for raw in cast(list[object], payload["rows"]))
    seen: set[str] = set()
    for row in rows:
        if row.id in seen:
            raise ValueError(f"provider_table.json declares {row.id!r} twice")
        seen.add(row.id)
    return rows


PROVIDER_TABLE: Final[tuple[ProviderTableRow, ...]] = _rows_from_payload(_load_payload())

PROVIDER_TABLE_BY_ID: Final[Mapping[str, ProviderTableRow]] = {row.id: row for row in PROVIDER_TABLE}


def require_provider_id(provider_id: str, *, source: str) -> str:
    """``provider_id`` when the table declares it, else a loud import-time failure.

    The rule files (``api_routes``, ``thinking_rules``, ``pricing_rules``) address
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
    "WireSource",
    "require_provider_id",
]
