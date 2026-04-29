# Learnings - Opencode-Style Tool UI

## T1: Branch creation and contract tests

### Files modified
- `tests/unit/tools/test_shell_exec_tool.py` — Added 3 RED contract tests for `ShellExecArgs.description`
- `tests/unit/runtime/test_runtime_events.py` — Added 6 schema contract tests for `ToolDisplay`/`ToolStatusPayload` shapes
- `frontend/src/lib/runtime/status-contract.test.ts` — Added 4 RED contract tests for `tool_status.display` parsing
- `frontend/src/components/ChatThread.test.tsx` — Moved `baseProps` to module scope; added 3 RED contract tests for raw JSON suppression and collapsible tool behavior

### Test commands run
- Backend: `uv run python -X utf8 -m pytest tests/unit/tools/test_shell_exec_tool.py tests/unit/runtime/test_runtime_events.py -v`
  - 21 passed (all existing + new schema tests), 3 failed (only `description` RED tests)
- Frontend: `bun run --cwd frontend test:run -- src/lib/runtime/status-contract.test.ts src/components/ChatThread.test.tsx`
  - 26 passed, 4 failed (all RED on missing implementation)

### Patterns used
- Backend tests follow existing pytest patterns (`test_` prefix, type annotations, `tmp_path` fixtures)
- Frontend tests use `describe`/`it` from Vitest, `render`/`screen` from Testing Library
- RED tests annotated with `// RED:` comments to distinguish intentional failures
- Schema contract tests don't need runtime — they validate payload shapes in isolation

### Key decisions
- Used `AttributeError` on Pydantic model as the RED failure mechanism for `ShellExecArgs.description` (extra fields silently dropped)
- ChatThread `baseProps` moved to module scope so both `describe` blocks can access it
- Collapsible tool test uses conditional assertion (passes either way) to avoid crashing before T5 implementation
- Pre-existing `@chenglou/pretext` dependency gap required `bun install` to load the test suite

## T1 corrective round (Atlas verification feedback)

### Additional file modified
- `tests/unit/runtime/test_tool_execution_timeout.py` — Added 2 RED runtime emission tests using `_make_runtime` + `run_stream()` harness

### Corrective test commands
- Backend (corrective): `uv run python -X utf8 -m pytest tests/unit/tools/test_shell_exec_tool.py tests/unit/runtime/test_runtime_events.py tests/unit/runtime/test_tool_execution_timeout.py -v`
  - 33 passed, 6 failed (3 `description` + 2 runtime emission RED + 1 pre-existing unrelated)
- Frontend (corrective): `bun run --cwd frontend test:run -- src/lib/runtime/status-contract.test.ts src/components/ChatThread.test.tsx`
  - 26 passed, 4 failed (1 `display.summary` parsing + 3 ChatThread RED)

### Key corrections
- Added runtime emission RED tests using existing `_make_runtime(_InstantTool())` harness — these exercise the real `runtime.tool_started`/`runtime.tool_completed` event payload and assert `display`/`tool_status` keys are present, which they are not (RED).
- Removed conditional pass path from `supports collapsible tool card` test — now strictly requires a disclosure button via `screen.getByRole("button", { name: /expand|collapse|toggle/i })` with no fallback.
- Pre-existing failure in `test_shell_exec_timeout_wins_when_shorter_than_runtime_timeout` noted (unrelated to T1, asserts `"tool-native timeout"` but receives `"shell_exec command timed out after 1s"`).

## T1 corrective round 2 (Atlas diagnostics feedback)

### Diagnostics fixes applied
- **`tests/unit/tools/test_shell_exec_tool.py`:** `args.description` → `getattr(args, "description")` (3 sites). Removed unused `import pytest`. Pydantic `__getattr__` raises `AttributeError` for unknown fields, preserving the RED behavior.
- **`tests/unit/runtime/test_runtime_events.py`:** Heterogeneous dict literal annotated as `dict[str, object]`. All nested access chains (`payload["tool_status"]["display"]["kind"]`) replaced with `isinstance`-guarded intermediate locals. Zero basedpyright errors on all T1-modified files.
- No type suppressions (`type: ignore`, `pyright: ignore`, `cast`, `as any`) were used — all fixes use structural narrowing.

### Post-fix test results (unchanged from pre-fix)
- Backend: 33 passed, 5 T1 RED, 1 pre-existing fail (same as before)
- Frontend: 26 passed, 4 RED (same as before)
- All 4 T1-modified files: zero LSP diagnostics

## T2: Additive backend display metadata contract

### Files modified
- `src/voidcode/tools/_pydantic_args.py` — Added `description: str | None = None` to `ShellExecArgs` with `field_validator` that rejects empty/whitespace-only descriptions.
- `src/voidcode/tools/shell_exec.py` — Added `"description": {"type": "string", "description": "Human-readable description of the command"}` to `input_schema`.
- `src/voidcode/runtime/tool_display.py` — New runtime-owned module (~340 lines) with `build_tool_display()` and `build_tool_status()`. Maps all 24+ builtin tools to curated `display` metadata (kind/title/summary/args/copyable/hidden) with safe fallback for unknown/MCP tools.
- `src/voidcode/runtime/run_loop.py` — Added import of `build_tool_display`/`build_tool_status`. Enriched `runtime.tool_started` payload with `tool_call_id`, `display`, `tool_status`. Enriched `runtime.tool_completed` payload with `tool_status`.
- `tests/unit/tools/test_shell_exec_tool.py` — Updated `test_shell_exec_args_description_non_empty_when_provided` to catch `ValidationError` with `pytest.raises` (the contract asserts empty description must be rejected).

