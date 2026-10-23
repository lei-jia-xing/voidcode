"""Malformed provider counts remain unknown instead of fabricated integers."""

from types import SimpleNamespace

import pytest

from voidcode.capabilities.providers.native._wire_common import usage_int
from voidcode.capabilities.providers.native.anthropic_native import _usage as anthropic_usage
from voidcode.capabilities.providers.native.google_native import GoogleGenAIProvider
from voidcode.capabilities.providers.native.openai_native import _usage as openai_usage


@pytest.mark.parametrize("value", [True, False, -1, -0.1, 1.5, float("nan"), float("inf"), float("-inf"), "12", None])
def test_invalid_counts_remain_unknown_on_all_wires(value):
    assert usage_int(value) is None
    assert openai_usage({"usage": {"prompt_tokens": value, "completion_tokens": value}}) is None
    assert anthropic_usage({"usage": {"input_tokens": value, "output_tokens": value}}) is None
    assert GoogleGenAIProvider._usage(SimpleNamespace(usage_metadata=SimpleNamespace(prompt_token_count=value, candidates_token_count=value))) is None


@pytest.mark.parametrize("value", [0, 12, 12.0])
def test_integral_counts_and_known_zero_are_preserved(value):
    assert usage_int(value) == value
    assert type(usage_int(value)) is int


def test_google_missing_cache_count_does_not_invent_uncached_usage():
    usage = GoogleGenAIProvider._usage(SimpleNamespace(usage_metadata=SimpleNamespace(prompt_token_count=20, candidates_token_count=3)))
    assert usage.input_tokens == 20
    assert usage.cache_read_tokens is None
    assert usage.uncached_input_tokens is None
