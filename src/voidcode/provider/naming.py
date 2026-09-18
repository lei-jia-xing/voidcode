"""Provider naming: one canonical id and one human label per provider.

Machine identifier
    The lowercase canonical vendor id (``minimax``). It is the registry key, the
    ``providers.<id>`` config key, the ``<id>/<model>`` reference prefix, the
    ``<ID>_API_KEY`` credential environment variable, the model-catalog key,
    ``/api/providers`` ``name`` and every internal lookup. Provider input is
    trimmed and lowercased at every boundary and canonicalised to this id, so
    ``MiniMax``, ``MINIMAX`` and `` minimax `` all name the same provider.

Human label
    ``provider_label`` (``MiniMax``), used by every surface that shows a provider
    name to a human: the web ``/api/providers`` payload and
    ``voidcode provider inspect`` (both through ``ProviderSummary.label``) and
    ``voidcode doctor``. A provider without an explicit label degrades to its
    canonical id.

Model ids are deliberately NOT canonicalised: the ``<model>`` half of a
``provider/model`` reference is sent to the vendor verbatim, including its case.
Catalog metadata, reasoning-effort capability tables and fallback-chain
comparison match model ids case-insensitively, and ``model_map`` is the mechanism
for vendor casings and aliases.

``PROVIDER_LABELS`` is the one authoritative table of built-in provider ids: the
registry, the ``providers`` config payload and ``providers.custom`` name
reservations all derive from it instead of carrying a second list.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

PROVIDER_LABELS: Final[Mapping[str, str]] = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "google": "Google",
    "copilot": "Copilot",
    "endpoint": "Endpoint",
    "opencode": "OpenCode",
    "opencode-go": "OpenCode Go",
    "openrouter": "OpenRouter",
    "deepseek": "DeepSeek",
    "zai": "Z.AI",
    "zhipuai": "ZhipuAI",
    "grok": "Grok",
    "minimax": "MiniMax",
    "kimi": "Kimi",
    "qwen": "Qwen",
    "groq": "Groq",
    "together": "Together",
    "fireworks": "Fireworks",
    "mistral": "Mistral",
}

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


def canonical_model_reference(raw_model: str) -> str:
    """``provider/model`` with the provider segment canonicalised.

    Used where a model reference is written back to configuration, so the stored
    form always carries the canonical provider id.
    """
    provider_name, model_name = split_provider_model_reference(raw_model)
    return f"{provider_name}/{model_name}"


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
    "canonical_model_reference",
    "canonical_provider_id",
    "provider_label",
    "split_provider_model_reference",
    "unknown_provider_id_message",
]