### Test results
All 42 test pass:
- `tests/unit/tools/test_shell_exec_tool.py` — 12/12 (3 previously RED)
- `tests/unit/runtime/test_runtime_events.py` — 12/12 (unchanged, schema contracts)
- `tests/unit/runtime/test_tool_execution_timeout.py` — 18/18 (2 previously RED + 16 pre-existing)

### Patterns used
- `tool_display.py` is runtime-owned per `runtime/AGENTS.md` (runtime owns event enrichment, not tools or graph).
- `build_tool_display()` accepts sanitized arguments + optional result_data; result_data is only available at completion.
- `build_tool_status()` wraps display into a ToolStatusPayload with phase/status/label.
- Shell summary: prefers `arguments.description`, falls back to synthesized command (truncated 120 chars).
- Unknown/MCP fallback: kind="generic", title=tool_name, summary=first non-empty descriptive arg (description/query/url/filePath/path/pattern/name), never raw JSON.
- args extraction: max 3 primitive values, preferred keys first, skips sensitive fields (content, oldString, newString, edits, todos, data_uri, patch).
- copyable: shell_exec gets command+output; read_file/write_file/edit tools get path.

### Key decisions
- `tool_display.py` placed in `src/voidcode/runtime/` (not `tools/`) to honor runtime governance boundary.
- Whitespace-only description is rejected with `ValidationError` (consistent with command validation).
- `tool_call_id` is always included in `tool_started` payload (always present via `uuid4()` fallback).
- Deterministic runs also get display metadata (no provider dependency).
- Label in tool_status mirrors display.summary for legacy parser compatibility.
- Existing flat payload keys (`tool`, `status`, `arguments`, `content`, `error`, etc.) are fully preserved — only additive.
- Zero basedpyright diagnostics on all changed files.
- Zero type suppressions (`type: ignore`, `pyright: ignore`) used.

### T2 correction round (Atlas verification feedback)

- **Issue:** `runtime.tool_completed.payload` was missing top-level `display`. The plan's contract requires `display` additively on both `runtime.tool_started.payload` and `runtime.tool_completed.payload`, mirrored inside `tool_status.display`.
- **Fix:** Added `completed_payload["display"] = completed_display` before `completed_payload["tool_status"] = completed_status` in `run_loop.py`. Strengthened `test_tool_completed_event_includes_tool_status_metadata` to assert `"display" in payload`, verify `kind`/`title`/`summary` values, and verify nested `tool_status.display` consistency.
- **Result:** 42/42 pass with zero diagnostics. Contract now matches plan spec exactly.

---

## T3: Monochrome token/control primitive foundation

### Files modified
- `frontend/src/index.css` — Added opencode-style monochrome CSS tokens (`#0a0a0a`, `#141414/#1c1c1c/#232323`, `#eeeeee/#a0a0a0/#808080`, `rgba(255,255,255,.08/.16)`), 6px control radius, 150ms transitions, explicit high-contrast `:focus-visible`, and reusable `.vc-control` variant classes.
- `frontend/src/components/ui/ControlButton.tsx` / `controlButtonClassName.ts` / `index.ts` — Added typed reusable control primitive and class composer for primary, secondary, ghost, danger, confirm, icon, and compact controls.
- `frontend/src/components/ui/ControlButton.test.tsx` — Added focused Vitest coverage for shared control classes and variants.
- `frontend/src/components/ChatThread.test.tsx` — Replaced a pre-existing `any[]` in `baseProps.messages` with `ChatMessage[]` so full frontend lint can pass.

### Verification
- `lsp_diagnostics` on all T3-modified frontend files: zero diagnostics.
- `bun run --cwd frontend test:run -- src/components/ui/ControlButton.test.tsx`: 7 passed.
- `bun run --cwd frontend lint`: passed after fixing the fast-refresh helper export and the pre-existing test `any`.
- `bun run --cwd frontend typecheck`: passed.
- `bun run --cwd frontend build`: passed with the existing Vite chunk-size warning.

### Evidence
- `.sisyphus/evidence/task-3-color-audit.txt` — Forbidden color audit for `indigo|violet|purple|sky|blue`; new T3 primitive files contain no matches.
- `.sisyphus/evidence/task-3-focus-buttons.png` — Playwright screenshot of focused monochrome control primitives.

---

## T4: Frontend parser display metadata

### Files modified
- `frontend/src/lib/runtime/types.ts` — Added `ToolDisplay` and `ToolStatusPayload` frontend runtime types matching the backend additive schema.
- `frontend/src/lib/runtime/event-parser.ts` — Extended `ChatMessage.tools[]` with `summary`, `display`, `copyable`, and `legacy` metadata. `tool_status.label` remains label-preferred, while `tool_status.display.summary` fills label when explicit label is absent and always populates summary/display.
- `frontend/src/lib/runtime/status-contract.test.ts` — Strengthened parser contracts to assert display/copyable preservation, explicit-label precedence, completed status retention, structured args/results, and curated legacy fallback metadata.

### Patterns used
- New backend events are parsed through `tool_status.display` first, with top-level `payload.display` as the mirror fallback.
- Legacy `graph.tool_request_created` and `runtime.tool_completed` still upsert tool rows and now attach curated non-JSON labels such as `Read: README.md`.
- Raw structured `arguments` and completed `result` payloads stay on the tool object for later T5 rendering, but labels/summaries never stringify raw JSON.

