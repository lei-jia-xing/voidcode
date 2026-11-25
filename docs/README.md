# VoidCode Documentation

Welcome to the VoidCode documentation. Here you will find guides on architecture, configuration, development standards, and technical contracts.

## Guides

- [`architecture.md`](./architecture.md): Core + plugin architecture, layer rules, durability model, and state layout.
- [`configuration.md`](./configuration.md): `voidcode.json`, approval policy, providers, plugin trust, and legacy handling.
- [`coding-standards.md`](./coding-standards.md): Engineering standards, type boundaries, and Conventional Commits conventions.
- [`testing.md`](./testing.md): Test tiers, loopback providers, offline guards, and verification commands.

## Technical Contracts

- [`contracts/README.md`](./contracts/README.md): Core durability semantics and the Core/SDK contract index.

## Plugins

Each bundled plugin documents its own surface, configuration, behavior, and limits in its package README. The Markdown agent format is canonical in the [subagents](../src/voidcode/plugins/subagents/README.md) README; scripted workflows live in the [workflows](../src/voidcode/plugins/workflows/README.md) README; goal accounting lives in the [goal](../src/voidcode/plugins/goal/README.md) README.

| Plugin | Entry doc |
|---|---|
| `ast_grep` | [README](../src/voidcode/plugins/ast_grep/README.md) |
| `btw` | [README](../src/voidcode/plugins/btw/README.md) |
| `compaction` | [README](../src/voidcode/plugins/compaction/README.md) |
| `cron` | [README](../src/voidcode/plugins/cron/README.md) |
| `exec` | [README](../src/voidcode/plugins/exec/README.md) |
| `files` | [README](../src/voidcode/plugins/files/README.md) |
| `find` | [README](../src/voidcode/plugins/find/README.md) |
| `goal` | [README](../src/voidcode/plugins/goal/README.md) |
| `hooks` | [README](../src/voidcode/plugins/hooks/README.md) |
| `lsp` | [README](../src/voidcode/plugins/lsp/README.md) |
| `mcp` | [README](../src/voidcode/plugins/mcp/README.md) |
| `plan` | [README](../src/voidcode/plugins/plan/README.md) |
| `providers` | [README](../src/voidcode/plugins/providers/README.md) |
| `review` | [README](../src/voidcode/plugins/review/README.md) |
| `skills` | [README](../src/voidcode/plugins/skills/README.md) |
| `subagents` | [README](../src/voidcode/plugins/subagents/README.md) |
| `todo` | [README](../src/voidcode/plugins/todo/README.md) |
| `user_shell` | [README](../src/voidcode/plugins/user_shell/README.md) |
| `web` | [README](../src/voidcode/plugins/web/README.md) |
| `workflows` | [README](../src/voidcode/plugins/workflows/README.md) |

## Open work

- [`src/voidcode/TODO.md`](../src/voidcode/TODO.md): Product, transport, web, and engineering open work. Module TODOs live beside their code; see [command UX acceptance](./command-ux.md) for per-command checks.
