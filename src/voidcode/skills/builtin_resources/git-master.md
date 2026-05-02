---
name: git-master
description: Help with git history, commit preparation, branch review, rebasing, squashing, and recovering a safe working state. Use this skill when the user asks about commits, branch state, history investigation, or any git workflow that needs careful review and auditability.
---

# Git Master

Use this skill for git work that needs careful judgment, such as preparing a commit, reviewing local changes, understanding branch state, investigating history, or deciding how to handle a rebase or squash.

## Workflow

1. Start with the current state. Check what is modified, staged, untracked, ahead, or behind so you know what is actually changing.
2. Review the diff before staging. Keep the change set small and intentional, and separate unrelated edits when that makes the result easier to review.
3. Stage only what belongs in the next commit. If the work is still in progress, leave it out rather than hiding it inside a broad commit.
4. Write the commit message from intent, not file names. Explain why the change exists and what future reader it helps.
5. Preserve hooks, approvals, and repository policy. If a hook or approval step blocks the action, treat that as part of the workflow and fix the underlying issue instead of bypassing it.
6. Be careful with history rewriting. Rebase, squash, and similar operations should only be used when they match the user’s intent and the branch situation is understood.
7. When investigating history, use the git tools that answer the question directly, such as status, diff, log, show, blame, and bisect.

## Safety and review

- Confirm the target branch and remote state before pushing or rewriting shared history.
- Call out any uncommitted work, merge conflicts, or risky history changes before taking the next step.
- Keep the explanation short and concrete so the user can decide whether the branch is ready.
- Follow runtime approvals, configured tools, hooks, and explicit user intent as the authority for what is allowed.

## Good outputs

Return a concise summary that includes:

- the current branch state
- what changed
- whether anything is staged or still dirty
- the safest next action