### Verification
- `lsp_diagnostics` on `event-parser.ts`, `types.ts`, and `status-contract.test.ts`: zero diagnostics.
- `bun run --cwd frontend test:run -- src/lib/runtime/status-contract.test.ts`: 10 passed.
- `bun run --cwd frontend typecheck`: passed.

---

## T5: Opencode-style curated tool cards

### Files modified
- `frontend/src/components/ChatThread.tsx` — Replaced raw tool rendering with compact monochrome disclosure cards. Shell tools are collapsed by default when complete, expand to command/output/stderr/error/exit details, and include `navigator.clipboard.writeText` copy buttons with copied state. Assistant prose now renders as plain chat text outside bordered/card wrappers.
- `frontend/src/components/ChatThread.test.tsx` — Added/updated coverage for shell collapse/expand, copy behavior, failure details, legacy fallback, context grouping, hidden todo metadata, generic no-raw-JSON rendering, and unboxed assistant prose.
- `frontend/src/i18n/locales/en.json` / `zh-CN.json` — Added disclosure, copy, shell, status, and context labels in both locales.
- `.sisyphus/evidence/task-5-no-raw-json.txt` and `.sisyphus/evidence/task-5-shell-tool.png` — Added verification/evidence artifacts.

### Patterns used
- New tool-card UI uses the T3 monochrome variables (`--vc-bg`, `--vc-surface-*`, `--vc-text-*`, `--vc-border-*`, `--vc-radius-control`) and `ControlButton` for compact copy actions.
- Generic/unknown tools intentionally never render result details; they show only title/subtitle plus max three primitive args from display metadata or safe primitive arguments.
- Context tools (`read_file`, `list`, `glob`, `grep`, `code_search`, `ast_grep_search`) collapse into one context disclosure when multiple appear in the same assistant turn.
- `todo_write` returns `null` when `tool.display.hidden` is true; legacy/no-display todos stay compact and collapsed by default.

### Verification
- `lsp_diagnostics` on `ChatThread.tsx`, `ChatThread.test.tsx`, `en.json`, and `zh-CN.json`: zero diagnostics.
- `bun run --cwd frontend test:run -- src/components/ChatThread.test.tsx`: 25 passed.
- `bun run --cwd frontend typecheck`: passed.
- `bun run --cwd frontend lint`: passed.

### T5 visual correction learning
- The accepted opencode-like target is not a generic card component. Collapsed tool activity should be a plain activity row with minimal spacing, no row border/background, and only a subtle chevron for expandable rows.
- Shell details should be modeled as a single transcript (`$ command` + output/stderr/error/exit) so the execution reads as one terminal event, not as separate labelled cards.

---

## T7: Persisted resizable session sidebar

### Files modified
- `frontend/src/components/SessionSidebar.tsx` — Added expanded sidebar width clamping (default 344px, min 244px, dynamic max), desktop-only resize separator, pointer drag cleanup, and ArrowLeft/ArrowRight/Home/End keyboard resizing.
- `frontend/src/store/index.ts` / `frontend/src/App.tsx` — Persisted `sessionSidebarWidth` through the existing Zustand `app-storage` partialize path and passed it into the sidebar explicitly.
- `frontend/src/components/SessionSidebar.test.tsx`, `frontend/src/App.test.tsx`, `frontend/src/store.integration.test.ts` — Added focused coverage for default CSS variable width, invalid-width clamping, pointer cleanup, keyboard resizing, collapsed rail behavior, and store persistence.
- `frontend/src/i18n/locales/en.json` / `zh-CN.json` — Added localized resize handle labels.

### Patterns used
- Sidebar mobile behavior remains base `w-16`; desktop expanded width uses `md:w-[var(--session-sidebar-width)]` so compact/mobile fallback stays rail-like.
- Resize handle uses the semantic separator pattern with `aria-valuemin`, `aria-valuemax`, `aria-valuenow`, `aria-orientation`, and a localized accessible name.
- New visual handle styling uses T3 `--vc-*` tokens (`--vc-space-2`, `--vc-border-*`, `--vc-focus-ring`) instead of new ad-hoc colors.

### Verification
- `lsp_diagnostics` on all T7-modified frontend files: zero diagnostics.
- `bun run --cwd frontend test:run -- src/components/SessionSidebar.test.tsx src/App.test.tsx src/store.integration.test.ts`: 59 passed.
- `bun run --cwd frontend typecheck`: passed.
- `bun run --cwd frontend lint`: passed.
- Playwright smoke captured `.sisyphus/evidence/task-7-sidebar-resize.png` and `.sisyphus/evidence/task-7-sidebar-keyboard.txt`; keyboard smoke verified Home 244px, ArrowRight 260px, End 448px at 1280px viewport, and reload persistence.

---

## T6: Approval/question/audit/review/runtime action controls

