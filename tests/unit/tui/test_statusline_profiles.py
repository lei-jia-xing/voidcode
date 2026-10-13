from voidcode.runtime.config.loading import RuntimeTuiStatuslineConfig
from voidcode.tui.statusline import StatusLine, StatusSegmentData, statusline_profile_segments, statusline_profiles
from voidcode.tui.term import visible_width
from voidcode.tui.theme import resolve_theme


def test_each_statusline_profile_renders_its_configured_segments_with_bounded_width() -> None:
    data = StatusSegmentData(
        state="probe-status",
        model="probe-model",
        mode="probe-mode",
        path="/probe/workspace",
        session_name="probe-session",
        cost_usd=1.25,
        context_percent=73.0,
        context_window=1000,
        lsp="probe-server",
        input_tokens=123,
        output_tokens=456,
        cache_read_tokens=20,
        cache_write_tokens=30,
        cache_hit_rate=0.125,
    )
    expected_text = {
        "status": "probe-status",
        "model": "probe-model",
        "mode": "probe-mode",
        "path": "probe/workspace",
        "session_name": "probe-session",
        "cost": "$1.25",
        "context_pct": "73.0%",
        "lsp": "probe-server",
        "token_in": "123",
        "token_out": "456",
        "cache_read": "20",
        "cache_write": "30",
        "cache_hit": "12.5%",
    }
    theme = resolve_theme("voidcode-dark", "dark", color_system=None)

    assert set(statusline_profiles()) == {"codex", "claude", "minimal", "compact", "full", "ascii", "custom"}
    for profile in statusline_profiles():
        config = RuntimeTuiStatuslineConfig(
            layout=profile,
            left=("status", "model", "mode", "path") if profile == "custom" else None,
            right=("cost",) if profile == "custom" else None,
        )
        left, right = statusline_profile_segments(profile)
        if profile == "custom":
            left, right = config.left or (), config.right or ()
        line = StatusLine(theme, 200, config)
        rendered = line.render(data)
        for segment in (*left, *right):
            assert expected_text[segment] in rendered
        assert visible_width(rendered) <= 200
        line.set_width(20)
        assert visible_width(line.render(data)) <= 20
