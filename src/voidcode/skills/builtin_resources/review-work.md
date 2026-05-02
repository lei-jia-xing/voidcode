# Review Work - VoidCode-Compatible Read-Only Review Guidance

Use this skill when asked to review, verify, QA, or validate implementation work. It is a local VoidCode adaptation, not an OhMyOpenAgent orchestration copy. It must only describe capabilities VoidCode actually supports: top-level `leader` and `product` execution, delegated child presets `advisor`, `explore`, `product`, `researcher`, and `worker`, runtime tools, local files, git history, and configured MCP descriptors such as `context7`, `websearch`, `grep_app`, and `playwright`.

## Review Contract

- Stay read-only unless the user explicitly asks for fixes.
- Verify the requested scope against the original goal, constraints, and changed files.
- Prefer concrete evidence: file paths, line references, command output, test failures, or exact missing behavior.
- Separate blocking issues from minor suggestions.
- Do not invent unsupported agents, tool buses, or platform services.

## Suggested Review Flow

1. **Collect scope**: identify the original goal, relevant constraints, changed files, and available verification commands. Use local repo evidence before asking follow-up questions.
2. **Check requirement fit**: compare the implementation with each explicit requirement and likely implied requirement. Flag scope creep and missing behavior.
3. **Inspect code quality**: review correctness, consistency with nearby patterns, error handling, type safety, maintainability, performance, and tests.
4. **Assess security and safety**: look for unsafe file/network/process use, secret handling, injection risks, path traversal, overly broad permissions, and sensitive output leakage.
5. **Verify behavior**: run focused tests or commands when available. For frontend/browser work, use configured browser or Playwright capability only when present.
6. **Mine context when needed**: use git history, docs, tests, and code search to confirm whether prior decisions or related contracts affect the review.

## Optional Delegation Guidance

When the runtime exposes background task delegation, use supported child presets only:

- `advisor` for read-only goal, code-quality, or security review.
- `explore` for repository search and context gathering.
- `researcher` for public documentation or standards research.
- `worker` for hands-on verification tasks that may run commands under runtime permissions.
- `product` only when product-context evaluation is explicitly relevant.

Delegation is optional. If delegation is unavailable or unnecessary, perform the review directly with the same evidence standards.

## Output Format

Report findings in priority order:

```text
<verdict>PASS or FAIL</verdict>
<summary>One to three sentences explaining the overall result.</summary>
<findings>
- [SEVERITY] Category: concise issue title
  File: path:line or path
  Evidence: what proves the issue
  Impact: why it matters
  Recommendation: minimal corrective action
</findings>
<verification>Commands or checks run, with pass/fail result.</verification>
<limitations>Any relevant review limits or unavailable configured capabilities.</limitations>
```

Severity guide:

- **CRITICAL**: likely data loss, security break, crash, or unusable core flow.
- **MAJOR**: incorrect behavior or missing requirement that should block merge.
- **MINOR**: useful fix that should not block if the risk is low.
- **NITPICK**: style or clarity improvement.

If no blocking findings exist, say so directly and still list the verification evidence.
