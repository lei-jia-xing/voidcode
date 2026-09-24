"""Import-time validation for the checked-in rule/catalog data files.

Each of these files addresses providers and models by id. A typo used to become a
row nothing ever consults (the audit's "data nothing validates" class), so the
loaders now fail loudly instead of silently ignoring the entry.
"""

from __future__ import annotations

import pytest

from voidcode.provider.api_routes import _route
from voidcode.provider.model_catalog import _static_catalog_from_payload
from voidcode.provider.pricing_rules import _row as pricing_row
from voidcode.provider.provider_table import _rows_from_payload, require_provider_id
from voidcode.provider.thinking_rules import _row as thinking_row


def test_a_known_provider_id_passes_the_table_check() -> None:
    assert require_provider_id("openai", source="test") == "openai"


def test_an_api_route_naming_no_table_row_fails_at_import() -> None:
    with pytest.raises(ValueError, match="api_routes.json names an unknown provider 'opena1'"):
        _route(
            {
                "provider": "opena1",
                "match": {"type": "exact", "value": "gpt-5.5"},
                "api": "openai-completions",
            }
        )


def test_a_thinking_rule_naming_no_table_row_fails_at_import() -> None:
    with pytest.raises(ValueError, match="thinking_rules.json names an unknown provider 'moonsho'") as failure:
        thinking_row({"provider": "moonsho", "mode": "effort"}, {})

    assert "provider_table.json" in str(failure.value)


def test_a_pricing_rule_naming_no_table_row_fails_at_import() -> None:
    with pytest.raises(ValueError, match="pricing_rules.json names an unknown provider 'xaI'"):
        pricing_row(
            {
                "provider": "xaI",
                "match": {"type": "exact", "value": "grok-4.6"},
                "threshold": 200000,
                "multiplier": 2.0,
            }
        )


def test_the_provider_table_rejects_a_duplicate_id() -> None:
    row = {"id": "openai", "label": "OpenAI", "wire": "openai-completions", "default_base_url": "https://api.openai.com/v1"}

    with pytest.raises(ValueError, match="provider_table.json declares 'openai' twice"):
        _rows_from_payload({"rows": [row, dict(row)]})


def test_the_catalog_rejects_an_unknown_entry_field() -> None:
    """A renamed generator field must fail here instead of being dropped silently,
    which used to leave the model reading as "no metadata" rather than an error."""
    with pytest.raises(ValueError, match=r"gpt-5\.5 carries unknown fields: \['max_outputs'\]"):
        _static_catalog_from_payload({"openai": {"gpt-5.5": {"context_window": 400000, "max_outputs": 128000}}})


def test_the_catalog_accepts_every_field_the_metadata_type_carries() -> None:
    catalog = _static_catalog_from_payload(
        {"openai": {"gpt-5.5": {"context_window": 400000, "max_output_tokens": 128000, "api": "openai-completions"}}}
    )

    metadata = catalog["openai"]["gpt-5.5"]
    assert metadata.context_window == 400000
    assert metadata.max_output_tokens == 128000
    assert metadata.api == "openai-completions"
