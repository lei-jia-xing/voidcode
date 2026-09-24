"""Per-model thinking rules: which wire knob carries the effort, and how to turn it off.

Mirrors OMP's per-model ``thinking`` config (``packages/catalog/src/types.ts:75-133``):
the *mode* selects the wire knob, ``effort_map`` remaps a ladder member to the
vendor's own value, ``budgets`` is the per-effort token table a budget wire reads,
``disable_mode`` is the vendor's spelling for "do not reason", ``max_tokens_field``
names the output-cap field, and ``requires_effort`` marks a model that always
reasons. ``thinking_rules.json`` carries the rows; this module is the only reader,
so the adapters never hardcode a mode, a budget or a disable spelling.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from importlib.resources import files as _resource_files
from typing import Final, Literal, cast

from .model_match import MATCHERS, Matcher, matches
from .provider_table import require_provider_id

type ThinkingMode = Literal["effort", "binary", "budget", "google-level"]

type DisableMode = Literal[
    "lowest-effort",
    "none-effort",
    "openrouter-enabled-false",
    "zai-thinking-disabled",
    "qwen-enable-thinking-false",
]

type MaxTokensField = Literal["max_tokens", "max_completion_tokens"]

_THINKING_MODES: Final[tuple[ThinkingMode, ...]] = (
    "effort",
    "binary",
    "budget",
    "google-level",
)
_DISABLE_MODES: Final[tuple[DisableMode, ...]] = (
    "lowest-effort",
    "none-effort",
    "openrouter-enabled-false",
    "zai-thinking-disabled",
    "qwen-enable-thinking-false",
)
_MAX_TOKENS_FIELDS: Final[tuple[MaxTokensField, ...]] = ("max_tokens", "max_completion_tokens")


@dataclass(frozen=True, slots=True)
class ThinkingRule:
    """One provider/model's resolved thinking behaviour."""

    provider: str
    mode: ThinkingMode = "effort"
    budgets: Mapping[str, int] = field(default_factory=dict)
    disable_mode: DisableMode = "lowest-effort"
    effort_map: Mapping[str, str] = field(default_factory=dict)
    max_tokens_field: MaxTokensField | None = None
    requires_effort: bool = False
    #: Whether the request always carries an output cap. True for the kimi family
    #: (see `is_kimi_family_model`); no row has to state it.
    sends_output_cap_by_default: bool = False
    reasoning_content_field: str | None = None
    requires_reasoning_content_for_tool_calls: bool = False

    def max_tokens_field_or_default(self) -> MaxTokensField:
        """The output-cap field: the row's own value, else OMP's default rule.

        OMP sends ``max_tokens`` only for the vendors its resolver lists
        (``resolve.ts:403-411``); those rows name the field explicitly, so an
        unnamed field means the ``max_completion_tokens`` default.
        """
        return self.max_tokens_field or "max_completion_tokens"

    def budget_for(self, effort: str) -> int | None:
        """The token budget this rule gives one ladder member, ``None`` when it has none."""
        return self.budgets.get(effort)

    def mapped_effort(self, effort: str) -> str:
        """``effort`` in the vendor's own vocabulary (identity when unmapped)."""
        return self.effort_map.get(effort, effort)


@dataclass(frozen=True, slots=True)
class ThinkingRuleRow:
    """One row of ``thinking_rules.json``: a provider default or a model-scoped override."""

    provider: str
    matcher: Matcher | None
    value: str | None
    mode: ThinkingMode | None
    budgets: Mapping[str, int]
    disable_mode: DisableMode | None
    effort_map: Mapping[str, str]
    max_tokens_field: MaxTokensField | None
    requires_effort: bool | None
    reasoning_content_field: str | None
    requires_reasoning_content_for_tool_calls: bool | None
    source: str

    def selects(self, model_id: str) -> bool:
        if self.matcher is None or self.value is None:
            return False
        return matches(self.matcher, self.value, model_id)


def _text(entry: Mapping[str, object], key: str, provider: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"thinking rule for provider {provider!r} is missing a non-empty {key!r}")
    return value


def _literal[T: str](value: object, allowed: tuple[T, ...], *, key: str, provider: str) -> T | None:
    if value is None:
        return None
    if value not in allowed:
        raise ValueError(f"thinking rule for provider {provider!r} has an unknown {key}: {value!r}")
    return cast(T, value)


def _int_map(value: object, *, key: str, provider: str, tables: Mapping[str, Mapping[str, int]]) -> Mapping[str, int]:
    if value is None:
        return {}
    if isinstance(value, str):
        # A named table: the six numbers of one OMP budget table live once, at
        # the top of the file, and every row that reads it names it.
        table = tables.get(value)
        if table is None:
            raise ValueError(f"thinking rule for provider {provider!r} names an unknown budget table: {value!r}")
        return table
    if not isinstance(value, dict):
        raise ValueError(f"thinking rule for provider {provider!r} field {key!r} must be an object")
    parsed: dict[str, int] = {}
    for raw_key, raw_value in cast(dict[str, object], value).items():
        if not isinstance(raw_value, int) or isinstance(raw_value, bool) or raw_value <= 0:
            raise ValueError(f"thinking rule for provider {provider!r} field {key!r}.{raw_key} must be a positive integer")
        parsed[raw_key] = raw_value
    return parsed


def _str_map(value: object, *, key: str, provider: str) -> Mapping[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"thinking rule for provider {provider!r} field {key!r} must be an object")
    parsed: dict[str, str] = {}
    for raw_key, raw_value in cast(dict[str, object], value).items():
        if not isinstance(raw_value, str) or not raw_value:
            raise ValueError(f"thinking rule for provider {provider!r} field {key!r}.{raw_key} must be a non-empty string")
        parsed[raw_key] = raw_value
    return parsed


