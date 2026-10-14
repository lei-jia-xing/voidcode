from __future__ import annotations

import hashlib
import json
import os
import select
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.provider_loopback import (
    OpenAILoopbackServer,
    configure_openai_provider,
    openai_response,
)
from voidcode.cli.support import EXIT_APPROVAL_REQUIRED
from voidcode.provider.model_catalog import static_catalog_metadata
from voidcode.provider.pricing_rules import usage_cost_usd
from voidcode.provider.protocol import ProviderTokenUsage
from voidcode.runtime.contracts import RuntimeRequest
from voidcode.runtime.service import VoidCodeRuntime


def _provider_process_environment(root: Path) -> dict[str, str]:
    paths = {name: root / name for name in ("home", "config", "cache", "data", "state", "localappdata")}
    for path in paths.values():
        path.mkdir(parents=True)
    project_root = Path(__file__).resolve().parents[2]
    return {
        "APPDATA": str(paths["config"]),
        "DEEPSEEK_API_KEY": "loopback-key",
        "HOME": str(paths["home"]),
        "LOCALAPPDATA": str(paths["localappdata"]),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(project_root / "src"),
        "USERPROFILE": str(paths["home"]),
        "VOIDCODE_DB_PATH": str(root / "voidcode.sqlite3"),
        "XDG_CACHE_HOME": str(paths["cache"]),
        "XDG_CONFIG_HOME": str(paths["config"]),
        "XDG_DATA_HOME": str(paths["data"]),
        "XDG_STATE_HOME": str(paths["state"]),
    }


def _start_provider_server(
    replies: list[dict[str, object]],
) -> tuple[OpenAILoopbackServer, threading.Thread]:
    server = OpenAILoopbackServer(replies)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _stop_provider_server(
    server: OpenAILoopbackServer,
    thread: threading.Thread,
) -> None:
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def _read_acp_message(process: subprocess.Popen[str], *, timeout: float = 10) -> dict[str, object]:
    assert process.stdout is not None
    deadline = time.monotonic() + timeout
    message = bytearray()
    while time.monotonic() < deadline:
        ready, _, _ = select.select(
            [process.stdout],
            [],
            [],
            max(0.0, deadline - time.monotonic()),
        )
        assert ready, "ACP process did not produce its JSON-RPC response"
        chunk = os.read(process.stdout.fileno(), 1)
        if not chunk:
            break
        message.extend(chunk)
        if chunk == b"\n":
            break
    assert message, "ACP process closed stdout before its JSON-RPC response"
    return json.loads(message)


