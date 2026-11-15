"""Unit tests for the Review plugin: target resolution, prompt text, diff budget, and its child."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.fixtures.fake_services import FakeServices
from voidcode.plugins.review import create_plugin
from voidcode.plugins.review.review_plugin import (
    DEFAULT_JOIN_TIMEOUT_SECONDS,
    REVIEW_CHILD_LABEL,
    ReviewCommand,
    ReviewTarget,
    build_review_prompt,
    resolve_review_target,
)
from voidcode.plugins.review.workspace_review import WorkspaceReviewService, git_status_snapshot
from voidcode.sdk.commands import CommandInvocation, CommandResult
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.limits import CHILD_PROMPT_MAX_BYTES
from voidcode.sdk.tools import ToolEffect


def _context(workspace: Path) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        session_id="session-1",
        run_id="run-1",
        invocation_id="inv-1",
        revision_id="rev-1",
        actor="client",
        logical_turn="turn-1",
    )


def _invocation(workspace: Path, arguments: Mapping[str, object]) -> CommandInvocation:
    return CommandInvocation(context=_context(workspace), name="review", arguments=arguments)


def _review(services: FakeServices, workspace: Path, arguments: Mapping[str, object]) -> CommandResult:
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
        ({"type": "base_branch", "branch": "main"}, None, ReviewTarget(type="base_branch", branch="main")),
        ({"type": "base_branch", "branch": "main"}, None, ReviewTarget(type="base_branch", branch="main")),
        ({"type": "commit", "sha": "abc123"}, None, ReviewTarget(type="commit", sha="abc123")),
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

    prompt = build_review_prompt(workspace=repo, target=resolve_review_target(None, focus=None), focus=None)

    assert prompt == (
        "Review the current workspace changes (staged, unstaged, and untracked files) and provide prioritized findings."
        f"\n\nAggregate Git diff:\n{diff}"
        "\n\nReturn the final review findings in your final answer."
    )


def test_custom_prompt_text_carries_the_reviewer_focus(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")
    diff = _diff_all(repo)

    prompt = build_review_prompt(
        workspace=repo,
        target=resolve_review_target({"type": "custom", "instructions": ""}, focus="  focus on the parser  "),
        focus="  focus on the parser  ",
    )

    assert prompt == (
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

    prompt = build_review_prompt(
        workspace=repo,
        target=resolve_review_target({"type": "base_branch", "branch": "main"}, focus=None),
        focus=None,
    )

    assert prompt == (
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

    prompt = build_review_prompt(
        workspace=repo,
        target=resolve_review_target({"type": "commit", "sha": sha}, focus=None),
        focus=None,
    )

    assert prompt == (
        f"Review the code changes introduced by commit {sha}."
        f"\n\nGit diff for commit {sha}:\n{diff}"
        "\n\nProvide prioritized, actionable findings in your final answer."
    )


# --- Refusals: the complete 256 KiB child prompt budget ---


def _big_repo(tmp_path: Path) -> Path:
    repo = _repo(tmp_path)
    body = "".join(f"line {index} {'x' * 60}\n" for index in range(4000))
    (repo / "big.txt").write_text(body, encoding="utf-8")
    return repo


def test_oversized_workspace_diff_is_refused(tmp_path: Path) -> None:
    repo = _big_repo(tmp_path)
    diff_bytes = len(_diff_all(repo).encode("utf-8"))
    assert diff_bytes > CHILD_PROMPT_MAX_BYTES

    services = FakeServices()
    result = _review(services, repo, {})

    assert result.status == "error"
    assert result.error is not None and result.error.startswith("review prompt is too large (")
    assert "review changes incrementally" in result.error
    assert services.spawn_calls == []


def test_oversized_commit_diff_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "big.txt").write_text("".join(f"line {index} {'x' * 60}\n" for index in range(4000)), encoding="utf-8")
    _git(repo, "add", "big.txt")
    _git(repo, "commit", "--quiet", "-m", "big commit")
    sha = _git(repo, "rev-parse", "HEAD").strip()
    diff = WorkspaceReviewService(workspace=repo).diff_commit(sha, git=git_status_snapshot(repo)).diff or ""
    diff_bytes = len(diff.encode("utf-8"))
    assert diff_bytes > CHILD_PROMPT_MAX_BYTES

    with pytest.raises(ValueError) as excinfo:
        build_review_prompt(
            workspace=repo,
            target=resolve_review_target({"type": "commit", "sha": sha}, focus=None),
            focus=None,
        )
    assert str(excinfo.value).startswith("review prompt is too large (")
    assert "review changes incrementally" in str(excinfo.value)


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
    services = FakeServices()

    result = _review(services, repo, arguments)

    assert result.status == "error"
    assert result.error is not None and result.error.startswith(message)
    assert services.spawn_calls == []
    assert services.staged_events == []


def test_missing_git_repository_is_refused(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    services = FakeServices()

    result = _review(services, plain, {})

    assert result.status == "error"
    assert result.error == "review requires a Git repository"
    assert services.spawn_calls == []


# --- The review child ---


def test_review_spawns_the_read_only_profile_child_and_returns_its_answer(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")
    services = FakeServices()

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
    assert spawn["tool_allowlist"] is None
    assert spawn["max_steps"] is None
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
    services = FakeServices()

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
    services = FakeServices()
    services.join_status = "running"
    services.join_output = None

    result = _review(services, repo, {})

    assert result.status == "ok"
    assert result.data is not None
    assert result.data["status"] == "running"
    assert result.output is not None and "still running" in result.output
    assert "voidcode show child-1" in result.output
    assert services.join_calls == [("child-1", 300.0)]


def test_failed_child_is_reported_as_an_error(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")
    services = FakeServices()
    services.join_status = "failed"
    services.join_output = None
    services.join_error = "provider exploded"

    result = _review(services, repo, {})

    assert result.status == "error"
    assert result.error == "provider exploded"
    assert result.data is not None and result.data["status"] == "failed"


# --- Git surface (ported behavior) ---


def test_commit_without_changes_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "commit", "--quiet", "--allow-empty", "-m", "empty commit")
    sha = _git(repo, "rev-parse", "HEAD").strip()
    service = WorkspaceReviewService(workspace=repo)
    assert service.diff_commit(sha, git=git_status_snapshot(repo)).state == "clean"

    services = FakeServices()
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
    services = FakeServices()

    result = _review(services, repo, {"target": "base_branch", "branch": "main"})

    assert result.status == "error"
    assert result.error is not None and result.error.startswith("git merge-base failed:")
    assert services.spawn_calls == []


def test_unknown_base_branch_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")
    services = FakeServices()

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


def test_git_inspection_failure_preserves_the_diagnostic(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".git" / "config").write_text("invalid config line\n", encoding="utf-8")
    services = FakeServices()
    result = _review(services, repo, {})
    assert result.status == "error"
    assert "bad config line" in (result.error or "")
    assert services.spawn_calls == []


def test_review_diff_does_not_execute_configured_text_conversion(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    marker = repo / "textconv-ran"
    _git(repo, "config", "diff.test.textconv", f"touch {marker}")
    (repo / ".gitattributes").write_text("tracked.txt diff=test\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")
    result = _review(FakeServices(), repo, {})
    assert result.status == "ok"
    assert not marker.exists()


@pytest.mark.parametrize("focus", [None, "检查" * 50_000], ids=["diff-with-prefix", "utf8-focus"])
def test_review_budget_counts_instructions_and_diff_in_utf8(tmp_path: Path, focus: str | None) -> None:
    repo = _repo(tmp_path)
    # The diff fits on its own; the complete prompt exceeds the child limit.
    body = "x" * (CHILD_PROMPT_MAX_BYTES - 180) if focus is None else "changed"
    (repo / "tracked.txt").write_text(body + "\n", encoding="utf-8")
    assert len(_diff_all(repo).encode("utf-8")) < CHILD_PROMPT_MAX_BYTES
    services = FakeServices()
    result = _review(services, repo, {} if focus is None else {"focus": focus})
    assert result.status == "error"
    assert "review prompt is too large" in (result.error or "")
    assert not services.spawn_calls


def test_review_prepares_target_form_without_starting_work(tmp_path: Path) -> None:
    from voidcode.sdk.views import FormViewData, validate_form_submission

    class NoServices:
        def __getattr__(self, name):
            raise AssertionError(f"Preparation accessed {name}")

    command = ReviewCommand()
    presentation = command.prepare_input(_invocation(tmp_path, {}), NoServices())
    form = FormViewData.from_payload(presentation.data)
    assert form.action == "review"
    assert form.initial_values == {"target": "uncommitted"}
    assert {option.id for option in form.fields[0].options} == {"uncommitted", "base_branch", "commit", "custom"}
    arguments = validate_form_submission(form, {"target": "base_branch", "branch": "main", "focus": "Check error handling"})
    assert arguments == {"target": "base_branch", "branch": "main", "focus": "Check error handling"}
    refused = command.invoke(_invocation(tmp_path, {"target": "base_branch"}), FakeServices())
    assert refused.status == "error"
    assert "requires a branch name" in refused.error


def test_reviewer_focus_is_preserved_for_explicit_workspace_target(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("changed\n")
    prompt = build_review_prompt(workspace=repo, target=ReviewTarget(type="uncommitted"), focus="Check error handling")
    assert "Reviewer focus: Check error handling" in prompt


def test_review_factory_rejects_unsupported_config() -> None:
    assert create_plugin({"providers": {}}).commands
    with pytest.raises(ValueError, match="review config does not support field: max_steps"):
        create_plugin({"max_steps": 8})
