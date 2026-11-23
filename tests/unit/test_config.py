"""Product config behavior: strict parse, loud rejection, mutation isolation."""

from __future__ import annotations

import json

import pytest

from voidcode.config import parse_config, resolve_providers
from voidcode.sdk.json_values import json_wire_object


def test_policy_approval_mode_type_safety_and_enum() -> None:
    for invalid in ([], {}, 1, 1.0, None, True, False, "unknown_mode", ""):
        with pytest.raises(ValueError):
            parse_config({"policy": {"approval_mode": invalid}})


def test_rejects_malformed_version_types() -> None:
    with pytest.raises(ValueError):
        parse_config({"schema_version": "1"})
    with pytest.raises(ValueError):
        parse_config({"schema_version": True})
    with pytest.raises(ValueError):
        parse_config({"schema_version": 2})


def test_rejects_malformed_identities_and_collisions() -> None:
    for invalid in ("Anthropic", " anthropic", "anthropic ", "1provider", "-provider", "prov id"):
        with pytest.raises(ValueError):
            parse_config({"providers": {invalid: {"base_url": "https://example.invalid/v1"}}})
        with pytest.raises(ValueError):
            parse_config({"plugins": {invalid: {}}})


def test_reasoning_effort_type_safety_and_enum() -> None:
    with pytest.raises(ValueError):
        parse_config({"model": {"reasoning_effort": "ultra"}})
    with pytest.raises(ValueError):
        parse_config({"model": {"reasoning_effort": -1}})
    for non_type in ([], {}, 1.5, True, False):
        with pytest.raises(ValueError):
            parse_config({"model": {"reasoning_effort": non_type}})

    for valid in ("off", "none", "minimal", "low", "medium", "high", "xhigh", "max", 0, 1024):
        cfg = parse_config({"model": {"reasoning_effort": valid}})
        assert cfg.model.reasoning_effort == valid


def test_policy_read_only_strict_boolean() -> None:
    for non_bool in (1, 0, "true", "false", 1.0):
        with pytest.raises(ValueError):
            parse_config({"policy": {"read_only": non_bool}})


def test_provider_entry_is_opaque_and_api_key_masked() -> None:
    default_cfg = parse_config({"providers": {"deepseek": {"base_url": "https://example.invalid/v1"}}})
    assert default_cfg.providers["deepseek"].api_key is None
    assert default_cfg.providers["deepseek"].base_url == "https://example.invalid/v1"

    for non_str in (1, 1.0, True, False, [], {}):
        with pytest.raises(ValueError):
            parse_config({"providers": {"deepseek": {"api_key": non_str, "base_url": "https://example.invalid/v1"}}})
        with pytest.raises(ValueError):
            parse_config({"providers": {"deepseek": {"base_url": non_str}}})

    cfg = parse_config({"providers": {"deepseek": {"api_key": "sk-super-secret-key", "base_url": "https://example.invalid/v1"}}})
    assert "sk-super-secret-key" not in repr(cfg)


def test_provider_vendor_fields_are_an_opaque_verbatim_passthrough() -> None:
    vendor = {
        "headers": {"x-api-version": "2026-01-01", "x-endpoint": "https://example.invalid/v1"},
        "region": "eu-central-1",
        "timeout_seconds": 5,
        "stream": True,
        "retries": None,
        "scopes": ["chat", "tools"],
    }
    cfg = parse_config({"providers": {"azure-ish": {"api_key": "opaque-secret", "base_url": "https://example.invalid/v1", **vendor}}})
    entry = cfg.providers["azure-ish"]
    assert entry.api_key == "opaque-secret"
    assert entry.base_url == "https://example.invalid/v1"
    assert entry.extra["headers"]["x-endpoint"] == "https://example.invalid/v1"
    assert entry.extra["region"] == "eu-central-1"
    assert entry.extra["timeout_seconds"] == 5
    assert entry.extra["stream"] is True
    assert entry.extra["retries"] is None
    assert entry.extra["scopes"] == ("chat", "tools")

    vendor["headers"]["x-api-version"] = "mutated"
    vendor["scopes"].append("mutated")
    assert entry.extra["headers"]["x-api-version"] == "2026-01-01"
    assert entry.extra["scopes"] == ("chat", "tools")
    with pytest.raises(TypeError):
        entry.extra["region"] = "mutated"  # type: ignore[index]

    assert "opaque-secret" not in repr(entry)
    assert "x-endpoint" not in repr(entry)

    with pytest.raises(ValueError, match="nested 'extra'"):
        parse_config({"providers": {"bad": {"base_url": "https://example.invalid/v1", "extra": {"k": "v"}}}})