### Files modified
- `frontend/src/components/ChatThread.tsx` — Approval/question cards now use monochrome token surfaces and `ControlButton` actions; approval allow/deny decisions and question answer payload mapping are unchanged. Thinking toggle now exposes `aria-expanded`/`aria-controls`.
- `frontend/src/components/ReviewPanel.tsx` — Close, mode, and refresh controls use `ControlButton`; mode buttons expose `aria-pressed`; selected review rows and resize handle moved away from indigo styling.
- `frontend/src/components/RuntimeOpsPanel.tsx` — Close, refresh, acknowledge, and cancel actions use shared controls with neutral/default styling and restrained danger for cancellation.
- `frontend/src/components/SettingsPanel.tsx` / `SettingsPanel.test.tsx` — Close/language/test/save controls use `ControlButton`; provider selection exposes `aria-pressed`; provider configured state moved from `title` tooltip to screen-reader text plus token dots.
- `frontend/src/components/OpenProjectModal.tsx` — Modal close uses `ControlButton`; search focus and current workspace styling moved to neutral/confirm tokens; hook helper moved outside component to satisfy diagnostics.
- `frontend/src/components/StatusBar.tsx` — Status toggle and MCP retry use `ControlButton`; toggle now has `aria-controls`; status dots use T3 confirm/danger/neutral tokens.
- `frontend/src/App.tsx` — Header review/runtime/test buttons and empty-project open action use `ControlButton`; icon-only buttons have `aria-label`; panel toggles expose `aria-expanded`.

### Verification
- LSP diagnostics: zero diagnostics on all modified TSX files plus `SettingsPanel.test.tsx`.
- Focused tests: `bun run --cwd frontend test:run -- src/App.test.tsx src/components/ChatThread.test.tsx src/components/ReviewPanel.test.tsx src/components/SettingsPanel.test.tsx src/components/OpenProjectModal.test.tsx` — 5 files / 67 tests passed.
- `bun run --cwd frontend typecheck`: passed.
- `bun run --cwd frontend lint`: passed.

### Evidence
- `.sisyphus/evidence/task-6-approval-keyboard.txt` — Automated approval/question evidence and screenshot feasibility note.
- `.sisyphus/evidence/task-6-review-runtime.txt` — Automated review/runtime/settings/project/header/status evidence and screenshot feasibility note.

---

## T7 correction: Neutral session sidebar styling

### Files modified
- `frontend/src/components/SessionSidebar.tsx` — Removed blue/purple/indigo/emerald/rose/amber Tailwind classes from brand, open project actions, new-session active state, active session row, status dots/badges, collapsed rail open action, and sidebar error text. Replaced them with T3 `--vc-*` neutral surfaces/text/borders, preserving all resize logic.
- `frontend/src/App.tsx` — Replaced root indigo text selection and adjacent shell warning/error banners with tokenized neutral/danger styling; neutralized the header agent-idle/running pill.
- `.sisyphus/evidence/task-7-sidebar-resize.png` / `task-7-sidebar-keyboard.txt` — Updated evidence after the neutral visual correction.

### Verification
- Forbidden color audit: no `indigo|blue|purple|violet|emerald|rose|amber|sky` matches remain in `SessionSidebar.tsx` or `App.tsx`.
- LSP diagnostics on `SessionSidebar.tsx` and `App.tsx`: zero diagnostics.
- `bun run --cwd frontend test:run -- src/components/SessionSidebar.test.tsx src/App.test.tsx src/store.integration.test.ts`: 59 passed.
- `bun run --cwd frontend lint`: passed.
- Playwright smoke re-verified keyboard resize + persistence (Home 244px, ArrowRight 260px, End/reload 448px) and captured the neutral sidebar screenshot.

---

## T8: Frontend dependency upgrade and Tailwind/ESLint config migration

**Date:** 2026-04-29

### Files modified
- `frontend/package.json` — Updated all direct dependency ranges to plan targets (React 19, Vite 8, Tailwind 4, ESLint 9, Zustand 5, lucide 1, react-markdown 10, react-router-dom 7, tailwind-merge 3, etc.). Added `@eslint/js`, `@tailwindcss/postcss`, and `typescript-eslint`. Removed `@typescript-eslint/eslint-plugin` and `@typescript-eslint/parser`.
- `frontend/eslint.config.js` — Migrated to ESLint 9 flat config using `tseslint.config()` wrapper, `js.configs.recommended`, and `...tseslint.configs.recommended`. Added `coverage/**` to ignores (previously caught by default ESLint 8 behavior, now requires explicit ignore in flat config).
- `frontend/postcss.config.js` — Changed `tailwindcss` plugin to `@tailwindcss/postcss`.
- `frontend/src/index.css` — Replaced `@tailwind base/components/utilities` with `@import "tailwindcss"`. Moved `fontFamily` config to `@theme` block. Removed `@layer` wrappers (CSS cascade handles order in v4). All `--vc-*` monochrome tokens, `--background`/`--foreground` HSL vars, `.vc-control-*` variants, and `.markdown-body` styles preserved exactly.
- `frontend/tailwind.config.js` — **Removed.** Tailwind v4 is CSS-first; the `@theme` block in `index.css` handles font configuration. Old `colors.void` grayscale palette and `colors.accent` (blue, forbidden color) are not needed — components use `--vc-*` CSS token references.
- `frontend/src/components/OpenProjectModal.tsx` — Added `// eslint-disable-next-line react-hooks/set-state-in-effect` for the `setDidInitiateSwitch(false)` call (intentional trigger-clearing pattern flagged by new React Hooks v7 rule).
- `frontend/src/components/SettingsPanel.tsx` — Added `// eslint-disable-next-line react-hooks/set-state-in-effect` for `setProvider`/`setModel` calls (intentional form initialisation from external settings).
- `frontend/src/store/index.ts` — Changed `catch (err)` to `catch` (unused parameter flagged by new `@typescript-eslint/no-unused-vars`).

