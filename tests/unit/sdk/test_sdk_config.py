"""New kernel config behavior: strict parse, loud rejection, mutation isolation."""

from __future__ import annotations

import pytest

from voidcode.sdk.config import parse_kernel_config


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


def test_provider_wire_and_api_key_masking() -> None:
    default_cfg = parse_kernel_config({"providers": {"openai": {}}})
    assert default_cfg.providers["openai"].wire == "openai-chat"

    with pytest.raises(ValueError):
        parse_kernel_config({"providers": {"openai": {"wire": "custom-wire"}}})

    with pytest.raises(ValueError):
        parse_kernel_config({"providers": {"openai": {"model": "gpt-4o"}}})

    cfg = parse_kernel_config({"providers": {"openai": {"api_key": "sk-super-secret-key"}}})
    assert cfg.providers["openai"].api_key == "sk-super-secret-key"
    assert "sk-super-secret-key" not in repr(cfg.providers["openai"])
    assert "sk-super-secret-key" not in repr(cfg)


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
        parse_kernel_config({"providers": {"openai": {"unknown_field": True}}})
    with pytest.raises(ValueError):
        parse_kernel_config({"plugins": {"plug": {"unknown_field": True}}})