def test_plugins_path_and_config_mutation_isolation() -> None:
    for bad_path in ("", "   "):
        with pytest.raises(ValueError):
            parse_config({"plugins": {"tool": {"path": bad_path}}})

    raw_config = {"nested": {"items": [1, 2]}}
    cfg = parse_config({"plugins": {"tool": {"config": raw_config}}})

    raw_config["nested"]["items"].append(3)  # type: ignore[attr-defined]
    raw_config["extra"] = "injected"

    assert cfg.plugins["tool"].config["nested"]["items"] == (1, 2)  # type: ignore[index]
    assert "extra" not in cfg.plugins["tool"].config

    with pytest.raises(TypeError):
        cfg.plugins["tool"].config["extra"] = "fail"  # type: ignore[index]


def test_legacy_and_unknown_fields_rejected() -> None:
    with pytest.raises(ValueError):
        parse_config({"unsupported": 1})
    with pytest.raises(ValueError):
        parse_config({"approval": "yolo"})
    with pytest.raises(ValueError):
        parse_config({"tools": {"local": {"enabled": True}}})
    with pytest.raises(ValueError):
        parse_config({"model": {"unknown_field": True}})
    with pytest.raises(ValueError):
        parse_config({"policy": {"unknown_field": True}})
    with pytest.raises(ValueError):
        parse_config({"plugins": {"plug": {"unknown_field": True}}})


def test_tui_prefs_reject_unknown_fields_and_invalid_types() -> None:
    from voidcode.config import parse_config

    cfg = parse_config({"tui": {"border": "borderless", "footer": "compact", "show_session": True}})
    assert (cfg.tui.border, cfg.tui.footer, cfg.tui.show_session) == ("borderless", "compact", True)
    bad_cases = ({"tui": {"border": "round"}}, {"tui": {"footer": "tiny"}}, {"tui": {"show_session": "yes"}})
    bad_cases += ({"tui": {"icons": "ascii"}}, {"tui": {"icons": "glyph"}}, {"tui": {"nope": 1}})
    for bad in bad_cases:
        with pytest.raises(ValueError):
            parse_config(bad)


def test_tui_prefs_cause_zero_revision_churn(tmp_path) -> None:
    """admit_live ignores tui: same file ± tui block admits identical payload/snapshot/config."""
    from voidcode.bootstrap import admit_live, bundled_sources

    base = {"schema_version": 1, "plugins": {}}
    with_prefs = {
        **base,
        "tui": {
            "border": "borderless",
            "footer": "off",
            "show_session": True,
            "banner": {"text": "╲╱\n∨", "colors": ["cyan", "#abcdef"]},
        },
    }
    first = admit_live(parse_config(base), workspace=tmp_path, bundled_sources=bundled_sources(), revision_id="rev-fixed")
    second = admit_live(parse_config(with_prefs), workspace=tmp_path, bundled_sources=bundled_sources(), revision_id="rev-fixed")
    assert dict(first.revision_payload) == dict(second.revision_payload)
    assert dict(first.private_snapshot) == dict(second.private_snapshot)
    assert first.config == second.config


