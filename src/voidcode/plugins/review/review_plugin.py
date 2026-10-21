"""Review plugin: the governed ``review`` command over the workspace git surface.

Ported from ``VoidCodeRuntime.review_session`` (``src/voidcode/runtime/service.py``): the target
resolution (dict aliases included), the per-target prompt parts, the reviewer-focus line, the
256 KiB inline-diff budget with its refusal messages, and the review child itself.

Where the legacy runtime ran the review through its internal ``run`` with ``read_only`` metadata,
this plugin uses the kernel's delegation seam: ``spawn_child(prompt, label="review",
tool_allowlist=..., max_steps=32, readonly=True)`` followed by ``join_child``, and the child's
answer is the command's answer. The git surface the prompt needs lives in ``workspace_review.py``.

Read-only enforcement is two layers, neither of them faked here: the review profile's tool
allowlist (read/glob/grep/ast_grep/lsp), copied as a literal because plugins cannot import
each other's manifests, plus the kernel-owned read-only stance declared at spawn. The kernel
forces that stance whenever the calling lease is already restrictive, so a plan-mode or frozen
read-only session can still delegate a review, and the child stays read-only for its whole life.

The legacy 256 KiB inline budget is kept unchanged, so a larger diff is refused with the legacy
message and never truncated. The kernel's delegated-prompt bound is 256 KiB as well, so the whole
legacy band fits; a refusal never leaves a partially prompted child behind.
instead of being cut down here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandResult
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.services import ChildOutcome, PluginServices
from voidcode.sdk.tools import ToolEffect

from .workspace_review import GitStatusSnapshot, ReviewFileDiff, WorkspaceReviewService, git_status_snapshot

#: Legacy inline review budget: a larger diff is refused instead of injected into the child prompt.
MAX_REVIEW_DIFF_BYTES: int = 256 * 1024

#: The agents plugin's builtin REVIEW manifest allowlist, copied as a literal (plugins cannot
#: import each other's manifests). This is the read-only scope of the review child.
REVIEW_TOOL_ALLOWLIST: tuple[str, ...] = ("read", "glob", "grep", "ast_grep", "lsp")

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
    title: str | None = None
    instructions: str | None = None


def resolve_review_target(raw: Mapping[str, object] | None, *, focus: str | None) -> ReviewTarget:
    """Resolve a review target the way the legacy review service resolved one."""
    if raw is None:
        if focus and focus.strip():
            return ReviewTarget(type="custom", instructions=focus.strip())
        return ReviewTarget(type="uncommitted")
    raw_type = raw.get("type", "uncommitted")
    if raw_type in ("uncommittedChanges", "uncommitted"):
        resolved = ReviewTarget(type="uncommitted")
    elif raw_type in ("baseBranch", "base_branch"):
        resolved = ReviewTarget(type="base_branch", branch=str(raw.get("branch") or ""))
    elif raw_type == "commit":
        resolved = ReviewTarget(type="commit", sha=str(raw.get("sha") or ""), title=str(raw.get("title") or "") or None)
    elif raw_type == "custom":
        resolved = ReviewTarget(type="custom", instructions=str(raw.get("instructions") or ""))
    else:
        raise ValueError(f"unknown review target type '{raw_type}'")
    if resolved.type == "custom" and not resolved.instructions and focus and focus.strip():
        return ReviewTarget(type="custom", instructions=focus.strip())
    return resolved


# --- Prompt construction (verbatim from the legacy review service) ---


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    """One resolved review: the target it reviews and the exact prompt its child receives."""

    target: ReviewTarget
    prompt: str


def _review_diff(service: WorkspaceReviewService, git: GitStatusSnapshot, target: ReviewTarget) -> tuple[ReviewFileDiff, int]:
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
    return diff, len(diff.diff.encode("utf-8"))


def build_review_request(*, workspace: Path, target: ReviewTarget, focus: str | None) -> ReviewRequest:
    """Build the review child's prompt for one target, refusing exactly as the legacy service did."""
    service = WorkspaceReviewService(workspace=workspace)
    git = git_status_snapshot(workspace)

    if target.type == "custom":
        instructions = (target.instructions or focus or "").strip()
        if not instructions:
            raise ValueError("custom review instructions must not be empty")
        diff, diff_bytes = _review_diff(service, git, target)
        if diff_bytes > MAX_REVIEW_DIFF_BYTES:
            raise ValueError(
                f"workspace diff is too large ({diff_bytes} bytes) for inline review; reduce working tree changes or review changes incrementally"
            )
        return ReviewRequest(
            target=target,
            prompt="\n\n".join(
                [
                    "Review the current workspace changes and report concrete correctness, regression, and safety findings.",
                    f"Reviewer focus: {instructions}",
                    f"Aggregate Git diff:\n{diff.diff}",
                    "Return the final review findings in your final answer.",
                ]
            ),
        )

    if target.type == "uncommitted":
        diff, diff_bytes = _review_diff(service, git, target)
        if diff_bytes > MAX_REVIEW_DIFF_BYTES:
            raise ValueError(
                f"workspace diff is too large ({diff_bytes} bytes) for inline review; reduce working tree changes or review changes incrementally"
            )
        return ReviewRequest(
            target=target,
            prompt="\n\n".join(
                [
                    "Review the current workspace changes (staged, unstaged, and untracked files) and provide prioritized findings.",
                    f"Aggregate Git diff:\n{diff.diff}",
                    "Return the final review findings in your final answer.",
                ]
            ),
        )

    if target.type == "base_branch":
        if not target.branch:
            raise ValueError("base branch target requires a branch name")
        diff, diff_bytes = _review_diff(service, git, target)
        if diff_bytes > MAX_REVIEW_DIFF_BYTES:
            raise ValueError(f"base branch diff is too large ({diff_bytes} bytes) for inline review; review changes incrementally")
        merge_base = service.merge_base_sha(target.branch, git=git)
        merge_base_text = f" The merge base commit for this comparison is {merge_base}." if merge_base else ""
        return ReviewRequest(
            target=target,
            prompt="\n\n".join(
                [
                    f"Review the code changes against the base branch '{target.branch}'.{merge_base_text}",
                    f"Git diff against base branch '{target.branch}':\n{diff.diff}",
                    "Provide prioritized, actionable findings in your final answer.",
                ]
            ),
        )

    if not target.sha:
        raise ValueError("commit target requires a commit sha")
    diff, diff_bytes = _review_diff(service, git, target)
    if diff_bytes > MAX_REVIEW_DIFF_BYTES:
        raise ValueError(f"commit diff is too large ({diff_bytes} bytes) for inline review; review changes incrementally")
    title_text = f' ("{target.title}")' if target.title else ""
    return ReviewRequest(
        target=target,
        prompt="\n\n".join(
            [
                f"Review the code changes introduced by commit {target.sha}{title_text}.",
                f"Git diff for commit {target.sha}:\n{diff.diff}",
                "Provide prioritized, actionable findings in your final answer.",
            ]
        ),
    )


# --- Command arguments (the kernel schema stays the first gate; these are second-line messages) ---


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
            f"review child {child_id} is still {outcome.status} after the {DEFAULT_JOIN_TIMEOUT_SECONDS:.0f}s wait; "
            f'await it with task(operation="output", task_id="{child_id}").'
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
                    "description": (
                        "Optional target type: uncommitted (default), base_branch, commit, or custom. "
                        "The legacy aliases uncommittedChanges and baseBranch are accepted too."
                    ),
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
            request = build_review_request(workspace=workspace, target=target, focus=args.focus)
        except ValueError as exc:
            return CommandResult(status="error", error=str(exc))
        try:
            child_id = services.spawn_child(
                request.prompt,
                label=REVIEW_CHILD_LABEL,
                tool_allowlist=REVIEW_TOOL_ALLOWLIST,
                max_steps=REVIEW_CHILD_MAX_STEPS,
                # A review child never mutates: declaring the stance is what lets a read-only or
                # plan-mode session delegate it at all, and the kernel forces it anyway whenever
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


def create_plugin(_config: Mapping[str, object] | None = None) -> Plugin:
    return Plugin(commands=(ReviewCommand(),))
