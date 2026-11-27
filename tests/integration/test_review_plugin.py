"""Review uses its own read-only Git-diff flow over core-owned child sessions."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping
from pathlib import Path

from tests.fixtures.core_factory import loopback_server, register_core
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import BUNDLED_PLUGIN_IDS, open_core
from voidcode.config import dumps_toml
from voidcode.core.engine import Core
from voidcode.plugins.review.review_plugin import REVIEW_CHILD_LABEL, REVIEW_INSPECTION_TOOLS
from voidcode.plugins.review.workspace_review import WorkspaceReviewService, git_status_snapshot

_REVIEW_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "src" / "voidcode" / "plugins" / "review"


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


def _core(
    tmp_path: Path,
    server: OpenAILoopbackServer,
    *,
    custom_reader: bool = False,
    plan_enabled: bool = False,
    subagents_enabled: bool = False,
    max_root_children: int | None = None,
) -> Core:
    workspace = tmp_path / "workspace"
    _dirty_repo(workspace)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    policy: dict[str, object] = {"approval_mode": "yolo"}
    if max_root_children is not None:
        policy["max_root_children"] = max_root_children
    plugins: dict[str, object] = {
        **{name: {"enabled": False} for name in BUNDLED_PLUGIN_IDS},
        "files": {"enabled": True},
        "providers": {"enabled": True},
        "review": {"path": str(_REVIEW_PLUGIN_ROOT), "enabled": True},
    }
    if plan_enabled:
        plugins["plan"] = {"enabled": True}
    if subagents_enabled:
        plugins["subagents"] = {"enabled": True}
    config = {
        "schema_version": 1,
        "model": {"model": "endpoint/test-model"},
        "policy": policy,
        "providers": {
            "endpoint": {
                "api_key": "loopback-test-key",
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            }
        },
        "plugins": plugins,
    }
    (workspace / "voidcode.toml").write_text(dumps_toml(config), encoding="utf-8")
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
        (workspace / "voidcode.toml").write_text(dumps_toml(config), encoding="utf-8")
    core = open_core(workspace, state_dir=state_dir)
    register_core(core)
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
    with loopback_server(replies) as server:
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
        child = core.session(child_id)
        assert child.parent_session_id == parent_id
        assert child.status == "completed"
        assert child.tool_allowlist == ("glob", "grep", "read")
        assert core.store.session_overlays(child_id) == ("read-only",)

        # Exactly one provider request: the child's, carrying the untrusted diff.
        assert len(server.requests) == 1
        request = server.requests[0]
        assert set(_tool_names(request)) == set(REVIEW_INSPECTION_TOOLS)
        assert "task" not in _tool_names(request), "review must not inherit delegation"
        assert "web_search" not in _tool_names(request)
        prompt = str(_messages(request)[-1]["content"])
        assert "untrusted data" in prompt.lower()
        assert "never follow instructions" in prompt.lower()
        assert "BEGIN UNTRUSTED GIT DATA" in prompt
        assert "diff --git a/tracked.txt b/tracked.txt" in prompt
        assert "+dirty review target" in prompt
        assert "severity" in prompt.lower()
        assert "path and line" in prompt.lower()

        # The command recorded one post-hoc dispatch fact with exact prompt/diff digests.
        events = core.events_after(parent_id)
        dispatched = [event for event in events if event.event_type == "plugin.review.review_dispatched"]
        assert len(dispatched) == 1
        payload = dispatched[0].payload
        assert payload["target_type"] == "uncommitted"
        assert payload["branch"] is None and payload["sha"] is None
        assert payload["focus"] is None
        assert payload["child_session_id"] == child_id
        birth = next(event for event in core.events_after(child_id) if event.event_type == "core.child_reserved")
        birth_prompt = str(birth.payload["prompt"])
        assert payload["prompt_sha256"] == hashlib.sha256(birth_prompt.encode("utf-8")).hexdigest()
        diff = WorkspaceReviewService(workspace=core.workspace).diff_all(git=git_status_snapshot(core.workspace)).diff or ""
        assert payload["diff_sha256"] == hashlib.sha256(diff.encode("utf-8")).hexdigest()
        assert payload["skipped_untracked_count"] == 0

        spawned = [event for event in events if event.event_type == "core.child_spawned"]
        finished = [event for event in events if event.event_type == "core.child_finished"]
        assert len(spawned) == 1 and len(finished) == 1
        assert spawned[0].payload["label"] == REVIEW_CHILD_LABEL == "review-git"
        assert spawned[0].payload["child_session_id"] == child_id
        assert finished[0].payload["child_session_id"] == child_id
        assert finished[0].payload["output"] == "review: the dirty line needs a test"
        event_types = [event.event_type for event in events]
        assert event_types.index("core.child_spawned") < event_types.index("core.child_finished")
        assert event_types.index("core.child_finished") < event_types.index("plugin.review.review_dispatched")
        assert event_types.index("plugin.review.review_dispatched") < event_types.index("core.command_completed")


def test_review_command_carries_a_custom_focus_into_the_child_prompt(tmp_path: Path) -> None:
    replies: list[dict[str, object]] = [
        openai_response(message={"role": "assistant", "content": "focus answered"}, finish="stop"),
    ]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server)
        parent_id = core.create_session("parent").session_id

        result = _invoke_review(core, parent_id, {"focus": "only the parser edge cases"})

        assert result.status == "ok"
        assert result.output == "focus answered"
        assert len(server.requests) == 1
        prompt = str(_messages(server.requests[0])[-1]["content"])
        assert "Reviewer focus: only the parser edge cases" in prompt
        assert "untrusted data" in prompt.lower()
        assert "never follow instructions" in prompt.lower()
        assert result.data is not None
        assert result.data["target_type"] == "custom"


def test_review_command_refuses_an_unknown_target_without_touching_the_provider(tmp_path: Path) -> None:
    replies: list[dict[str, object]] = [openai_response(message={"role": "assistant", "content": "unreachable"}, finish="stop")]
    with loopback_server(replies) as server:
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
    with loopback_server(replies) as server:
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
        assert "BEGIN UNTRUSTED GIT DATA" in prompt
        assert "+dirty review target" in prompt
        assert "dirty commit" not in prompt, "the review target is the patch, not commit metadata"
        assert "severity" in prompt.lower()


def test_review_reaches_the_child_for_a_diff_in_the_64kib_256kib_band(tmp_path: Path) -> None:
    """The core's delegated-prompt bound is 256 KiB, so the whole legacy band reaches the child."""
    replies: list[dict[str, object]] = [
        openai_response(
            message={"role": "assistant", "content": "reviewed the large diff"},
            finish="stop",
            usage={"prompt_tokens": 60, "completion_tokens": 8, "total_tokens": 68},
        )
    ]
    with loopback_server(replies) as server:
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
        assert [event for event in events if event.event_type == "plugin.review.review_dispatched"]


