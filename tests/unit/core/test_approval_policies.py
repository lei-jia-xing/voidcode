"""Tests for session tool approval policies and denial reasons."""

from __future__ import annotations

import json
from pathlib import Path

from tests.fixtures.core_factory import loopback_server
from tests.fixtures.s5_plugin import PLUGIN_ID, WRITE_TOOL, write_s5_plugin
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.config import dumps_toml
from voidcode.core.events import EventType


def _call(identity: str, tool_name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": tool_name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(message={"role": "assistant", "content": output, "tool_calls": list(calls)}, finish="tool_calls" if calls else "stop")


def _configure(tmp_path: Path, server: OpenAILoopbackServer) -> Path:
    source = write_s5_plugin(tmp_path / "source")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "voidcode.toml").write_text(
        dumps_toml(
            {
                "schema_version": 1,
                "model": {"model": "endpoint/exact-model"},
                "policy": {"approval_mode": "ask", "read_only": False},
                "providers": {"endpoint": {"api_key": "secret", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
                "plugins": {PLUGIN_ID: {"path": str(source)}},
            }
        ),
        encoding="utf-8",
    )
    return workspace


def _open(tmp_path: Path):
    return open_core(tmp_path / "workspace", state_dir=tmp_path / "state")


def test_grant_approval_policy_auto_approves_future_calls(tmp_path: Path) -> None:
    with loopback_server(
        [
            # Turn 1: call write tool
            _turn(_call("call_1", WRITE_TOOL, action="write", path="file1.txt", text="first")),
            # Turn 2: after allow, model calls write tool again on file2.txt
            _turn(_call("call_2", WRITE_TOOL, action="write", path="file2.txt", text="second")),
            # Turn 3: completed
            _turn(output="all done"),
        ]
    ) as server:
        workspace = _configure(tmp_path, server)
        with _open(tmp_path) as core:
            # 1. First run parks for approval on WRITE_TOOL
            res = core.run("Do writes")
            assert res.status == "waiting"
            assert res.pending_approval is not None
            request_id = res.pending_approval["request_id"]

            # 2. Resume with allow AND grant_session_policy=True
            res2 = core.resume(res.session_id, approval_request_id=request_id, decision="allow", grant_session_policy=True)
            # The second write tool call should be AUTO-APPROVED by the session policy, so the run completes!
            assert res2.status == "completed", res2.error
            assert (workspace / "file1.txt").exists()
            assert (workspace / "file2.txt").exists()

            # 3. List approval policies
            policies = core.list_approval_policies(res.session_id)
            assert len(policies) == 1
            assert policies[0].tool_name == WRITE_TOOL
            rule_id = policies[0].rule_id

            # 4. Revoke the policy
            revoked = core.revoke_approval_policy(res.session_id, rule_id)
            assert revoked is True
            assert len(core.list_approval_policies(res.session_id)) == 0

            # 5. Revoking again returns False
            assert core.revoke_approval_policy(res.session_id, rule_id) is False


def test_deny_with_reason_reports_to_model(tmp_path: Path) -> None:
    with loopback_server(
        [
            # Turn 1: call write tool
            _turn(_call("call_1", WRITE_TOOL, path="dangerous.txt", text="data")),
            # Turn 2: model sees denial reason and responds
            _turn(output="I understand, skipping dangerous write"),
        ]
    ) as server:
        workspace = _configure(tmp_path, server)
        with _open(tmp_path) as core:
            res = core.run("Write dangerous file")
            assert res.status == "waiting"
            assert res.pending_approval is not None
            request_id = res.pending_approval["request_id"]

            # Resume with deny and custom reason
            res2 = core.resume(res.session_id, approval_request_id=request_id, decision="deny", reason="file is protected by policy")
            assert res2.status == "completed", res2.error
            assert not (workspace / "dangerous.txt").exists()

            events = core.store.events(res.session_id)
            # Check APPROVAL_DECIDED event carries reason
            decided = next(e for e in events if e.event_type == EventType.APPROVAL_DECIDED)
            assert decided.payload.get("reason") == "file is protected by policy"

            # Check tool completed failure carries denial reason
            tool_res = next(e for e in events if e.event_type == EventType.TOOL_COMPLETED)
            assert "user denied this invocation: file is protected by policy" in str(tool_res.payload)


def test_approval_policy_operations_enforce_workspace_ownership(tmp_path: Path) -> None:
    import pytest

    with loopback_server([_turn(_call("call_1", WRITE_TOOL, path="a.txt", text="hi")), _turn(output="ok")]) as server:
        ws_a = tmp_path / "workspace_a"
        ws_b = tmp_path / "workspace_b"
        ws_a.mkdir()
        ws_b.mkdir()
        source = write_s5_plugin(tmp_path / "source")
        cfg = {
            "schema_version": 1,
            "model": {"model": "endpoint/exact-model"},
            "policy": {"approval_mode": "ask", "read_only": False},
            "providers": {"endpoint": {"api_key": "secret", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
            "plugins": {PLUGIN_ID: {"path": str(source)}},
        }
        (ws_a / "voidcode.toml").write_text(dumps_toml(cfg), encoding="utf-8")
        (ws_b / "voidcode.toml").write_text(dumps_toml(cfg), encoding="utf-8")
        state = tmp_path / "state"
        with open_core(ws_a, state_dir=state) as core_a:
            res = core_a.run("write")
            assert res.pending_approval is not None
            res2 = core_a.resume(
                res.session_id,
                approval_request_id=res.pending_approval["request_id"],
                decision="allow",
                grant_session_policy=True,
            )
            assert res2.status == "completed"
            assert len(core_a.list_approval_policies(res.session_id)) == 1

        with open_core(ws_b, state_dir=state) as core_b:
            with pytest.raises(ValueError, match="belongs to another workspace"):
                core_b.list_approval_policies(res.session_id)
            with pytest.raises(ValueError, match="belongs to another workspace"):
                core_b.revoke_approval_policy(res.session_id, "any-rule")
