# Web OpenCode Alignment Matrix

## Purpose

This document records the implementation-level comparison between VoidCode's web MVP target and production-grade reference patterns from OpenCode, with limited category/delegation lessons from Oh My OpenAgent (OMOA) only where those lessons help reduce product complexity.

This is not a parity checklist. Each concern is deliberately classified as one of:

- **Adopt**: carry the pattern over with minimal structural change
- **Adapt**: keep the core idea but reshape it to fit VoidCode's runtime-centric architecture
- **Reject**: do not bring the pattern over because it adds complexity or solves a problem VoidCode does not currently have

## Alignment Matrix

### 1. Launcher

- **OpenCode reference**: `https://github.com/anomalyco/opencode/blob/4877eccc0d06c747624bf61aaa6f3e65cea9cc8d/packages/opencode/src/cli/cmd/web.ts`
- **Local seams**: `src/voidcode/cli.py`, `src/voidcode/server.py`
- **Decision**: **Adopt**
- **Why**: OpenCode's split between a headless server command and a user-friendly web launcher is production-grade, intuitive, and matches the user's requested end state.
- **VoidCode target**:
  - Add `voidcode web` as the user-facing launcher
  - Keep `voidcode serve` as the advanced/headless server surface
  - Print banner + usable URL
  - Best-effort browser open must not be fatal

### 2. CLI modularity

- **OpenCode references**:
  - `https://github.com/anomalyco/opencode/blob/4877eccc0d06c747624bf61aaa6f3e65cea9cc8d/packages/opencode/src/index.ts`
  - `https://github.com/anomalyco/opencode/blob/4877eccc0d06c747624bf61aaa6f3e65cea9cc8d/packages/opencode/src/cli/cmd/cmd.ts`
- **Local seams**: `src/voidcode/cli.py`, `src/voidcode/__main__.py`
- **Decision**: **Adapt**
- **Why**: OpenCode's per-command module pattern is cleaner than the current monolithic argparse assembly, but VoidCode should keep Python-native command wiring rather than mimic the TypeScript/yargs structure directly.
- **VoidCode target**:
  - Extract `serve` / `web` command handling into clearer command-owned units if needed
  - Preserve shared startup primitives rather than duplicating server logic
  - Keep Python entrypoint semantics unchanged

### 3. Server ownership

- **OpenCode references**:
  - `https://github.com/anomalyco/opencode/blob/4877eccc0d06c747624bf61aaa6f3e65cea9cc8d/packages/opencode/src/cli/cmd/serve.ts`
  - `https://github.com/anomalyco/opencode/blob/4877eccc0d06c747624bf61aaa6f3e65cea9cc8d/packages/opencode/src/server/server.ts`
- **Local seams**: `src/voidcode/server.py`, `src/voidcode/runtime/http.py`
- **Decision**: **Adopt**
- **Why**: OpenCode's `web` command reuses the same server listen path as `serve`, which cleanly preserves one control plane. VoidCode already has this shape available through `server.py` and `runtime/http.py`.
- **VoidCode target**:
  - `web` wraps the same runtime HTTP server primitive used by `serve`
  - No second web architecture
  - Runtime HTTP remains the sole transport authority

### 4. Config source of truth

- **OpenCode references**:
  - `https://github.com/anomalyco/opencode/blob/4877eccc0d06c747624bf61aaa6f3e65cea9cc8d/packages/opencode/src/config/config.ts`
  - `https://github.com/anomalyco/opencode/blob/4877eccc0d06c747624bf61aaa6f3e65cea9cc8d/packages/opencode/src/config/paths.ts`
- **Local seams**: `src/voidcode/runtime/config.py`, `src/voidcode/runtime/http.py`, `frontend/src/store/index.ts`, `frontend/src/lib/runtime/types.ts`
- **Decision**: **Adapt**
- **Why**: OpenCode's canonical backend schema and layered precedence are exactly the right production lesson, but VoidCode should express this in Pydantic/runtime contracts instead of trying to clone OpenCode's Effect Schema stack.
- **VoidCode target**:
  - Backend runtime config remains canonical
  - Frontend reads/writes only a versioned projection of that schema
  - Local UI-only preferences remain browser-local
  - Overlapping execution config is removed from frontend persistence or treated as non-authoritative

