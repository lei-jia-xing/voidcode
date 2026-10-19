"""New kernel config behavior: strict parse, loud rejection, mutation isolation."""

from __future__ import annotations

import pytest

from voidcode.sdk.config import KernelProviderEntry, parse_kernel_config


def test_policy_approval_mode_type_safety_and_enum() -> None:
    for invalid in ([], {}, 1, 1.0, None, True, False, "unknown_mode", ""):
        with pytest.raises(ValueError):
            parse_kernel_config({"policy": {"approval_mode": invalid}})


def test_rejects_malformed_version_types() -> None:
    with pytest.raises(ValueError):
        parse_kernel_config({"schema_version": True})
    with pytest.raises(ValueError):
        parse_kernel_config({"schema_version": 1.0})
    with pytest.raises(ValueError):
        parse_kernel_config({"schema_version": 2})


def test_rejects_malformed_identities_and_collisions() -> None:
    for invalid in ("Anthropic", " anthropic", "anthropic ", "1provider", "-provider", "prov id"):
        with pytest.raises(ValueError):
            parse_kernel_config({"providers": {invalid: {}}})
        with pytest.raises(ValueError):
            parse_kernel_config({"plugins": {invalid: {}}})


def test_reasoning_effort_type_safety_and_enum() -> None:
    with pytest.raises(ValueError):
        parse_kernel_config({"model": {"reasoning_effort": ["high"]}})
    with pytest.raises(ValueError):
        parse_kernel_config({"model": {"reasoning_effort": {"level": "high"}}})
    with pytest.raises(ValueError):
        parse_kernel_config({"model": {"reasoning_effort": "ultra"}})

    for valid in ("none", "low", "medium", "high"):
        cfg = parse_kernel_config({"model": {"reasoning_effort": valid}})
        assert cfg.model.reasoning_effort == valid


def test_policy_read_only_strict_boolean() -> None:
    for non_bool in (1, 0, "true", "false", 1.0):
        with pytest.raises(ValueError):
            parse_kernel_config({"policy": {"read_only": non_bool}})


def test_provider_entry_is_opaque_and_api_key_masked() -> None:
    # The configured name IS the selection: no wire vocabulary exists in the neutral config, so
    # every vendor field is unknown to the kernel and none of them is rejected.
    default_cfg = parse_kernel_config({"providers": {"deepseek": {"base_url": "https://example.invalid/v1"}}})
    assert default_cfg.providers["deepseek"].base_url == "https://example.invalid/v1"
    assert default_cfg.providers["deepseek"].extra == {}

    with pytest.raises(ValueError):
        parse_kernel_config({"providers": {"openai": {"api_key": ""}}})

    with pytest.raises(ValueError):
        parse_kernel_config({"providers": {"openai": {"base_url": 7}}})

    cfg = parse_kernel_config({"providers": {"openai": {"api_key": "sk-super-secret-key"}}})
    assert cfg.providers["openai"].api_key == "sk-super-secret-key"
    assert "sk-super-secret-key" not in repr(cfg.providers["openai"])
    assert "sk-super-secret-key" not in repr(cfg)


def test_provider_vendor_fields_are_an_opaque_verbatim_passthrough() -> None:
    # Anything that is not the typed api_key/base_url is the provider's own vocabulary: the
    # kernel stores it without validating, renaming, or reinterpreting a single key.
    vendor = {
        "headers": {"x-api-version": "2026-01-01", "x-endpoint": "https://example.invalid/v1"},
        "region": "eu-central-1",
        "timeout_seconds": 5,
        "stream": True,
        "retries": None,
        "scopes": ["chat", "tools"],
    }
    cfg = parse_kernel_config({"providers": {"azure-ish": {"api_key": "opaque-secret", "base_url": "https://example.invalid/v1", **vendor}}})
    entry = cfg.providers["azure-ish"]
    assert entry.api_key == "opaque-secret"
    assert entry.base_url == "https://example.invalid/v1"
    assert entry.extra["headers"]["x-endpoint"] == "https://example.invalid/v1"
    assert entry.extra["region"] == "eu-central-1"
    assert entry.extra["timeout_seconds"] == 5
    assert entry.extra["stream"] is True
    assert entry.extra["retries"] is None
    assert entry.extra["scopes"] == ("chat", "tools")

    # The snapshot is owned: mutating the parsed source cannot change what the config holds.
    vendor["headers"]["x-api-version"] = "mutated"
    vendor["scopes"].append("mutated")
    assert entry.extra["headers"]["x-api-version"] == "2026-01-01"
    assert entry.extra["scopes"] == ("chat", "tools")
    with pytest.raises(TypeError):
        entry.extra["region"] = "mutated"  # type: ignore[index]

    # A repr cannot sanitize what the kernel does not recognize, so extras are never rendered.
    assert "opaque-secret" not in repr(entry)
    assert "x-endpoint" not in repr(entry)

    # The typed fields keep their own validation, and the opaque bag cannot shadow them.
    with pytest.raises(ValueError):
        KernelProviderEntry(api_key="k", extra={"api_key": "shadow"})
    with pytest.raises(ValueError):
        KernelProviderEntry(base_url="https://example.invalid/v1", extra={"base_url": "shadow"})
    with pytest.raises(ValueError):
        KernelProviderEntry(extra={"region": object()})
    with pytest.raises(ValueError):
        KernelProviderEntry(extra={"": "unnamed"})


def test_plugins_path_and_config_mutation_isolation() -> None:
    for bad_path in ("", "   "):
        with pytest.raises(ValueError):
            parse_kernel_config({"plugins": {"tool": {"path": bad_path}}})

    raw_config = {"nested": {"items": [1, 2]}}
    cfg = parse_kernel_config({"plugins": {"tool": {"config": raw_config}}})

    raw_config["nested"]["items"].append(3)  # type: ignore[attr-defined]
    raw_config["extra"] = "injected"

    assert cfg.plugins["tool"].config["nested"]["items"] == (1, 2)  # type: ignore[index]
    assert "extra" not in cfg.plugins["tool"].config

    with pytest.raises(TypeError):
        cfg.plugins["tool"].config["extra"] = "fail"  # type: ignore[index]


def test_legacy_and_unknown_fields_rejected() -> None:
    with pytest.raises(ValueError):
        parse_kernel_config({"unsupported": 1})
    with pytest.raises(ValueError):
        parse_kernel_config({"approval": "yolo"})
    with pytest.raises(ValueError):
        parse_kernel_config({"tools": {"local": {"enabled": True}}})
    with pytest.raises(ValueError):
        parse_kernel_config({"model": {"unknown_field": True}})
    with pytest.raises(ValueError):
        parse_kernel_config({"policy": {"unknown_field": True}})
    with pytest.raises(ValueError):
        parse_kernel_config({"plugins": {"plug": {"unknown_field": True}}})
