"""Set up a gated live monitor scene on an already-served core, then hold the gate.

Contract (stdout, one line each):
  SCENE-ROOT <root_id>
  SCENE-NODES child=<id> grandchild=<id> sibling=<id>
  SCENE-READY
then blocks until stdin closes or RELEASE is read, releases the provider
gate, waits for the root run thread to return, prints SCENE-RELEASED, exits 0.

Arguments: <workspace_dir> <base_url> <token>
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))  # noqa: E402 - path setup must precede repo imports
sys.path.insert(0, str(_REPO_ROOT / "src"))  # noqa: E402 - path setup must precede repo imports

from tests.provider_loopback import OpenAILoopbackServer, openai_response  # noqa: E402
from voidcode.sdk.client import RunSpec  # noqa: E402
from voidcode.tui.client import CoreRpcClient  # noqa: E402


def _turn(text: str) -> dict[str, object]:
    return openai_response(message={"role": "assistant", "content": text, "tool_calls": []}, finish="stop")


def _child_id(data: Mapping[str, Any] | None) -> str:
    assert isinstance(data, Mapping) and isinstance(data.get("child"), str)
    return str(data["child"])


def main() -> int:
    workspace = Path(sys.argv[1])
    base = sys.argv[2]
    token = sys.argv[3]
    server = OpenAILoopbackServer([_turn("done")] * 30)
    gate = threading.Event()
    entered: dict[str, threading.Event] = {}

    def _hold(payload: dict[str, object], _index: int) -> dict[str, object] | None:
        text = json.dumps(payload.get("messages"), ensure_ascii=False)
        for marker in ("LIVE-CHILD", "LIVE-GRAND", "LIVE-SIB", "LIVE-ROOT"):
            if marker in text:
                entered.setdefault(marker, threading.Event()).set()
                break
        assert gate.wait(timeout=120), "scene never released the provider gate"
        return None

    server.before_reply = _hold
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        plugin_dir = workspace.parent / "livemon_plugin"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        (plugin_dir / "__init__.py").write_text(
            "\n".join(
                [
                    "from voidcode.sdk.commands import CommandDefinition, CommandResult",
                    "from voidcode.sdk.plugin import Plugin",
                    "from voidcode.sdk.tools import ToolEffect",
                    '_SCHEMA = {"type": "object", "properties": {"marker": {"type": "string"}}, "additionalProperties": False}',
                    "class SpawnOne:",
                    "    def __init__(self, command_id):",
                    "        self.definition = CommandDefinition(name=command_id, description='s',",
                    "            argument_schema=_SCHEMA, effects=frozenset({ToolEffect.SPAWN}), busy_gate=False)",
                    "    def invoke(self, invocation, services):",
                    "        marker = str(invocation.arguments.get('marker', 'LIVE'))",
                    "        child = services.spawn_child(f'smoke work {marker}', label=f'{marker}-label')",
                    "        return CommandResult(status='ok', output=child, data={'child': child})",
                    "def create_plugin(config):",
                    "    return Plugin(commands=(SpawnOne(str(config.get('command_id', 'livemon.spawn_one'))),))",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        from voidcode.config import dumps_toml

        (workspace / "voidcode.toml").write_text(
            dumps_toml(
                {
                    "schema_version": 1,
                    "model": {"model": "endpoint/exact-model"},
                    "policy": {"approval_mode": "yolo"},
                    "providers": {"endpoint": {"api_key": "loopback-secret", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
                    "plugins": {
                        "livemon": {"path": str(plugin_dir), "enabled": True, "config": {"command_id": "livemon.spawn_one"}},
                        "livemon2": {"path": str(plugin_dir), "enabled": True, "config": {"command_id": "livemon2.spawn_one"}},
                    },
                }
            ),
            encoding="utf-8",
        )
        with CoreRpcClient(base, token=token) as client:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    if client.health().status == "ok":
                        break
                except Exception:
                    time.sleep(0.1)
            else:
                print("SCENE-FAIL core never healthy", flush=True)
                return 1
            root_id = client.create_session("live-browse-root").session_id
            revision = client.command_catalog().revision_id
            # The server booted on the pre-fixture config: reload to admit the
            # fixture owners before spawning.
            reloaded = client.invoke_command(root_id, "core.reload", {}, expected_revision=revision)
            assert reloaded.status == "ok", reloaded.error
            revision = client.command_catalog().revision_id
            child = client.invoke_command(root_id, "livemon.spawn_one", {"marker": "LIVE-CHILD"}, expected_revision=revision)
            assert child.status == "ok", child.error
            child_id = _child_id(child.data)
            assert entered.setdefault("LIVE-CHILD", threading.Event()).wait(timeout=60), "child never hit provider"
            grandchild = client.invoke_command(child_id, "livemon2.spawn_one", {"marker": "LIVE-GRAND"}, expected_revision=revision)
            assert grandchild.status == "ok", grandchild.error
            grandchild_id = _child_id(grandchild.data)
            assert entered.setdefault("LIVE-GRAND", threading.Event()).wait(timeout=60), "grandchild never hit provider"
            sibling = client.invoke_command(root_id, "livemon.spawn_one", {"marker": "LIVE-SIB"}, expected_revision=revision)
            assert sibling.status == "ok", sibling.error
            sibling_id = _child_id(sibling.data)
            assert entered.setdefault("LIVE-SIB", threading.Event()).wait(timeout=60), "sibling never hit provider"
            assert client.get_session(child_id).status == "running"
            assert client.get_session(grandchild_id).status == "running"
            assert client.get_session(sibling_id).status == "running"

            run_outcome: dict[str, Any] = {}

            def _run_root() -> None:
                try:
                    with CoreRpcClient(base, token=token) as runner:
                        run_outcome["result"] = runner.run(root_id, RunSpec("root work LIVE-ROOT"))
                except Exception as exc:
                    run_outcome["error"] = exc

            run_thread = threading.Thread(target=_run_root, daemon=True)
            run_thread.start()
            assert entered.setdefault("LIVE-ROOT", threading.Event()).wait(timeout=60), "root run never hit provider"
            assert client.get_session(root_id).status == "running"
            print(f"SCENE-ROOT {root_id}", flush=True)
            print(f"SCENE-NODES child={child_id} grandchild={grandchild_id} sibling={sibling_id}", flush=True)
            print("SCENE-READY", flush=True)
            for line in sys.stdin:
                if line.strip() == "RELEASE":
                    break
            gate.set()
            run_thread.join(timeout=90)
            assert not run_thread.is_alive(), "root run thread never returned"
            assert "error" not in run_outcome, run_outcome["error"]
            assert run_outcome["result"].status in ("completed", "failed", "interrupted"), run_outcome["result"]
            print("SCENE-RELEASED", flush=True)
            return 0
    finally:
        gate.set()
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