### 5. Provider and Model configuration

- **OpenCode references**:
  - `https://github.com/anomalyco/opencode/blob/4877eccc0d06c747624bf61aaa6f3e65cea9cc8d/packages/opencode/src/config/provider.ts`
  - `https://github.com/anomalyco/opencode/blob/4877eccc0d06c747624bf61aaa6f3e65cea9cc8d/packages/opencode/src/provider/provider.ts`
- **Local seams**: `src/voidcode/runtime/config.py`, `src/voidcode/provider/config.py`, `frontend/src/components/SettingsPanel.tsx`, `frontend/src/components/Composer.tsx`
- **Decision**: **Adapt**
- **Why**: OpenCode's provider/model structure demonstrates production-grade ownership and shape, but VoidCode's MVP only needs the subset required for provider selection, key handling, and model selection in the web path.
- **VoidCode target**:
  - Runtime-owned provider/model configuration surface
  - Web projection supports provider, model, key-present flag, and optional key submission
  - No broad provider marketplace/admin features

### 6. API key flow

- **OpenCode reference**: `https://github.com/anomalyco/opencode/blob/4877eccc0d06c747624bf61aaa6f3e65cea9cc8d/packages/opencode/src/config/provider.ts`
- **Local seams**: `src/voidcode/runtime/http.py`, `src/voidcode/runtime/config.py`, `src/voidcode/provider/config.py`, `frontend/src/components/SettingsPanel.tsx`
- **Decision**: **Adapt**
- **Why**: The production lesson is clear secret ownership and configuration layering, not necessarily OpenCode's exact entry UX. The user explicitly wants dual mode, so VoidCode must support both env-backed and browser-submitted keys while keeping backend authority.
- **VoidCode target**:
  - `.env` / backend config remains the default happy path
  - Browser submission is allowed through `/api/settings`
  - `GET /api/settings` never returns raw API keys
  - UI surfaces only presence/absence, not secret contents

### 7. Tool lifecycle states

- **OpenCode references**:
  - `https://github.com/anomalyco/opencode/blob/4877eccc0d06c747624bf61aaa6f3e65cea9cc8d/packages/opencode/src/session/message-v2.ts`
  - `https://github.com/anomalyco/opencode/blob/4877eccc0d06c747624bf61aaa6f3e65cea9cc8d/packages/opencode/src/session/status.ts`
- **Local seams**: `src/voidcode/runtime/events.py`, `src/voidcode/runtime/service.py`, `src/voidcode/runtime/http.py`, `frontend/src/lib/runtime/event-parser.ts`
- **Decision**: **Adapt**
- **Why**: OpenCode's per-tool `pending/running/completed/error` model is the right production pattern, but VoidCode already has richer runtime/task lifecycle concepts. The correct move is to add a stable per-tool web contract without flattening the rest of the runtime.
- **VoidCode target**:
  - Add backend-owned tool invocation state fields for web rendering
  - Preserve existing runtime/delegation/task semantics separately
  - Frontend must consume explicit status/phase/label values rather than infer from event names

### 8. Pending labels and progress text

- **OpenCode references**:
  - `https://github.com/anomalyco/opencode/blob/4877eccc0d06c747624bf61aaa6f3e65cea9cc8d/packages/ui/src/components/tool-status-title.tsx`
  - `https://github.com/anomalyco/opencode/blob/4877eccc0d06c747624bf61aaa6f3e65cea9cc8d/packages/ui/src/components/basic-tool.tsx`
  - `https://github.com/anomalyco/opencode/blob/4877eccc0d06c747624bf61aaa6f3e65cea9cc8d/packages/ui/src/components/message-part.tsx`
