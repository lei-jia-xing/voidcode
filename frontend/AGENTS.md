# Frontend Guide

Bun + Vite + React 19 client for the VoidCode core's WebSocket RPC transport. `src/main.tsx` mounts `src/core/CoreApp.tsx` unconditionally; there is no second app and no other backend client.

## Where to Look

| Area | Path |
|---|---|
| Entrypoint | `src/main.tsx` |
| Client shell | `src/core/CoreApp.tsx` |
| Declarative plugin views | `src/core/views/` (`PluginViewRenderer` dispatches by `view_type`) |
| View/component tests | `src/core/views.test.tsx` |
| Transport client | `src/lib/core/client.ts` |
| Wire types | `src/lib/core/types.ts` |
| Generated API types | `src/lib/core/generated/api.d.ts` |
| Translations | `src/i18n/index.ts`, `src/i18n/locales/*.json` |
| End-to-end specs | `e2e/core.spec.ts`, `e2e/rpc-reconnect.spec.ts` |

## Core client contract

- The core is the source of truth: sessions, events, and plugin state live in its append-only store. The client projects what it reads; it never keeps a parallel store or derives state the core owns.
- Every command or confirmation carries the `revisionId` the catalog was displayed under. A command from another revision must not be re-sent automatically — re-read the catalog and views instead.
- Views are declarative (`markdown`/`fields`/`choices`/`form`/`diff`/`status`/`notice`/`confirmation`); rendering a new shape means adding a view renderer, not a bespoke panel.
- The client identity comes from the `server` and `token` query parameters (see `frontend/README.md`); a change of either invalidates everything read under the previous one.

## Generated API types

`src/lib/core/generated/api.d.ts` comes from the core transport schema document, including its RPC method/notification registry. After changing the transport models, run `mise run frontend:api:generate`; `mise run frontend:api:check` fails when the committed types are stale. Never hand-edit the generated file.

## i18n

User-facing copy lives under the `core.*` namespace, present in both `en.json` and `zh-CN.json`. Keep both files in sync and keep keys in the `core.*` namespace.

## Commands

```bash
bun install
bun run dev
bun run lint
bun run typecheck
bun run test
bun run build
bun run test:e2e   # requires `bun run build` first; spawns a real core on a temp workspace
```
