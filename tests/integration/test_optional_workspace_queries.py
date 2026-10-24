"""File capabilities depend on optional contracts, not the skills or LSP plugin."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from threading import Thread

import pytest

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.core.activation import BUNDLED_PLUGIN_IDS
from voidcode.core.bootstrap import open_core
from voidcode.sdk.codec import decode_tool_result
from voidcode.sdk.tools import ToolFailure, ToolSuccess

_PROVIDER = """
from hashlib import sha256
from pathlib import Path
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.workspace_queries import RULE_PAGES, DOCUMENT_STATES, RulePage, DocumentState


def create_plugin(config):
    def rule(request, caller):
        assert caller.session_id
        return RulePage("" if config.get("broken") else "style", "a" * 64, "Independent rule provider", None)

    def document(request, caller):
        target = Path(request.path)
        return DocumentState(
            path="wrong-document" if config.get("broken") else target.as_posix(),
            version=1, content_hash=sha256(target.read_bytes()).hexdigest(), position_encoding="utf-16",
        )

    return Plugin(queries=(RULE_PAGES.bind(rule), DOCUMENT_STATES.bind(document)))
"""


def _turn(name: str | None, arguments: dict[str, object] | None = None) -> dict[str, object]:
    message: dict[str, object] = {"role": "assistant", "content": "done" if name is None else None}
    if name is not None:
        message["tool_calls"] = [
            {"id": f"call-{name}-{arguments}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
        ]
    return openai_response(message=message, finish="tool_calls" if name else "stop")


@pytest.mark.parametrize("mode", ["replacement", "absent", "broken"])
def test_file_tools_use_any_optional_workspace_provider(tmp_path: Path, mode: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "note.txt"
    target.write_text("abc\n", encoding="utf-8")
    provider = tmp_path / "alternate-provider"
    provider.mkdir()
    (provider / "__init__.py").write_text(_PROVIDER, encoding="utf-8")
    plugins: dict[str, object] = {name: {"enabled": False} for name in BUNDLED_PLUGIN_IDS}
    if mode != "absent":
        plugins["alternate-provider"] = {"path": str(provider), "config": {"broken": mode == "broken"}}
    edit = {
        "path": "note.txt",
        "startLine": 1,
        "startCharacter": 1,
        "endLine": 1,
        "endCharacter": 2,
        "newText": "Z",
        "expectedHash": hashlib.sha256(b"abc\n").hexdigest(),
        "expectedVersion": 1,
    }
    server = OpenAILoopbackServer(
        [
            _turn("read", {"path": "note.txt"}),
            _turn("read", {"path": "voidcode://rule/style"}),
            _turn("apply_workspace_edit", {"edits": [edit]}),
            _turn(None),
        ]
    )
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        (workspace / "voidcode.json").write_text(
            json.dumps(
                {
                    "model": {"model": "endpoint/test-model"},
                    "providers": {"endpoint": {"base_url": f"http://127.0.0.1:{server.server_port}/v1", "api_key": "test"}},
                    "policy": {"approval_mode": "yolo"},
                    "plugins": plugins,
                }
            ),
            encoding="utf-8",
        )
        with open_core(workspace, state_dir=tmp_path / "state", auto_drain=False) as core:
            assert core.plugin_runtime.plugin_ids == (() if mode == "absent" else ("alternate-provider",))
            result = core.run("Read a rule and apply a version-constrained edit")
            assert result.status == "completed", result.error
            answers = [
                decode_tool_result(event.payload["result"])
                for event in core.store.events(result.session_id)
                if event.event_type == "core.tool_completed"
            ]
            assert len(answers) == 3 and isinstance(answers[0], ToolSuccess)
            expected = ToolSuccess if mode == "replacement" else ToolFailure
            assert isinstance(answers[1], expected), answers[1]
            assert isinstance(answers[2], expected), answers[2]
            assert target.read_text() == ("Zbc\n" if mode == "replacement" else "abc\n")
            if mode == "replacement":
                assert "Independent rule provider" in str(answers[1].output)
            elif mode == "absent":
                assert "unavailable" in answers[1].error
                assert answers[2].diagnostics.details["reason"] == "document_version_unavailable"
            else:
                assert "provider shape violated" in answers[1].error
                assert answers[2].diagnostics.details["reason"] == "document_answer_shape_violation"
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()
