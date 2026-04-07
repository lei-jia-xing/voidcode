# VoidCode Web Frontend

Modern web interface for VoidCode - built with React, TypeScript, and Bun.

## Quick Start

```bash
# Install dependencies (using bun)
bun install

# Start development server
bun run dev

# Build for production
bun run build

# Preview production build
bun run preview
```

## Development

### Available Scripts

- `bun run dev` - Start Vite dev server with HMR
- `bun run build` - Type-check and build for production
- `bun run preview` - Preview production build locally
- `bun run lint` - Run ESLint
- `bun run format` - Format code with Prettier
- `bun run typecheck` - Run TypeScript type checking
- `bun run test` - Run tests with Vitest
- `bun run test:run` - Run tests once without watch mode
- `bun run test:coverage` - Run tests with coverage reporting

### Tech Stack

- **Build Tool**: [Vite](https://vitejs.dev/) + [Bun](https://bun.sh/)
- **Framework**: [React](https://react.dev/) 18
- **Language**: [TypeScript](https://www.typescriptlang.org/)
- **Styling**: [Tailwind CSS](https://tailwindcss.com/)
- **State Management**: [Zustand](https://github.com/pmndrs/zustand)
- **Data Fetching**: [TanStack Query](https://tanstack.com/query)
- **Icons**: [Lucide React](https://lucide.dev/)

### Project Structure

> **Note:** The structure below describes the intended frontend growth path, not a complete representation of the current tree. Today the frontend is still relatively flat and centered around `src/App.tsx`, `src/main.tsx`, `src/store/`, `src/i18n/`, and `src/types/`.

```
frontend/
├── src/
│   ├── components/        # Reusable UI components
│   ├── pages/            # Route pages
│   ├── stores/           # Zustand state stores
│   ├── hooks/            # Custom React hooks
│   ├── lib/              # Utilities and API clients
│   ├── types/            # TypeScript type definitions
│   └── styles/           # Global styles and Tailwind config
├── public/               # Static assets
└── index.html            # Entry HTML
```

## Implementation Status

> **Important:** The current frontend is still a pre-MVP **UI shell**, but its main session/task/activity path now consumes the local runtime transport for session listing, replay, and streamed runs.

- [x] UI Shell & Navigation
- [x] Runtime-backed session list and replay
- [x] Runtime-backed streamed task execution
- [x] Live API Integration for the current single-agent MVP path
- [ ] WebSocket Event Streaming (Planned)

## Architecture

The frontend is designed to communicate with the VoidCode runtime through:

1. **HTTP API** - For session management and configuration
2. **WebSocket** - For real-time event streaming (agent thoughts, tool calls, approvals)

**Note:** These interfaces are still **partial** today. The current integrated path uses the local HTTP/SSE transport for session listing, replay, and streamed runs, while broader client work such as WebSocket transport and richer UX remains future work.

## Contributing

Please follow the same guidelines as the main project:
- Prefer the root `mise run frontend:*` tasks when working from the repo root; `bun install` and `bun run ...` in `frontend/` remain valid direct equivalents.
- Run `bun run lint` before committing
- Run `bun run typecheck` to ensure type safety
- Run `bun run test:run` for component coverage changes
- Follow the existing code style
