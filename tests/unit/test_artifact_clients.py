"""Artifact clients and paged TUI consume real immutable workspace bytes."""

from __future__ import annotations

import base64
import json
import os
import shutil
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fixtures.png import decode_rgb_png, rgb_png
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from tests.unit.test_session_client_conformance import _EXAMPLE, _client, _configure
from voidcode.core.artifacts import MAX_PAGE_BYTES, ArtifactStore
from voidcode.sdk.client import ArtifactPageWire, CoreClientError, RunSpec, SessionClient
from voidcode.sdk.provider import ImageAttachment
from voidcode.sdk.services import ArtifactReference
from voidcode.sdk.tools import EmptyOutput, OpaqueToolBody, OutputBounds, TextOutput, ToolCall, ToolSuccess
from voidcode.sdk.transcript import TranscriptTool
from voidcode.tui.artifact_viewer import InteractiveArtifactViewer, artifact_sources, read_artifact_bytes
from voidcode.tui.keys import Key
from voidcode.tui.term import Terminal
from voidcode.tui.terminal_app import CoreTuiApp


def spill(state: Path, data: bytes, media_type: str) -> ArtifactReference:
    store = ArtifactStore(state / "artifacts")
    identity, writer = store.start_spill(media_type, owner="client-conformance-producer")
    writer.append(data)
    return ArtifactReference(*store.finish(identity))