### Dependency versions resolved
All plan target ranges resolved successfully — no deviations needed:
| Package | Target | Resolved |
|---------|--------|----------|
| react | ^19.2.1 | 19.2.5 |
| react-dom | ^19.2.1 | 19.2.5 |
| vite | ^8.0.10 | 8.0.10 |
| tailwindcss | ^4.1.4 | 4.2.4 |
| @tailwindcss/postcss | ^4.1.4 | 4.2.4 |
| eslint | ^9.17.0 | 9.39.4 |
| @eslint/js | ^9.17.0 | 9.39.4 |
| typescript-eslint | ^8.58.2 | 8.59.1 |
| eslint-plugin-react-hooks | ^7.1.1 | 7.1.1 |
| zustand | ^5.0.12 | 5.0.12 |
| lucide-react | ^1.14.0 | 1.14.0 |
| react-markdown | ^10.1.0 | 10.1.0 |
| react-router-dom | ^7.14.1 | 7.14.2 |
| tailwind-merge | ^3.5.0 | 3.5.0 |
| @vitejs/plugin-react-swc | ^4.3.0 | 4.3.0 |
| vitest | ^4.1.5 | 4.1.5 |
| @vitest/coverage-v8 | ^4.1.5 | 4.1.5 |
| typescript | ^5.8.3 | 5.8.3 |
| @types/react | ^19.2.0 | 19.2.14 |
| @types/react-dom | ^19.2.0 | 19.2.3 |
| jsdom | ^29.0.2 | 29.1.0 |
| prettier | ^3.8.3 | 3.8.3 |

### Key decisions
- **CSS `@theme` block replaced with JS config.** The initial T8 pass used `@theme { --font-mono: ...; --font-sans: ...; }` in `index.css`, but the CSS language server (biome) flags `@theme` as a non-standard at-rule (`Tailwind-specific syntax is disabled`). **Correction (2026-04-29):** Removed the `@theme` block from CSS and created a minimal `frontend/tailwind.config.js` with only `theme.extend.fontFamily`. Tailwind v4's PostCSS plugin (`@tailwindcss/postcss`) auto-detects a JS config, so fonts resolve correctly without any CSS at-rule diagnostics. Component styling continues to use explicit `--vc-*` token references rather than Tailwind theme colors, which aligns with the monochrome design direction.
- **`@layer` wrappers removed.** Tailwind v4 does not use explicit `@layer base/components/utilities` — the cascade order is implicit. The CSS variables, body styles, `.vc-control-*` variants, and `.markdown-body` styles are now defined at root level after `@import "tailwindcss"`.
- **ESLint `react-hooks/set-state-in-effect` handled with inline suppression.** The new React Hooks v7 plugin flags `setState` in `useEffect`, but both affected sites use intentional patterns (clearing an activation trigger, loading external settings into local form state). Inline `eslint-disable-next-line` with descriptive comments is the minimal migration-safe fix.
- **`coverage/**` added to ESLint ignores.** ESLint 8 implicitly skipped non-source directories; ESLint 9 flat config requires explicit globs.

### Verification (post-correction)
- `lsp_diagnostics` on `frontend/src/index.css`: zero diagnostics.
- `lsp_diagnostics` on `frontend/tailwind.config.js`: zero diagnostics.
- `bun run --cwd frontend lint`: passed (zero errors, zero warnings).
- `bun run --cwd frontend typecheck`: passed (zero errors).
- `bun run --cwd frontend test:run`: 11 files, 139 tests passed.
- `bun run --cwd frontend build`: passed (Vite 8 build succeeds with pre-existing chunk-size warning).
- All `--vc-*` monochrome tokens and control styles intact.


---

## T9: Final monochrome theme sweep

**Date:** 2026-04-29

### Files modified
- `frontend/src/index.css` — Added tokenized overlay/scrollbar styling while preserving Tailwind v4 import and existing `--vc-*` monochrome/control/markdown tokens.
- `frontend/src/App.tsx`, `Composer.tsx`, `SessionSidebar.tsx`, `ChatThread.tsx`, `ReviewPanel.tsx`, `RuntimeOpsPanel.tsx`, `SettingsPanel.tsx`, `OpenProjectModal.tsx`, `StatusBar.tsx`, `RuntimeDebug.tsx` — Removed legacy blue/purple/indigo/sky/emerald/rose/amber Tailwind color classes and hardcoded shell surface colors in favor of `--vc-*` monochrome tokens plus restrained semantic danger/confirm tokens.

### Color audit result
- `indigo|violet|purple|sky|blue|emerald|rose|amber` across `frontend/src` returned no matches after the sweep.
- `text-slate|bg-slate|border-slate|bg-[#|border-[#|bg-black` across `frontend/src` TSX sources returned no matches after the sweep.
- Assistant/agent prose remains unboxed; collapsed tool rows remain lightweight text rows, with subtle surfaces reserved for expanded terminal/detail blocks.

### Verification
- LSP diagnostics on all T9-modified files: zero diagnostics.
- `bun run --cwd frontend test:run -- src/App.test.tsx src/components/ChatThread.test.tsx src/components/Composer.test.tsx src/components/SessionSidebar.test.tsx src/components/ReviewPanel.test.tsx src/components/SettingsPanel.test.tsx src/components/OpenProjectModal.test.tsx` — 7 files / 87 tests passed.
- `bun run --cwd frontend lint` — passed.
- `bun run --cwd frontend typecheck` — passed.
- `bun run --cwd frontend build` — passed with the pre-existing Vite SWC deprecation and chunk-size warnings.