def test_cli_run_and_resume_use_the_private_openai_provider(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "sample.txt"
    source.write_text("before from provider\n", encoding="utf-8")
    expected_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    reasoning_text = "Read the requested file before considering the approved write."
    server, thread = _start_provider_server(
        [
            openai_response(
                message={
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": reasoning_text,
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-cli-read",
                            "type": "function",
                            "function": {
                                "name": "read",
                                "arguments": json.dumps({"path": "sample.txt"}),
                            },
                        },
                        {
                            "index": 1,
                            "id": "call-cli-write",
                            "type": "function",
                            "function": {
                                "name": "write",
                                "arguments": json.dumps(
                                    {
                                        "path": "sample.txt",
                                        "content": "after from provider\n",
                                        "expectedHash": expected_hash,
                                    }
                                ),
                            },
                        },
                    ],
                },
                finish="tool_calls",
            ),
            openai_response(
                message={"role": "assistant", "content": "The file was updated."},
                finish="stop",
            ),
        ]
    )
    configure_openai_provider(
        workspace,
        f"http://127.0.0.1:{server.server_address[1]}/v1",
    )
    env = _provider_process_environment(tmp_path / "process")
    prompt = "Please inspect sample.txt, then replace its contents."
    try:
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "voidcode",
                "run",
                prompt,
                "--workspace",
                str(workspace),
                "--json",
            ],
            capture_output=True,
            check=False,
            env=env,
            text=True,
            timeout=30,
        )
        assert run.returncode == EXIT_APPROVAL_REQUIRED, run.stderr
        payload = json.loads(run.stdout)
        assert payload["session"]["status"] == "waiting"
        session_id = payload["session"]["session"]["id"]
        blocked = payload["blocked"]
        assert blocked["kind"] == "approval_required"
        assert source.read_text(encoding="utf-8") == "before from provider\n"

        resume = subprocess.run(
            [
                sys.executable,
                "-m",
                "voidcode",
                "sessions",
                "resume",
                session_id,
                "--workspace",
                str(workspace),
                "--approval-request-id",
                blocked["request_id"],
                "--approval-decision",
                "allow",
            ],
            capture_output=True,
            check=False,
            env=env,
            text=True,
            timeout=30,
        )
        assert resume.returncode == 0, resume.stderr
        assert source.read_text(encoding="utf-8") == "after from provider\n"
        assert len(server.requests) == 2
        resumed_messages = server.requests[1]["messages"]
        assert isinstance(resumed_messages, list)
        replayed_assistant = next(
            message for message in resumed_messages if isinstance(message, dict) and message.get("role") == "assistant" and message.get("tool_calls")
        )
        assert replayed_assistant.get("reasoning_content") == reasoning_text
        read_result = next(
            message
            for message in resumed_messages
            if isinstance(message, dict) and message.get("role") == "tool" and message.get("tool_call_id") == "call-cli-read"
        )
        assert "before from provider" in str(read_result.get("content"))
    finally:
        _stop_provider_server(server, thread)


def test_acp_jsonrpc_prompt_uses_the_private_openai_provider(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sample.txt").write_text("ACP provider read\n", encoding="utf-8")
    reasoning_text = "Read the requested file before reporting its contents."
    server, thread = _start_provider_server(
        [
            openai_response(
                message={
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": reasoning_text,
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-acp-read",
                            "type": "function",
                            "function": {
                                "name": "read",
                                "arguments": json.dumps({"path": "sample.txt"}),
                            },
                        }
                    ],
                },
                finish="tool_calls",
            ),
            openai_response(
                message={"role": "assistant", "content": "Read through ACP."},
                finish="stop",
            ),
        ]
    )
    configure_openai_provider(
        workspace,
        f"http://127.0.0.1:{server.server_address[1]}/v1",
    )
    env = _provider_process_environment(tmp_path / "process")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "voidcode",
            "acp",
            "--workspace",
            str(workspace),
        ],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        assert process.stdin is not None
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n")
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {}}) + "\n")
        process.stdin.flush()
        initialized = _read_acp_message(process)
        new_session = _read_acp_message(process)
        assert initialized["id"] == 1
        acp_session_id = new_session["result"]["sessionId"]
        process.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "session/prompt",
                    "params": {
                        "sessionId": acp_session_id,
                        "prompt": "Please inspect sample.txt and report its contents.",
                    },
                }
            )
            + "\n"
        )
        process.stdin.flush()
        prompt_messages: list[dict[str, object]] = []
        deadline = time.monotonic() + 60
        while not any(message.get("id") == 3 for message in prompt_messages) and time.monotonic() < deadline:
            prompt_messages.append(_read_acp_message(process, timeout=60))
        prompt_result = next((message for message in prompt_messages if message.get("id") == 3), None)
        assert prompt_result is not None, {
            "messages": prompt_messages,
            "provider_requests": server.requests,
            "response_content_types": server.response_content_types,
            "response_chunks": [body.decode() for body in server.response_bodies],
        }
        process.stdin.close()
        process.stdin = None
        process.wait(timeout=30)
        stderr = process.stderr.read() if process.stderr is not None else ""
        assert process.returncode == 0, stderr
        result = prompt_result.get("result")
        assert isinstance(result, dict) and result.get("stopReason") == "end_turn", {
            "prompt_result": prompt_result,
            "stderr": stderr,
            "provider_requests": server.requests,
            "response_content_types": server.response_content_types,
            "response_chunks": [body.decode() for body in server.response_bodies],
        }
        acp_updates: list[dict[str, object]] = []
        for message in prompt_messages:
            params = message.get("params")
            if message.get("method") == "session/update" and isinstance(params, dict):
                update = params.get("update")
                if isinstance(update, dict):
                    acp_updates.append(update)
        tool_calls = [update for update in acp_updates if update.get("sessionUpdate") == "tool_call"]
        tool_updates = [update for update in acp_updates if update.get("sessionUpdate") == "tool_call_update"]
        assert len(tool_calls) == len(tool_updates) == 1
        assert tool_calls[0]["toolCallId"] != "call-acp-read"
        assert tool_updates[0]["toolCallId"] == tool_calls[0]["toolCallId"]
        assert "call-acp-read" not in json.dumps(prompt_messages)
        acp_messages = server.requests[1]["messages"]
        assert isinstance(acp_messages, list)
        replayed_assistant = next(
            message for message in acp_messages if isinstance(message, dict) and message.get("role") == "assistant" and message.get("tool_calls")
        )
        assert replayed_assistant.get("reasoning_content") == reasoning_text
        tool_result = next(
            message
            for message in acp_messages
            if isinstance(message, dict) and message.get("role") == "tool" and message.get("tool_call_id") == "call-acp-read"
        )
        assert "ACP provider read" in str(tool_result.get("content"))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        _stop_provider_server(server, thread)


