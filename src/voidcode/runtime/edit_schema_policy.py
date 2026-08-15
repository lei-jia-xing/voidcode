"""Per-model edit matching strategy selection.

The edit tool applies a fixed fuzzy-matching pipeline to every model, but
models differ in how reliably they reproduce exact file text. A model whose
edits frequently end in ``ambiguous_match`` benefits from a strict profile
that rejects any non-exact match immediately instead of silently applying a
fuzzy transformation the model cannot predict. The matching strategy is
therefore selectable per model from observed edit effectiveness.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .effectiveness import ToolEffectivenessReport

#: Fraction of a model's edit calls ending in ``ambiguous_match`` at or above
#: which the model is considered to have low tolerance for fuzzy matching.
AMBIGUOUS_MATCH_STRICT_THRESHOLD = 0.5


class EditSchema(StrEnum):
    """Matching strategy profile for the edit tool, selectable per model."""

    FLEXIBLE = "flexible"
    """The current 9-replacer fuzzy pipeline; the default for unknown models."""

    STRICT = "strict"
    """Exact-match only; any non-exact input fails with ``ambiguous_match``."""


EditSchemaResolver = Callable[[str | None], EditSchema]
"""Resolves the edit schema for a model (``None`` when the model is unknown)."""


def select_edit_schema(
    model: str | None,
    effectiveness: ToolEffectivenessReport | None,
) -> EditSchema:
    """Select the edit matching schema for ``model`` from observed effectiveness.

    Returns ``EditSchema.STRICT`` when the model's observed edit
    ``ambiguous_match`` error rate is at or above
    :data:`AMBIGUOUS_MATCH_STRICT_THRESHOLD`, otherwise
    ``EditSchema.FLEXIBLE``. Models without observed edit data (including
    ``None`` models) default to ``EditSchema.FLEXIBLE``, preserving current
    behavior.
    """
    if model is None or effectiveness is None:
        return EditSchema.FLEXIBLE
    stats = effectiveness.edit_stats_for_model(model)
    if stats is None or stats.edit_calls <= 0:
        return EditSchema.FLEXIBLE
    ambiguous_rate = stats.edit_ambiguous_match_count / stats.edit_calls
    if ambiguous_rate >= AMBIGUOUS_MATCH_STRICT_THRESHOLD:
        return EditSchema.STRICT
    return EditSchema.FLEXIBLE