---

## T10: Backend/frontend tool metadata flow integration coverage

**Date:** 2026-04-29T19:22:12+08:00

### Files modified
- `tests/integration/test_http_transport.py` — Added SSE transport integration coverage proving `runtime.tool_completed` preserves top-level `display`, nested `tool_status.display`, and `display.copyable` for failed shell metadata.
- `frontend/src/lib/runtime/client.integration.test.ts` — Strengthened stream parsing coverage for context display metadata and added failed shell metadata/detail preservation.
- `frontend/src/lib/runtime/status-contract.test.ts` — Added parser coverage for interleaved same-name tool calls with distinct `invocation_id`s so completions update the correct row.
- `frontend/src/store.integration.test.ts` — Added store streaming/replay coverage that preserves flat and nested display metadata through current session events.
- `frontend/src/components/ChatThread.test.tsx` — Added UI integration coverage rendering derived backend metadata for failed shell details, grouped context tools, generic unknown tools, legacy fallback, no raw JSON leakage, and unboxed assistant prose.

### Verification
- LSP diagnostics on all T10-modified files: zero diagnostics.
- `uv run python -X utf8 -m pytest tests/integration/test_http_transport.py -q` — 65 passed.
- `bun run --cwd frontend test:run -- src/lib/runtime/client.integration.test.ts src/lib/runtime/status-contract.test.ts src/store.integration.test.ts src/components/ChatThread.test.tsx` — 4 files / 74 tests passed.
- `bun run --cwd frontend lint` — passed.
- `bun run --cwd frontend typecheck` — passed.

### Patterns confirmed
- New-event path keeps `tool_status.display` plus flat `display` intact through HTTP/SSE client/store/parser/UI boundaries.
- Legacy events without `tool_status` remain curated via existing fallback labels and safe primitive args; tests assert internal/raw JSON fields do not render.
- Same-name tool calls rely on `invocation_id` correlation, so out-of-order completions do not overwrite the wrong tool row.

---

## T11: Browser QA and launcher e2e coverage

**Date:** 2026-04-29T19:36:00+08:00

### Files modified
- `frontend/e2e/launcher.spec.ts` — Added deterministic mocked launcher coverage for compact shell collapse/expand/copy, context grouping, approval controls, sidebar keyboard resize persistence, review controls, settings/project modals, keyboard focus, and project-first empty state. Updated the live smoke selector from the removed `.prose` class to `.markdown-body`.
- `.sisyphus/evidence/task-11-tool-browser.png` / `task-11-layout-buttons.png` / `task-11-browser-qa.txt` — Added browser QA evidence.

### Patterns confirmed
- Full browser coverage can mock `/api/*` at the Playwright page layer while still exercising the real `voidcode web --no-open` launcher shell and compiled frontend bundle.
- Sidebar persistence must not clear `localStorage` on reload; the e2e helper clears storage once per browser context using `sessionStorage` as a guard, then verifies `--session-sidebar-width: 448px` survives reload.
- Assistant prose should be asserted via `.markdown-body` ancestry, not a broad ancestor `border` search, because app-level layout containers may legitimately contain border classes.
- Screenshot evidence is generated directly from e2e using repository-root `.sisyphus/evidence` paths, avoiding generated frontend artifacts.

### Verification
- `lsp_diagnostics` on `frontend/e2e/launcher.spec.ts`: zero diagnostics.
- `bun run --cwd frontend test:e2e`: 5 passed.
- `bun run --cwd frontend test:run -- src/components/ChatThread.test.tsx src/components/SessionSidebar.test.tsx src/App.test.tsx src/components/ReviewPanel.test.tsx src/components/SettingsPanel.test.tsx src/components/OpenProjectModal.test.tsx`: 6 files / 73 tests passed.
- `bun run --cwd frontend lint`: passed.
- `bun run --cwd frontend typecheck`: passed.
- Forbidden color audit on changed e2e file for `indigo|violet|purple|sky|blue|emerald|rose|amber`: no matches.
- Playwright MCP hands-on smoke opened the live launcher, inspected sidebar/header/review/settings surfaces, and found zero browser console warnings/errors.

---

## T12: Final verification and PR preparation

**Date:** 2026-04-29T20:02:00+08:00

### Verification
- `mise run check` passed after final regression-test updates; output saved to `.sisyphus/evidence/task-12-mise-check.txt`.
- Final check covered Ruff, basedpyright on `src`, Python pytest (`1637 passed`), frontend lint, frontend typecheck, and frontend Vitest (`11 files / 143 tests passed`).
- LSP diagnostics were clean on the final touched files: `src/voidcode/runtime/tool_display.py`, `tests/unit/tools/test_shell_exec_tool.py`, and `tests/integration/test_read_only_slice.py`.

### Commit preparation
- Planned atomic commit split follows the plan strategy while keeping direct implementation/tests together where practical.
- Generated artifacts intentionally excluded from staging: `frontend/test-results/` and `t11-atlas-smoke.png`.

### Final T12 gate
- Final post-commit `mise run check` passed on 2026-04-29: Ruff passed, basedpyright reported 0 errors/warnings, Python pytest reported `1637 passed`, frontend lint/typecheck passed, and frontend Vitest reported `11 files / 143 tests passed`.
- Full output was saved to `.sisyphus/evidence/task-12-mise-check.txt`.

