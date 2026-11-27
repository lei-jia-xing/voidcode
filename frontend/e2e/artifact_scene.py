"""Prepare durable tool artifacts through a real Core and ordinary PNG read tool."""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from pathlib import Path

from tests.fixtures.png import decode_rgb_png, rgb_png
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import BUNDLED_PLUGIN_IDS, open_core
from voidcode.config import dumps_toml

_PLUGIN = """from pathlib import Path
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.services import ArtifactReference
from voidcode.sdk.tools import EmptyOutput, OpaqueToolBody, OutputBounds, ToolDefinition, ToolSuccess

class Scene:
    definition = ToolDefinition(
        "artifact_scene",
        "Produce immutable artifact viewer sources",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )
    def invoke(self, call, *, context, services):
        text = services.spill_artifact(b"a" * 65535 + "🎯 complete later page\\n".encode(), media_type="text/plain; charset=utf-8")
        stderr = services.spill_artifact("stderr is independent", media_type="text/plain; charset=utf-8")
        binary = services.spill_artifact(b"binary\\x00\\xff\\x80original", media_type="application/octet-stream")
        corrupt = services.spill_artifact(Path(__file__).with_name("corrupt.png").read_bytes(), media_type="image/png")
        missing = ArtifactReference("f" * 64, 10, "text/plain")
        return ToolSuccess(call.tool_name, EmptyOutput("SELECTED_INLINE_SUMMARY", bounds=OutputBounds(reference=text)),
            OpaqueToolBody({"stdout": text.as_payload(), "stderr": stderr.as_payload(), "binary": binary.as_payload(),
                "corrupt_png": corrupt.as_payload(), "missing": missing.as_payload(), "metadata": "WHOLE_STRUCTURED_RECORD"}))

def create_plugin(config):
    return Plugin(tools=(Scene(),))
"""


def prepare(workspace: Path, state: Path) -> dict[str, object]:
    workspace.mkdir(parents=True, exist_ok=True)
    source = workspace.parent / "artifact-scene-plugin"
    source.mkdir(exist_ok=True)
    (source / "__init__.py").write_text(_PLUGIN, encoding="utf-8")
    png = rgb_png()
    assert len(png) > 256 * 1024 and decode_rgb_png(png)[:2] == (384, 300)
    (workspace / "screen").write_bytes(png)
    corrupt = bytearray(png)
    # Break the IDAT zlib header, not merely a CRC some browsers tolerate.
    corrupt[41:43] = b"\x00\x00"
    (source / "corrupt.png").write_bytes(corrupt)
    try:
        decode_rgb_png(bytes(corrupt))
    except ValueError:
        pass
    else:
        raise AssertionError("corrupt fixture decoded")

    def respond(request: dict[str, object], _index: int) -> dict[str, object]:
        if request.get("method") == "GET":
            return {"object": "list", "data": [{"id": "test-model"}]}
        messages = request["messages"]
        prompt = next(message["content"] for message in messages if message["role"] == "user")
        # Follow-up dispatch: tool feedback arrives either as a tool role or, for
        # image-carrying results, as an extra synthetic user message (which drops
        # the assistant tool-call turn by design, so assistant presence is absent).
        if any(message["role"] == "tool" for message in messages) or sum(1 for message in messages if message["role"] == "user") > 1:
            return openai_response(message={"role": "assistant", "content": "scene recorded"}, finish="stop")
        image = prompt == "PNG_SCENE_REQUEST"
        name, arguments = ("read", {"path": "screen"}) if image else ("artifact_scene", {})
        call = {"id": "image" if image else "text", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
        return openai_response(message={"role": "assistant", "content": None, "tool_calls": [call]}, finish="tool_calls")

    server = OpenAILoopbackServer([])
    server.before_reply = respond
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    (workspace / "voidcode.toml").write_text(
        dumps_toml(
            {
                "schema_version": 1,
                "model": {"model": "endpoint/test-model"},
                "providers": {"endpoint": {"api_key": "surface-loopback-key", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
                "policy": {"approval_mode": "yolo"},
                "plugins": {
                    **{identity: {"enabled": identity in {"providers", "files"}} for identity in BUNDLED_PLUGIN_IDS},
                    "artifact-scene": {"path": str(source)},
                },
            }
        ),
        encoding="utf-8",
    )
    try:
        with open_core(workspace, state_dir=state) as core:
            text = core.run("TEXT_SCENE_REQUEST", session_id="artifact-text")
            assert text.status == "completed", text.error
            image = core.run("PNG_SCENE_REQUEST", session_id="artifact-png")
            # Synthetic tool feedback carries the read image to OpenAI-compatible wires.
            assert image.status == "completed", image.error
            manifest: dict[str, object] = {
                "workspace": str(workspace),
                "state": str(state),
                "text_session": text.session_id,
                "image_session": image.session_id,
                "png_bytes": len(png),
                "png_sha256": hashlib.sha256(png).hexdigest(),
            }
            for label, run in (("text", text), ("image", image)):
                event = next(event for event in core.store.events(run.session_id) if event.event_type == "core.tool_completed")
                reference = event.payload["result"]["output"]["bounds"]["reference"]
                manifest[f"{label}_sequence"] = event.sequence
                manifest[f"{label}_reference"] = dict(reference)
            assert manifest["image_reference"]["sha256"] == manifest["png_sha256"]
            return manifest
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


if __name__ == "__main__":
    print(json.dumps(prepare(Path(sys.argv[1]), Path(sys.argv[2]))), flush=True)
