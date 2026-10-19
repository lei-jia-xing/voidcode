"""Unit tests for the Review plugin: target resolution, prompt text, diff budget, and its child."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import pytest

from voidcode.plugins.review.review_plugin import (
    DEFAULT_JOIN_TIMEOUT_SECONDS,
    MAX_REVIEW_DIFF_BYTES,
    REVIEW_CHILD_LABEL,
    REVIEW_CHILD_MAX_STEPS,
    REVIEW_TOOL_ALLOWLIST,
    ReviewCommand,
    ReviewTarget,
    build_review_request,
    create_plugin,
    resolve_review_target,
)
from voidcode.plugins.review.workspace_review import WorkspaceReviewService, git_status_snapshot
from voidcode.sdk.commands import CommandInvocation, CommandResult
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import ChildOutcome, PluginServices
from voidcode.sdk.tools import ToolEffect


class MockServices(PluginServices):
    """In-memory PluginServices: the spawned child, its outcome, and the staged events."""

    def __init__(self) -> None:
        self.spawn_calls: list[dict[str, object]] = []
        self.join_calls: list[tuple[str, float]] = []
        self.staged_events: list[tuple[str, Mapping[str, object]]] = []
        #: Outcome the kernel's join returns for the spawned child.
        self.join_status: Literal["completed", "failed", "interrupted", "cancelled", "running", "waiting"] = "completed"
        self.join_output: str | None = "review findings: 3 issues"
        self.join_error: str | None = None

    def get_state(self, scope: Literal["session", "workspace"] = "session") -> tuple[Mapping[str, object] | None, int]:
        raise AssertionError("review keeps no state")

    def get_state_schema_version(self, scope: Literal["session", "workspace"] = "session") -> int:
        raise AssertionError("review keeps no state")

    def replace_state(
        self,
        state: Mapping[str, object],
        expected_version: int,
        scope: Literal["session", "workspace"] = "session",
    ) -> int:
        raise AssertionError("review writes no state")

    def stage_event(self, event_type: str, payload: Mapping[str, object]) -> None:
        self.staged_events.append((event_type, dict(payload)))

    def spawn_child(
        self,
        prompt: str,
        *,
        label: str = "",
        tool_allowlist: tuple[str, ...] | None = None,
        max_steps: int = 32,
        readonly: bool = False,
    ) -> str:
        self.spawn_calls.append(
            {
                "prompt": prompt,
                "label": label,
                "tool_allowlist": tool_allowlist,
                "max_steps": max_steps,
                "readonly": readonly,
            }
        )
        return f"child-{len(self.spawn_calls)}"

    def join_child(self, child_session_id: str, *, timeout_seconds: float = 300.0) -> ChildOutcome:
        self.join_calls.append((child_session_id, timeout_seconds))
        return ChildOutcome(
            child_session_id=child_session_id,
            status=self.join_status,
            output=self.join_output,
            error=self.join_error,
        )


def _context(workspace: Path) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        session_id="session-1",
        run_id="run-1",
        invocation_id="inv-1",
        revision_id="rev-1",
        actor="client",
        capabilities=frozenset({"workspace.read"}),
        logical_turn="turn-1",
    )


def _invocation(workspace: Path, arguments: Mapping[str, object]) -> CommandInvocation:
    return CommandInvocation(context=_context(workspace), name="review", arguments=arguments)


def _review(services: MockServices, workspace: Path, arguments: Mapping[str, object]) -> CommandResult:
    return ReviewCommand().invoke(_invocation(workspace, arguments), services)


# --- git fixtures ---


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def _repo(tmp_path: Path, *, branch: str = "main") -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "--quiet", f"--initial-branch={branch}")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "--quiet", "-m", "initial commit")
    return root


def _diff_all(repo: Path) -> str:
    service = WorkspaceReviewService(workspace=repo)
    diff = service.diff_all(git=git_status_snapshot(repo))
    return diff.diff or ""


# --- Target resolution ---


@pytest.mark.parametrize(
    ("raw", "focus", "expected"),
    [
        (None, "look for races", ReviewTarget(type="custom", instructions="look for races")),
        (None, "  look for races  ", ReviewTarget(type="custom", instructions="look for races")),
        (None, None, ReviewTarget(type="uncommitted")),
        (None, "   ", ReviewTarget(type="uncommitted")),
        ({"type": "uncommitted"}, None, ReviewTarget(type="uncommitted")),
        ({"type": "uncommittedChanges"}, None, ReviewTarget(type="uncommitted")),
        ({"type": "base_branch", "branch": "main"}, None, ReviewTarget(type="base_branch", branch="main")),
        ({"type": "baseBranch", "branch": "main"}, None, ReviewTarget(type="base_branch", branch="main")),
        ({"type": "commit", "sha": "abc123", "title": "fix it"}, None, ReviewTarget(type="commit", sha="abc123", title="fix it")),
        ({"type": "commit", "sha": "abc123", "title": ""}, None, ReviewTarget(type="commit", sha="abc123", title=None)),
        ({"type": "custom", "instructions": "check the parser"}, None, ReviewTarget(type="custom", instructions="check the parser")),
        ({"type": "custom", "instructions": ""}, "check the parser", ReviewTarget(type="custom", instructions="check the parser")),
        ({"type": "custom", "instructions": "explicit"}, "ignored", ReviewTarget(type="custom", instructions="explicit")),
    ],
)
def test_target_resolution_table(raw: Mapping[str, object] | None, focus: str | None, expected: ReviewTarget) -> None:
    assert resolve_review_target(raw, focus=focus) == expected


def test_unknown_target_type_is_refused() -> None:
    with pytest.raises(ValueError) as excinfo:
        resolve_review_target({"type": "nonsense"}, focus=None)
    assert str(excinfo.value) == "unknown review target type 'nonsense'"


# --- Prompt construction ---


def test_uncommitted_prompt_text(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("fresh\n", encoding="utf-8")
    diff = _diff_all(repo)
    assert "+changed" in diff and "+fresh" in diff

    request = build_review_request(workspace=repo, target=resolve_review_target(None, focus=None), focus=None)

    assert request.target == ReviewTarget(type="uncommitted")
    assert request.prompt == (
        "Review the current workspace changes (staged, unstaged, and untracked files) and provide prioritized findings."
        f"\n\nAggregate Git diff:\n{diff}"
        "\n\nReturn the final review findings in your final answer."
    )


def test_custom_prompt_text_carries_the_reviewer_focus(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")
    diff = _diff_all(repo)

    request = build_review_request(
        workspace=repo,
        target=resolve_review_target({"type": "custom", "instructions": ""}, focus="  focus on the parser  "),
        focus="  focus on the parser  ",
    )

    assert request.target == ReviewTarget(type="custom", instructions="focus on the parser")
    assert request.prompt == (
        "Review the current workspace changes and report concrete correctness, regression, and safety findings."
        "\n\nReviewer focus: focus on the parser"
        f"\n\nAggregate Git diff:\n{diff}"
        "\n\nReturn the final review findings in your final answer."
    )


def test_base_branch_prompt_text_names_the_merge_base(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "checkout", "--quiet", "-b", "feature")
    (repo / "tracked.txt").write_text("base\nfeature\n", encoding="utf-8")
    diff = WorkspaceReviewService(workspace=repo).diff_base_branch("main", git=git_status_snapshot(repo)).diff or ""
    merge_base = _git(repo, "rev-parse", "main").strip()

    request = build_review_request(
        workspace=repo,
        target=resolve_review_target({"type": "baseBranch", "branch": "main"}, focus=None),
        focus=None,
    )

    assert request.prompt == (
        f"Review the code changes against the base branch 'main'. The merge base commit for this comparison is {merge_base}."
        f"\n\nGit diff against base branch 'main':\n{diff}"
        "\n\nProvide prioritized, actionable findings in your final answer."
    )


def test_commit_prompt_text_carries_sha_and_diff(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("base\ncommitted\n", encoding="utf-8")
    _git(repo, "commit", "--quiet", "-am", "second commit")
    sha = _git(repo, "rev-parse", "HEAD").strip()
    diff = WorkspaceReviewService(workspace=repo).diff_commit(sha, git=git_status_snapshot(repo)).diff or ""

    request = build_review_request(
        workspace=repo,
        target=resolve_review_target({"type": "commit", "sha": sha}, focus=None),
        focus=None,
    )

    assert request.prompt == (
        f"Review the code changes introduced by commit {sha}."
        f"\n\nGit diff for commit {sha}:\n{diff}"
        "\n\nProvide prioritized, actionable findings in your final answer."
    )


# --- Refusals (the 256 KiB inline budget and the legacy messages) ---


def _big_repo(tmp_path: Path) -> Path:
    repo = _repo(tmp_path)
    body = "".join(f"line {index} {'x' * 60}\n" for index in range(4000))
    (repo / "big.txt").write_text(body, encoding="utf-8")
    return repo


def test_oversized_workspace_diff_is_refused_with_the_legacy_message(tmp_path: Path) -> None:
    repo = _big_repo(tmp_path)
    diff_bytes = len(_diff_all(repo).encode("utf-8"))
    assert diff_bytes > MAX_REVIEW_DIFF_BYTES

    services = MockServices()
    result = _review(services, repo, {})

    assert result.status == "error"
    assert result.error == (
        f"workspace diff is too large ({diff_bytes} bytes) for inline review; reduce working tree changes or review changes incrementally"
    )
    assert services.spawn_calls == []


def test_oversized_commit_diff_is_refused_with_the_legacy_message(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "big.txt").write_text("".join(f"line {index} {'x' * 60}\n" for index in range(4000)), encoding="utf-8")
    _git(repo, "add", "big.txt")
    _git(repo, "commit", "--quiet", "-m", "big commit")
    sha = _git(repo, "rev-parse", "HEAD").strip()
    diff = WorkspaceReviewService(workspace=repo).diff_commit(sha, git=git_status_snapshot(repo)).diff or ""
    diff_bytes = len(diff.encode("utf-8"))
    assert diff_bytes > MAX_REVIEW_DIFF_BYTES

    with pytest.raises(ValueError) as excinfo:
        build_review_request(
            workspace=repo,
            target=resolve_review_target({"type": "commit", "sha": sha}, focus=None),
            focus=None,
        )
    assert str(excinfo.value) == f"commit diff is too large ({diff_bytes} bytes) for inline review; review changes incrementally"


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({}, "review found no workspace changes"),
        ({"target": "   "}, "review found no workspace changes"),
        ({"target": "nonsense"}, "unknown review target type 'nonsense'"),
        ({"target": "custom"}, "custom review instructions must not be empty"),
        ({"target": "custom", "focus": "   "}, "custom review instructions must not be empty"),
        ({"target": "base_branch"}, "base branch target requires a branch name"),
        ({"target": "base_branch", "branch": "  "}, "base branch target requires a branch name"),
        ({"target": "commit"}, "commit target requires a commit sha"),
        ({"target": "commit", "sha": "   "}, "commit target requires a commit sha"),
        ({"focus": 5}, "review focus must be a string"),
        ({"target": "commit", "sha": "not-a-sha"}, "git show failed:"),
    ],
)
def test_bad_arguments_fail_without_spawning(tmp_path: Path, arguments: Mapping[str, object], message: str) -> None:
    repo = _repo(tmp_path)
    services = MockServices()

    result = _review(services, repo, arguments)

    assert result.status == "error"
    assert result.error is not None and result.error.startswith(message)
    assert services.spawn_calls == []
    assert services.staged_events == []


def test_missing_git_repository_is_refused(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    services = MockServices()

    result = _review(services, plain, {})

    assert result.status == "error"
    assert result.error == "review requires a Git repository"
    assert services.spawn_calls == []


# --- The review child ---


def test_review_spawns_the_read_only_profile_child_and_returns_its_answer(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")
    services = MockServices()

    result = _review(services, repo, {"focus": "look for races"})

    assert result.status == "ok"
    assert result.output == "review findings: 3 issues"
    assert result.data is not None
    assert result.data["child_session_id"] == "child-1"
    assert result.data["status"] == "completed"
    assert result.data["target_type"] == "custom"
    assert result.data["output"] == "review findings: 3 issues"

    assert len(services.spawn_calls) == 1
    spawn = services.spawn_calls[0]
    assert spawn["label"] == REVIEW_CHILD_LABEL == "review"
    assert spawn["tool_allowlist"] == REVIEW_TOOL_ALLOWLIST == ("read", "glob", "grep", "ast_grep", "lsp")
    assert "yield" not in spawn["tool_allowlist"], "the legacy handoff tool does not exist in the new world"
    assert spawn["max_steps"] == REVIEW_CHILD_MAX_STEPS == 32
    assert spawn["readonly"] is True, "a review child is always read-only"
    assert "+changed" in str(spawn["prompt"])
    assert "Reviewer focus: look for races" in str(spawn["prompt"])
    assert services.join_calls == [("child-1", DEFAULT_JOIN_TIMEOUT_SECONDS)]

    assert services.staged_events == [
        (
            "review_started",
            {
                "target_type": "custom",
                "branch": None,
                "sha": None,
                "focus": "look for races",
                "child_session_id": "child-1",
            },
        )
    ]


def test_review_event_is_bounded_and_records_the_commit_target(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("base\ncommitted\n", encoding="utf-8")
    _git(repo, "commit", "--quiet", "-am", "second commit")
    sha = _git(repo, "rev-parse", "HEAD").strip()
    services = MockServices()

    result = _review(services, repo, {"target": "commit", "sha": sha})

    assert result.status == "ok"
    assert services.staged_events == [
        (
            "review_started",
            {"target_type": "commit", "branch": None, "sha": sha, "focus": None, "child_session_id": "child-1"},
        )
    ]


def test_join_timeout_surfaces_the_running_child(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")
    services = MockServices()
    services.join_status = "running"
    services.join_output = None

    result = _review(services, repo, {})

    assert result.status == "ok"
    assert result.data is not None
    assert result.data["status"] == "running"
    assert result.output is not None and "still running" in result.output
    assert 'task(operation="output", task_id="child-1")' in result.output
    assert services.join_calls == [("child-1", 300.0)]


def test_failed_child_is_reported_as_an_error(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")
    services = MockServices()
    services.join_status = "failed"
    services.join_output = None
    services.join_error = "provider exploded"

    result = _review(services, repo, {})

    assert result.status == "error"
    assert result.error == "provider exploded"
    assert result.data is not None and result.data["status"] == "failed"


# --- Git surface (ported behavior) ---


def test_branch_listing_keeps_the_default_branch_first_and_marks_the_current_branch(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "checkout", "--quiet", "-b", "feature")
    _git(repo, "checkout", "--quiet", "-b", "another")
    service = WorkspaceReviewService(workspace=repo)
    git = git_status_snapshot(repo)

    assert service.list_branches(git=git) == ("main", "another", "feature")
    assert service.current_branch(git=git) == "another"
    entries = service.recent_commits(git=git, limit=5)
    assert len(entries) == 1
    assert entries[0].subject == "initial commit"
    assert len(entries[0].sha) == 40


def test_commit_without_changes_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "commit", "--quiet", "--allow-empty", "-m", "empty commit")
    sha = _git(repo, "rev-parse", "HEAD").strip()
    service = WorkspaceReviewService(workspace=repo)
    assert service.diff_commit(sha, git=git_status_snapshot(repo)).state == "clean"

    services = MockServices()
    result = _review(services, repo, {"target": "commit", "sha": sha})

    assert result.status == "error"
    assert result.error == f"review found no changes in commit {sha}"
    assert services.spawn_calls == []


def test_base_branch_without_common_history_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "checkout", "--quiet", "--orphan", "orphan")
    _git(repo, "rm", "--quiet", "-rf", ".")
    (repo / "other.txt").write_text("other\n", encoding="utf-8")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "--quiet", "-m", "orphan commit")
    services = MockServices()

    result = _review(services, repo, {"target": "base_branch", "branch": "main"})

    assert result.status == "error"
    assert result.error is not None and result.error.startswith("git merge-base failed:")
    assert services.spawn_calls == []


def test_unknown_base_branch_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")
    services = MockServices()

    result = _review(services, repo, {"target": "base_branch", "branch": "missing"})

    assert result.status == "error"
    assert result.error == "git rev-parse failed: unknown base branch 'missing'"


# --- Registration ---


def test_plugin_registers_the_review_command_with_spawn_effects() -> None:
    plugin = create_plugin()

    assert len(plugin.commands) == 1
    definition = plugin.commands[0].definition
    assert definition.name == "review"
    assert definition.busy_gate is True
    assert definition.effects == frozenset({ToolEffect.READ, ToolEffect.SPAWN})
    assert definition.argument_schema["properties"].keys() == {"focus", "target", "branch", "sha"}
    assert definition.argument_schema["additionalProperties"] is False


def test_snapshot_keeps_the_legacy_tree_and_change_shapes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "subdir").mkdir()
    (repo / "subdir" / "inner.txt").write_text("inner\n", encoding="utf-8")
    (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (repo / "ignored.txt").write_text("ignored\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("fresh\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")
    service = WorkspaceReviewService(workspace=repo)
    git = git_status_snapshot(repo)
    assert (git.state, git.root, git.branch) == ("git_ready", str(repo.resolve()), "main")

    snapshot = service.snapshot(git=git)

    assert snapshot.root == str(repo.resolve())
    assert snapshot.git.state == "git_ready"
    assert [(item.path, item.change_type) for item in snapshot.changed_files] == [
        (".gitignore", "untracked"),
        ("subdir/inner.txt", "untracked"),
        ("tracked.txt", "modified"),
        ("untracked.txt", "untracked"),
    ]
    # Directories sort before files in the legacy tree, and a directory is changed when a child is.
    assert [(node.name, node.kind, node.changed) for node in snapshot.tree] == [
        ("subdir", "directory", True),
        (".gitignore", "file", True),
        ("tracked.txt", "file", True),
        ("untracked.txt", "file", True),
    ]
    subdir = next(node for node in snapshot.tree if node.name == "subdir")
    assert [(child.path, child.kind, child.changed) for child in subdir.children] == [("subdir/inner.txt", "file", True)]
    assert _tree_paths(snapshot.tree) == {".gitignore", "tracked.txt", "untracked.txt", "subdir", "subdir/inner.txt"}


def test_snapshot_in_a_non_repository_workspace_reports_the_plain_tree(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "notes.txt").write_text("notes\n", encoding="utf-8")
    service = WorkspaceReviewService(workspace=plain)
    git = git_status_snapshot(plain)
    assert git.state == "not_git_repo"

    snapshot = service.snapshot(git=git)

    assert snapshot.git.state == "not_git_repo"
    assert snapshot.changed_files == ()
    assert [(node.name, node.kind, node.changed) for node in snapshot.tree] == [("notes.txt", "file", False)]
    assert service.diff_all(git=git).state == "not_git_repo"


def test_file_diff_normalizes_paths_and_reports_clean_states(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")
    service = WorkspaceReviewService(workspace=repo)
    git = git_status_snapshot(repo)

    diff = service.diff(path="tracked.txt", git=git)
    assert (diff.path, diff.state) == ("tracked.txt", "changed")
    assert diff.diff is not None and "+changed" in diff.diff
    assert service.diff(path="subdir", git=git).state == "clean"
    assert service.diff(path="missing.txt", git=git).state == "clean"
    with pytest.raises(ValueError) as excinfo:
        service.diff(path="../outside.txt", git=git)
    assert str(excinfo.value) == "path must stay within the workspace"
    with pytest.raises(ValueError) as excinfo:
        service.diff(path="", git=git)
    assert str(excinfo.value) == "path must be a non-empty relative path"


def _tree_paths(tree) -> set[str]:
    paths: set[str] = set()
    for node in tree:
        paths.add(node.path)
        paths.update(_tree_paths(node.children))
    return paths
