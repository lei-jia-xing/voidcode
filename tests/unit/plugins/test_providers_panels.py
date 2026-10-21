"""Behavior tests for the providers plugin's ``providers`` and ``model`` information panels.

Both panels are read-only reports over facts the kernel and the plugin already own, so the
stub services here serve a fed event history and fail loudly on any write path.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Literal

import voidcode.plugins.providers as providers_plugin
from voidcode.plugins.providers.native.model_catalog import static_catalog_model_ids
from voidcode.plugins.providers.native.provider_table import PROVIDER_TABLE
from voidcode.plugins.providers.panels import DISPATCH_EVENT
from voidcode.sdk.commands import Command, CommandInvocation, CommandResult
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.services import EventView, PluginServices, ToolInvocationReceipt
from voidcode.sdk.tools import ToolEffect
from voidcode.sdk.views import PluginViewDescriptor

DEEPSEEK_KEY = "deepseek-secret-credential-value"
GOOGLE_KEY = "google-secret-credential-value"


class StubServices(PluginServices):
    """Read-only services: the fed event history, and a loud failure for every write path."""

    def __init__(self, events: Sequence[EventView] = ()) -> None:
        self.events = tuple(events)
        self.reads: list[tuple[int, int, bool]] = []

    def read_events(self, *, after_sequence: int = 0, limit: int = 100, include_payload: bool = True) -> tuple[EventView, ...]:
        self.reads.append((after_sequence, limit, include_payload))
        page = [event for event in self.events if event.sequence > after_sequence][:limit]
        # A payload-free read serves no payload, exactly as the kernel's own event read does.
        return tuple(event if include_payload else replace(event, payload=None) for event in page)

    def replace_state(self, state: Mapping[str, object], expected_version: int, scope: Literal["session", "workspace"] = "session") -> int:
        raise AssertionError("provider panels write no state")

    def stage_event(self, event_type: str, payload: Mapping[str, object]) -> None:
        raise AssertionError("provider panels stage no events")

    def invoke_tool(self, name: str, arguments: Mapping[str, object] | None = None, *, expected_revision: str) -> ToolInvocationReceipt:
        raise AssertionError("provider panels invoke no tools")


def _config() -> dict[str, object]:
    return {
        "providers": {
            "deepseek": {"api_key": DEEPSEEK_KEY, "base_url": "https://api.deepseek.example/v1"},
            "google": {"api_key": GOOGLE_KEY, "base_url": "https://generativelanguage.example"},
            "endpoint": {"base_url": "http://127.0.0.1:1234/v1"},
        }
    }


def _plugin(config: Mapping[str, object] | None = None) -> Plugin:
    return providers_plugin.create_plugin(_config() if config is None else config)


def _command(plugin: Plugin, name: str) -> Command:
    return next(command for command in plugin.commands if command.definition.name == name)


def _invoke(plugin: Plugin, name: str, services: PluginServices, arguments: Mapping[str, object] | None = None) -> CommandResult:
    return _command(plugin, name).invoke(_invocation(name, arguments or {}), services)


def _invocation(name: str, arguments: Mapping[str, object]) -> CommandInvocation:
    return CommandInvocation(
        context=ToolContext(
            workspace=Path("/tmp/workspace"),
            session_id="session-1",
            run_id="run-1",
            invocation_id="inv-1",
            revision_id="rev-1",
            actor="client",
            logical_turn="turn-1",
        ),
        name=name,
        arguments=arguments,
    )


def _view(plugin: Plugin, name: str) -> PluginViewDescriptor:
    return next(view for view in plugin.views if view.name == name).render("session-1", _services(), client_capabilities=frozenset())


def _services(events: Sequence[EventView] = ()) -> StubServices:
    return StubServices(events)


def _event(sequence: int, event_type: str, payload: Mapping[str, object], *, source: str = "kernel", actor: str = "kernel") -> EventView:
    return EventView(sequence=sequence, event_type=event_type, run_id="run-1", revision_id="rev-1", source=source, actor=actor, payload=payload)


def _dispatch(sequence: int, provider: str, model: str) -> tuple[EventView, ...]:
    """One real dispatch: the kernel intent that names it, then its completed model turn."""
    dispatch_id = f"dispatch-{sequence}"
    return (
        _event(sequence, DISPATCH_EVENT, {"provider": provider, "model": model, "dispatch_id": dispatch_id}),
        _event(
            sequence + 1,
            "kernel.model_turn",
            {"dispatch_sequence": sequence, "dispatch_id": dispatch_id, "result": {"output": "ok"}},
            source="provider",
            actor="model",
        ),
    )


def _configured_names(result: CommandResult) -> list[str]:
    assert result.data is not None
    return [str(entry["name"]) for entry in result.data["configured"]]  # type: ignore[index,union-attr]


# --- providers ---


def test_providers_joins_the_configured_entries_with_the_shipped_table() -> None:
    result = _invoke(_plugin(), "providers", _services())

    assert result.status == "ok"
    assert result.output is not None and "deepseek" in result.output
    assert _configured_names(result) == ["deepseek", "endpoint", "google"]
    assert result.data is not None
    rows = {str(entry["name"]): entry for entry in result.data["configured"]}  # type: ignore[index,union-attr]
    assert rows["deepseek"]["label"] == "DeepSeek"
    assert rows["deepseek"]["wire"] == "openai-completions"
    assert rows["deepseek"]["models"] == len(static_catalog_model_ids("deepseek"))
    assert rows["endpoint"]["credential"] is False
    assert rows["deepseek"]["credential"] is True
    assert all(isinstance(entry["credential"], bool) for entry in rows.values())  # type: ignore[union-attr]
    unconfigured = {str(entry["name"]) for entry in result.data["unconfigured"]}  # type: ignore[index,union-attr]
    assert unconfigured == {row.id for row in PROVIDER_TABLE} - set(rows)
    assert "openai" in unconfigured and "`openai`" in result.output


def test_providers_reports_credential_presence_without_the_credential() -> None:
    result = _invoke(_plugin(), "providers", _services())

    rendered = json.dumps(result.as_payload())
    assert DEEPSEEK_KEY not in rendered
    assert GOOGLE_KEY not in rendered
    assert "api_key" not in rendered


def test_providers_without_any_configuration_lists_the_whole_shipped_table() -> None:
    result = _invoke(_plugin({}), "providers", _services())

    assert result.status == "ok"
    assert result.output is not None and "No provider is configured in this workspace." in result.output
    assert result.data is not None and not result.data["configured"]
    assert result.data is not None and len(result.data["unconfigured"]) == len(PROVIDER_TABLE)


# --- model ---


def test_model_reports_no_dispatch_in_an_empty_history() -> None:
    services = _services()
    result = _invoke(_plugin(), "model", services)

    assert result.status == "ok"
    assert result.output is not None and "No provider dispatch recorded in this session yet." in result.output
    assert result.data is not None and result.data["last_dispatch"] is None
    assert _configured_names(result) == ["deepseek", "endpoint", "google"]
    assert services.reads and not any(read[2] for read in services.reads)


def test_model_reports_the_most_recent_dispatch_this_session_made() -> None:
    events = (*_dispatch(3, "deepseek", "deepseek-chat"), *_dispatch(9, "google", "gemini-3-pro"))
    services = _services(events)
    result = _invoke(_plugin(), "model", services)

    assert result.status == "ok"
    assert result.data is not None
    assert result.data["last_dispatch"] == {"sequence": 9, "provider": "google", "model": "gemini-3-pro"}
    assert result.output is not None and "`google/gemini-3-pro`" in result.output
    # The payload-free scan finds the intent; only that one event is re-read with its payload.
    assert any(read[2] for read in services.reads)


def test_model_reports_a_turn_without_its_intent_as_no_dispatch() -> None:
    result = _invoke(_plugin(), "model", _services(_dispatch(4, "deepseek", "deepseek-chat")[1:]))

    assert result.status == "ok"
    assert result.data is not None and result.data["last_dispatch"] is None
    assert result.output is not None and "No provider dispatch recorded in this session yet." in result.output


def test_model_lists_the_shipped_catalog_of_one_provider() -> None:
    result = _invoke(_plugin(), "model", _services(), {"provider": "deepseek"})

    assert result.status == "ok"
    expected = static_catalog_model_ids("deepseek")
    assert expected
    assert result.data is not None and result.data["provider"] == "deepseek"
    assert result.data is not None and tuple(result.data["models"]) == expected
    assert result.output is not None and all(f"`{model}`" in result.output for model in expected)


def test_model_reports_a_provider_without_shipped_models() -> None:
    result = _invoke(_plugin(), "model", _services(), {"provider": "ghost"})

    assert result.status == "ok"
    assert result.data is not None and result.data["provider"] == "ghost"
    assert result.data is not None and not result.data["models"]
    assert result.output is not None and "No shipped catalog models for `ghost`." in result.output


def test_model_rejects_a_blank_provider() -> None:
    result = _invoke(_plugin(), "model", _services(), {"provider": "   "})

    assert result.status == "error"
    assert result.error == "provider must be a non-empty provider id"


# --- declarations and views ---


def test_registration_declares_exactly_the_registered_commands() -> None:
    plugin = _plugin()

    definitions = {command.definition.name: command.definition for command in plugin.commands}
    assert set(definitions) == {"providers", "model"}
    for definition in definitions.values():
        assert definition.effects == frozenset({ToolEffect.READ})
        assert definition.busy_gate is False
        assert definition.client_capabilities == frozenset()
    assert definitions["providers"].argument_schema == {"type": "object", "properties": {}, "additionalProperties": False}
    model_schema = definitions["model"].argument_schema
    assert model_schema["additionalProperties"] is False
    assert model_schema["properties"] == {"provider": {"type": "string"}}
    assert "required" not in model_schema


def test_both_panels_register_a_view_under_the_command_name() -> None:
    plugin = _plugin()

    assert [view.name for view in plugin.views] == ["providers", "model"]
    providers_view = _view(plugin, "providers")
    assert providers_view.owner == "providers"
    assert providers_view.title == "Providers"
    assert providers_view.view_type == "markdown"
    assert "deepseek" in str(providers_view.data["content"])
    model_view = _view(plugin, "model")
    assert (model_view.owner, model_view.title, model_view.view_type) == ("providers", "Model", "markdown")
    assert "No provider dispatch recorded in this session yet." in str(model_view.data["content"])


def test_view_render_reports_the_session_dispatch() -> None:
    services = _services(_dispatch(3, "deepseek", "deepseek-chat"))
    view = next(view for view in _plugin().views if view.name == "model")

    descriptor = view.render("session-1", services, client_capabilities=frozenset())

    assert "`deepseek/deepseek-chat`" in str(descriptor.data["content"])