- **OMOA metadata reference**:
  - `https://github.com/code-yeongyu/oh-my-openagent/blob/708891dabe5d4f37c76a0de63dc353cdf05902b0/src/features/tool-metadata-store/publish-tool-metadata.ts`
- **Local seams**: `src/voidcode/runtime/events.py`, `src/voidcode/runtime/http.py`, `frontend/src/lib/runtime/event-parser.ts`, `frontend/src/App.tsx`
- **Decision**: **Adapt**
- **Why**: The user explicitly wants labels like `Preparing patch...` and `Delegating...`. OpenCode and OMOA together show the right shape: backend-authored metadata/title, frontend rendering.
- **VoidCode target**:
  - Emit stable backend label/title fields for tool lifecycle UI
  - Required labels include at least: `Preparing patch...`, `Delegating...`, `Reading file...`, `Searching content...`, `Writing command...`
  - No heuristic string synthesis in the browser

### 9. Invocation identity

- **OpenCode reference**: `https://github.com/anomalyco/opencode/blob/4877eccc0d06c747624bf61aaa6f3e65cea9cc8d/packages/opencode/src/session/message-v2.ts`
- **OMOA reference**: `https://github.com/code-yeongyu/oh-my-openagent/blob/708891dabe5d4f37c76a0de63dc353cdf05902b0/src/features/tool-metadata-store/publish-tool-metadata.ts`
- **Local seams**: `src/voidcode/runtime/events.py`, `src/voidcode/runtime/contracts.py`, `src/voidcode/runtime/http.py`
- **Decision**: **Adapt**
- **Why**: Repeated same-name tool calls are a real ambiguity risk. OpenCode's `callID` model and OMOA's metadata-publishing pattern both support the same conclusion: the web contract needs a stable invocation identifier.
- **VoidCode target**:
  - Add per-tool invocation identifiers to web-visible status payloads
  - Make status updates correlate to one tool call deterministically
  - Keep subagent/delegation contracts separate from per-tool identity

### 10. Leader-only top-level execution

- **OpenCode reference**: `https://github.com/anomalyco/opencode/blob/4877eccc0d06c747624bf61aaa6f3e65cea9cc8d/packages/opencode/src/agent/agent.ts`
- **Local seams**: `src/voidcode/runtime/service.py`, `src/voidcode/agent/builtin.py`
- **Decision**: **Adopt**
- **Why**: OpenCode has a clear distinction between primary agents and subagents. VoidCode already enforces `leader` as the only top-level executable preset, which should remain intact for the MVP.
- **VoidCode target**:
  - Preserve `leader` as the only web-top-level execution surface
  - Continue allowing internal delegated subagent execution where already supported

### 11. Category route surface

- **OMOA references**:
  - `https://github.com/code-yeongyu/oh-my-openagent/blob/708891dabe5d4f37c76a0de63dc353cdf05902b0/src/tools/delegate-task/builtin-categories.ts`
  - `https://github.com/code-yeongyu/oh-my-openagent/blob/708891dabe5d4f37c76a0de63dc353cdf05902b0/src/tools/delegate-task/category-resolver.ts`
- **Local seams**: `src/voidcode/runtime/task.py`, `src/voidcode/runtime/contracts.py`, `src/voidcode/runtime/http.py`, `src/voidcode/tools/task.py`
- **Decision**: **Reject** for product surface; **Adapt** internally only if needed
- **Why**: OMOA proves category-routing can work, but it also proves category/subagent confusion becomes a recurring product and model-behavior failure mode. For VoidCode's web MVP, category is unnecessary public complexity.
- **VoidCode target**:
  - Remove category from web-facing request payloads, docs, UI, and frontend contracts
  - Keep any internal preset resolution private if still required by runtime internals
  - Prefer direct subagent or resolved preset semantics over exposed category strings

### 12. Status rendering architecture

