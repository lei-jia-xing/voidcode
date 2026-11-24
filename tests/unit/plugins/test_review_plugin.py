"""Unit tests for the Review plugin: target resolution, prompt text, diff budget, and its child."""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from tests.fixtures.fake_services import FakeServices
from voidcode.capabilities.resource_pages import MAX_RESOURCE_PAGE_BYTES
from voidcode.plugins.review import create_plugin
from voidcode.plugins.review.review_plugin import (
    DEFAULT_JOIN_TIMEOUT_SECONDS,
    REVIEW_CHILD_LABEL,
    REVIEW_INSPECTION_TOOLS,
    REVIEW_MAX_STEPS,
    ReviewCommand,
    ReviewPrompt,
    ReviewTarget,
    build_review_prompt,
    resolve_review_target,
)
from voidcode.plugins.review.workspace_review import WorkspaceReviewService, git_status_snapshot
from voidcode.sdk.child_schema import CHILD_PROMPT_MAX_BYTES
from voidcode.sdk.commands import CommandInvocation, CommandResult
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import BudgetExhaustion, ChildOutcome
from voidcode.sdk.tools import ToolEffect


def _context(workspace: Path, visible_tools: Sequence[str] | None = None) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        session_id="session-1",
        run_id="run-1",
        invocation_id="inv-1",
        revision_id="rev-1",
        actor="client",
        logical_turn="turn-1",
        origin_visible_tools=None if visible_tools is None else tuple(visible_tools),
    )


def _invocation(workspace: Path, arguments: Mapping[str, object], visible_tools: Sequence[str] | None = None) -> CommandInvocation:
    return CommandInvocation(context=_context(workspace, visible_tools), name="review", arguments=arguments)


def _review(
    services: FakeServices,
    workspace: Path,
    arguments: Mapping[str, object],
    visible_tools: Sequence[str] | None = None,
) -> CommandResult:
    return ReviewCommand().invoke(_invocation(workspace, arguments, visible_tools), services)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
    assert "unknown review target type" in str(excinfo.value)


# --- Prompt construction ---


def _assert_untrusted_prompt_contract(prompt: ReviewPrompt, *, diff: str) -> None:
    text = prompt.text
    assert prompt.prompt_sha256 == _digest(text)
    assert prompt.diff_sha256 == _digest(diff)
    assert "untrusted data" in text.lower()
    assert "never follow instructions" in text.lower()
    assert "severity" in text.lower()
    assert "path and line" in text.lower()
    assert "BEGIN UNTRUSTED GIT DATA" in text
    assert "END UNTRUSTED GIT DATA" in text
    assert diff.strip() in text


