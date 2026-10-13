import pytest

from voidcode.runtime.config.loading import RuntimeTuiThemeConfig
from voidcode.runtime.config.models import TuiCustomThemePayload
from voidcode.tui.theme import validate_theme_configuration


def test_inactive_custom_palette_is_still_validated() -> None:
    config = RuntimeTuiThemeConfig(
        custom={
            "unused-cycle": TuiCustomThemePayload(
                mode="dark",
                vars={"a": "b", "b": "a"},
            )
        }
    )

    with pytest.raises(ValueError, match="cycle"):
        validate_theme_configuration(config)


def test_custom_color_reference_error_keeps_role_and_reports_custom_source() -> None:
    source = "/tmp/配置\nconfig.json"
    field = "tui.theme.custom['brand-dark'].colors['accent']"
    config = RuntimeTuiThemeConfig(
        custom={"brand-dark": TuiCustomThemePayload(mode="dark", colors={"accent": "missing"})},
        field_sources={field: source},
    )

    with pytest.raises(ValueError) as error:
        validate_theme_configuration(config)

    message = str(error.value)
    assert f"runtime config field '{field}'" in message
    assert "got 'missing'" in message
    assert "'accent'" in message
    assert repr(source) in message
    assert "\n" not in message
