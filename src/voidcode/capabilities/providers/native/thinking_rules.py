"""SDK thinking parameter defaults combined with official model capabilities.

The provider table here only describes protocol spellings and budget conversion.
Per-model effort levels, disable support and interleaved fields come from the
models.dev snapshot; no model-name matching is used for those facts.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import field, replace
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, StrictBool, TypeAdapter
from pydantic.dataclasses import dataclass

from voidcode.sdk.provider import ProviderEffortLevel

from ..registry import require_provider_id
from ._resources import load_json

type ThinkingMode = Literal["effort", "binary", "budget", "google-level"]

type DisableMode = Literal[
    "lowest-effort",
    "none-effort",
    "openrouter-enabled-false",
    "zai-thinking-disabled",
    "qwen-enable-thinking-false",
]

type MaxTokensField = Literal["max_tokens", "max_completion_tokens"]


@dataclass(frozen=True, slots=True, config=ConfigDict(extra="forbid"))
class ThinkingRule:
    """One provider/model's resolved thinking behaviour."""

    provider: str
    mode: ThinkingMode = "effort"
    budgets: Mapping[str, Annotated[int, Field(strict=True, gt=0)]] = field(default_factory=dict)
    disable_mode: DisableMode = "lowest-effort"
    max_tokens_field: MaxTokensField | None = None
    #: Whether the request always carries an output cap. True for the kimi family
    #: (see `is_kimi_family_model`); no row has to state it.
    sends_output_cap_by_default: StrictBool = False
    reasoning_content_field: str | None = None
    requires_reasoning_content_for_tool_calls: StrictBool = False

    def max_tokens_field_or_default(self) -> MaxTokensField:
        """The output-cap field: the row's own value, else OMP's default rule.

        OMP sends ``max_tokens`` only for the vendors its resolver lists
        (``resolve.ts:403-411``); those rows name the field explicitly, so an
        unnamed field means the ``max_completion_tokens`` default.
        """
        return self.max_tokens_field or "max_completion_tokens"

    def budget_for(self, effort: ProviderEffortLevel) -> int | None:
        """The token budget this rule gives one ladder member, ``None`` when it has none."""
        return self.budgets.get(effort)


_RULES = TypeAdapter(list[ThinkingRule])


def _load() -> dict[str, ThinkingRule]:
    payload = load_json("thinking_rules.json")
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        raise ValueError("thinking_rules.json must contain a rows list")
    tables = payload["budget_tables"]
    if not isinstance(tables, dict):
        raise ValueError("thinking_rules.json budget_tables must be an object")
    rows = []
    for row in payload["rows"]:
        entry = {key: value for key, value in row.items() if key != "source"}
        if isinstance(entry.get("budgets"), str):
            entry["budgets"] = tables[entry["budgets"]]
        rows.append(entry)
    rules = {}
    for rule in _RULES.validate_python(rows):
        provider = require_provider_id(rule.provider, source="thinking_rules.json")
        if provider in rules:
            raise ValueError(f"duplicate thinking protocol default: {provider}")
        rules[provider] = rule
    return rules


THINKING_RULES = _load()


_MODEL_ID_SEPARATOR = re.compile(r"[^a-z0-9]+")


def is_kimi_family_model(model_id: str) -> bool:
    """Whether one model id belongs to the kimi lineage.

    OMP classifies the kimi family by model lineage, not by provider (the
    ``class "kimi"`` rules in ``compat/rules/classes/kimi.kdl`` select on model
    ids), and ``alwaysSendMaxTokens = facts.is("kimi")`` (``compat/resolve.ts:488``)
    reads that class -- so a kimi-lineage model on *any* provider always sends its
    output cap. The lineage is the id's own ``kimi`` token, which covers every id
    OMP classifies (``kimi-k3``, ``kimi/kimi-k2.5``, ``moonshotai/kimi-k2.6``,
    ``accounts/fireworks/models/kimi-k3``).
    """
    return "kimi" in _MODEL_ID_SEPARATOR.split(model_id.lower())


def thinking_rule_for(provider_id: str, model_id: str) -> ThinkingRule:
    """Protocol defaults plus authoritative upstream model capabilities."""
    # Local import keeps the catalog's canonical effort vocabulary independent.
    from .model_catalog import static_catalog_metadata

    rule = THINKING_RULES.get(provider_id, ThinkingRule(provider=provider_id))
    metadata = static_catalog_metadata(provider_id, model_id)
    if metadata is not None:
        if metadata.api == "anthropic-messages":
            rule = replace(rule, mode="budget", budgets=THINKING_RULES["anthropic"].budgets)
        elif metadata.api == "google-generative-ai":
            mode = "google-level" if metadata.supports_reasoning_effort else "budget"
            rule = replace(rule, mode=mode, budgets=THINKING_RULES["google"].budgets)
        elif metadata.supports_reasoning_effort:
            rule = replace(rule, mode="effort")
        if metadata.supports_reasoning_off:
            rule = replace(rule, disable_mode="none-effort")
        if metadata.reasoning_content_field is not None:
            rule = replace(rule, reasoning_content_field=metadata.reasoning_content_field)
    if is_kimi_family_model(model_id):
        rule = replace(rule, sends_output_cap_by_default=True)
    return rule


__all__ = [
    "DisableMode",
    "MaxTokensField",
    "THINKING_RULES",
    "ThinkingMode",
    "ThinkingRule",
    "is_kimi_family_model",
    "thinking_rule_for",
]
