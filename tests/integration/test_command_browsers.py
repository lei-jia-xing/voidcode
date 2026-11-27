"""Browse and edit plugin state through the admitted client command surface."""

from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.bootstrap import bundled_sources, open_core
from voidcode.config import dumps_toml
from voidcode.sdk.views import ChoicesViewData, FormViewData, MarkdownViewData, choices_dispatch, validate_form_submission
from voidcode.tui.local_client import CoreLocalClient


@pytest.fixture
def client(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skills = workspace / ".voidcode" / "skills" / "guide"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("---\nname: guide\ndescription: Test guide\n---\n\n" + "Read the workspace.\n" * 300)
    plugins = {name: {"enabled": False} for name in bundled_sources()}
    plugins.update({"skills": {"enabled": True, "config": {"enabled": True}}, "todo": {"enabled": True}})
    (workspace / "voidcode.toml").write_text(dumps_toml({"schema_version": 1, "plugins": plugins}))
    with open_core(workspace, state_dir=tmp_path / "state") as core:
        adapter = CoreLocalClient(core, workspace=workspace)
        adapter.create_session("s")
        yield adapter


def _invoke(client, name, arguments=None):
    return client.invoke_command("s", name, arguments or {}, client.command_catalog().revision_id)


def test_skill_choices_details_and_next_page_survive_wire_decoding(client):
    listed = _invoke(client, "skills")
    assert listed.status == "ok"
    name, arguments = choices_dispatch(ChoicesViewData.from_payload(listed.presentation.data), "guide")
    shown = _invoke(client, name, arguments)
    assert shown.status == "ok"
    detail = MarkdownViewData.from_payload(shown.presentation.data)
    next_page = next(action for action in detail.actions if action.label == "Next page")
    following = _invoke(client, next_page.action, next_page.arguments)
    assert following.status == "ok" and following.data["offset"] == shown.data["next_offset"]
    assert "Previous page" in [action.label for action in MarkdownViewData.from_payload(following.presentation.data).actions]


def test_todo_editor_holds_fixed_revision_and_rejects_another_writer(client):
    revision = client.command_catalog().revision_id
    before = client.get_session("s").last_event_sequence
    prepared = client.command_input("s", "todo.add", revision)
    assert client.get_session("s").last_event_sequence == before
    form = FormViewData.from_payload(prepared.presentation.data)
    created = _invoke(client, form.action, validate_form_submission(form, {"phase": "Build", "items": "First\nSecond"}))
    assert created.status == "ok"
    detail = MarkdownViewData.from_payload(created.presentation.data)
    edit = next(action for action in detail.actions if action.label == "Edit: First")
    editor = _invoke(client, edit.action, edit.arguments)
    assert editor.status == "ok"
    update_form = FormViewData.from_payload(editor.presentation.data)
    assert update_form.arguments["expected_revision"] == 1
    latest_add = FormViewData.from_payload(client.command_input("s", "todo.add", revision).presentation.data)
    assert _invoke(client, latest_add.action, validate_form_submission(latest_add, {"phase": "Build", "items": "Third"})).status == "ok"
    stale = _invoke(client, update_form.action, validate_form_submission(update_form, {"op": "done"}))
    assert stale.status == "error" and "changed" in stale.error
    current = _invoke(client, "todo")
    assert current.data["revision"] == 2
    assert current.data["phases"][0]["tasks"][0]["status"] == "in_progress"
    fresh = FormViewData.from_payload(_invoke(client, edit.action, edit.arguments).presentation.data)
    assert _invoke(client, fresh.action, validate_form_submission(fresh, {"op": "done"})).status == "ok"
    settled = _invoke(client, "todo")
    assert settled.data["phases"][0]["tasks"][0]["status"] == "completed"
    assert settled.data["phases"][0]["tasks"][1]["status"] == "in_progress"


def test_internal_form_helpers_stay_callable_without_polluting_human_help(client):
    descriptor = next(command for command in client.command_catalog().commands if command.name == "todo.update")
    assert descriptor.slash_visible is False
    help_result = _invoke(client, "help")
    assert "/todo edit" in help_result.output
    assert "/todo update" not in help_result.output
    assert any(command["name"] == "todo.update" for command in help_result.data["commands"])