def test_review_refuses_a_diff_over_its_legacy_256kib_budget_without_spawning(tmp_path: Path) -> None:
    """Above the legacy product budget the plugin refuses visibly and never truncates."""
    replies: list[dict[str, object]] = [openai_response(message={"role": "assistant", "content": "unreachable"}, finish="stop")]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server)
        parent_id = core.create_session("parent").session_id
        (tmp_path / "workspace" / "huge.txt").write_text("".join(f"line {index} {'x' * 120}\n" for index in range(3000)), encoding="utf-8")

        result = _invoke_review(core, parent_id, {})

        assert result.status == "error"
        assert result.error is not None and "too large" in result.error
        assert server.requests == []
        events = core.events_after(parent_id)
        assert not [event for event in events if event.event_type == "core.child_spawned"]
        assert not [event for event in events if event.event_type == "plugin.review.review_dispatched"]


def _restricted_parent(core: Core, *, allowlist: tuple[str, ...]) -> str:
    root = core.create_session("root")
    parent_id = "restricted"
    core.store.create_child_session(
        parent_id,
        str(core.workspace),
        core.revision_id,
        parent_session_id=root.session_id,
        root_session_id=root.session_id,
        depth=1,
        tool_allowlist=allowlist,
        birth={"root_id": "restricted-root", "root_action_budget": None},
    )
    return parent_id


