"""Native read-only info commands: /status /export /copy /context /mcp /hooks /skills /agents /usage."""

from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.core.turns import FinalTurn, ModelTurnFact, TurnRequest
from voidcode.runtime.active_session import ACTIVE_SESSION_REGISTRY
from voidcode.runtime.contracts import (
    RuntimeRequest,
    SessionBusyConflictError,
    UnknownSessionError,
)
from voidcode.runtime.coordinators.inspection import extract_copy_targets, render_session_export_markdown
from voidcode.runtime.events import EventEnvelope
from voidcode.runtime.service import VoidCodeRuntime


class _AnswerProducer:
    def __init__(self, answer: str = "side answer") -> None:
        self.answer = answer

    def produce(self, request: TurnRequest, tool_results: tuple[object, ...], *, session: object):  # type: ignore[no-untyped-def]
        _ = tool_results, session
        return FinalTurn(facts=(ModelTurnFact(request.run_step, "deterministic", request.prompt),), output=self.answer)


def _seeded_session(tmp_path: Path, answer: str = "Intro\n\n```python\nprint(1)\n```\n\n> quoted\n") -> str:
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_AnswerProducer(answer)) as runtime:
        return runtime.run(RuntimeRequest(prompt="seed prompt")).session.session.id


def test_status_panel_fields_present(tmp_path: Path) -> None:
    session_id = _seeded_session(tmp_path)
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_AnswerProducer()) as runtime:
        panel = runtime.session_status_panel(session_id=session_id)
    assert panel.session_id == session_id
    assert panel.status == "completed"
    assert panel.collaboration_mode == "default"
    assert panel.workspace == str(tmp_path.resolve())
    assert panel.turn >= 1


def test_status_panel_unknown_session(tmp_path: Path) -> None:
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_AnswerProducer()) as runtime:
        with pytest.raises((UnknownSessionError, ValueError)):
            runtime.session_status_panel(session_id="does-not-exist")


def test_export_markdown_content_and_filename(tmp_path: Path) -> None:
    session_id = _seeded_session(tmp_path)
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_AnswerProducer()) as runtime:
        export = runtime.export_session_markdown(session_id=session_id)
    assert export.session_id == session_id
    assert export.filename == f"voidcode-session-{session_id}.md"
    assert f"# Session {session_id}" in export.markdown
    assert "## User" in export.markdown
    assert "seed prompt" in export.markdown
    assert "## Assistant" in export.markdown


def test_export_render_orders_turns_and_tools() -> None:
    events = (
        EventEnvelope(session_id="s", sequence=1, event_type="runtime.request_received", source="runtime", payload={"prompt": "do it"}),
        EventEnvelope(
            session_id="s",
            sequence=2,
            event_type="core.model_turn",
            source="core",
            payload={"tool_batch_calls": [{"tool_call_id": "c1", "tool": "read", "arguments": {"path": "a"}}]},
        ),
    )
    markdown = render_session_export_markdown(session_id="s", title=None, events=events, output="done")
    assert markdown.index("## User") < markdown.index("## Assistant")
    assert "do it" in markdown and "done" in markdown


def test_export_write_and_traversal_guard(tmp_path: Path) -> None:
    session_id = _seeded_session(tmp_path)
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_AnswerProducer()) as runtime:
        result = runtime.write_session_export(session_id=session_id, path="exports/out.md")
        assert result["path"] == "exports/out.md"
        assert (tmp_path / "exports" / "out.md").read_text().startswith(f"# Session {session_id}")
        with pytest.raises(ValueError, match="inside the workspace"):
            runtime.write_session_export(session_id=session_id, path="../escape.md")


def test_copy_target_extraction_labels() -> None:
    targets = extract_copy_targets("Intro\n\n```python\nprint(1)\n```\n\n> quoted\n")
    assert [target.label for target in targets] == ["Whole response", "python code", "Blockquote"]
    assert targets[1].text == "print(1)\n"
    assert targets[2].text == "quoted\n"
    assert all(target.preview for target in targets)


