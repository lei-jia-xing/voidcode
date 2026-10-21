# VoidCode Web Client

Browser client for the VoidCode core, built with React 19, TypeScript, Tailwind CSS, Vite, and Bun.

The client is `src/core/CoreApp.tsx` (mounted unconditionally by `src/main.tsx`). It speaks only to the core's HTTP/SSE transport under `/api/*`, renders the core's declarative plugin views, and keeps no local model of session state: the core's append-only event log and its per-revision command catalog are the source of truth.

## Quickstart

Run commands from the `frontend/` directory:

```bash
bun install
bun run dev        # Vite dev server with HMR on http://127.0.0.1:5173
bun run build      # typecheck + production bundle into dist/
bun run preview    # preview the production bundle
```

## Scripts

- `bun run dev` - Vite development server with HMR (`/api` proxied to `VITE_BACKEND_URL`).
- `bun run build` - Typecheck and build for production into `dist/`.
- `bun run preview` - Preview the production build locally.
- `bun run lint` - ESLint (zero warnings).
- `bun run format` / `bun run format:check` - Prettier.
- `bun run typecheck` - TypeScript, no emit.
- `bun run test` - Vitest unit/component tests (jsdom).
- `bun run test:e2e` - Playwright end-to-end smoke against a real core server (build `dist/` first).

## Running against a core server

From the repository root, start the core CLI's server. It serves `/api/*` and, once `frontend/dist` is built, the client itself at `/`:

```bash
bun install --cwd frontend && bun run --cwd frontend build

uv run voidcode --workspace . serve --port 8765
```

The command prints `core bearer token: <token>` at launch and `web client: http://127.0.0.1:8765/`. Open the printed URL with the token as a query parameter:

```text
http://127.0.0.1:8765/?token=<token>
```

Only `/api/*` is authenticated; the static bundle is public. The client takes its identity from two query parameters (both optional; same origin and no token by default):

| Parameter | Meaning |
|---|---|
| `server` | Core base URL, e.g. `http://127.0.0.1:8765`; default is the page's own origin. |
| `token` | Bearer token sent as `Authorization: Bearer <token>` on every `/api` request. |

For development with HMR, run the dev server against a core server instead of building:

```bash
VITE_BACKEND_URL=http://127.0.0.1:8765 bun run dev
# then open http://127.0.0.1:5173/?token=<token>
```

The core reads one config file, `voidcode.json` in the workspace (`{"schema_version": 1}` is a complete minimal file). A legacy `.voidcode.json` is an error and is never converted.

## API surface

The client uses `GET /api/commands` (catalog + revision), `GET|POST /api/sessions`, and per session `GET /show`, `POST /run`, `POST /resume`, `POST /invoke/{name}`, `POST /confirm`, `POST /cancel`, `GET /views`, `GET /usage`, and `GET /events` (SSE). Types come from `GET /api/openapi.json` and are generated into `src/lib/core/generated/api.d.ts`; never edit that file by hand:

```bash
mise run frontend:api:generate   # regenerate after a transport model change
mise run frontend:api:check      # fail when the committed types are stale
```

## End-to-end tests

`e2e/core.spec.ts` is the only end-to-end spec. It spawns the real core server on a throwaway workspace and state dir whose `voidcode.json` is just `{"schema_version": 1}`, opens the bundle the core serves at `/`, and asserts that the client renders, creates a session, and renders the committed result of the `/session_status` plugin command. No provider is configured, so nothing in the smoke reaches a model.

```bash
bun run build       # the served bundle is dist/index.html
bun run test:e2e    # spawns `uv run python -X utf8 -m voidcode.core ... serve` itself
```

Requirements: `uv` on `PATH` (the spec starts the core from the repository root) and the Playwright Chromium build (`bunx playwright install chromium`). `mise run frontend:e2e` runs the build and the spec in one step.
