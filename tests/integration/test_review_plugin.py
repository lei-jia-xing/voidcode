"""Review uses built-in file tools with every other plugin disabled."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from threading import Thread

import pytest

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import BUNDLED_PLUGIN_IDS, open_core
from voidcode.core.engine import Core

_REVIEW_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "src" / "voidcode" / "plugins" / "review"

_CREATED_CORES: list[Core] = []


@pytest.fixture(autouse=True)
def _close_created_cores() -> Iterator[None]:
    yield
    while _CREATED_CORES:
        _CREATED_CORES.pop().close()


@contextmanager
def _loopback_server(replies: list[dict[str, object]]) -> Iterator[OpenAILoopbackServer]:
    server = OpenAILoopbackServer(replies)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def _dirty_repo(workspace: Path) -> None:
    """One commit and one dirty file: the review target of this suite."""
    workspace.mkdir(parents=True, exist_ok=True)
    _git(workspace, "init", "--quiet", "--initial-branch=main")
    _git(workspace, "config", "user.email", "test@example.com")
    _git(workspace, "config", "user.name", "Test")
    (workspace / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(workspace, "add", "tracked.txt")
    _git(workspace, "commit", "--quiet", "-m", "initial commit")
    (workspace / "tracked.txt").write_text("base\ndirty review target\n", encoding="utf-8")


def _core(tmp_path: Path, server: OpenAILoopbackServer, *, custom_reader: bool = False) -> Core:
    workspace = tmp_path / "workspace"
    _dirty_repo(workspace)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "schema_version": 1,
        "model": {"model": "endpoint/test-model"},
        # The command spawns an external child: approval_mode "ask" would park the spawn instead.
        "policy": {"approval_mode": "yolo"},
        "providers": {
            "endpoint": {
                "api_key": "loopback-test-key",
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            }
        },
        "plugins": {
            **{name: {"enabled": False} for name in BUNDLED_PLUGIN_IDS},
            "files": {"enabled": True},
            "providers": {"enabled": True},
            "review": {"path": str(_REVIEW_PLUGIN_ROOT), "enabled": True},
        },
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    if custom_reader:
        source = tmp_path / "custom_reader"
        source.mkdir()
        (source / "__init__.py").write_text(
            "from voidcode.sdk.plugin import Plugin\n"
            "from voidcode.sdk.tools import ToolDefinition, ToolEffect, ToolSuccess, TextOutput\n"
            "class Inspect:\n"
            "    definition = ToolDefinition(name='inspect_repository', description='Inspect repository', effects=frozenset({ToolEffect.READ}))\n"
            "    def invoke(self, call, *, context, services):\n"
            "        return ToolSuccess(tool_name='inspect_repository', output=TextOutput('inspection complete'))\n"
            "def create_plugin(config):\n"
            "    return Plugin(tools=(Inspect(),))\n",
            encoding="utf-8",
        )
        config["plugins"]["custom_reader"] = {"path": str(source)}
        (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    core = open_core(workspace, state_dir=state_dir)
    _CREATED_CORES.append(core)
    return core


def _invoke_review(core: Core, session_id: str, arguments: Mapping[str, object]):
    return core.invoke_command(session_id, "review", arguments, expected_revision=core.command_catalog().revision_id)


def _messages(request: dict[str, object]) -> list[dict[str, object]]:
    messages = request.get("messages")
    return [entry for entry in messages if isinstance(entry, dict)] if isinstance(messages, list) else []


def _tool_names(request: dict[str, object]) -> list[str]:
    tools = request.get("tools")
    if not isinstance(tools, list):
        return []
    return [str(entry["function"]["name"]) for entry in tools if isinstance(entry, dict) and isinstance(entry.get("function"), dict)]  # type: ignore[union-attr]


def test_review_command_spawns_a_real_child_that_receives_the_git_diff(tmp_path: Path) -> None:
    replies: list[dict[str, object]] = [
        # The review child: one turn, one answer over the prompt the command built.
        openai_response(
            message={"role": "assistant", "content": "review: the dirty line needs a test"},
            finish="stop",
            usage={"prompt_tokens": 40, "completion_tokens": 9, "total_tokens": 49},
        ),
    ]
    with _loopback_server(replies) as server:
        core = _core(tmp_path, server)
        parent_id = core.create_session("parent").session_id

        result = _invoke_review(core, parent_id, {})

        assert result.status == "ok"
        assert result.output == "review: the dirty line needs a test"
        assert result.data is not None
        child_id = str(result.data["child_session_id"])
        assert result.data["status"] == "completed"
        assert result.data["target_type"] == "uncommitted"
        assert result.data["output"] == "review: the dirty line needs a test"

        # The review ran as a real delegated child session holding the review profile's scope.
        child = core.session(child_id)
        assert child.parent_session_id == parent_id
        assert child.status == "completed"
        assert child.tool_allowlist is None
        assert core.store.session_overlays(child_id) == ("read-only",)

        # Exactly one provider request: the child's, carrying the legacy prompt and the real diff.
        assert len(server.requests) == 1
        request = server.requests[0]
        assert sorted(_tool_names(request)) == ["glob", "grep", "read"]
        prompt = str(_messages(request)[-1]["content"])
        assert "Review the current workspace changes (staged, unstaged, and untracked files) and provide prioritized findings." in prompt
        assert "Aggregate Git diff:" in prompt
        assert "diff --git a/tracked.txt b/tracked.txt" in prompt
        assert "+dirty review target" in prompt
        assert prompt.endswith("Return the final review findings in your final answer.")

        # The command recorded the bounded review fact and the core its child lifecycle facts.
        events = core.events_after(parent_id)
        started = [event for event in events if event.event_type == "plugin.review.review_started"]
        assert len(started) == 1
        assert started[0].payload == {
            "target_type": "uncommitted",
            "branch": None,
            "sha": None,
            "focus": None,
            "child_session_id": child_id,
        }
        spawned = [event for event in events if event.event_type == "core.child_spawned"]
        finished = [event for event in events if event.event_type == "core.child_finished"]
        assert len(spawned) == 1 and len(finished) == 1
        assert spawned[0].payload["label"] == "review"
        assert spawned[0].payload["child_session_id"] == child_id
        assert finished[0].payload["child_session_id"] == child_id
        assert finished[0].payload["output"] == "review: the dirty line needs a test"


def test_review_command_carries_a_custom_focus_into_the_child_prompt(tmp_path: Path) -> None:
    replies: list[dict[str, object]] = [
        openai_response(message={"role": "assistant", "content": "focus answered"}, finish="stop"),
    ]
    with _loopback_server(replies) as server:
        core = _core(tmp_path, server)
        parent_id = core.create_session("parent").session_id

        result = _invoke_review(core, parent_id, {"focus": "only the parser edge cases"})

        assert result.status == "ok"
        assert result.output == "focus answered"
        assert len(server.requests) == 1
        prompt = str(_messages(server.requests[0])[-1]["content"])
        assert "Reviewer focus: only the parser edge cases" in prompt
        assert "Review the current workspace changes and report concrete correctness, regression, and safety findings." in prompt
        assert result.data is not None
        assert result.data["target_type"] == "custom"


def test_review_command_refuses_an_unknown_target_without_touching_the_provider(tmp_path: Path) -> None:
    replies: list[dict[str, object]] = [openai_response(message={"role": "assistant", "content": "unreachable"}, finish="stop")]
    with _loopback_server(replies) as server:
        core = _core(tmp_path, server)
        parent_id = core.create_session("parent").session_id

        result = _invoke_review(core, parent_id, {"target": "nonsense"})

        assert result.status == "error"
        assert result.error == "unknown review target type 'nonsense'"
        assert server.requests == []
        assert core.events_after(parent_id)  # the refusal is committed, nothing spawned
        assert not [event for event in core.events_after(parent_id) if event.event_type == "core.child_spawned"]


def test_review_command_reviews_a_commit_sha(tmp_path: Path) -> None:
    replies: list[dict[str, object]] = [openai_response(message={"role": "assistant", "content": "commit reviewed"}, finish="stop")]
    with _loopback_server(replies) as server:
        core = _core(tmp_path, server)
        parent_id = core.create_session("parent").session_id
        _git(tmp_path / "workspace", "commit", "--quiet", "-am", "dirty commit")
        sha = _git(tmp_path / "workspace", "rev-parse", "HEAD").strip()

        result = _invoke_review(core, parent_id, {"target": "commit", "sha": sha})

        assert result.status == "ok"
        assert result.output == "commit reviewed"
        assert len(server.requests) == 1
        prompt = str(_messages(server.requests[0])[-1]["content"])
        assert f"Review the code changes introduced by commit {sha}." in prompt
        assert f"Git diff for commit {sha}:" in prompt
        assert "+dirty review target" in prompt
        assert "Provide prioritized, actionable findings in your final answer." in prompt


def test_review_reaches_the_child_for_a_diff_in_the_64kib_256kib_band(tmp_path: Path) -> None:
    """The core's delegated-prompt bound is 256 KiB, so the whole legacy band reaches the child."""
    replies: list[dict[str, object]] = [
        openai_response(
            message={"role": "assistant", "content": "reviewed the large diff"},
            finish="stop",
            usage={"prompt_tokens": 60, "completion_tokens": 8, "total_tokens": 68},
        )
    ]
    with _loopback_server(replies) as server:
        core = _core(tmp_path, server)
        parent_id = core.create_session("parent").session_id
        (tmp_path / "workspace" / "big.txt").write_text("".join(f"line {index} {'x' * 80}\n" for index in range(1500)), encoding="utf-8")

        result = _invoke_review(core, parent_id, {})

        assert result.status == "ok", result.error
        assert result.output == "reviewed the large diff"
        assert len(server.requests) == 1
        diff_text = json.dumps(server.requests[0].get("messages"), ensure_ascii=False)
        assert "diff --git a/big.txt b/big.txt" in diff_text
        events = core.events_after(parent_id)
        assert [event for event in events if event.event_type == "core.child_spawned"]
        assert [event for event in events if event.event_type == "plugin.review.review_started"]