def test_copy_targets_from_session_and_empty(tmp_path: Path) -> None:
    session_id = _seeded_session(tmp_path)
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_AnswerProducer()) as runtime:
        targets = runtime.copy_targets(session_id=session_id)
    assert [target.label for target in targets] == ["Whole response", "python code", "Blockquote"]
    assert extract_copy_targets("") == ()
    assert extract_copy_targets("plain text") == (extract_copy_targets("plain text")[0],) or len(extract_copy_targets("plain text")) == 1


def test_context_numbers_match_real_projection(tmp_path: Path) -> None:
    session_id = _seeded_session(tmp_path)
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_AnswerProducer()) as runtime:
        usage = runtime.session_context_usage(session_id=session_id)
        expanded = runtime.session_context_usage(session_id=session_id, expand_items=True)
    assert usage.session_id == session_id
    assert usage.used_tokens > 0
    # Buckets size each segment alone (ceil per segment); the headline sizes
    # the joined payload like the window does — parts land within one token
    # per segment of the headline.
    assert abs(usage.used_tokens - sum(segment.tokens for segment in usage.segments)) <= len(usage.segments)
    assert all(not segment.items for segment in usage.segments)
    assert any(segment.items for segment in expanded.segments)
    assert expanded.used_tokens == usage.used_tokens


def test_busy_gate_matrix_export_gated_others_not(tmp_path: Path) -> None:
    session_id = _seeded_session(tmp_path)
    workspace = tmp_path.resolve()
    ACTIVE_SESSION_REGISTRY.register(workspace=workspace, session_id=session_id, run_id="live-run", metadata={})
    try:
        with VoidCodeRuntime(workspace=tmp_path, turn_producer=_AnswerProducer()) as runtime:
            with pytest.raises(SessionBusyConflictError):
                runtime.export_session_markdown(session_id=session_id)
            with pytest.raises(SessionBusyConflictError):
                runtime.write_session_export(session_id=session_id, path="out.md")
            # Read-only info commands run mid-stream.
            assert runtime.session_status_panel(session_id=session_id).session_id == session_id
            assert isinstance(runtime.copy_targets(session_id=session_id), tuple)
            assert runtime.session_context_usage(session_id=session_id).used_tokens > 0
            assert runtime.session_mcp_inventory(session_id=session_id).session_id == session_id
            assert runtime.session_hooks_listing(session_id=session_id).session_id == session_id
            assert runtime.session_skills_listing(session_id=session_id).session_id == session_id
            assert runtime.session_agents_overview(session_id=session_id).session_id == session_id
            assert runtime.session_token_usage(session_id=session_id).session_id == session_id
    finally:
        ACTIVE_SESSION_REGISTRY.unregister(workspace=workspace, session_id=session_id, run_id="live-run")


def test_mcp_inventory_compact_and_verbose(tmp_path: Path) -> None:
    session_id = _seeded_session(tmp_path)
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_AnswerProducer()) as runtime:
        compact = runtime.session_mcp_inventory(session_id=session_id)
        verbose = runtime.session_mcp_inventory(session_id=session_id, verbose=True)
    assert compact.session_id == session_id and not compact.verbose
    assert verbose.verbose and [s.name for s in verbose.servers] == [s.name for s in compact.servers]
    assert [s.name for s in compact.servers] == sorted(s.name for s in compact.servers)
    for server in compact.servers:
        assert server.command is None and server.url is None and server.auth == server.transport
    for server in verbose.servers:
        assert server.command is not None
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_AnswerProducer()) as runtime:
        with pytest.raises((UnknownSessionError, ValueError)):
            runtime.session_mcp_inventory(session_id="does-not-exist")