### PR result
- Branch `feat/web-opencode-ui-redesign` was pushed with upstream `origin/feat/web-opencode-ui-redesign`.
- GitHub PR created: https://github.com/lei-jia-xing/voidcode/pull/319
- PR URL saved to `.sisyphus/evidence/task-12-pr.txt`.

---

## T13: Approval flow blocker fix (post-PR live QA bug)

**Date:** 2026-04-29T21:00:00+08:00

### Root cause
Provider model non-determinism causes different tool call arguments on approval replay. The original `run_loop.py` raised `ValueError` which the HTTP handler returned as 404, and the frontend kept the composer disabled because `currentSessionState.status` stayed `"waiting"`.

### Key fix: Re-emit as fresh approval instead of ValueError
When `approval_resolution` is provided but the replayed tool call differs from the pending approval (same tool_name, different arguments), clear the stale `approval_resolution` and fall through to `_resolve_permission()`. This creates a new pending approval for the updated tool call, keeping the session usable.

### Files changed
- `src/voidcode/runtime/run_loop.py` — Replaced ValueError with fallthrough to normal permission check
- `src/voidcode/runtime/http.py` — 404 → 409 for approval ValueErrors (better HTTP semantics)
- `frontend/src/store/index.ts` — After approval error, reload session from backend and set `runStatus: "idle"` to recover composer
- `tests/integration/test_read_only_slice.py` — Added `_DivergentWriteFileGraph` mock + regression test
- `tests/integration/test_http_transport.py` — Updated test to expect 409 (renamed: `_returns_conflict_`)

### Patterns learned
- Approval replay must tolerate provider non-determinism — the model may legitimately produce different arguments for the same tool on re-evaluation
- Stateful mock graphs with `_call_count` can simulate non-deterministic behavior in deterministic test engines
- `RuntimeFactory` protocol explicitly accepts `graph: object | None`, enabling graph injection tests
- The `ToolCallFactory` protocol cast pattern is: `cast(ToolCallFactory, importlib.import_module(...).ToolCall)`

### Verification
- 14 approval-related integration tests pass (130 total in affected files)
- Frontend: 11 files, 143 tests pass; lint/typecheck clean
- Zero diagnostics on all 5 modified files

### T13 correction: Store regression test for approval error recovery
- Added `recovers composer state after approval resolution failure` to `frontend/src/store.integration.test.ts`
- Uses `mockRejectedValue` on `resolveApprovalMock` to simulate approval failure, then verifies `runStatus === "idle"`, `approvalStatus === "error"`, replay data loaded, and sessions refreshed
- 32 store integration tests pass (31 existing + 1 new)

---

## P2: Skill display metadata canonical argument

- `src/voidcode/tools/skill.py` defines the skill tool input contract with `name` (plus optional `user_message`); `skill` only appears in successful result data under `data["skill"]["name"]`.
- `build_tool_display("skill", ...)` should therefore derive started/display metadata from `arguments["name"]`, with `arguments["skill"]` only as a legacy defensive fallback.
- Regression coverage lives in `tests/unit/runtime/test_runtime_events.py` and asserts `name` wins even when a legacy `skill` key is also present.

---

## T14: Opencode-style frontend chrome corrections

**Date:** 2026-04-29T21:46:00+08:00

### Files modified
- `frontend/src/components/sessionTitle.ts` — Added deterministic concise prompt titles with session-id fallback for long/noisy session prompts.
- `frontend/src/App.tsx` / `SessionSidebar.tsx` — Moved sidebar expansion to `App`, added visible `Files` and `Review` header controls, and reused concise titles in header/sidebar rows.
- `frontend/src/components/Composer.tsx` — Flattened composer footer selectors into quiet text controls with a subtle separator instead of nested bordered selector boxes.
- `frontend/src/components/StatusBar.tsx` — Removed Git from compact status/details and grouped Server/LSP/MCP in the top-right status control.
- `frontend/src/i18n/locales/en.json` / `zh-CN.json` — Added localized file-tree/review/server labels.
- `frontend/src/App.test.tsx`, `Composer.test.tsx`, `SessionSidebar.test.tsx` — Added coverage for concise titles, visible toggles, no-Git status popover, and accessible flat composer selectors.

### Patterns confirmed
- The opencode-like chrome target works best as text-first controls using existing `ControlButton` and `--vc-*` tokens; avoid new colored accents and avoid nested bordered controls in the composer footer.
- Sidebar expansion must be controlled by `App` when a header toggle also controls it; component tests should assert `onExpandedChange(false)` then rerender collapsed.
- Runtime status should present ACP transport as user-facing `Server` while keeping internal `acp` detail fields for transport/last-request diagnostics.

### Verification
- LSP diagnostics on all modified frontend files: zero diagnostics.
- `bun run --cwd frontend test:run -- src/App.test.tsx src/components/Composer.test.tsx src/components/SessionSidebar.test.tsx` — 47 passed.
- `bun run --cwd frontend lint` — passed.
- `bun run --cwd frontend typecheck` — passed.

---

## T15: User-facing tool labels and safe Thinking affordance

**Date:** 2026-04-29T22:10:00+08:00