def _row(raw: object, tables: Mapping[str, Mapping[str, int]]) -> ThinkingRuleRow:
    if not isinstance(raw, dict):
        raise ValueError("thinking rule entries must be objects")
    entry = cast(dict[str, object], raw)
    provider = require_provider_id(_text(entry, "provider", "?"), source="thinking_rules.json")
    match_entry = entry.get("match")
    matcher: Matcher | None = None
    value: str | None = None
    if match_entry is not None:
        if not isinstance(match_entry, dict):
            raise ValueError(f"thinking rule for provider {provider!r} has a non-object 'match'")
        match_map = cast(dict[str, object], match_entry)
        raw_matcher = match_map.get("type")
        if raw_matcher not in MATCHERS:
            raise ValueError(f"thinking rule for provider {provider!r} has an unknown matcher: {raw_matcher!r}")
        matcher = cast(Matcher, raw_matcher)
        value = _text(match_map, "value", provider)
    requires_effort = entry.get("requires_effort")
    if requires_effort is not None and not isinstance(requires_effort, bool):
        raise ValueError(f"thinking rule for provider {provider!r} field 'requires_effort' must be a boolean")
    rc_required = entry.get("requires_reasoning_content_for_tool_calls")
    if rc_required is not None and not isinstance(rc_required, bool):
        raise ValueError(f"thinking rule for provider {provider!r} field 'requires_reasoning_content_for_tool_calls' must be a boolean")
    rc_field = entry.get("reasoning_content_field")
    if rc_field is not None and (not isinstance(rc_field, str) or not rc_field):
        raise ValueError(f"thinking rule for provider {provider!r} field 'reasoning_content_field' must be a non-empty string")
    source = entry.get("source")
    return ThinkingRuleRow(
        provider=provider,
        matcher=matcher,
        value=value,
        mode=_literal(entry.get("mode"), _THINKING_MODES, key="mode", provider=provider),
        budgets=_int_map(entry.get("budgets"), key="budgets", provider=provider, tables=tables),
        disable_mode=_literal(entry.get("disable_mode"), _DISABLE_MODES, key="disable_mode", provider=provider),
        effort_map=_str_map(entry.get("effort_map"), key="effort_map", provider=provider),
        max_tokens_field=_literal(entry.get("max_tokens_field"), _MAX_TOKENS_FIELDS, key="max_tokens_field", provider=provider),
        requires_effort=requires_effort if isinstance(requires_effort, bool) else None,
        reasoning_content_field=rc_field if isinstance(rc_field, str) else None,
        requires_reasoning_content_for_tool_calls=rc_required if isinstance(rc_required, bool) else None,
        source=source if isinstance(source, str) else "",
    )


def _load() -> Mapping[str, tuple[ThinkingRuleRow, ...]]:
    payload = json.loads(_resource_files("voidcode.provider").joinpath("thinking_rules.json").read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        raise ValueError("thinking_rules.json must hold a 'rows' list")
    raw_tables = payload.get("budget_tables")
    tables = {
        name: _int_map(table, key=f"budget_tables.{name}", provider=name, tables={})
        for name, table in cast(dict[str, object], raw_tables if isinstance(raw_tables, dict) else {}).items()
    }
    rows: dict[str, list[ThinkingRuleRow]] = {}
    for raw in cast(list[object], payload["rows"]):
        row = _row(raw, tables)
        rows.setdefault(row.provider, []).append(row)
    return {provider: tuple(provider_rows) for provider, provider_rows in rows.items()}


#: Canonical provider id -> its rows, in declaration order (provider default first).
THINKING_RULES: Final[Mapping[str, tuple[ThinkingRuleRow, ...]]] = _load()


def _apply(rule: ThinkingRule, row: ThinkingRuleRow) -> ThinkingRule:
    updates: dict[str, object] = {}
    if row.mode is not None:
        updates["mode"] = row.mode
    if row.budgets:
        updates["budgets"] = row.budgets
    if row.disable_mode is not None:
        updates["disable_mode"] = row.disable_mode
    if row.effort_map:
        updates["effort_map"] = row.effort_map
    if row.max_tokens_field is not None:
        updates["max_tokens_field"] = row.max_tokens_field
    if row.requires_effort is not None:
        updates["requires_effort"] = row.requires_effort
    if row.reasoning_content_field is not None:
        updates["reasoning_content_field"] = row.reasoning_content_field
    if row.requires_reasoning_content_for_tool_calls is not None:
        updates["requires_reasoning_content_for_tool_calls"] = row.requires_reasoning_content_for_tool_calls
    return replace(rule, **updates) if updates else rule


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
    """The thinking rule for one provider/model: provider default, then first scoped match."""
    rule = ThinkingRule(provider=provider_id)
    rows = THINKING_RULES.get(provider_id, ())
    for row in rows:
        if row.matcher is None:
            rule = _apply(rule, row)
            break
    for row in rows:
        if row.selects(model_id):
            rule = _apply(rule, row)
            break
    if is_kimi_family_model(model_id):
        rule = replace(rule, sends_output_cap_by_default=True)
    return rule


__all__ = [
    "DisableMode",
    "MaxTokensField",
    "THINKING_RULES",
    "ThinkingMode",
    "ThinkingRule",
    "ThinkingRuleRow",
    "is_kimi_family_model",
    "thinking_rule_for",
]