def test_hooks_listing_surfaces_and_unknown_session(tmp_path: Path) -> None:
    session_id = _seeded_session(tmp_path)
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_AnswerProducer()) as runtime:
        listing = runtime.session_hooks_listing(session_id=session_id)
    assert listing.session_id == session_id
    # Default workspace has no hooks config: unconfigured reads as disabled + empty.
    assert listing.enabled is False
    assert listing.surfaces == ()
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_AnswerProducer()) as runtime:
        with pytest.raises((UnknownSessionError, ValueError)):
            runtime.session_hooks_listing(session_id="does-not-exist")


def test_hooks_listing_projects_configured_surfaces(tmp_path: Path) -> None:
    from dataclasses import replace

    from voidcode.hook.config import RuntimeHooksConfig

    session_id = _seeded_session(tmp_path)
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_AnswerProducer()) as runtime:
        coordinator = runtime._inspection_coordinator
        coordinator._config = replace(
            coordinator._config,
            hooks=RuntimeHooksConfig(on_session_start=(("echo", "hi"),), pre_tool=(("lint",),), pre_tool_match=("write*",)),
        )
        listing = runtime.session_hooks_listing(session_id=session_id)
    assert listing.enabled is True
    by_surface = {surface.surface: surface for surface in listing.surfaces}
    assert by_surface["session_start"].commands == ("echo hi",)
    assert by_surface["pre_tool"].commands == ("lint",)
    assert by_surface["pre_tool"].match == ("write*",)
    assert by_surface["session_end"].commands == ()


def test_skills_listing_matches_registry(tmp_path: Path) -> None:
    session_id = _seeded_session(tmp_path)
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_AnswerProducer()) as runtime:
        listing = runtime.session_skills_listing(session_id=session_id)
        registry = [skill.name for skill in runtime.list_skill_summaries()]
    assert [skill.name for skill in listing.skills] == sorted(registry)
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_AnswerProducer()) as runtime:
        with pytest.raises((UnknownSessionError, ValueError)):
            runtime.session_skills_listing(session_id="does-not-exist")


def test_agents_overview_contains_current_and_presets(tmp_path: Path) -> None:
    session_id = _seeded_session(tmp_path)
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_AnswerProducer()) as runtime:
        overview = runtime.session_agents_overview(session_id=session_id)
        presets = [agent.id for agent in runtime.list_agent_summaries()]
    assert overview.current_session_id == session_id
    assert session_id in [row.session_id for row in overview.sessions]
    assert [preset.id for preset in overview.presets] == presets
    assert isinstance(overview.tasks, tuple)
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_AnswerProducer()) as runtime:
        with pytest.raises((UnknownSessionError, ValueError)):
            runtime.session_agents_overview(session_id="does-not-exist")


def test_token_usage_scopes_project_session_totals(tmp_path: Path) -> None:
    session_id = _seeded_session(tmp_path)
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_AnswerProducer()) as runtime:
        bare = runtime.session_token_usage(session_id=session_id)
        daily = runtime.session_token_usage(session_id=session_id, scope="daily")
        weekly = runtime.session_token_usage(session_id=session_id, scope="weekly")
        cumulative = runtime.session_token_usage(session_id=session_id, scope="cumulative")
    assert bare.scope == "session"
    for scoped in (daily, weekly, cumulative):
        assert (scoped.input_tokens, scoped.output_tokens, scoped.cost_usd) == (bare.input_tokens, bare.output_tokens, bare.cost_usd)
    assert daily.scope == "daily" and weekly.scope == "weekly" and cumulative.scope == "cumulative"
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_AnswerProducer()) as runtime:
        with pytest.raises((UnknownSessionError, ValueError)):
            runtime.session_token_usage(session_id="does-not-exist")


def test_native_catalog_contains_info_commands(tmp_path: Path) -> None:
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_AnswerProducer()) as runtime:
        names = {command.name: command.source for command in runtime.list_command_summaries()}
    assert names["status"] == "native"
    assert names["export"] == "native"
    assert names["copy"] == "native"
    assert names["context"] == "native"
    assert names["mcp"] == "native"
    assert names["hooks"] == "native"
    assert names["skills"] == "native"
    assert names["agents"] == "native"
    assert names["usage"] == "native"
