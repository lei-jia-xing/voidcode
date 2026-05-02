# Builtin skill Markdown resources

These files are local vendored runtime resources. VoidCode loads builtin skill bodies from this package directory via `importlib.resources`; runtime skill loading must not fetch network URLs.

The files in this directory intentionally mix upstream-vendored guidance with VoidCode-local adaptations. They must remain truthful to VoidCode's current runtime capabilities.

- `git-master.md` is a VoidCode-local git workflow guide adapted from the upstream builtin skill metadata. It stays local package content and is written to match VoidCode's approval and hook boundaries.
- `frontend-design.md` was adapted from Anthropic's public `skills/frontend-design/SKILL.md` on 2026-05-02. It is local package content, not a runtime URL dependency or a claim of exact Anthropic parity.
- `playwright.md` is a VoidCode-local concise browser verification guide inspired by common Playwright workflows. It intentionally avoids Claude plugin-specific assumptions and remains descriptor/config-gated in VoidCode.
- `review-work.md` is a VoidCode-local adaptation. It is not an exact upstream copy because the upstream OpenAgent resource referenced unsupported review roles and orchestration behavior.

Do not replace these files with placeholders or runtime URL pointers. Refresh upstream-derived files by fetching an explicit source revision, extracting the Markdown/template content locally, and updating this provenance note.
