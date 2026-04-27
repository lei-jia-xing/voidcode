# FRONTEND KNOWLEDGE BASE

**Generated:** 2026-04-04
**Commit:** d6157c7
**Branch:** master

## OVERVIEW
Bun/Vite/React frontend shell. The app now has a minimal live runtime transport path for session listing, replay, and streamed runs, while broader Web UX polish and deeper runtime-driven flows remain incomplete.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| App entry | `src/main.tsx` | renders `<App />` |
| Main shell | `src/App.tsx` | primary layout, task list, activity panel |
| Local state | `src/store/index.ts` | Zustand + persisted local state |
| i18n setup | `src/i18n/index.ts` | `en` and `zh-CN` resources |
| Types | `src/types/index.ts` | frontend-only data shapes |
| Tooling | `package.json` | `bun run` command surface |
| Vite wiring | `vite.config.ts` | dev/build config, future proxy surface |

## STRUCTURE
```text
frontend/
├── src/App.tsx          # current UI shell
├── src/main.tsx         # React entry
├── src/store/index.ts   # persisted Zustand store
├── src/i18n/            # translations + i18n init
├── src/types/           # shared TS types
└── public/              # static assets
```

## CONVENTIONS
- Use Bun for install/run/build/lint/typecheck.
- Validate with `bun run lint` and `bun run typecheck`; `bun run build` also runs TypeScript before Vite build.
- Preserve EN/zh-CN support when changing user-facing copy.
- Keep state changes explicit in the Zustand store; current app state is local and persisted.

## ANTI-PATTERNS
- Do not claim full live API parity or WebSocket behavior exists; the current implementation exposes a minimal runtime-backed path for listing sessions, replaying sessions, and streaming runs, but it is not yet a fully productized runtime-driven web app.
- Do not introduce backend assumptions into the UI without corresponding backend work.
- Do not commit generated build artifacts from `dist/`.
- Do not duplicate repo-wide coding standards here; root docs own commit and PR policy.

## COMMANDS
```bash
bun install
bun run dev
bun run lint
bun run typecheck
bun run test:run
bun run test:e2e
bun run build
```

## NOTES
- `frontend/README.md` describes an aspirational component/page structure that the current `src/` tree does not fully implement yet.
- Current UI is mostly centered in `App.tsx`; avoid over-documenting nonexistent substructure.
- The frontend now consumes a minimal runtime transport path through `src/lib/runtime/client.ts`, `src/store/index.ts`, and `src/App.tsx`; update this file again when the remaining web flows stop lagging behind the runtime surface.
