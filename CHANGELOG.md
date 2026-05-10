# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- Web frontend delegated child sessions now expose an in-context return action,
  keep replay run state aligned with running child sessions, and allow returning
  to the parent session while the delegated child is still active.
- Web runtime status surfaces no longer crash when partial status payloads omit
  nested `git`, `lsp`, or `mcp` details.

## [0.1.0] - 2026-05-06

First productionized release of the VoidCode runtime control plane.

VoidCode is a local-first coding agent runtime inspired by OpenCode and Claude Code.
This release ships a stable CLI → runtime → single-agent execution loop, persistent
sessions, the built-in tool surface, and the runtime-owned governance primitives
needed to run a real end-to-end coding session.

### Added

- Runtime control plane (`voidcode.runtime`) owning sessions, permissions, hooks,
  capability lifecycle, streaming, and provider fallback.
- CLI entry point `voidcode` with `run`, `sessions list/resume/answer/export/import`,
  `tasks`, `commands`, and `mcp` subcommands.
- Provider-backed execution path (default product mode) plus an explicit
  deterministic engine for offline/test/no-key harness flows
  (`VOIDCODE_EXECUTION_ENGINE=deterministic`).
- Local session persistence at `$XDG_STATE_HOME/voidcode/sessions.sqlite3` with
  schema-versioned `PRAGMA user_version` and fail-fast mismatch handling.
- Portable session bundle import/export (`voidcode.session.bundle.v1`) with
  redaction defaults.
- Built-in tool surface: `read_file`, `write_file`, `edit`, `multi_edit`,
  `apply_patch`, `glob`, `grep`, `ast_grep_search`, `ast_grep_preview`,
  `ast_grep_replace`, `shell_exec`, `format_file`, `web_search`, `web_fetch`,
  `task`, `todo_write`, `skill`, `lsp`, `mcp/*`, `question`, plus
  `background_output` / `background_cancel` / `background_retry`.
- Approval modes (`ask` / `trusted` / `yolo`) with inline TTY approval and
  resumable pending-approval state.
- Hook system with builtin preset catalog (`role_reminder`, `delegation_guard`,
  `background_output_quality_guidance`, `delegated_retry_guidance`,
  `todo_continuation_guidance`).
- Agent presets: `leader` (default top-level), `product` (top-level planning),
  and delegated child presets `worker`, `advisor`, `explore`, `researcher`.
- Slash commands shipped as a markdown-authored surface, discovered from
  `commands/**/*.md` and `.voidcode/commands/**/*.md`.
- Skills discovery from `.voidcode/skills/<name>/SKILL.md` with bundled built-ins.
- Runtime-owned background task delegation with parent/child session lineage,
  retry/cancel semantics, and bounded result retrieval.
- Runtime-owned continuation loops (`/continuation-loop`, `/intensive-loop`,
  `/cancel-continuation`).
- Local HTTP/SSE transport (`voidcode serve`) and runtime-backed web launcher
  (`voidcode web`).
- Minimal Bun/Vite/React frontend consuming the runtime transport for session
  list, replay, streaming run, approval/question handling, review tree/diff,
  workspace registry, and runtime status.
- Textual-based TUI client.
- Provider fallback chain with retryable failure detection and graph rebuild.
- Structured hook diagnostics and long-running task stability hardening.
- Context continuity safeguards and intensive loop verification state.
- PyPI release pipeline via GitHub Actions trusted publishing.

### Documentation

- English contributor docs at the repository root (`README.md`,
  `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`).
- Architecture, roadmap, and contract docs under `docs/` (currently mostly in
  Chinese; English contributor surface lives at the root).

### Known limitations

- `voidcode.json` and other user-editable config schemas are versionless in this
  release; breaking changes before v1.0 may require manual config or session
  reset rather than automatic migration.
- The TUI and web frontend are functional but not yet at full CLI parity.
- True multi-agent execution semantics are intentionally post-MVP. The current
  agent layer is a declaration/configuration surface, not an arbitrary
  multi-agent topology.
