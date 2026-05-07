from __future__ import annotations

from typing import Any

import pytest

STABLE_PREFIX_MIN_RATIO = 0.35
PROMPT_TOKEN_GROWTH_MAX_PCT = 15
WARMUP_REQUEST_INDEX = 1


def assert_cache_metric_or_skip(usage: Any, reason_log: str) -> None:
    cache_read_tokens = getattr(usage, "cache_read_tokens", None)
    if cache_read_tokens is None:
        pytest.skip(f"cache metric unavailable: {reason_log}")
    assert cache_read_tokens > 0
