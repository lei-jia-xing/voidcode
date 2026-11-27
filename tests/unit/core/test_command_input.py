"""Prepared command inputs are read-only, bounded and revision-bound."""

from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.bootstrap import bundled_sources, open_core
from voidcode.config import dumps_toml
from voidcode.sdk.commands import CommandInputSnapshot
from voidcode.sdk.views import FormViewData, validate_form_submission

_SOURCE = """
from voidcode.sdk.commands import CommandDefinition, CommandPresentation, CommandResult
from voidcode.sdk.plugin import Plugin, StateSchema

class Pick:
    definition = CommandDefinition(name="pick", description="Pick target", text_argument="target",
        argument_schema={"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"], "additionalProperties": False})
    def prepare_input(self, invocation, services):
        return CommandPresentation(title="Pick target", view_type="form", data={"action": "pick", "fields": [
            {"name": "target", "label": "Target", "options": [{"id": "workspace", "label": invocation.context.require_workspace().name}]}]})
    def invoke(self, invocation, services):
        _, version = services.get_state()
        services.replace_state({"target": invocation.arguments["target"]}, expected_version=version)
        return CommandResult(status="ok", output=invocation.arguments["target"])

class Mutating(Pick):
    definition = CommandDefinition(name="mutating", description="Invalid preparation")
    def prepare_input(self, invocation, services):
        _, version = services.get_state()
        services.replace_state({"bad": True}, expected_version=version)
        raise AssertionError("read-only services permitted state mutation")

class Wrong(Pick):
    definition = CommandDefinition(name="wrong", description="Wrong target")

class Malformed(Pick):
    definition = CommandDefinition(name="malformed", description="Malformed data")
    def prepare_input(self, invocation, services):
        return {"view_type": "form"}

class Plain:
    definition = CommandDefinition(name="plain", description="No preparation")
    def invoke(self, invocation, services):
        return CommandResult(status="ok")

def create_plugin(config):
    return Plugin(commands=(Pick(), Mutating(), Wrong(), Malformed(), Plain()), state=StateSchema(version=1))
"""


@pytest.fixture
def core(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "probe"
    source.mkdir()
    (source / "__init__.py").write_text(_SOURCE)
    plugins = {name: {"enabled": False} for name in bundled_sources()}
    plugins["probe"] = {"path": str(source)}
    (workspace / "voidcode.toml").write_text(dumps_toml({"schema_version": 1, "plugins": plugins}))
    with open_core(workspace, state_dir=tmp_path / "state") as instance:
        instance.create_session("s")
        yield instance


def test_preparation_does_not_invoke_and_submission_uses_normal_command(core):
    catalog = core.command_catalog()
    descriptor = next(command for command in catalog.commands if command.name == "pick")
    assert descriptor.has_input and descriptor.text_argument == "target"
    assert not next(command for command in catalog.commands if command.name == "plain").has_input
    before = core.events_after("s")
    snapshot = core.command_input("s", "pick", expected_revision=catalog.revision_id)
    assert CommandInputSnapshot.from_payload(snapshot.as_payload()) == snapshot
    assert core.events_after("s") == before
    assert core.store.plugin_state("probe", "session", "s")[0] is None
    form = FormViewData.from_payload(snapshot.presentation.data)
    assert form.fields[0].options[0].label == "workspace"
    args = validate_form_submission(form, {"target": "workspace"})
    assert core.invoke_command("s", form.action, args, expected_revision=snapshot.revision_id).output == "workspace"
    assert core.store.plugin_state("probe", "session", "s")[0] == {"target": "workspace"}


@pytest.mark.parametrize(
    "name,match",
    [
        ("mutating", "read.only"),
        ("wrong", "different command"),
        ("malformed", "malformed"),
        ("plain", "no interactive"),
        ("missing", "no interactive"),
    ],
)
def test_invalid_preparations_fail_without_committing(core, name, match):
    before = core.events_after("s")
    with pytest.raises((ValueError, RuntimeError, PermissionError), match=match):
        core.command_input("s", name, expected_revision=core.revision_id)
    assert core.events_after("s") == before
    assert core.store.plugin_state("probe", "session", "s")[0] is None


def test_stale_preparation_is_refused_before_handler(core):
    with pytest.raises(ValueError, match="stale"):
        core.command_input("s", "pick", expected_revision="old-revision")
