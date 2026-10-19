# Frontend Guide

Bun + Vite + React 19 client for the VoidCode kernel's HTTP/SSE transport. `src/main.tsx` mounts `src/kernel/KernelApp.tsx` unconditionally; there is no second app and no other backend client.

## Where to Look

| Area | Path |
|---|---|
| Entrypoint | `src/main.tsx` |
| Client shell | `src/kernel/KernelApp.tsx` |
| Declarative plugin views | `src/kernel/views/` (`PluginViewRenderer` dispatches by `view_type`) |
| View/component tests | `src/kernel/views.test.tsx` |
| Transport client | `src/lib/kernel/client.ts` |
| Wire types | `src/lib/kernel/types.ts` |
| Generated API types | `src/lib/kernel/generated/api.d.ts` |
| Translations | `src/i18n/index.ts`, `src/i18n/locales/*.json` |
| End-to-end spec | `e2e/kernel.spec.ts` |

## Kernel client contract

- The kernel is the source of truth: sessions, events, and plugin state live in its append-only store. The client projects what it reads; it never keeps a parallel store or derives state the kernel owns.
- Every command or confirmation carries the `revisionId` the catalog was displayed under. A command from another revision must not be re-sent automatically — re-read the catalog and views instead.
- Views are declarative (`markdown`/`fields`/`choices`/`form`/`diff`/`status`/`notice`/`confirmation`); rendering a new shape means adding a view renderer, not a bespoke panel.
- The client identity comes from the `server` and `token` query parameters (see `frontend/README.md`); a change of either invalidates everything read under the previous one.

## Generated API types

`src/lib/kernel/generated/api.d.ts` comes from the kernel transport's OpenAPI document. After changing the transport models, run `mise run frontend:api:generate`; `mise run frontend:api:check` fails when the committed types are stale. Never hand-edit the generated file.

## i18n

User-facing copy lives under the `kernel.*` namespace, present in both `en.json` and `zh-CN.json`. Keep both files in sync and keep keys in the `kernel.*` namespace.

## Commands

```bash
bun install
bun run dev
bun run lint
bun run typecheck
bun run test
bun run build
bun run test:e2e   # requires `bun run build` first; spawns a real kernel on a temp workspace
```
