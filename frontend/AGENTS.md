# Frontend Guide

Bun, Vite, and React frontend for VoidCode's HTTP/SSE runtime client.

## Where to Look

| Area | Path |
|---|---|
| App shell | `src/App.tsx` |
| Entrypoint | `src/main.tsx` |
| State | `src/store/index.ts` |
| Translations | `src/i18n/index.ts` |
| Runtime client | `src/lib/runtime/client.ts` |
| Runtime types | `src/lib/runtime/types.ts` |
| Generated API types | `src/lib/runtime/generated/api.d.ts` |

`api.d.ts` is generated from the backend OpenAPI schema. Update the backend model and run `mise run frontend:api:generate`; `mise run frontend:api:check` checks synchronization.

Keep English and zh-CN translations together when changing user-facing copy. Bun runs frontend install, development, tests, and builds.

```bash
bun install
bun run dev
bun run lint
bun run typecheck
bun run test
bun run test:e2e
bun run build
```