- Provider `graph.provider_stream` reasoning payloads may contain raw chain-of-thought, code-like fragments, or internal scratch text. The frontend should treat reasoning events as a presence/timing signal only, never as user-rendered content.
- The safe UI pattern is to keep the compact `Thinking` disclosure and duration affordance, but show a localized placeholder when expanded so users understand reasoning happened without exposing internal provider text.
- Read/list/grep/glob grouped activity is clearer as `Project lookups` / `{{count}} lookups`; avoid user-facing `Context` because it reads like a mysterious separate tool.

## T16: Remove visible Thinking block

**Date:** 2026-04-29T22:14:00+08:00

- User rejected even the safe placeholder Thinking affordance; normal chat should show no message-level Thinking disclosure/button/panel for reasoning events, while keeping the transient running placeholder `chat.thinking` before assistant output.

---

## T17: Post-PR approval acknowledgement follow-up

**Date:** 2026-04-29T22:45:00+08:00

- `frontend/src/store/index.ts::resolveApproval` now acknowledges approvals locally before awaiting the non-streaming approval POST. It appends a local `runtime.approval_resolved` event, clears the stale pending approval card, and marks allow as running / deny as settled so the UI no longer stays on `Submitting...` for the whole resumed run.
- Pending approval lookup now ignores already-resolved request IDs, preventing old approval cards from reappearing after the local acknowledgement.
- While the approval POST is in flight, the store polls session replay best-effort and skips stale replay payloads that still show the same pending request, allowing fresh progress or a fresh approval to appear without overwriting the local ack with old waiting state.
- Regression coverage in `frontend/src/store.integration.test.ts` simulates a slow `RuntimeClient.resolveApproval` promise and verifies immediate `approvalStatus === "success"`, `runStatus === "running"`, local `runtime.approval_resolved`, and final replay replacement once the promise resolves.

### Verification
- LSP diagnostics on `frontend/src/store/index.ts` and `frontend/src/store.integration.test.ts`: zero diagnostics.
- `bun run --cwd frontend test:run -- src/store.integration.test.ts src/components/ChatThread.test.tsx` — 2 files / 60 tests passed.
- `bun run --cwd frontend lint` — passed.
- `bun run --cwd frontend typecheck` — passed.

---

## T18: Separate file tree and code review header controls

**Date:** 2026-04-29T23:24:00+08:00

- `frontend/src/App.tsx` now keeps `Sessions` for the left session sidebar and exposes distinct `File Tree` / `Code Review` header controls for the shared `ReviewPanel`.
- The header controls reuse `ControlButton` plus `--vc-*` monochrome tokens; `File Tree` sets `reviewMode: "files"`, `Code Review` sets `reviewMode: "changes"`, and clicking the already-active review surface closes the panel.
- Localized EN/zh-CN labels use distinct file-tree/code-review wording (`File Tree`/`文件树`, `Code Review`/`代码审查`) with separate aria labels.
- Regression coverage in `frontend/src/App.test.tsx` asserts the generic `Toggle review` button is absent and the two controls set independent modes; stale Playwright selectors in `frontend/e2e/launcher.spec.ts` were updated to the new header aria labels.
- Verification: zero LSP diagnostics on all touched frontend files; `bun run --cwd frontend test:run -- src/App.test.tsx` passed (27 tests); `bun run --cwd frontend lint` passed; `bun run --cwd frontend typecheck` passed.

---

## T19: File Tree diff URL encoding fix

**Date:** 2026-04-29T23:35:00+08:00

- `RuntimeClient.getReviewDiff(path)` must encode path segments separately (`path.split("/").map(encodeURIComponent).join("/")`) so nested review paths request `/api/review/diff/src/app.ts` instead of the proxy-hostile `/api/review/diff/src%2Fapp.ts`.
- File Tree selection remains path-transparent: `ReviewPanel` still calls `onSelectPath(node.path)`, and store coverage verifies a nested special-character path can resolve to `state: "clean"` without surfacing a diff error.
- Launcher e2e coverage now asserts the mocked diff route receives `/api/review/diff/src/app.ts` and not `src%2Fapp.ts`, then clicks the File Tree row and verifies diff text renders.
- Verification: LSP diagnostics clean on all T19-touched frontend files; focused Vitest (`App`, `ReviewPanel`, `store.integration`, `client.integration`) passed 73 tests; frontend lint/typecheck passed; frontend build passed with known Vite warnings; Playwright e2e passed 5/5 after rebuilding assets.

---

## P2: background_cancel display metadata taskId fix

- `background_cancel` pre-result display metadata must read the tool input contract key `taskId`, because `runtime.tool_started` only has original arguments; `task_id` remains a defensive legacy/result-shaped fallback.
- `background_output` should keep using snake_case `task_id`; avoid broad key normalization in `tool_display.py` so unrelated tool display arguments are not changed.
- Regression coverage belongs in `tests/unit/runtime/test_runtime_events.py` beside the existing pure `build_tool_display()` metadata contracts.

---

## P1: Approval fallback replay runStatus recovery

- `frontend/src/store/index.ts::resolveApproval` catch-path replay recovery must recompute `runStatus` with `runStatusForReplay(replay.session)`. Keeping the initial catch fallback `runStatus: "idle"` preserves composer recovery when replay reload fails, while successful replay replacement follows the backend session status.
- Regression coverage in `frontend/src/store.integration.test.ts` keeps the existing waiting replay case idle and adds the rejected-approval/running-replay case, asserting both `currentSessionState.status` and `runStatus` stay `"running"`.
