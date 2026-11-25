"""Pure composer live-band layout: budgets, ordering, tails, and streams."""

from __future__ import annotations

import re

from voidcode.sdk.views import PluginViewDescriptor
from voidcode.tui.rendering import LiveRegion, LiveToolSnapshot, layout_live_region
from voidcode.tui.styles import Styles


def _plain(rows: tuple[str, ...]) -> list[str]:
    return [re.sub(r"\x1b\[[0-9;]*m", "", row) for row in rows]


def _live_view() -> PluginViewDescriptor:
    return PluginViewDescriptor(
        owner="subagents",
        name="monitor",
        title="Live status",
        view_type="status",
        data={"label": "Agents", "value": "running"},
        live=True,
    )


def _tool(name: str, tail: tuple[str, ...] = ()) -> LiveToolSnapshot:
    return LiveToolSnapshot(tool_name=name, elapsed_seconds=1.0, icon=None, tail=tail)


def test_no_space_or_no_content_lays_out_nothing() -> None:
    styles = Styles(None)
    assert layout_live_region(LiveRegion(remaining=0, views=(_live_view(),), tools=(_tool("build"),)), width=80, styles=styles) == ()
    assert layout_live_region(LiveRegion(remaining=-1), width=80, styles=styles) == ()
    assert layout_live_region(LiveRegion(remaining=5), width=80, styles=styles) == ()


def test_live_views_yield_one_row_to_active_tools() -> None:
    region = LiveRegion(remaining=3, views=(_live_view(),), tools=(_tool("build_engine"),))
    rows = layout_live_region(region, width=80, styles=Styles(None))

    assert len(rows) == 3
    plain = _plain(rows)
    assert "Live status" in plain[0]
    assert "Agents: running" in plain[1]
    assert "build_engine" in plain[2]


def test_more_tools_than_space_uses_the_compact_summary_and_newest_headers() -> None:
    region = LiveRegion(remaining=2, tools=(_tool("first"), _tool("second"), _tool("third")))
    plain = _plain(layout_live_region(region, width=80, styles=Styles(None)))

    assert len(plain) == 2
    assert "running 3 tools" in plain[0]
    assert "third" in plain[1]


def test_only_the_latest_tool_receives_the_remaining_tail_budget() -> None:
    region = LiveRegion(
        remaining=4,
        tools=(
            _tool("first", ("first tail",)),
            _tool("latest", ("latest one", "latest two", "latest three")),
        ),
    )
    plain = _plain(layout_live_region(region, width=80, styles=Styles(None)))

    assert len(plain) == 4
    assert "first" in plain[0] and "latest" in plain[1]
    assert "first tail" not in "\n".join(plain)
    assert "latest two" in plain[2] and "latest three" in plain[3]


def test_stream_rows_keep_only_the_newest_live_space() -> None:
    region = LiveRegion(
        remaining=2,
        stream_rows=("oldest", "middle", "newest"),
        has_stream_dispatch=True,
    )
    plain = _plain(layout_live_region(region, width=80, styles=Styles(None)))

    assert plain == ["middle", "newest"]


def test_views_alone_fill_the_remaining_budget() -> None:
    region = LiveRegion(remaining=2, views=(_live_view(),))
    plain = _plain(layout_live_region(region, width=80, styles=Styles(None)))

    assert len(plain) == 2
    assert "Live status" in plain[0]
    assert "Agents: running" in plain[1]
