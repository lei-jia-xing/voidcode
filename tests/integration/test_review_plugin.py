"""End-to-end integration test for the Review plugin on the real kernel stack.

Drives the real kernel, plugin runtime, loopback provider, git workspace and durable storage: the
``review`` command resolves the workspace diff from a real repository, spawns a real child session
whose provider request carries the legacy review instructions plus that diff, and returns the
child's answer as the command result.

The review profile's allowlist (read/glob/grep/ast_grep/lsp) names tools that only exist once the
files/ast_grep/lsp plugins are admitted, and those sources are under active rewrite by another
lane -- so this suite declares the same names with a minimal admitted source of its own instead of
depending on them. The bundled ``files``, ``ast_grep`` and ``lsp`` plugins are disabled for the
same reason: a tool catalog refuses two plugins declaring the same name.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from threading import Thread

import pytest

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.kernel.bootstrap import open_kernel
from voidcode.kernel.engine import Kernel
from voidcode.plugins.review.review_plugin import REVIEW_TOOL_ALLOWLIST

_REVIEW_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "src" / "voidcode" / "plugins" / "review"
_PROFILE_TOOL_NAMES = REVIEW_TOOL_ALLOWLIST

_CREATED_KERNELS: list[Kernel] = []


@pytest.fixture(autouse=True)
def _close_created_kernels() -> Iterator[None]:
    yield
    while _CREATED_KERNELS:
        _CREATED_KERNELS.pop().close()


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


def _admitted_review_source(tmp_path: Path) -> Path:
    """The real review plugin root, referenced by path."""
    _ = tmp_path
    return _REVIEW_PLUGIN_ROOT


def _admitted_profile_tools_source(tmp_path: Path, names: tuple[str, ...]) -> Path:
    """An admitted source declaring the review profile's tool names.

    The child's allowlist is validated against the active revision's catalog, so the names the
    review profile grants have to exist for the spawn to prove the real path. This source only
    declares them; every invocation succeeds trivially, and the child in this suite never calls one.
    """
    source = tmp_path / "profile-tools-source"
    source.mkdir()
    names_literal = repr(tuple(names))
    code = (
        "from voidcode.sdk.plugin import Plugin\n"
        "from voidcode.sdk.tools import TextOutput, ToolDefinition, ToolEffect, ToolSuccess\n"
        "\n"
        "\n"
        "class DeclaredTool:\n"
        "    def __init__(self, definition):\n"
        "        self.definition = definition\n"
        "\n"
        "    def invoke(self, call, *, context, services):\n"
        "        return ToolSuccess(call.tool_name, TextOutput('declared review profile tool'))\n"
        "\n"
        "\n"
        "def create_plugin(config):\n"
        f"    tools = tuple(\n"
        f"        DeclaredTool(ToolDefinition(name=name, description=f'Declared review profile tool {{name}}.',\n"
        f"            input_schema={{'type': 'object', 'additionalProperties': True}}, effects=frozenset({{ToolEffect.READ}}),\n"
        f"            replay_policy='safe'))\n"
        f"        for name in {names_literal}\n"
        f"    )\n"
        "    return Plugin(tools=tools)\n"
    )
    (source / "__init__.py").write_text(code, encoding="utf-8")
    return source


def _kernel(tmp_path: Path, server: OpenAILoopbackServer) -> Kernel:
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
            # The bundled files plugin is not under test here and is rewritten by another P4 lane.
            "files": {"enabled": False},
            # This synthetic source declares ast_grep/lsp itself, so their bundled owners stay out:
            # the revision refuses two plugins declaring the same tool name.
            "ast_grep": {"enabled": False},
            "lsp": {"enabled": False},
            "profile-tools": {"path": str(_admitted_profile_tools_source(tmp_path, _PROFILE_TOOL_NAMES)), "enabled": True},
            "review": {"path": str(_admitted_review_source(tmp_path)), "enabled": True},
        },
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    kernel = open_kernel(workspace, state_dir=state_dir)
    _CREATED_KERNELS.append(kernel)
    return kernel


def _invoke_review(kernel: Kernel, session_id: str, arguments: Mapping[str, object]):
    return kernel.invoke_command(session_id, "review", arguments, expected_revision=kernel.command_catalog().revision_id)


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
        kernel = _kernel(tmp_path, server)
        parent_id = kernel.create_session("parent").session_id

        result = _invoke_review(kernel, parent_id, {})

        assert result.status == "ok"
        assert result.output == "review: the dirty line needs a test"
        assert result.data is not None
        child_id = str(result.data["child_session_id"])
        assert result.data["status"] == "completed"
        assert result.data["target_type"] == "uncommitted"
        assert result.data["output"] == "review: the dirty line needs a test"

        # The review ran as a real delegated child session holding the review profile's scope.
        child = kernel.session(child_id)
        assert child.parent_session_id == parent_id
        assert child.status == "completed"
        assert child.tool_allowlist is not None
        assert sorted(child.tool_allowlist) == sorted(REVIEW_TOOL_ALLOWLIST)

        # Exactly one provider request: the child's, carrying the legacy prompt and the real diff.
        assert len(server.requests) == 1
        request = server.requests[0]
        assert sorted(_tool_names(request)) == sorted(REVIEW_TOOL_ALLOWLIST)
        prompt = str(_messages(request)[-1]["content"])
        assert "Review the current workspace changes (staged, unstaged, and untracked files) and provide prioritized findings." in prompt
        assert "Aggregate Git diff:" in prompt
        assert "diff --git a/tracked.txt b/tracked.txt" in prompt
        assert "+dirty review target" in prompt
        assert prompt.endswith("Return the final review findings in your final answer.")

        # The command recorded the bounded review fact and the kernel its child lifecycle facts.
        events = kernel.events_after(parent_id)
        started = [event for event in events if event.event_type == "plugin.review.review_started"]
        assert len(started) == 1
        assert started[0].payload == {
            "target_type": "uncommitted",
            "branch": None,
            "sha": None,
            "focus": None,
            "child_session_id": child_id,
        }
        spawned = [event for event in events if event.event_type == "kernel.child_spawned"]
        finished = [event for event in events if event.event_type == "kernel.child_finished"]
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
        kernel = _kernel(tmp_path, server)
        parent_id = kernel.create_session("parent").session_id

        result = _invoke_review(kernel, parent_id, {"focus": "only the parser edge cases"})

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
        kernel = _kernel(tmp_path, server)
        parent_id = kernel.create_session("parent").session_id

        result = _invoke_review(kernel, parent_id, {"target": "nonsense"})

        assert result.status == "error"
        assert result.error == "unknown review target type 'nonsense'"
        assert server.requests == []
        assert kernel.events_after(parent_id)  # the refusal is committed, nothing spawned
        assert not [event for event in kernel.events_after(parent_id) if event.event_type == "kernel.child_spawned"]


def test_review_command_reviews_a_commit_sha(tmp_path: Path) -> None:
    replies: list[dict[str, object]] = [openai_response(message={"role": "assistant", "content": "commit reviewed"}, finish="stop")]
    with _loopback_server(replies) as server:
        kernel = _kernel(tmp_path, server)
        parent_id = kernel.create_session("parent").session_id
        _git(tmp_path / "workspace", "commit", "--quiet", "-am", "dirty commit")
        sha = _git(tmp_path / "workspace", "rev-parse", "HEAD").strip()

        result = _invoke_review(kernel, parent_id, {"target": "commit", "sha": sha})

        assert result.status == "ok"
        assert result.output == "commit reviewed"
        assert len(server.requests) == 1
        prompt = str(_messages(server.requests[0])[-1]["content"])
        assert f"Review the code changes introduced by commit {sha}." in prompt
        assert f"Git diff for commit {sha}:" in prompt
        assert "+dirty review target" in prompt
        assert "Provide prioritized, actionable findings in your final answer." in prompt


def test_review_reaches_the_child_for_a_diff_in_the_64kib_256kib_band(tmp_path: Path) -> None:
    """The kernel's delegated-prompt bound is 256 KiB, so the whole legacy band reaches the child."""
    replies: list[dict[str, object]] = [
        openai_response(
            message={"role": "assistant", "content": "reviewed the large diff"},
            finish="stop",
            usage={"prompt_tokens": 60, "completion_tokens": 8, "total_tokens": 68},
        )
    ]
    with _loopback_server(replies) as server:
        kernel = _kernel(tmp_path, server)
        parent_id = kernel.create_session("parent").session_id
        (tmp_path / "workspace" / "big.txt").write_text("".join(f"line {index} {'x' * 80}\n" for index in range(1500)), encoding="utf-8")

        result = _invoke_review(kernel, parent_id, {})

        assert result.status == "ok", result.error
        assert result.output == "reviewed the large diff"
        assert len(server.requests) == 1
        diff_text = json.dumps(server.requests[0].get("messages"), ensure_ascii=False)
        assert "diff --git a/big.txt b/big.txt" in diff_text
        events = kernel.events_after(parent_id)
        assert [event for event in events if event.event_type == "kernel.child_spawned"]
        assert [event for event in events if event.event_type == "plugin.review.review_started"]


def test_review_refuses_a_diff_over_its_legacy_256kib_budget_without_spawning(tmp_path: Path) -> None:
    """Above the legacy product budget the plugin refuses visibly and never truncates."""
    replies: list[dict[str, object]] = [openai_response(message={"role": "assistant", "content": "unreachable"}, finish="stop")]
    with _loopback_server(replies) as server:
        kernel = _kernel(tmp_path, server)
        parent_id = kernel.create_session("parent").session_id
        (tmp_path / "workspace" / "huge.txt").write_text("".join(f"line {index} {'x' * 120}\n" for index in range(3000)), encoding="utf-8")

        result = _invoke_review(kernel, parent_id, {})

        assert result.status == "error"
        assert result.error is not None and "too large" in result.error
        assert server.requests == []
        events = kernel.events_after(parent_id)
        assert not [event for event in events if event.event_type == "kernel.child_spawned"]
        assert not [event for event in events if event.event_type == "plugin.review.review_started"]