def test_review_refuses_a_diff_over_its_legacy_256kib_budget_without_spawning(tmp_path: Path) -> None:
    """Above the legacy product budget the plugin refuses visibly and never truncates."""
    replies: list[dict[str, object]] = [openai_response(message={"role": "assistant", "content": "unreachable"}, finish="stop")]
    with _loopback_server(replies) as server:
        core = _core(tmp_path, server)
        parent_id = core.create_session("parent").session_id
        (tmp_path / "workspace" / "huge.txt").write_text("".join(f"line {index} {'x' * 120}\n" for index in range(3000)), encoding="utf-8")

        result = _invoke_review(core, parent_id, {})

        assert result.status == "error"
        assert result.error is not None and "too large" in result.error
        assert server.requests == []
        events = core.events_after(parent_id)
        assert not [event for event in events if event.event_type == "core.child_spawned"]
        assert not [event for event in events if event.event_type == "plugin.review.review_started"]


@pytest.mark.parametrize("restricted_parent", [False, True])
def test_review_inherits_new_inspection_tools_without_widening_parent_scope(tmp_path: Path, restricted_parent: bool) -> None:
    replies = [
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "inspect", "type": "function", "function": {"name": "inspect_repository", "arguments": "{}"}}],
            },
            finish="tool_calls",
        ),
        openai_response(message={"role": "assistant", "content": "review complete"}, finish="stop"),
    ]
    with _loopback_server(replies) as server:
        core = _core(tmp_path, server, custom_reader=True)
        root = core.create_session("root")
        parent_id = root.session_id
        if restricted_parent:
            parent_id = "restricted"
            core.store.create_child_session(
                parent_id,
                str(core.workspace),
                core.revision_id,
                parent_session_id=root.session_id,
                root_session_id=root.session_id,
                depth=1,
                tool_allowlist=("inspect_repository",),
            )
        result = _invoke_review(core, parent_id, {})
        assert result.status == "ok", result.error
        assert result.output == "review complete"
        assert result.data is not None
        child_id = str(result.data["child_session_id"])
        assert core.session(child_id).tool_allowlist == (("inspect_repository",) if restricted_parent else None)
        assert core.store.session_overlays(child_id) == ("read-only",)
        tools = _tool_names(server.requests[0])
        assert "inspect_repository" in tools
        assert "write" not in tools and "shell_exec" not in tools
        if restricted_parent:
            assert tools == ["inspect_repository"]
        assert "inspection complete" in json.dumps(server.requests[1]["messages"])