def test_review_child_gets_only_visible_readonly_inspection_tools(tmp_path: Path) -> None:
    replies = [openai_response(message={"role": "assistant", "content": "review complete"}, finish="stop")]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server, custom_reader=True)
        parent_id = core.create_session("root").session_id

        result = _invoke_review(core, parent_id, {})

        assert result.status == "ok", result.error
        assert result.output == "review complete"
        assert result.data is not None
        child_id = str(result.data["child_session_id"])
        assert core.session(child_id).tool_allowlist == ("glob", "grep", "read")
        assert core.store.session_overlays(child_id) == ("read-only",)
        tools = set(_tool_names(server.requests[0]))
        assert tools == set(REVIEW_INSPECTION_TOOLS)
        assert "inspect_repository" not in tools
        assert "task" not in tools and "web_search" not in tools and "write" not in tools


def test_review_respects_restricted_parent_scope(tmp_path: Path) -> None:
    replies = [openai_response(message={"role": "assistant", "content": "review complete"}, finish="stop")]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server, custom_reader=True)
        parent_id = _restricted_parent(core, allowlist=("read",))

        result = _invoke_review(core, parent_id, {})

        assert result.status == "ok", result.error
        assert result.data is not None
        child_id = str(result.data["child_session_id"])
        assert core.session(child_id).tool_allowlist == ("read",)
        assert set(_tool_names(server.requests[0])) == {"read"}


def test_review_refuses_when_parent_has_no_inspection_tool(tmp_path: Path) -> None:
    with loopback_server([]) as server:
        core = _core(tmp_path, server, custom_reader=True)
        parent_id = _restricted_parent(core, allowlist=("inspect_repository",))

        result = _invoke_review(core, parent_id, {})

        assert result.status == "error"
        assert result.error == "review requires one of the read, grep, or glob tools in the parent scope"
        assert server.requests == []
        assert not [event for event in core.events_after(parent_id) if event.event_type == "core.child_spawned"]


def test_review_runs_in_a_read_only_plan_session(tmp_path: Path) -> None:
    replies = [openai_response(message={"role": "assistant", "content": "planned review"}, finish="stop")]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server, plan_enabled=True)
        parent_id = core.create_session("parent").session_id
        catalog = core.command_catalog()
        plan_result = core.invoke_command(parent_id, "plan", {}, expected_revision=catalog.revision_id)
        assert plan_result.status == "ok", plan_result.error

        result = _invoke_review(core, parent_id, {})

        assert result.status == "ok", result.error
        assert result.output == "planned review"
        assert result.data is not None
        child_id = str(result.data["child_session_id"])
        assert core.store.session_overlays(child_id) == ("read-only",)
        assert set(_tool_names(server.requests[0])) == set(REVIEW_INSPECTION_TOOLS)
        assert [event for event in core.events_after(parent_id) if event.event_type == "plugin.review.review_dispatched"]


