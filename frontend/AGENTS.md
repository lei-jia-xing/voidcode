# FRONTEND KNOWLEDGE BASE

**Generated:** 2026-04-04
**Commit:** d6157c7
**Branch:** master

## OVERVIEW
Bun/Vite/React frontend shell. The main session/task/activity path now consumes the local runtime transport for session list, replay, and streamed runs; broader client work is still pre-MVP.

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
- Do not claim full live API or WebSocket behavior exists; the current shell only covers the minimal HTTP/SSE-backed MVP path for session list, replay, and streamed runs.
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
bun run build
```

## NOTES
- `frontend/README.md` describes an aspirational component/page structure that the current `src/` tree does not fully implement yet.
- Current UI is mostly centered in `App.tsx`; avoid over-documenting nonexistent substructure.
- The frontend now has a live minimal runtime transport path for the main state/timeline shell; update this file again when broader client capabilities move beyond the current MVP slice.