@pytest.fixture(params=["local", "remote"])
def artifact_client(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[tuple[SessionClient, Path]]:
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    workspace.mkdir()
    (workspace / "voidcode.json").write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    with _client(request.param, workspace, state) as client:
        yield client, state


def test_actual_local_and_rpc_pages_preserve_binary_identity_and_past_eof(artifact_client: tuple[SessionClient, Path]) -> None:
    client, state = artifact_client
    png = rgb_png()
    assert len(png) > MAX_PAGE_BYTES
    assert decode_rgb_png(png)[:2] == (384, 300)
    reference = spill(state, png, "image/png")
    page = client.read_artifact(reference.sha256)
    assert page.artifact == reference and page.offset == 0 and not page.eof
    assert page.data == png[:65536]
    largest = client.read_artifact(reference.sha256, offset=7, limit=MAX_PAGE_BYTES)
    assert largest.data == png[7 : 7 + MAX_PAGE_BYTES]
    assert read_artifact_bytes(client, reference.sha256) == png
    for offset in (reference.bytes, reference.bytes + 23, 10**100):
        final = client.read_artifact(reference.sha256, offset=offset)
        assert (final.offset, final.data, final.eof, final.artifact) == (offset, b"", True, reference)
    empty = spill(state, b"", "text/plain; charset=utf-8")
    final = client.read_artifact(empty.sha256)
    assert final.data == b"" and final.eof and final.artifact.bytes == 0


def test_actual_local_and_rpc_distinguish_bad_request_from_missing(artifact_client: tuple[SessionClient, Path]) -> None:
    client, state = artifact_client
    reference = spill(state, b"binary\x00\xff", "application/octet-stream")
    for digest, kwargs in [
        ("../outside", {}),
        ("A" * 64, {}),
        ("a" * 64 + "\n", {}),
        (reference.sha256, {"offset": -1}),
        (reference.sha256, {"offset": True}),
        (reference.sha256, {"offset": 1.5}),
        (reference.sha256, {"limit": 0}),
        (reference.sha256, {"limit": False}),
        (reference.sha256, {"limit": 1.5}),
        (reference.sha256, {"limit": MAX_PAGE_BYTES + 1}),
    ]:
        with pytest.raises(CoreClientError) as refusal:
            client.read_artifact(digest, **kwargs)  # type: ignore[arg-type]
        assert (refusal.value.status_code, refusal.value.code) == (400, "invalid_params")
    with pytest.raises(CoreClientError) as missing:
        client.read_artifact("f" * 64)
    assert (missing.value.status_code, missing.value.code) == (404, "not_found")
    wire = ArtifactPageWire.from_page(client.read_artifact(reference.sha256))
    payload = wire.model_dump(mode="json")
    assert base64.b64decode(payload["data"], validate=True) == b"binary\x00\xff"
    assert ArtifactPageWire.from_payload(payload).as_page().data == b"binary\x00\xff"
    with pytest.raises(ValueError, match="base64"):
        ArtifactPageWire.from_payload({**payload, "data": "not base64!"})


def settle(app: CoreTuiApp, viewer: InteractiveArtifactViewer) -> None:
    deadline = time.monotonic() + 10
    while viewer.loading_offset is not None and time.monotonic() < deadline:
        app._drain_events()
        time.sleep(0.005)
    assert viewer.loading_offset is None


def test_real_client_tui_pages_keep_unicode_boundaries_and_never_preview_fallback(artifact_client: tuple[SessionClient, Path]) -> None:
    client, state = artifact_client
    data = b"a" * 65535 + "🎯 complete later page\n".encode()
    reference = spill(state, data, "text/plain; charset=utf-8")
    secondary = spill(state, b"stderr is independent", "text/plain; charset=utf-8")
    canonical = spill(state, b'{"status":"ok","whole_record":true}', "application/json")
    result = ToolSuccess(
        "capture",
        EmptyOutput("captured summary", bounds=OutputBounds(reference=reference)),
        OpaqueToolBody({"stdout": reference.as_payload(), "stderr": secondary.as_payload()}),
        result_reference=canonical,
    )
    sources = artifact_sources(
        {
            "output": {"type": "empty", "bounds": {"reference": reference.as_payload()}},
            "body": {"stdout": reference.as_payload(), "stderr": secondary.as_payload()},
            "result_reference": canonical.as_payload(),
        }
    )
    assert [source.sha256 for source in sources] == [reference.sha256, secondary.sha256, canonical.sha256]
    session = client.create_session()
    output_fd = os.open(os.devnull, os.O_WRONLY)
    terminal = Terminal(output_fd=output_fd)
    app = CoreTuiApp(client, session_id=session.session_id, terminal=terminal)
    try:
        app._record_tool_artifact(TranscriptTool(ToolCall("capture", {}), result, False, 1, False))
        app._show_truncated_output()
        viewer = app.active_overlay
        assert isinstance(viewer, InteractiveArtifactViewer)
        settle(app, viewer)
        assert viewer.page is not None and viewer.page.data == data[:65536]
        assert viewer.displayed_text == "a" * 65535
        app._handle_overlay_key(Key("n"))
        settle(app, viewer)
        assert viewer.page is not None and viewer.page.offset == 65536 and viewer.page.eof
        assert viewer.displayed_text == "🎯 complete later page\n"
        assert "captured summary" not in viewer.displayed_text
        app._handle_overlay_key(Key("p"))
        settle(app, viewer)
        assert viewer.displayed_text == "a" * 65535
        app._handle_overlay_key(Key("tab"))
        settle(app, viewer)
        assert viewer.displayed_text == "stderr is independent"
        app._handle_overlay_key(Key("tab"))
        settle(app, viewer)
        assert viewer.media_type == "application/json" and '"whole_record":true' in (viewer.displayed_text or "")
        stale = client.read_artifact(canonical.sha256)
        old_request = viewer.begin_loading(0)
        viewer.begin_loading(7)
        assert not viewer.accept_page(old_request, stale)
        assert viewer.loading_offset == 7
        app._handle_overlay_key(Key("esc"))
        missing = ArtifactReference("f" * 64, 10, "text/plain")
        app._record_tool_artifact(
            TranscriptTool(
                ToolCall("capture", {}),
                ToolSuccess("capture", TextOutput("selected inline stays selected", bounds=OutputBounds(reference=missing))),
                False,
                2,
                False,
            )
        )
        app._show_truncated_output()
        viewer = app.active_overlay
        assert isinstance(viewer, InteractiveArtifactViewer)
        settle(app, viewer)
        assert viewer.page is None and "not stored" in (viewer.load_error or "")
        assert viewer.displayed_text is None
        assert any("artifact://" + missing.sha256 in row for row in viewer.render_rows(100))
    finally:
        app._artifact_exports.cleanup()
        terminal.close()
        os.close(output_fd)


def test_rpc_user_image_reaches_native_provider_with_original_valid_png(tmp_path: Path) -> None:
    server = OpenAILoopbackServer([])
    server.before_reply = lambda request, _index: (
        {"object": "list", "data": [{"id": "test-model"}]}
        if request.get("method") == "GET"
        else openai_response(message={"role": "assistant", "content": "image received"}, finish="stop")
    )
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    plugin = tmp_path / "tally-counter"
    shutil.copytree(_EXAMPLE, plugin, ignore=shutil.ignore_patterns("__pycache__"))
    _configure(workspace, server, plugin)
    png = rgb_png(19, 17)
    try:
        with _client("remote", workspace, state) as client:
            session = client.create_session()
            attachment = ImageAttachment("image/png", base64.b64encode(png).decode("ascii"))
            result = client.run(session.session_id, RunSpec(prompt="Inspect this screenshot", images=(attachment,)))
            assert result.status == "completed", result.error
            requests = [request for request in server.requests if "messages" in request]
            assert requests
            user = next(message for message in requests[-1]["messages"] if message["role"] == "user")
            image = next(block for block in user["content"] if block["type"] == "image_url")
            url = image["image_url"]["url"]
            header, encoded = url.split(",", 1)
            assert header == "data:image/png;base64"
            received = base64.b64decode(encoded, validate=True)
            assert received == png and decode_rgb_png(received)[:2] == (19, 17)
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()
