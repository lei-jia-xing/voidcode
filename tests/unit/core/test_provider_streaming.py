"""Streamed turns aggregate to propose_turn shape with observational progress."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.fixtures.core_factory import loopback_server
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.core.engine import Core
from voidcode.sdk.provider import ProviderExecutionError


def _call(identity: str, tool_name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": tool_name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(message={"role": "assistant", "content": output, "tool_calls": list(calls)}, finish="tool_calls" if calls else "stop")


def _configure(tmp_path: Path, server: OpenAILoopbackServer) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "voidcode.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model": {"model": "endpoint/exact-model"},
                "policy": {"approval_mode": "yolo"},
                "providers": {"endpoint": {"api_key": "stream-secret", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
                "plugins": {"files": {"enabled": False}},
            }
        ),
        encoding="utf-8",
    )
    return workspace


def _open(tmp_path: Path) -> Core:
    return open_core(tmp_path / "workspace", state_dir=tmp_path / "state")


def test_streamed_turn_aggregates_to_propose_shape_with_progress(tmp_path: Path) -> None:
    with loopback_server([_turn(output="streamed answer")]) as server:
        _configure(tmp_path, server)
        with _open(tmp_path) as core:
            result = core.run("Stream me an answer.")
            assert result.status == "completed", result.error
            assert result.output == "streamed answer"
            assert server.requests[0]["stream"] is True
            events = core.store.events(result.session_id)
            progress = [event for event in events if event.event_type == "core.provider_progress"]
            assert progress, "a streamed turn emits observational progress before its turn"
            assert "".join(str(event.payload["text_delta"]) for event in progress) == "streamed answer"
            turn = next(event for event in events if event.event_type == "core.model_turn")
            assert max(event.sequence for event in progress) < turn.sequence
            assert progress[0].payload["dispatch_sequence"] == turn.payload["dispatch_sequence"]


def test_non_streamable_provider_falls_back_to_propose_turn(tmp_path: Path) -> None:
    """A registered provider without stream_turn still answers through propose_turn."""
    source = tmp_path / "plugin"
    source.mkdir(parents=True, exist_ok=True)
    (source / "__init__.py").write_text(
        "from voidcode.sdk.plugin import Plugin\n"
        "from voidcode.sdk.provider import ProviderTurnResult\n"
        "class PlainProvider:\n"
        "    name = 'plain'\n"
        "    def propose_turn(self, request):\n"
        "        return ProviderTurnResult(output='plain answer', tool_calls=(), done_reason='stop')\n"
        "def create_plugin(config=None):\n"
        "    return Plugin(providers=(PlainProvider(),))\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "voidcode.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model": {"model": "plain/exact-model"},
                "policy": {"approval_mode": "yolo"},
                "providers": {"plain": {"api_key": "plain-secret", "base_url": "http://127.0.0.1:1/v1"}},
                "plugins": {"files": {"enabled": False}, "plain-fixture": {"path": str(source)}},
            }
        ),
        encoding="utf-8",
    )
    with _open(tmp_path) as core:
        result = core.run("Answer without streaming.")
        assert result.status == "completed", result.error
        assert result.output == "plain answer"
        events = core.store.events(result.session_id)
        assert not [event for event in events if event.event_type == "core.provider_progress"]
        assert any(event.event_type == "core.model_turn" for event in events)


def test_unknown_provider_stream_is_not_configured(tmp_path: Path) -> None:
    from voidcode.core.plugins import PluginRuntime

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with PluginRuntime((), workspace=workspace, revision_id="rev-stream") as runtime:
        assert runtime.provider_supports_streaming("missing") is False
        with pytest.raises(ProviderExecutionError, match="not configured"):
            next(iter(runtime.stream_turn(_turn_request(), provider="missing")))


def test_non_streamable_provider_stream_is_unsupported_feature(tmp_path: Path) -> None:
    from voidcode.core.plugins import PluginRuntime, PluginSource

    source = tmp_path / "plugin"
    source.mkdir(parents=True, exist_ok=True)
    (source / "__init__.py").write_text(
        "from voidcode.sdk.plugin import Plugin\n"
        "class PlainProvider:\n"
        "    name = 'plain'\n"
        "    def propose_turn(self, request):\n"
        "        raise AssertionError('unreachable')\n"
        "def create_plugin(config=None):\n"
        "    return Plugin(providers=(PlainProvider(),))\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with PluginRuntime((PluginSource(root=source, plugin_id="plain-fixture", config={}),), workspace=workspace, revision_id="rev-stream") as runtime:
        assert runtime.provider_supports_streaming("plain") is False
        with pytest.raises(ProviderExecutionError) as excinfo:
            next(iter(runtime.stream_turn(_turn_request(), provider="plain")))
        assert excinfo.value.kind == "unsupported_feature"
        assert excinfo.value.retryable is False


def _turn_request():
    from voidcode.sdk.provider import ProviderMessage, ProviderTurnRequest

    return ProviderTurnRequest(messages=(ProviderMessage("user", "hi"),), provider_name="plain", model_name="m")
