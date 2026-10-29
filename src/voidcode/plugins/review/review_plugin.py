"""Review Git changes in a child that inherits tool scope and stays read-only."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandResult
from voidcode.sdk.limits import CHILD_PROMPT_MAX_BYTES
from voidcode.sdk.services import ChildOutcome, PluginServices
from voidcode.sdk.tools import ToolEffect

from .workspace_review import GitStatusSnapshot, ReviewFileDiff, WorkspaceReviewService, git_status_snapshot

REVIEW_CHILD_LABEL = "review"
REVIEW_CHILD_MAX_STEPS = 32
DEFAULT_JOIN_TIMEOUT_SECONDS = 300.0

ReviewTargetType = Literal["uncommitted", "base_branch", "commit", "custom"]


# --- Target resolution ---


@dataclass(frozen=True, slots=True)
class ReviewTarget:
    type: ReviewTargetType = "uncommitted"
    branch: str | None = None
    sha: str | None = None
    instructions: str | None = None


def resolve_review_target(raw: Mapping[str, object] | None, *, focus: str | None) -> ReviewTarget:
    """Resolve the supported target and optional reviewer instructions."""
    if raw is None:
        if focus and focus.strip():
            return ReviewTarget(type="custom", instructions=focus.strip())
        return ReviewTarget(type="uncommitted")
    raw_type = raw.get("type", "uncommitted")
    if raw_type == "uncommitted":
        resolved = ReviewTarget(type="uncommitted")
    elif raw_type == "base_branch":
        resolved = ReviewTarget(type="base_branch", branch=str(raw.get("branch") or ""))
    elif raw_type == "commit":
        resolved = ReviewTarget(type="commit", sha=str(raw.get("sha") or ""))
    elif raw_type == "custom":
        resolved = ReviewTarget(type="custom", instructions=str(raw.get("instructions") or ""))
    else:
        raise ValueError(f"unknown review target type '{raw_type}'")
    if resolved.type == "custom" and not resolved.instructions and focus and focus.strip():
        return ReviewTarget(type="custom", instructions=focus.strip())
    return resolved


# --- Prompt construction ---


def _review_diff(service: WorkspaceReviewService, git: GitStatusSnapshot, target: ReviewTarget) -> ReviewFileDiff:
    if target.type == "base_branch":
        diff = service.diff_base_branch(target.branch or "", git=git)
    elif target.type == "commit":
        diff = service.diff_commit(target.sha or "", git=git)
    else:
        diff = service.diff_all(git=git)
    if diff.state == "not_git_repo":
        raise ValueError("review requires a Git repository")
    if diff.state == "clean" or not diff.diff:
        if target.type == "base_branch":
            raise ValueError(f"review found no changes against base branch '{target.branch}'")
        if target.type == "commit":
            raise ValueError(f"review found no changes in commit {target.sha}")
        raise ValueError("review found no workspace changes")
    return diff


def build_review_prompt(*, workspace: Path, target: ReviewTarget, focus: str | None) -> str:
    """Build a complete review prompt or refuse an invalid or oversized target."""
    service = WorkspaceReviewService(workspace=workspace)
    git = git_status_snapshot(workspace)
    if git.state == "git_error":
        raise ValueError(git.error or "git repository inspection failed")

    if target.type == "base_branch" and not target.branch:
        raise ValueError("base branch target requires a branch name")
    if target.type == "commit" and not target.sha:
        raise ValueError("commit target requires a commit sha")
    instructions = (target.instructions or focus or "").strip()
    if target.type == "custom" and not instructions:
        raise ValueError("custom review instructions must not be empty")
    diff = _review_diff(service, git, target)
    if target.type == "custom":
        parts = [
            "Review the current workspace changes and report concrete correctness, regression, and safety findings.",
            f"Reviewer focus: {instructions}",
            f"Aggregate Git diff:\n{diff.diff}",
            "Return the final review findings in your final answer.",
        ]
    elif target.type == "uncommitted":
        parts = [
            "Review the current workspace changes (staged, unstaged, and untracked files) and provide prioritized findings.",
            f"Aggregate Git diff:\n{diff.diff}",
            "Return the final review findings in your final answer.",
        ]
    elif target.type == "base_branch":
        parts = [
            f"Review the code changes against the base branch '{target.branch}'. The merge base commit for this comparison is {diff.merge_base}.",
            f"Git diff against base branch '{target.branch}':\n{diff.diff}",
            "Provide prioritized, actionable findings in your final answer.",
        ]
    else:
        parts = [
            f"Review the code changes introduced by commit {target.sha}.",
            f"Git diff for commit {target.sha}:\n{diff.diff}",
            "Provide prioritized, actionable findings in your final answer.",
        ]
    prompt = "\n\n".join(parts)
    prompt_bytes = len(prompt.encode("utf-8"))
    if prompt_bytes > CHILD_PROMPT_MAX_BYTES:
        raise ValueError(f"review prompt is too large ({prompt_bytes} bytes); review changes incrementally")
    return prompt


# --- Command arguments (the core schema stays the first gate; these are second-line messages) ---


@dataclass(frozen=True, slots=True)
class _ReviewArgs:
    focus: str | None = None
    target: str | None = None
    branch: str | None = None
    sha: str | None = None


def _parse_optional_text(raw: object, *, where: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"review {where} must be a string")
    return raw.strip() or None


def _parse_review_args(arguments: Mapping[str, object]) -> _ReviewArgs:
    return _ReviewArgs(
        focus=_parse_optional_text(arguments.get("focus"), where="focus"),
        target=_parse_optional_text(arguments.get("target"), where="target"),
        branch=_parse_optional_text(arguments.get("branch"), where="branch"),
        sha=_parse_optional_text(arguments.get("sha"), where="sha"),
    )


def _target_input(args: _ReviewArgs) -> Mapping[str, object] | None:
    if args.target is None:
        return None
    return {"type": args.target, "branch": args.branch, "sha": args.sha}


# --- Command ---


def _data(target: ReviewTarget, child_id: str, outcome: ChildOutcome) -> dict[str, object]:
    return {
        "child_session_id": child_id,
        "status": outcome.status,
        "target_type": target.type,
        "output": outcome.output,
        "error": outcome.error,
        "stop_cause": outcome.stop_cause,
    }


def _outcome_result(target: ReviewTarget, child_id: str, outcome: ChildOutcome) -> CommandResult:
    data = _data(target, child_id, outcome)
    if outcome.status == "completed":
        text = outcome.output if outcome.output is not None else f"review child {child_id} completed without output"
        return CommandResult(status="ok", output=text, data=data)
    if outcome.status in ("running", "waiting"):
        text = (
            f"review child {child_id} is still {outcome.status} after the {DEFAULT_JOIN_TIMEOUT_SECONDS:.0f}s wait. "
            f"Inspect it with `voidcode show {child_id}`."
        )
        return CommandResult(status="ok", output=text, data=data)
    error = outcome.error or f"review child {child_id} {outcome.status}"
    return CommandResult(status="error", error=error, data=data)


class ReviewCommand:
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="review",
        description="Review current workspace changes with the governed review agent.",
        argument_schema={
            "type": "object",
            "properties": {
                "focus": {"type": "string", "description": "Optional reviewer focus; a focus alone reviews it as a custom target."},
                "target": {
                    "type": "string",
                    "description": ("Optional target type: uncommitted (default), base_branch, commit, or custom. "),
                },
                "branch": {"type": "string", "description": "Base branch name, required by target=base_branch."},
                "sha": {"type": "string", "description": "Commit sha, required by target=commit."},
            },
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.READ, ToolEffect.SPAWN}),
        busy_gate=True,
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        workspace = invocation.context.require_workspace()
        try:
            args = _parse_review_args(invocation.arguments)
            target = resolve_review_target(_target_input(args), focus=args.focus)
            prompt = build_review_prompt(workspace=workspace, target=target, focus=args.focus)
        except ValueError as exc:
            return CommandResult(status="error", error=str(exc))
        try:
            child_id = services.spawn_child(
                prompt,
                label=REVIEW_CHILD_LABEL,
                max_steps=REVIEW_CHILD_MAX_STEPS,
                # A review child never mutates: declaring the stance is what lets a read-only or
                # plan-mode session delegate it at all, and the core forces it anyway whenever
                # the calling lease is restrictive.
                readonly=True,
            )
        except (ValueError, PermissionError, RuntimeError) as exc:
            return CommandResult(status="error", error=str(exc))
        services.stage_event(
            "review_started",
            {
                "target_type": target.type,
                "branch": target.branch,
                "sha": target.sha,
                "focus": args.focus,
                "child_session_id": child_id,
            },
        )
        try:
            outcome = services.join_child(child_id, timeout_seconds=DEFAULT_JOIN_TIMEOUT_SECONDS)
        except (ValueError, PermissionError, RuntimeError) as exc:
            return CommandResult(status="error", error=str(exc), data={"child_session_id": child_id, "target_type": target.type})
        return _outcome_result(target, child_id, outcome)
