"""Provider/model reports and explicit, read-only model discovery."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Literal

import pytest

import voidcode.plugins.providers as providers_plugin
from tests.provider_loopback import OpenAILoopbackServer
from voidcode.capabilities.providers.native.model_catalog import static_catalog_model_ids
from voidcode.capabilities.providers.registry import PROVIDER_TABLE
from voidcode.sdk.commands import Command, CommandInvocation, CommandResult
from voidcode.sdk.hooks import RequestWindow
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.provider import ProviderTurnResult
from voidcode.sdk.services import PluginServices, ToolInvocationReceipt
from voidcode.sdk.transcript import TranscriptDispatch, TranscriptEntry

DEEPSEEK_KEY = "deepseek-secret-credential-value"
GOOGLE_KEY = "google-secret-credential-value"


@pytest.fixture
def model_server() -> Iterator[OpenAILoopbackServer]:
    server = OpenAILoopbackServer([])
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


class StubServices(PluginServices):
    """Read-only services: the fed event history, and a loud failure for every write path."""

    def __init__(self, events: Sequence[TranscriptEntry] = ()) -> None:
        self.events = tuple(events)
        self.latest: list[tuple[str, str | None]] = []

    def latest_event(self, event_type: str, run_id: str | None = None):
        self.latest.append((event_type, run_id))
        assert event_type == "core.provider_intent"
        candidates = [entry for entry in self.events if isinstance(entry.content, TranscriptDispatch)]
        if run_id is not None:
            candidates = [entry for entry in candidates if entry.run_id == run_id]
        if not candidates:
            return None
        return max(candidates, key=lambda entry: entry.sequence)

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


def _services(events: Sequence[TranscriptEntry] = ()) -> StubServices:
    return StubServices(events)


def _dispatch(sequence: int, provider: str, model: str) -> tuple[TranscriptEntry, ...]:
    return (
        TranscriptEntry(sequence, "run-1", "rev-1", "core", TranscriptDispatch(provider, model, RequestWindow(0, 0))),
        TranscriptEntry(sequence + 1, "run-1", "rev-1", "provider", ProviderTurnResult(output="ok"), dispatch_sequence=sequence),
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
    unconfigured = {str(entry["name"]) for entry in result.data["unconfigured"]}  # type: ignore[index,union-attr]
    assert unconfigured == {row.id for row in PROVIDER_TABLE} - set(rows)
    assert "openai" in unconfigured and "`openai`" in result.output


def test_providers_reports_configuration_without_credentials() -> None:
    result = _invoke(_plugin(), "providers", _services())

    rendered = json.dumps(result.as_payload())
    assert DEEPSEEK_KEY not in rendered
    assert GOOGLE_KEY not in rendered
    assert "api_key" not in rendered
    assert "credential" not in rendered


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
    assert services.latest == [("core.provider_intent", None)]


def test_model_reports_the_most_recent_dispatch_this_session_made() -> None:
    events = (*_dispatch(3, "deepseek", "deepseek-chat"), *_dispatch(9, "google", "gemini-3-pro"))
    services = _services(events)
    result = _invoke(_plugin(), "model", services)

    assert result.status == "ok"
    assert result.data is not None
    assert result.data["last_dispatch"] == {"sequence": 9, "provider": "google", "model": "gemini-3-pro"}
    assert result.output is not None and "`google/gemini-3-pro`" in result.output
    assert services.latest == [("core.provider_intent", None)]


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


def test_model_rejects_an_unknown_provider() -> None:
    result = _invoke(_plugin(), "model", _services(), {"provider": "ghost"})

    assert result.status == "error"
    assert result.error is not None and "unknown provider id 'ghost'" in result.error
    assert "endpoint" in result.error


def test_model_rejects_a_known_provider_without_configuration() -> None:
    result = _invoke(_plugin(), "model", _services(), {"provider": "openrouter"})

    assert result.status == "error"
    assert result.error == "provider 'openrouter' is not configured in this workspace"


def test_model_lists_a_shipped_catalog_without_configuration() -> None:
    result = _invoke(_plugin(), "model", _services(), {"provider": "openai"})

    assert result.status == "ok"
    assert result.data is not None and tuple(result.data["models"]) == static_catalog_model_ids("openai")


def test_model_rejects_a_blank_provider() -> None:
    result = _invoke(_plugin(), "model", _services(), {"provider": "   "})

    assert result.status == "error"
    assert result.error == "provider must be a non-empty provider id"


@pytest.mark.parametrize("auth_scheme", ["bearer", "none"])
def test_model_discovers_live_ids_without_inference_or_credential_leaks(model_server, auth_scheme) -> None:
    model_server.replies.append({"object": "list", "data": [{"id": "vendor/Model.v2"}, {"id": "vendor/other"}, {"id": "vendor/Model.v2"}]})
    plugin = _plugin(
        {
            "providers": {
                "openrouter": {
                    "api_key": "catalog-only-key",
                    "base_url": f"http://127.0.0.1:{model_server.server_port}/v1",
                    "auth_scheme": auth_scheme,
                }
            }
        }
    )
    try:
        result = _invoke(plugin, "model", _services(), {"provider": "openrouter"})
    finally:
        if plugin.close is not None:
            plugin.close()

    assert result.status == "ok", result.error
    assert result.as_payload()["data"] == {"provider": "openrouter", "models": ["vendor/Model.v2", "vendor/other"]}
    assert model_server.requests == [{"method": "GET", "path": "/v1/models"}]
    headers = {name.lower(): value for name, value in model_server.request_headers[0].items()}
    assert headers.get("authorization") == ("Bearer catalog-only-key" if auth_scheme == "bearer" else None)
    assert "catalog-only-key" not in json.dumps(result.as_payload())


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (401, {"error": {"message": "catalog-only-key"}}),
        (200, {"data": [{}]}),
        (200, {"data": "catalog-only-key"}),
    ],
)
def test_model_discovery_errors_remain_errors_without_retry_or_secret_echo(model_server, status, payload) -> None:
    model_server.model_listing_status = status
    model_server.replies.append(payload)
    plugin = _plugin({"providers": {"openrouter": {"api_key": "catalog-only-key", "base_url": f"http://127.0.0.1:{model_server.server_port}/v1"}}})
    try:
        result = _invoke(plugin, "model", _services(), {"provider": "openrouter"})
    finally:
        if plugin.close is not None:
            plugin.close()

    assert result.status == "error"
    assert result.error
    assert result.data is None
    assert model_server.requests == [{"method": "GET", "path": "/v1/models"}]
    assert "catalog-only-key" not in json.dumps(result.as_payload())
    if status == 401:
        assert "401" in result.error


def test_view_render_reports_the_session_dispatch() -> None:
    services = _services(_dispatch(3, "deepseek", "deepseek-chat"))
    view = next(view for view in _plugin().views if view.name == "model")

    descriptor = view.render("session-1", services)

    assert "`deepseek/deepseek-chat`" in str(descriptor.data["content"])


def test_pending_dispatch_returns_newest_by_sequence_regardless_of_feed_order():
    events = (*_dispatch(201, "endpoint", "pending-model"), *_dispatch(3, "deepseek", "deepseek-chat"))
    services = _services(events)
    result = _invoke(_plugin(), "model", services)
    assert result.data["last_dispatch"] == {"sequence": 201, "provider": "endpoint", "model": "pending-model"}
    assert services.latest == [("core.provider_intent", None)]