def test_runtime_prices_real_openai_loopback_usage_from_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = _provider_process_environment(tmp_path / "process")
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("OPENCODE_API_KEY", "loopback-key")

    provider_reply = openai_response(
        message={"role": "assistant", "content": "priced response"},
        finish="stop",
    )
    provider_reply["model"] = "space-bunny"
    provider_reply["usage"] = {
        "prompt_tokens": 4_000,
        "completion_tokens": 200,
        "total_tokens": 4_200,
        "prompt_tokens_details": {"cached_tokens": 0},
    }
    server, thread = _start_provider_server([provider_reply])
    configure_openai_provider(
        workspace,
        f"http://127.0.0.1:{server.server_address[1]}/v1",
        provider="opencode-go",
        model="space-bunny",
    )
    try:
        with VoidCodeRuntime(workspace=workspace) as runtime:
            response = runtime.run(RuntimeRequest(prompt="return the priced reply"))

        assert response.output == "priced response"
        assert len(server.requests) == 1
        assert server.requests[0]["model"] == "space-bunny"
        assert server.requests[0]["stream"] is False

        provider_usage = response.session.metadata["provider_usage"]
        assert isinstance(provider_usage, dict)
        assert provider_usage["turn_count"] == 1
        latest = provider_usage["latest"]
        cumulative = provider_usage["cumulative"]
        assert isinstance(latest, dict) and isinstance(cumulative, dict)
        assert latest["input_tokens"] == 4_000
        assert latest["output_tokens"] == 200
        assert latest["cache_read_tokens"] == 0
        assert latest["cache_write_tokens"] is None

        metadata = static_catalog_metadata("opencode-go", "space-bunny")
        assert metadata is not None
        assert metadata.cost_per_cache_write_token == 0
        expected_cost = usage_cost_usd(
            provider_id="opencode-go",
            model_id="space-bunny",
            usage=ProviderTokenUsage(
                input_tokens=4_000,
                output_tokens=200,
                cache_read_tokens=0,
                cache_write_tokens=None,
                uncached_input_tokens=4_000,
            ),
            metadata=metadata,
        )
        assert expected_cost is not None and expected_cost > 0
        assert latest["cost_usd"] == expected_cost
        assert cumulative["cost_usd"] == expected_cost
    finally:
        _stop_provider_server(server, thread)