def test_environment_keys_admit_builtin_providers_without_explicit_entries(tmp_path, monkeypatch) -> None:
    from voidcode.bootstrap import admit_live, bundled_sources
    from voidcode.capabilities.providers.registry import PROVIDER_TABLE

    for row in PROVIDER_TABLE:
        for variable in row.env_vars:
            monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "environment-openrouter-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "environment-deepseek-key")
    admission = admit_live(
        parse_config({"model": {"model": "deepseek/a-model"}}),
        workspace=tmp_path,
        bundled_sources=bundled_sources(),
    )
    assert admission.private_snapshot["providers"] == {
        "deepseek": {"api_key": "environment-deepseek-key", "base_url": "https://api.deepseek.com"},
        "openrouter": {"api_key": "environment-openrouter-key", "base_url": "https://openrouter.ai/api/v1"},
    }
    assert admission.validate is not None
    admission.validate(("deepseek", "openrouter"))
    public = json.dumps(json_wire_object(admission.revision_payload))
    assert "environment-openrouter-key" not in public
    assert "environment-deepseek-key" not in public


def test_explicit_credentials_and_null_override_discovered_keys(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "environment-deepseek-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "environment-openrouter-key")
    providers = resolve_providers(
        parse_config(
            {
                "providers": {
                    "deepseek": {"api_key": "configured-key", "base_url": "https://example.invalid/v1"},
                    "openrouter": {"api_key": None},
                }
            }
        )
    )
    assert providers["deepseek"] == {"api_key": "configured-key", "base_url": "https://example.invalid/v1"}
    assert providers["openrouter"] == {"api_key": None, "base_url": "https://openrouter.ai/api/v1"}
    with pytest.raises(ValueError, match="explicit base_url"):
        resolve_providers(parse_config({"providers": {"custom": {}}}))


@pytest.mark.parametrize(
    ("provider", "auth", "env_var"),
    [
        ("google", {"method": "oauth", "access_token": "explicit-oauth-token"}, "GOOGLE_API_KEY"),
        ("google", {"method": "service_account", "service_account_json_path": "account.json"}, "GOOGLE_API_KEY"),
        ("github-copilot", {"method": "token", "token": "explicit-copilot-token"}, "GITHUB_COPILOT_TOKEN"),
    ],
)
def test_explicit_nested_auth_suppresses_discovered_flat_credentials(monkeypatch, provider, auth, env_var) -> None:
    from voidcode.capabilities.providers.registry import PROVIDER_TABLE

    for row in PROVIDER_TABLE:
        for variable in row.env_vars:
            monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv(env_var, "ambient-credential")
    providers = resolve_providers(parse_config({"providers": {provider: {"auth": auth, "base_url": "https://example.invalid"}}}))
    assert providers[provider]["api_key"] is None
    assert providers[provider]["auth"] == auth


def test_explicit_nested_auth_admits_while_an_ambient_key_exists(tmp_path, monkeypatch) -> None:
    from voidcode.bootstrap import admit_live, bundled_sources
    from voidcode.capabilities.providers.registry import PROVIDER_TABLE

    for row in PROVIDER_TABLE:
        for variable in row.env_vars:
            monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "ambient-google-key")
    auth = {"method": "oauth", "access_token": "explicit-oauth-token"}
    admission = admit_live(
        parse_config({"providers": {"google": {"auth": auth, "project": "project", "base_url": "https://example.invalid"}}}),
        workspace=tmp_path,
        bundled_sources=bundled_sources(),
    )
    snapshot = admission.private_snapshot["providers"]["google"]
    assert snapshot["api_key"] is None
    assert snapshot["auth"] == auth


def test_banner_colors_are_validated_and_owned_without_theme_fields() -> None:
    colors = ["cyan", "#abcdef"]
    config = parse_config({"tui": {"banner": {"text": "╲╱\n∨", "colors": colors}}})
    colors[0] = "mutated"
    assert config.tui.banner is not None
    assert config.tui.banner.colors == ("cyan", "#abcdef")
    for banner in ({"colors": []}, {"colors": ["\x1b[31m"]}, {"colors": ["#12"]}, {"text": 7}, {"theme": "dark"}):
        with pytest.raises(ValueError):
            parse_config({"tui": {"banner": banner}})
    with pytest.raises(ValueError):
        parse_config({"tui": {"logo": {}}})
