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

### Tech Stack

- **Build Tool**: [Vite](https://vitejs.dev/) + [Bun](https://bun.sh/)
- **Framework**: [React](https://react.dev/) 18
- **Language**: [TypeScript](https://www.typescriptlang.org/)
- **Styling**: [Tailwind CSS](https://tailwindcss.com/)
- **State Management**: [Zustand](https://github.com/pmndrs/zustand)
- **Data Fetching**: [TanStack Query](https://tanstack.com/query)
- **Icons**: [Lucide React](https://lucide.dev/)

### Project Structure

**Current structure** (as of this commit):

```
frontend/
├── src/
│   ├── App.tsx           # Main UI shell
│   ├── main.tsx          # React entry point
│   ├── index.css         # Global styles
│   ├── store/            # Zustand state stores
│   ├── i18n/             # Translations (en, zh-CN)
│   └── types/            # TypeScript type definitions
└── public/               # Static assets
```

**Intended growth** (planned, not yet implemented):
```
frontend/src/
├── components/           # Reusable UI components (planned)
├── pages/                # Route pages (planned)
├── hooks/                # Custom React hooks (planned)
└── lib/                  # Utilities and API clients (planned)
```

## Implementation Status

> **Important:** The current frontend is a **UI shell only**. It uses **mock data** for all features.

- [x] UI Shell & Navigation
- [x] Mock Session View
- [x] Mock Agent Interaction
- [ ] Live API Integration (Planned)
- [ ] WebSocket Event Streaming (Planned)

## Architecture

The frontend is designed to communicate with the VoidCode runtime through:

1. **HTTP API** - For session management and configuration
2. **WebSocket** - For real-time event streaming (agent thoughts, tool calls, approvals)

**Note:** These interfaces are currently **mocked** in the frontend through local state and translation-aware UI shell data. There is no live API client or Python backend connection yet.

## Contributing

Please follow the same guidelines as the main project:
- Run `bun run lint` before committing
- Run `bun run typecheck` to ensure type safety
- Follow the existing code style
