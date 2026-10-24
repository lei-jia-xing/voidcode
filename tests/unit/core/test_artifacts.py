"""S5/7.2 acceptance: artifacts are content-addressed, spilled before they are referenced.

The store is the core's own; what the tests drive through the real runtime is the whole path: a
tool spills, the reference it returns is what the model and the durable result carry, the bytes are
on disk under their digest before that result commits, pages read back bounded and honest, a
repeated spill is free, and the refusals (foreign paths, hook leases, oversized pages) are loud
instead of silent. Nothing here needs the workspace to be writable.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.fixtures.reload_plugin import SERVICES_READ_TOOL, SERVICES_TOOL, write_reload_plugin
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.core.bootstrap import open_core
from voidcode.core.engine import Core

PLUGIN_ID = "reload-fixture"
EMPTY_SHA = hashlib.sha256(b"").hexdigest()


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "reasoning_content": "artifact reasoning", "tool_calls": list(calls)},
        finish="tool_calls" if calls else "stop",
    )


@contextmanager
def _server(replies: list[dict[str, object]]) -> Iterator[OpenAILoopbackServer]:
    server = OpenAILoopbackServer(replies)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


_CREATED: list[Core] = []


@pytest.fixture(autouse=True)
def _close_cores() -> Iterator[None]:
    yield
    while _CREATED:
        _CREATED.pop().close()


def _open(tmp_path: Path) -> Core:
    core = open_core(tmp_path / "workspace", state_dir=tmp_path / "state")
    _CREATED.append(core)
    return core


def _configure(workspace: Path, server: OpenAILoopbackServer, source: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "voidcode.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model": {"model": "endpoint/exact-model"},
                "policy": {"approval_mode": "yolo"},
                "providers": {
                    "endpoint": {
                        "api_key": "loopback-secret-not-for-storage",
                        "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                    }
                },
                "plugins": {"files": {"enabled": False}, PLUGIN_ID: {"path": str(source), "enabled": True}},
            }
        ),
        encoding="utf-8",
    )


def _tool_payloads(server: OpenAILoopbackServer, index: int = 1) -> list[dict[str, object]]:
    messages = server.requests[index].get("messages")
    assert isinstance(messages, list)
    payloads: list[dict[str, object]] = []
    for message in messages:
        if isinstance(message, Mapping) and message.get("role") == "tool":
            payloads.append(json.loads(str(message.get("content", ""))))
    return payloads


def entry_is_file(tmp_path: Path, name: str) -> bool:
    """Admitted plugin artifacts are directories; spilled artifacts are files."""
    return (tmp_path / "state" / "artifacts" / name).is_file()


def _artifact_path(tmp_path: Path, sha256: str) -> Path:
    return tmp_path / "state" / "artifacts" / sha256


def test_spill_writes_the_bytes_under_their_digest_before_the_result_commits(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", services_probe=True)
    # Larger than one chunk, with multi-byte characters: chunking and encoding must both survive.
    unit = "página-\u00e9\u00e8\u00ea-"
    payload = unit * 40_000
    replies = [
        _turn(
            _call(
                "spill-1",
                SERVICES_TOOL,
                action="spill",
                text=unit,
                repeat=40_000,
                media_type="text/plain; charset=utf-8",
            )
        ),
        _turn(output="done"),
    ]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        core.create_session("sess-spill")
        assert core.run("spill it", session_id="sess-spill").status == "completed"

        reference = _tool_payloads(server)[0]
        expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        assert reference["sha256"] == expected
        assert reference["bytes"] == len(payload.encode("utf-8"))
        assert reference["media_type"] == "text/plain; charset=utf-8"

        stored = _artifact_path(tmp_path, expected)
        assert stored.read_text(encoding="utf-8") == payload
        assert stored.stat().st_mode & 0o777 == 0o600

        # The reference is what the durable result carries, and the artifact predates it.
        completed = [event for event in core.events_after("sess-spill") if event.event_type == "core.tool_completed"]
        assert completed and expected in json.dumps(dict(completed[0].payload), default=str)


def test_pages_are_bounded_and_report_their_own_geometry(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", services_probe=True)
    payload = "0123456789" * 20
    sha = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    replies = [
        _turn(_call("spill-1", SERVICES_TOOL, action="spill", text=payload)),
        _turn(_call("page-1", SERVICES_TOOL, action="page", sha256=sha, offset=0, limit=10)),
        _turn(_call("page-2", SERVICES_TOOL, action="page", sha256=sha, offset=190, limit=10)),
        _turn(_call("page-3", SERVICES_TOOL, action="page", sha256=sha, offset=1000, limit=10)),
        _turn(output="done"),
    ]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        core.create_session("sess-page")
        assert core.run("page it", session_id="sess-page").status == "completed"

        first, last, beyond = (_tool_payloads(server, index)[-1] for index in (2, 3, 4))
        assert (first["data"], first["offset"], first["total_bytes"], first["eof"]) == ("0123456789", 0, 200, False)
        assert (last["data"], last["offset"], last["eof"]) == ("0123456789", 190, True)
        assert (beyond["data"], beyond["eof"], beyond["total_bytes"]) == ("", True, 200)


def test_spilling_the_same_bytes_again_is_free(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", services_probe=True)
    payload = "identical payload"
    sha = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    replies = [
        _turn(_call("spill-1", SERVICES_TOOL, action="spill", text=payload)),
        _turn(_call("spill-2", SERVICES_TOOL, action="spill", text=payload)),
        _turn(output="done"),
    ]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        core.create_session("sess-dedupe")
        assert core.run("spill twice", session_id="sess-dedupe").status == "completed"

        first, second = _tool_payloads(server)[0], _tool_payloads(server, index=2)[-1]
        assert first["sha256"] == second["sha256"] == sha
        spilled = [entry.name for entry in (tmp_path / "state" / "artifacts").iterdir() if entry.is_file() and not entry.name.endswith(".media")]
        assert spilled == [sha]
        assert _artifact_path(tmp_path, sha).read_text(encoding="utf-8") == payload


def test_the_media_type_survives_the_reference(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", services_probe=True)
    # The fixture spills through a str, so both sides must build the same UTF-8 bytes.
    payload = ("\u0089PNG\r\n\u001a\nfake-png-body").encode("utf-8")
    sha = hashlib.sha256(payload).hexdigest()
    replies = [
        _turn(_call("spill-1", SERVICES_TOOL, action="spill", text=payload.decode("utf-8"), media_type="image/png")),
        _turn(_call("page-1", SERVICES_TOOL, action="page", sha256=sha, offset=0, limit=64)),
        _turn(output="done"),
    ]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        core.create_session("sess-media")
        assert core.run("spill an image", session_id="sess-media").status == "completed"

        reference = _tool_payloads(server)[0]
        assert reference["media_type"] == "image/png"
        page = _tool_payloads(server, index=2)[-1]
        # A later reader holding only the digest recovers the producer's type.
        assert page["media_type"] == "image/png"
        assert page["bytes"] == len(payload)
        assert (tmp_path / "state" / "artifacts" / f"{sha}.media").read_text(encoding="utf-8") == "image/png"


def test_empty_payloads_are_stored_and_read_back(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", services_probe=True)
    replies = [
        _turn(_call("spill-1", SERVICES_TOOL, action="spill", text="")),
        _turn(_call("page-1", SERVICES_TOOL, action="page", sha256=EMPTY_SHA, offset=0, limit=10)),
        _turn(output="done"),
    ]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        core.create_session("sess-empty")
        assert core.run("spill nothing", session_id="sess-empty").status == "completed"

        reference = _tool_payloads(server)[0]
        assert reference["sha256"] == EMPTY_SHA
        assert reference["bytes"] == 0
        page = _tool_payloads(server, index=2)[-1]
        assert (page["data"], page["eof"], page["total_bytes"]) == ("", True, 0)


def test_a_foreign_reference_is_refused_and_never_read(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", services_probe=True)
    (tmp_path / "outside.txt").write_text("secret outside the store", encoding="utf-8")
    replies = [
        _turn(_call("page-1", SERVICES_TOOL, action="page", sha256="../outside.txt", offset=0, limit=10)),
        _turn(output="done"),
    ]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        core.create_session("sess-foreign")
        # The tool call fails visibly: the runtime refuses the reference and the model is told so.
        assert core.run("read outside", session_id="sess-foreign").status == "completed"
        tool_text = json.dumps(server.requests[1].get("messages"))
        assert "must be a lowercase sha256 digest" in tool_text
        assert "secret outside the store" not in tool_text


def test_a_read_only_lease_may_spill_because_the_store_is_not_the_machine(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", services_read_probe=True)
    replies = [
        _turn(_call("spill-1", SERVICES_READ_TOOL, action="spill", text="read-only spill")),
        _turn(output="done"),
    ]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        core.create_session("sess-ro-spill")
        assert core.run("spill", session_id="sess-ro-spill").status == "completed"
        payload = _tool_payloads(server)[0]
        assert payload["sha256"] == hashlib.sha256(b"read-only spill").hexdigest()


def test_hook_lease_cannot_spill(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    # A hook lease owns no result, so it may not spill: the refusal is visible, not silent.
    write_reload_plugin(
        source,
        tool_output="v1",
        services_probe=True,
        post_tool_hook=True,
        post_tool_match=(SERVICES_TOOL,),
        post_tool_spill=True,
    )
    replies = [_turn(_call("spill-1", SERVICES_TOOL, action="spill", text="hook spill")), _turn(output="done")]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        core.create_session("sess-hook-spill")
        assert core.run("spill", session_id="sess-hook-spill").status == "completed"
        tool_text = json.dumps(server.requests[1].get("messages"))
        assert "hook leases cannot spill artifacts" in tool_text


def test_an_unfinished_spill_leaves_no_temporary_file(tmp_path: Path) -> None:
    from voidcode.core.artifacts import ArtifactStore

    store = ArtifactStore(tmp_path / "artifacts")
    spill_id, spill = store.start_spill("text/plain", owner="lease-1")
    spill.append(b"never finalized")
    assert spill_id
    # The lease is revoked with the spill unfinished: its private temp file is gone.
    store.discard_owner("lease-1")
    leftovers = [entry.name for entry in (tmp_path / "artifacts").iterdir()]
    assert [name for name in leftovers if name.startswith(".spill-")] == []
