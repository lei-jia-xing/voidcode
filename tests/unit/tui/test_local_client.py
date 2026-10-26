"""The in-process TUI client is the HTTP client without the socket.

Every test drives a real core over a real workspace: the local client must
return the same shapes and the same ``(status, code)`` refusals the HTTP
transport produces, and its event stream must carry the same frames.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from voidcode.bootstrap import open_core
from voidcode.tui.client import CoreClientError
from voidcode.tui.local_client import CoreLocalClient


def _configure(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "voidcode.json").write_text(json.dumps({"schema_version": 1, "plugins": {}}), encoding="utf-8")


def test_local_client_matches_http_shapes_and_refusals(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _configure(workspace)
    with open_core(workspace, state_dir=tmp_path / "state") as core:
        client = CoreLocalClient(core, workspace=workspace)
        health = client.health()
        assert health["status"] == "ok"
        assert health["stage"] == "serving"
        assert health["revision_id"] == core.revision_id
        revision, commands = client.command_catalog()
        assert revision == core.revision_id
        assert any(command["name"] == "status" for command in commands)
        record = client.create_session()
        session_id = str(record["session_id"])
        assert client.get_session(session_id)["session_id"] == session_id
        assert client.list_sessions(limit=1)[0]["session_id"] == session_id
        views_revision, views = client.views(session_id)
        assert views_revision == revision
        assert isinstance(views, list)
        usage = client.usage(session_id)
        assert usage["session_id"] == session_id

        with pytest.raises(CoreClientError) as stale:
            client.invoke_command(session_id, "status", {}, expected_revision="not-the-displayed-revision")
        assert stale.value.status_code == 409
        assert stale.value.code == "stale_revision"

        with pytest.raises(CoreClientError) as unknown:
            client.get_session("no-such-session")
        assert unknown.value.status_code == 404

        with pytest.raises(CoreClientError) as bad_id:
            client.get_session("has/slash")
        assert bad_id.value.status_code == 400
        assert bad_id.value.code == "invalid_params"

        with pytest.raises(CoreClientError) as bad_command:
            client.invoke_command(session_id, "no-such-command", {}, expected_revision=revision)
        assert bad_command.value.status_code == 400


def test_local_client_event_stream_replays_durable_frames(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _configure(workspace)
    with open_core(workspace, state_dir=tmp_path / "state") as core:
        client = CoreLocalClient(core, workspace=workspace)
        revision, _ = client.command_catalog()
        session_id = str(client.create_session()["session_id"])
        client.run(session_id, "hello")

        async def collect() -> list[dict]:
            frames = []
            async for frame in client.events_stream(session_id, follow=False):
                frames.append(frame)
            return frames

        frames = asyncio.run(collect())
        assert len(frames) == 1
        assert frames[0]["session"]["session_id"] == session_id
        assert any(event["type"] == "core.user_message" for event in frames[0]["events"])


def test_local_client_event_stream_follows_new_events(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _configure(workspace)
    with open_core(workspace, state_dir=tmp_path / "state") as core:
        client = CoreLocalClient(core, workspace=workspace)
        revision, _ = client.command_catalog()
        session_id = str(client.create_session()["session_id"])

        async def collect() -> list[dict]:
            frames = []
            async for frame in client.events_stream(session_id, follow=True):
                frames.append(frame)
                if "events" in frame:
                    client.run(session_id, "hello")
                if frame.get("kind") == "event":
                    break
            return frames

        frames = asyncio.run(collect())
        delivered = [event for event in frames[0]["events"]] + [
            frame["event"] for frame in frames[1:] if frame.get("kind") == "event" and "event" in frame
        ]
        assert any(event["type"] == "core.user_message" for event in delivered)
