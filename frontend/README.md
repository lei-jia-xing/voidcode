# VoidCode Web Client

Browser client for the VoidCode core, built with React 19, TypeScript, Tailwind CSS, Vite, and Bun.

The client is `src/core/CoreApp.tsx` (mounted unconditionally by `src/main.tsx`). It speaks only to the core's WebSocket RPC transport under `/api/*`, renders the core's declarative plugin views, and keeps no local model of session state: the core's append-only event log and its per-revision command catalog are the source of truth.

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

The command prints `core bearer token: <token>` at launch and `web client: http://127.0.0.1:8765/`. Open the printed URL and paste the token into the token gate when prompted; it is kept in per-tab session storage. The token never goes in the URL (no history, Referer, or access-log leak), and a `?token=` query parameter is ignored.

```text
http://127.0.0.1:8765/
```

Only `/api/*` is authenticated; the static bundle is public. The client takes one optional query parameter:

| Parameter | Meaning |
|---|---|
| `server` | Core base URL, e.g. `http://127.0.0.1:8765`; default is the page's own origin. |

For development with HMR, run the dev server against a core server instead of building, then paste the printed token into the gate:

```bash
VITE_BACKEND_URL=http://127.0.0.1:8765 bun run dev
# then open http://127.0.0.1:5173/?server=http://127.0.0.1:8765
```

The core reads one config file, `voidcode.json` in the workspace (`{"schema_version": 1}` is a complete minimal file). Any other filename (including a legacy `.voidcode.json`) is ignored, not detected or converted.

## API surface

The client uses JSON-RPC 2.0 over `/api/rpc` (`voidcode.rpc.v1`). Calls,
responses, and cursor-based event subscriptions share one WebSocket. The first
message authenticates with the token; URLs never carry WebSocket credentials.
Only health, the schema document, and static assets use HTTP. See the
[transport contract](../docs/contracts/client-transport.md) for methods,
acknowledgements, reconnect behavior, and lost-response semantics.

DTOs and the typed method/notification maps come from `GET /api/openapi.json`
and are generated into `src/lib/core/generated/api.d.ts`; never edit it by hand:

```bash
mise run frontend:api:generate   # regenerate after a transport model change
mise run frontend:api:check      # fail when the committed types are stale
```

## End-to-end tests

`e2e/core.spec.ts` covers initial connection and command execution.
`e2e/rpc-reconnect.spec.ts` restarts the real server while the browser stays open
and proves cursor replay without duplicate rows or command resends. The smoke spawns the real core server on a throwaway workspace and state dir whose `voidcode.json` is just `{"schema_version": 1}`, opens the bundle the core serves at `/`, and asserts that the client renders, creates a session, and renders the committed result of the `/plan status` plugin command. No provider is configured, so nothing in the smoke reaches a model.

```bash
bun run build       # the served bundle is dist/index.html
bun run test:e2e    # spawns `.venv/bin/python -m voidcode ... serve` itself
```

Requirements: the project `.venv` (the specs start the core from the repository root) and the Playwright Chromium build (`bunx playwright install chromium`). `mise run frontend:e2e` runs the build and the spec in one step.
