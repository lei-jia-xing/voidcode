from voidcode.provider.litellm_backend import _extract_token_usage


def test_extract_token_usage_reports_cache_read_and_write_tokens() -> None:
    usage = _extract_token_usage(
        {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_tokens_details": {
                    "cached_tokens": 80,
                    "cache_creation_input_tokens": 5,
                },
            }
        }
    )
    assert usage is not None
    assert usage.metadata_payload() == {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 80,
        "cache_write_tokens": 5,
        "uncached_input_tokens": 20,
    }
    assert usage.cache_hit_rate == 0.8