- **OpenCode references**:
  - `https://github.com/anomalyco/opencode/blob/4877eccc0d06c747624bf61aaa6f3e65cea9cc8d/packages/ui/src/components/message-part.tsx`
  - `https://github.com/anomalyco/opencode/blob/4877eccc0d06c747624bf61aaa6f3e65cea9cc8d/packages/ui/src/components/basic-tool.tsx`
  - `https://github.com/anomalyco/opencode/blob/4877eccc0d06c747624bf61aaa6f3e65cea9cc8d/packages/ui/src/components/tool-status-title.tsx`
- **Local seams**: `frontend/src/lib/runtime/event-parser.ts`, `frontend/src/App.tsx`, `frontend/src/components/ChatThread.tsx`
- **Decision**: **Adapt**
- **Why**: OpenCode's rendering stack is more layered than VoidCode needs, but the core principle is correct: UI components render backend-owned tool state rather than deriving hidden logic from raw streams.
- **VoidCode target**:
  - Keep frontend rendering lighter-weight than OpenCode
  - Still move to backend-authored status/title fields as the primary rendering source

### Delegation policy after Task 7

- Top-level web/runtime execution remains `leader`-only.
- Public HTTP and browser request contracts must not accept `delegation.category`.
- Public delegated payloads must describe child execution with `subagent_type` when explicitly requested, otherwise with resolved delegation fields such as `selected_preset` / `selected_execution_engine`.
- Category-to-preset mapping may remain as a private runtime/storage implementation detail for internal delegation and reconciliation only.
- Subagents are a behind-the-scenes tool-driven mechanism, not a public category-routed product surface.

### 13. QA, test maturity, and verification strategy

- **OpenCode production lesson**: web launcher, config, and status changes are treated as contract-bearing behavior, not just visual polish
- **Local seams**:
  - `tests/unit/interface/test_cli_smoke.py`
  - `tests/unit/interface/test_cli_delegated_parity.py`
  - `tests/integration/test_http_transport.py`
  - `tests/integration/test_http_delegated_parity.py`
  - `frontend/src/store.integration.test.ts`
  - `frontend/src/App.test.tsx`
- **Decision**: **Adopt**
- **Why**: VoidCode already has strong local deterministic coverage foundations. The missing pieces are launcher-specific tests, client/status contract tests, and browser E2E.
- **VoidCode target**:
  - Extend pytest/Vitest around the new launcher/config/status seams
  - Add Playwright for the actual `voidcode web` user path
  - Keep `.env`-backed live smoke narrow and skippable when credentials are absent

## Summary Decisions

### Adopt

- Separate `web` launcher from headless `serve`
- Keep one server/control-plane path underneath both commands
- Preserve leader-only top-level execution
- Treat launcher/config/status changes as contract-level, testable work

### Adapt

- CLI modularity into Python-appropriate command boundaries
- Backend-owned config schema and frontend projection
- Provider/model config surfaces using a smaller MVP subset
- Backend-authored tool status model and pending labels
- Invocation identity for tool calls
- Status rendering that consumes backend state directly

### Reject

- Exposed category-route product surface in the web MVP
- Full OpenCode parity or architecture copying for its own sake
- Frontend-owned execution/config truth

## Concrete Implications for Implementation

1. `Task 2` should refactor command ownership enough that `web` is not an ad hoc branch inside a monolithic parser.
2. `Task 3` should define the canonical runtime-owned settings schema and explicitly separate runtime-owned fields from local UI-only fields.
3. `Task 4` should add a stable backend tool status contract with label/title/invocation identity fields and explicit leader/category boundary behavior.
4. `Task 5` should implement the terminal launcher UX modeled on OpenCode's `web.ts`, but through VoidCode's existing `server.py` / `runtime/http.py` path.
5. `Task 6` should delete or bypass heuristic-only frontend status interpretation and move to backend-provided rendering inputs.
6. `Task 7` should remove category-route surface from web-visible contracts while preserving any necessary internal delegation mechanics.