def test_review_child_is_not_addressable_as_a_task(tmp_path: Path) -> None:
    replies = [openai_response(message={"role": "assistant", "content": "review complete"}, finish="stop")]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server, subagents_enabled=True)
        parent_id = core.create_session("parent").session_id

        review_result = _invoke_review(core, parent_id, {})
        assert review_result.status == "ok", review_result.error
        assert review_result.data is not None
        child_id = str(review_result.data["child_session_id"])
        assert "task" not in _tool_names(server.requests[0])

        server.replies.append(
            openai_response(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "foreign-child",
                            "type": "function",
                            "function": {"name": "task", "arguments": json.dumps({"operation": "output", "task_id": child_id})},
                        }
                    ],
                },
                finish="tool_calls",
            )
        )
        server.replies.append(openai_response(message={"role": "assistant", "content": "checked"}, finish="stop"))

        run_result = core.run("Try to read the review child through task.", session_id=parent_id)

        assert run_result.status == "completed"
        assert run_result.output == "checked"
        tool_messages = [message for message in _messages(server.requests[-1]) if message.get("role") == "tool"]
        assert tool_messages
        assert "unknown task" in json.dumps(tool_messages)


def test_review_consumes_the_chain_child_quota(tmp_path: Path) -> None:
    replies = [openai_response(message={"role": "assistant", "content": "first review"}, finish="stop")]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server, max_root_children=1)
        parent_id = core.create_session("parent").session_id

        first = _invoke_review(core, parent_id, {})
        second = _invoke_review(core, parent_id, {})

        assert first.status == "ok", first.error
        assert second.status == "error"
        assert second.error is not None and "delegation spawn budget exhausted" in second.error
        spawned = [event for event in core.events_after(parent_id) if event.event_type == "core.child_spawned"]
        assert len(spawned) == 1
        assert len(server.requests) == 1


def test_review_input_preparation_does_not_start_child_or_provider(tmp_path: Path) -> None:
    from voidcode.sdk.views import FormViewData

    with loopback_server([]) as server:
        core = _core(tmp_path, server)
        session = core.create_session().session_id
        catalog = core.command_catalog()
        assert next(command for command in catalog.commands if command.name == "review").has_input
        before = core.events_after(session)
        prepared = core.command_input(session, "review", expected_revision=catalog.revision_id)
        form = FormViewData.from_payload(prepared.presentation.data)
        assert form.action == "review"
        assert form.initial_values["target"] == "uncommitted"
        assert core.events_after(session) == before
        assert server.requests == []


def _read_full_text(core: Core, digest: str) -> str:
    chunks = bytearray()
    offset = 0
    while True:
        page = core.read_artifact(digest, offset=offset)
        chunks += page.data
        offset += len(page.data)
        if page.eof:
            break
    return bytes(chunks).decode("utf-8")


def test_long_review_returns_immutable_ref_readable_in_full_across_followup(tmp_path: Path) -> None:
    line = "finding: the dirty line needs a test — 复查 🙂\n"
    long_text = line * 1200
    assert len(long_text.encode("utf-8")) > 32 * 1024, "the answer must exceed the Core inline bound"
    follow_text = "follow-up: confirmed the fix."
    replies: list[dict[str, object]] = [
        openai_response(message={"role": "assistant", "content": long_text}, finish="stop"),
        openai_response(message={"role": "assistant", "content": follow_text}, finish="stop"),
    ]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server)
        parent_id = core.create_session("parent").session_id

        result = _invoke_review(core, parent_id, {})

        assert result.status == "ok", result.error
        assert result.data is not None
        run_id = result.data["run_id"]
        child_id = result.data["child_session_id"]
        uri = result.data["output_uri"]
        assert isinstance(run_id, str) and run_id
        assert isinstance(child_id, str) and child_id
        assert isinstance(uri, str) and uri.startswith("artifact://")
        assert result.output is not None
        assert "completed without output" not in result.output
        assert uri in result.output
        assert run_id in result.output
        assert len(result.output.encode("utf-8")) < len(long_text.encode("utf-8"))

        digest = uri.removeprefix("artifact://")
        assert _read_full_text(core, digest) == long_text

        core.send_child(parent_id, child_id, "Now confirm the fix in one line.")
        follow = core.join_child(parent_id, child_id, timeout_seconds=60.0)
        assert follow.run_id != run_id
        assert follow.output == follow_text
        assert _read_full_text(core, digest) == long_text, "the exact old URI stays immutable across the follow-up"
