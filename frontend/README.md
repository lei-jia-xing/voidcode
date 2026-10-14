# VoidCode Web Frontend

Web client for VoidCode built with React 19, TypeScript, Tailwind CSS, and Bun.

## Quickstart

Run development commands from the `frontend/` directory:

```bash
cd frontend

# Install dependencies
bun install

# Start Vite dev server with HMR
bun run dev

# Build for production
bun run build

# Preview production build locally
bun run preview
```

## Available Scripts

- `bun run dev` - Start Vite development server with HMR.
- `bun run build` - Typecheck and build for production.
- `bun run preview` - Preview production build locally.
- `bun run lint` - Run ESLint.
- `bun run format` - Format code with Prettier.
- `bun run typecheck` - Run TypeScript typecheck.
- `bun run test` - Run tests with Vitest (jsdom).
- `bun run test:e2e` - Run Playwright end-to-end tests.

## Connecting to Backend

The frontend communicates with VoidCode via HTTP/SSE. Before connecting, ensure your provider credentials and workspace configuration are initialized as described in the [Repository Quickstart](../README.md#quickstart).

From the repository root, start the local runtime server:

```bash
uv run voidcode serve --workspace . --port 8765
```

From `frontend/`, point the Vite dev server to your running backend:

```bash
VITE_BACKEND_URL=http://127.0.0.1:8765 bun run dev
```

To build and launch the integrated web interface from the repository root:

```bash
mise run frontend:build
uv run voidcode web --workspace . --port 8000 --no-open
```