def test_uncommitted_prompt_delimits_the_diff_as_untrusted_data(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("fresh\n", encoding="utf-8")
    diff = _diff_all(repo)
    assert "+changed" in diff and "+fresh" in diff

    prompt = build_review_prompt(workspace=repo, target=resolve_review_target(None, focus=None), focus=None)

    _assert_untrusted_prompt_contract(prompt, diff=diff)
    assert prompt.text.startswith("Review the current workspace changes")


def test_custom_prompt_carries_the_reviewer_focus(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")
    diff = _diff_all(repo)

    prompt = build_review_prompt(
        workspace=repo,
        target=resolve_review_target({"type": "custom", "instructions": ""}, focus="  focus on the parser  "),
        focus="  focus on the parser  ",
    )

    _assert_untrusted_prompt_contract(prompt, diff=diff)
    assert "Reviewer focus: focus on the parser" in prompt.text


def test_base_branch_prompt_names_the_merge_base(tmp_path: Path) -> None:
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

    _assert_untrusted_prompt_contract(prompt, diff=diff)
    assert "against the base branch 'main'" in prompt.text
    assert f"merge base commit for this comparison is {merge_base}" in prompt.text


def test_commit_prompt_carries_sha_and_patch(tmp_path: Path) -> None:
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

    _assert_untrusted_prompt_contract(prompt, diff=diff)
    assert f"introduced by commit {sha}" in prompt.text
    assert "second commit" not in prompt.text, "commit metadata is not part of the reviewed patch"


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
        ({}, "review found no reviewable workspace changes"),
        ({"target": "   "}, "review found no reviewable workspace changes"),
        ({"target": "nonsense"}, "unknown review target type 'nonsense'"),
        ({"target": "custom"}, "custom review instructions must not be empty"),
        ({"target": "custom", "focus": "   "}, "custom review instructions must not be empty"),
        ({"target": "base_branch"}, "base branch target requires a branch name"),
        ({"target": "base_branch", "branch": "  "}, "base branch target requires a branch name"),
        ({"target": "commit"}, "commit target requires a commit sha"),
        ({"target": "commit", "sha": "   "}, "commit target requires a commit sha"),
        ({"focus": 5}, "review focus must be a string"),
        ({"target": "commit", "sha": "not-a-sha"}, "git rev-parse failed:"),
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


def test_rev_parse_failure_is_a_repo_error_without_stderr_classification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    detail = "fatal: arbitrary repository refusal " + "x" * 5000

    def fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        _ = (args, kwargs)
        return subprocess.CompletedProcess([], 128, "", detail)

    monkeypatch.setattr("voidcode.plugins.review.workspace_review.subprocess.run", fail)
    snapshot = git_status_snapshot(tmp_path)

    assert snapshot.state == "not_git_repo"
    assert snapshot.root is None
    assert snapshot.error is not None
    assert snapshot.error.startswith("fatal: arbitrary repository refusal")
    assert len(snapshot.error) == 2000


# --- The review child ---


def test_review_spawns_a_narrow_notifying_child_and_returns_its_answer(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")
    services = FakeServices()
    services.join_output = "review findings: 3 issues"

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
    assert spawn["label"] == REVIEW_CHILD_LABEL == "review-git"
    assert spawn["tool_allowlist"] == REVIEW_INSPECTION_TOOLS
    assert spawn["max_steps"] == REVIEW_MAX_STEPS == 32
    assert spawn["readonly"] is True, "a review child is always read-only"
    assert spawn["notify_on_completion"] is False, "a synchronous review owns its result"
    prompt_text = str(spawn["prompt"])
    assert "+changed" in prompt_text
    assert "Reviewer focus: look for races" in prompt_text
    assert services.join_calls == [("child-1", DEFAULT_JOIN_TIMEOUT_SECONDS)]
    assert services.acks == []

    assert len(services.staged_events) == 1
    event_type, payload = services.staged_events[0]
    assert event_type == "review_dispatched"
    assert payload["target_type"] == "custom"
    assert payload["branch"] is None and payload["sha"] is None
    assert payload["focus"] == "look for races"
    assert payload["child_session_id"] == "child-1"
    assert payload["prompt_sha256"] == _digest(prompt_text)
    assert payload["diff_sha256"] == _digest(_diff_all(repo))
    assert payload["skipped_untracked_count"] == 0


def test_review_event_records_the_commit_target_and_diff_digest(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("base\ncommitted\n", encoding="utf-8")
    _git(repo, "commit", "--quiet", "-am", "second commit")
    sha = _git(repo, "rev-parse", "HEAD").strip()
    diff = WorkspaceReviewService(workspace=repo).diff_commit(sha, git=git_status_snapshot(repo)).diff or ""
    services = FakeServices()

    result = _review(services, repo, {"target": "commit", "sha": sha})

    assert result.status == "ok"
    event_type, payload = services.staged_events[0]
    assert event_type == "review_dispatched"
    assert payload["target_type"] == "commit"
    assert payload["sha"] == sha
    assert payload["child_session_id"] == "child-1"
    assert payload["diff_sha256"] == _digest(diff)
    assert len(str(payload["prompt_sha256"])) == 64


def test_join_timeout_surfaces_the_running_child_without_lying_about_the_wait(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")
    services = FakeServices()
    services.join_status = "running"
    services.join_output = None

    result = _review(services, repo, {})

    assert result.status == "ok"
    assert result.data is not None
    assert result.data["status"] == "running"
    assert result.data["wait_budget_seconds"] == 300.0
    assert result.data["waited_seconds"] >= 0
    assert result.output is not None and "still running after" in result.output
    assert "voidcode show child-1" in result.output
    assert "subagents.monitor" in result.output
    assert "after the 300s wait" not in result.output
    assert "not pushed to this parent" in result.output
    assert services.join_calls == [("child-1", 300.0)]
    assert services.spawn_calls[0]["notify_on_completion"] is False
    assert services.cancel_calls == []
    assert services.acks == []


def test_waiting_child_does_not_claim_a_300s_wait(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")
    services = FakeServices()
    services.join_status = "waiting"

    result = _review(services, repo, {})

    assert result.status == "ok"
    assert result.data is not None and result.data["status"] == "waiting"
    assert "waiting for approval" in (result.output or "")
    assert "after 300s" not in (result.output or "")
    assert services.cancel_calls == []
    assert services.acks == []


def test_staging_failure_cancels_the_child_and_surfaces_both_ids(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")

    class FailingStageServices(FakeServices):
        def stage_event(self, event_type: str, payload: Mapping[str, object]) -> None:
            raise PermissionError("read-only service lease refused staging")

    services = FailingStageServices()

    result = _review(services, repo, {})

    assert result.status == "error"
    assert result.data is not None
    assert result.data["child_session_id"] == "child-1"
    assert "dispatch event could not be staged" in (result.error or "")
    assert "read-only service lease refused staging" in (result.error or "")
    assert result.data["child_cancelled"] is True
    assert services.cancel_calls == [("child-1", "review dispatch record could not be committed")]
    assert services.spawn_calls[0]["notify_on_completion"] is False
    assert services.join_calls == []
    assert services.staged_events == []


def test_failed_child_surfaces_captured_output(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")
    services = FakeServices()
    services.join_status = "failed"
    services.join_output = "partial finding: missing test"
    services.join_error = "provider exploded"

    result = _review(services, repo, {})

    assert result.status == "error"
    assert result.error is not None
    assert "provider exploded" in result.error
    assert "partial finding: missing test" in result.error
    assert result.data is not None and result.data["status"] == "failed"


def test_budget_interrupted_child_preserves_typed_stop_in_review_result(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")
    budget = BudgetExhaustion(
        dimension="provider_turns",
        source="max_steps",
        limit=1,
        consumed=1,
        remaining=0,
        continue_with="resume_with_max_steps",
    )
    services = FakeServices()
    services.join_status = "interrupted"
    services.join_error = "provider turn cap exhausted"

    def join_child(child_session_id: str, *, timeout_seconds: float = 300.0) -> ChildOutcome:
        _ = timeout_seconds
        return ChildOutcome(
            child_session_id=child_session_id,
            status="interrupted",
            error="provider turn cap exhausted",
            stop_cause="limit_reached",
            run_id="child-run",
            budget_exhaustion=budget,
        )

    services.join_child = join_child  # type: ignore[method-assign]

    result = _review(services, repo, {})

    assert result.status == "error"
    assert result.error == "provider turn cap exhausted"
    assert result.data is not None
    assert result.data["status"] == "interrupted"
    assert result.data["stop_cause"] == "limit_reached"
    assert result.data["budget_exhaustion"] == budget.as_payload()


def test_long_completed_review_returns_run_and_immutable_ref_instead_of_empty(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")
    services = FakeServices()
    finding = "finding: the dirty line needs a test — 复查 🙂\n"
    full_text = finding * 2000
    full_bytes = full_text.encode("utf-8")
    assert len(full_bytes) > MAX_RESOURCE_PAGE_BYTES, "the fixture must exceed one shared artifact page"
    reference = services.spill_artifact(full_bytes, media_type="text/plain; charset=utf-8")
    outcome = ChildOutcome(
        child_session_id="child-1",
        status="completed",
        output=None,
        run_id="run-long",
        output_sha256=reference.sha256,
        output_bytes=reference.bytes,
        output_preview=full_text[:200],
    )

    def join_child(child_session_id: str, *, timeout_seconds: float = 300.0) -> ChildOutcome:
        _ = timeout_seconds
        assert child_session_id == "child-1"
        return outcome

    services.join_child = join_child  # type: ignore[method-assign]

    result = _review(services, repo, {})

    assert result.status == "ok"
    assert result.data is not None
    assert result.data["status"] == "completed"
    assert result.data["run_id"] == "run-long"
    assert result.data["output_sha256"] == reference.sha256
    assert result.data["output_bytes"] == reference.bytes
    assert result.data["output_preview"] == full_text[:200]
    uri = f"artifact://{reference.sha256}"
    assert result.data["output_uri"] == uri
    assert result.output is not None
    assert "completed without output" not in result.output
    assert "run-long" in result.output
    assert uri in result.output
    assert finding.strip() in result.output
    assert len(result.output.encode("utf-8")) < len(full_bytes), "only the first bounded page is inline"
    seen = bytearray()
    offset = 0
    while True:
        page = services.read_artifact(reference.sha256, offset=offset)
        seen += page.data
        offset += len(page.data)
        if page.eof:
            break
    assert bytes(seen) == full_bytes


def test_preview_only_completed_review_fails_visibly_without_relabeling_the_run(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")
    services = FakeServices()
    cause = "ArtifactStoreError: quota exceeded"
    history_cause = "HistoryStoreError: snapshot write failed"
    outcome = ChildOutcome(
        child_session_id="child-1",
        status="completed",
        output=None,
        run_id="run-capped",
        output_preview="partial finding: the dirty line…",
        output_error=cause,
        history_error=history_cause,
    )

    def join_child(child_session_id: str, *, timeout_seconds: float = 300.0) -> ChildOutcome:
        _ = timeout_seconds
        assert child_session_id == "child-1"
        return outcome

    services.join_child = join_child  # type: ignore[method-assign]

    result = _review(services, repo, {})

    assert result.status == "error"
    assert result.data is not None
    assert result.data["status"] == "completed", "a capture failure must not relabel the child run"
    assert result.data["run_id"] == "run-capped"
    assert result.data["output_preview"] == "partial finding: the dirty line…"
    assert result.data["output_error"] == cause
    assert result.data["history_error"] == history_cause
    assert "output_uri" not in result.data, "no URI may be invented for uncaptured output"
    assert result.error is not None
    assert cause in result.error
    assert "run-capped" in result.error
    assert "artifact://" not in result.error


def test_empty_completed_review_stays_ok_and_distinct_from_preview_only(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")
    services = FakeServices()

    def join_child(child_session_id: str, *, timeout_seconds: float = 300.0) -> ChildOutcome:
        _ = timeout_seconds
        return ChildOutcome(child_session_id=child_session_id, status="completed", output=None, run_id="run-empty")

    services.join_child = join_child  # type: ignore[method-assign]

    result = _review(services, repo, {})

    assert result.status == "ok"
    assert result.output is not None
    assert result.data is not None
    assert result.data["status"] == "completed"
    assert result.data["run_id"] == "run-empty"
    assert "output_uri" not in result.data
    assert "output_error" not in result.data


def test_missing_output_artifact_does_not_publish_an_unverified_uri(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")
    services = FakeServices()
    missing = "a" * 64

    def join_child(child_session_id: str, *, timeout_seconds: float = 300.0) -> ChildOutcome:
        _ = timeout_seconds
        return ChildOutcome(
            child_session_id=child_session_id,
            status="completed",
            output=None,
            run_id="run-missing",
            output_sha256=missing,
            output_bytes=123,
        )

    services.join_child = join_child  # type: ignore[method-assign]

    result = _review(services, repo, {})

    assert result.status == "error"
    assert result.data is not None
    assert result.data["output_sha256"] == missing
    assert "output_uri" not in result.data
    assert "full output is unavailable" in (result.error or "")


def test_inspection_allowlist_narrows_to_visible_tools(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")
    services = FakeServices()

    result = _review(services, repo, {}, visible_tools=("read", "task"))

    assert result.status == "ok"
    assert services.spawn_calls[0]["tool_allowlist"] == ("read",)


def test_no_visible_inspection_tool_refuses_before_spawning(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")
    services = FakeServices()

    result = _review(services, repo, {}, visible_tools=("task",))

    assert result.status == "error"
    assert result.error == "review requires one of the read, grep, or glob tools in the parent scope"
    assert services.spawn_calls == []


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
    assert result.error is not None and result.error.startswith("git rev-parse failed: unknown base branch 'missing'")


# --- Registration ---


def test_plugin_registers_the_review_command_with_spawn_and_session_effects() -> None:
    plugin = create_plugin()

    assert len(plugin.commands) == 1
    definition = plugin.commands[0].definition
    assert definition.name == "review"
    assert definition.busy_gate is True
    assert definition.effects == frozenset({ToolEffect.READ, ToolEffect.SPAWN, ToolEffect.SESSION})
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


def test_option_like_refs_are_refused_before_git(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    service = WorkspaceReviewService(workspace=repo)
    git = git_status_snapshot(repo)

    with pytest.raises(ValueError, match="must not start with '-'"):
        service.diff_commit("--textconv", git=git)
    with pytest.raises(ValueError, match="must not start with '-'"):
        service.diff_base_branch("--show-toplevel", git=git)

    services = FakeServices()
    result = _review(services, repo, {"target": "commit", "sha": "--output=/tmp/review"})
    assert result.status == "error"
    assert "must not start with '-'" in (result.error or "")
    assert services.spawn_calls == []


def test_commit_message_lines_do_not_look_like_a_diff(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "commit", "--quiet", "--allow-empty", "-m", "--- not a diff")
    sha = _git(repo, "rev-parse", "HEAD").strip()
    service = WorkspaceReviewService(workspace=repo)
    assert service.diff_commit(sha, git=git_status_snapshot(repo)).state == "clean"


def test_untracked_secret_files_are_omitted(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "visible.txt").write_text("review me\n", encoding="utf-8")
    (repo / ".env").write_text("TOKEN=supersecret\n", encoding="utf-8")
    (repo / "server.pem").write_text("PRIVATE KEY\n", encoding="utf-8")
    (repo / "id_rsa").write_text("PRIVATE KEY\n", encoding="utf-8")
    (repo / "credentials").write_text("password\n", encoding="utf-8")
    service = WorkspaceReviewService(workspace=repo)
    diff = service.diff_all(git=git_status_snapshot(repo))
    text = diff.diff or ""

    assert "visible.txt" in text
    assert "supersecret" not in text
    assert ".env" not in text
    assert "server.pem" not in text
    assert "id_rsa" not in text
    assert {Path(path).name for path in diff.skipped_untracked} == {".env", "server.pem", "id_rsa", "credentials"}


def test_only_secret_untracked_changes_are_refused_with_a_count(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".env.local").write_text("TOKEN=supersecret\n", encoding="utf-8")
    services = FakeServices()

    result = _review(services, repo, {})

    assert result.status == "error"
    assert result.error is not None and "review found no reviewable workspace changes" in result.error
    assert "1 likely-secret untracked file(s) omitted" in result.error
    assert services.spawn_calls == []


def test_workspace_subdirectory_scopes_git_diff(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "--quiet", "--initial-branch=main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "sub").mkdir()
    (root / "sub" / "inside.txt").write_text("inside\n", encoding="utf-8")
    (root / "outside.txt").write_text("outside\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "--quiet", "-m", "base")
    (root / "sub" / "inside.txt").write_text("inside changed\n", encoding="utf-8")
    (root / "outside.txt").write_text("outside changed\n", encoding="utf-8")
    (root / "sub" / "new.txt").write_text("new inside\n", encoding="utf-8")
    (root / "new-outside.txt").write_text("new outside\n", encoding="utf-8")
    workspace = root / "sub"
    service = WorkspaceReviewService(workspace=workspace)
    diff = service.diff_all(git=git_status_snapshot(workspace))
    text = diff.diff or ""

    assert "sub/inside.txt" in text
    assert "sub/new.txt" in text
    assert "outside.txt" not in text
    assert "new-outside.txt" not in text


def test_commit_target_scopes_to_workspace(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "--quiet", "--initial-branch=main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "sub").mkdir()
    (root / "sub" / "inside.txt").write_text("inside\n", encoding="utf-8")
    (root / "outside.txt").write_text("outside\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "--quiet", "-m", "base")
    (root / "sub" / "inside.txt").write_text("inside changed\n", encoding="utf-8")
    (root / "outside.txt").write_text("outside changed\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "--quiet", "-m", "both changed")
    sha = _git(root, "rev-parse", "HEAD").strip()
    service = WorkspaceReviewService(workspace=root / "sub")
    diff = service.diff_commit(sha, git=git_status_snapshot(root / "sub"))
    text = diff.diff or ""

    assert "sub/inside.txt" in text
    assert "outside.txt" not in text


def test_base_branch_uses_the_local_merge_base_only(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "main-only.txt").write_text("main advance\n", encoding="utf-8")
    _git(repo, "add", "main-only.txt")
    _git(repo, "commit", "--quiet", "-m", "advance main")
    _git(repo, "checkout", "--quiet", "-b", "feature")
    (repo / "feature-only.txt").write_text("feature\n", encoding="utf-8")
    _git(repo, "add", "feature-only.txt")
    _git(repo, "commit", "--quiet", "-m", "feature work")
    service = WorkspaceReviewService(workspace=repo)
    diff = service.diff_base_branch("main", git=git_status_snapshot(repo))
    text = diff.diff or ""

    assert "feature-only.txt" in text
    assert "main-only.txt" not in text


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (FileNotFoundError("git"), "git executable was not found"),
        (subprocess.TimeoutExpired(["git"], 30), "timed out after 30s"),
    ],
)
def test_git_launch_or_timeout_failure_is_a_typed_command_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception, message: str
) -> None:
    def fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        _ = (args, kwargs)
        raise failure

    monkeypatch.setattr("voidcode.plugins.review.workspace_review.subprocess.run", fail)
    services = FakeServices()

    result = _review(services, tmp_path, {})

    assert result.status == "error"
    assert message in (result.error or "")
    assert services.spawn_calls == []


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
    assert "Reviewer focus: Check error handling" in prompt.text


def test_review_factory_rejects_unsupported_config() -> None:
    assert create_plugin({"providers": {}}).commands
    with pytest.raises(ValueError, match="review config does not support field: max_steps"):
        create_plugin({"max_steps": 8})
