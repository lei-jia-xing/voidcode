"""Provider naming: canonical machine identifier and human label mappings."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from ..registry import PROVIDER_TABLE

PROVIDER_LABELS: Final[Mapping[str, str]] = {row.id: row.label for row in PROVIDER_TABLE}

# Built-in provider ids are the label table's keys: one table, no second list.
BUILTIN_PROVIDER_IDS: Final[frozenset[str]] = frozenset(PROVIDER_LABELS)


def canonical_provider_id(provider_name: str) -> str:
    """The canonical machine id for a provider name: trimmed and lowercase."""
    return provider_name.strip().lower()


def provider_label(provider_name: str) -> str:
    """Human label for a provider id; an unlabelled provider degrades to its id."""
    canonical = canonical_provider_id(provider_name)
    return PROVIDER_LABELS.get(canonical, canonical)


def split_provider_model_reference(raw_model: str) -> tuple[str, str]:
    """Split ``provider/model`` into the canonical provider id and the model id.

    The provider segment is trimmed and lowercased; the model segment is only
    trimmed, because the wire keeps the vendor's own casing.
    """
    provider_segment, separator, model_segment = raw_model.partition("/")
    provider_name = canonical_provider_id(provider_segment)
    model_name = model_segment.strip()
    if separator != "/" or not provider_name or not model_name:
        raise ValueError("model must use provider/model format")
    return provider_name, model_name


def unknown_provider_id_message(provider_name: str) -> str:
    """Loud, actionable message for a provider id nothing declares."""
    canonical = canonical_provider_id(provider_name)
    known = ", ".join(sorted(BUILTIN_PROVIDER_IDS))
    return (
        f"unknown provider id '{canonical}': known provider ids are {known}; "
        f"declare a custom OpenAI-compatible endpoint as providers.custom.{canonical} "
        f"and reference it as '{canonical}/<model>'"
    )


class UnknownProviderIdError(ValueError):
    """A provider id that is neither a built-in nor a declared custom provider."""

    def __init__(self, provider_name: str) -> None:
        canonical = canonical_provider_id(provider_name)
        self.provider_name = canonical
        self.message = unknown_provider_id_message(canonical)
        super().__init__(self.message)


__all__ = [
    "BUILTIN_PROVIDER_IDS",
    "PROVIDER_LABELS",
    "UnknownProviderIdError",
    "canonical_provider_id",
    "provider_label",
    "split_provider_model_reference",
    "unknown_provider_id_message",
]
