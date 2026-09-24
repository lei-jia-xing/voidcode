"""Catalog-wide invariants for the derived context budget and the Google usage split."""

from __future__ import annotations

from voidcode.provider.google_native import GoogleGenAIProvider
from voidcode.provider.model_catalog import ProviderModelMetadata, _load_static_catalog
from voidcode.provider.pricing_rules import usage_cost_usd
from voidcode.provider.thinking_rules import thinking_rule_for


def test_every_shipped_row_has_a_sane_input_budget() -> None:
    """The resolved budget is either the vendor's own ``limit.input`` or at least half
    the window: the derived form can never collapse to a token or two.

    Upstream sometimes reports an output cap equal to the whole window, which the
    old ``max(1, context - output)`` turned into a 1-token budget for 48 of 432
    shipped rows (e.g. ``xai/grok-4.6``: context 500000 -> budget 1), compacting
    every prompt.
    """
    offenders: list[str] = []
    catalog = _load_static_catalog()
    # A wiped or truncated artifact would make every loop below iterate nothing:
    # assert the precondition so the suite fails instead of passing vacuously.
    assert catalog, "the shipped catalog is empty"
    empty = sorted(provider_id for provider_id, models in catalog.items() if not models)
    assert not empty, f"providers without catalog rows: {empty}"

    for provider_id, models in catalog.items():
        for model_id, metadata in models.items():
            context_window = metadata.context_window
            budget = metadata.max_input_tokens
            if context_window is None or budget is None:
                offenders.append(f"{provider_id}/{model_id}: no budget (context={context_window})")
                continue
            if metadata.derived_max_input_tokens and budget < context_window // 2:
                offenders.append(f"{provider_id}/{model_id}: derived budget {budget} < half of {context_window}")
            if budget <= 0:
                offenders.append(f"{provider_id}/{model_id}: budget {budget}")

    assert not offenders, "insane input budget:\n" + "\n".join(offenders)


def test_a_window_sized_output_cap_falls_back_to_the_whole_window() -> None:
    """The exact shape that used to collapse: output cap == context window."""
    metadata = ProviderModelMetadata(context_window=500000, max_output_tokens=500000)

    assert metadata.max_input_tokens == 500000
    assert metadata.derived_max_input_tokens is True


def test_a_small_but_real_output_cap_still_derives() -> None:
    metadata = ProviderModelMetadata(context_window=200000, max_output_tokens=32000)

    assert metadata.max_input_tokens == 168000


def test_google_usage_splits_the_prompt_total_into_uncached() -> None:
    """``prompt_token_count`` includes the cached part; without the split every Google
    turn priced its input at zero."""

    class _Usage:
        prompt_token_count = 3000
        candidates_token_count = 100
        cached_content_token_count = 2000

    class _Response:
        usage_metadata = _Usage()

    usage = GoogleGenAIProvider._usage(_Response())

    assert usage is not None
    assert usage.input_tokens == 3000
    assert usage.cache_read_tokens == 2000
    assert usage.uncached_input_tokens == 1000

    metadata = _load_static_catalog()["google"]["gemini-2.5-pro"]
    cost = usage_cost_usd(provider_id="google", model_id="gemini-2.5-pro", usage=usage, metadata=metadata)
    assert cost is not None
    assert cost > (metadata.cost_per_output_token or 0.0) * 100


def test_the_kimi_family_flag_is_exactly_the_kimi_model_lineage() -> None:
    """`sends_output_cap_by_default` is a model identity, not a provider: OMP's
    `alwaysSendMaxTokens = facts.is("kimi")` classifies by model lineage
    (`rules/classes/kimi.kdl`), so any provider shipping a kimi-lineage model
    sends the cap."""
    offenders: list[str] = []
    catalog = _load_static_catalog()
    assert catalog, "the shipped catalog is empty"
    lineage_models = [
        f"{provider_id}/{model_id}"
        for provider_id, models in catalog.items()
        for model_id in models
        if "kimi" in model_id.lower().replace("/", "-").split("-")
    ]
    # The claim is cross-provider, so an artifact that lost the kimi rows must fail
    # here rather than pass with nothing to check.
    assert {name.split("/")[0] for name in lineage_models} >= {"moonshot", "fireworks", "opencode-zen", "opencode-go"}

    for provider_id, models in catalog.items():
        for model_id in models:
            expected = "kimi" in model_id.lower().replace("/", "-").split("-")
            actual = thinking_rule_for(provider_id, model_id).sends_output_cap_by_default
            if actual is not expected:
                offenders.append(f"{provider_id}/{model_id}: flag={actual} expected={expected}")

    assert not offenders, "kimi-family identity mismatch:\n" + "\n".join(offenders)
    # The flag is true for models outside the two kimi providers too.
    assert thinking_rule_for("fireworks", "accounts/fireworks/models/kimi-k3").sends_output_cap_by_default is True
    assert thinking_rule_for("opencode-go", "deepseek-v4-pro").sends_output_cap_by_default is False


def test_the_gateway_deepseek_models_replay_their_reasoning_content() -> None:
    """The deleted `deepseek-` prefix covered `opencode-go/deepseek-v4-*`; the data
    rows restore it (`opencode-go.kdl:19-23,43-44`)."""
    from voidcode.provider.openai_native import _requires_reasoning_content_with_tool_calls  # noqa: PLC0415

    for provider_id, model_id in (
        ("deepseek", "deepseek-v4-pro"),
        ("opencode-go", "deepseek-v4-pro"),
        ("opencode-zen", "deepseek-v4-flash"),
    ):
        rule = thinking_rule_for(provider_id, model_id)
        assert rule.reasoning_content_field == "reasoning_content"
        assert rule.requires_reasoning_content_for_tool_calls is True
        assert _requires_reasoning_content_with_tool_calls(provider_name=provider_id, model_name=model_id) is True

    # A model outside the family is unaffected.
    assert _requires_reasoning_content_with_tool_calls(provider_name="openai", model_name="gpt-5.4") is False
