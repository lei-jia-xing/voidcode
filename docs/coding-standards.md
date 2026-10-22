# Coding Standards

VoidCode prioritizes readability, explicit boundaries, and repeatable local verification. The best code is simple, robust, and easy to reason about six months later.

## Design Principles

- **Clear Responsibility**: Each module and function has one well-defined purpose. Functions are focused and deterministic where possible.
- **Explicit Boundaries & Types**: Use typed dataclasses, Pydantic models, and Protocols at subsystem boundaries. External inputs (CLI arguments, HTTP bodies, configuration files, external tool outputs) are validated once at ingress. Internal code relies on strongly typed contracts.
- **Fail Visibly**: Missing required data raises an explicit exception. We avoid default fallbacks (such as converting `None` to `0` or `""`) that obscure unknown or uninitialized state.
- **Intentional Fallbacks**: Distinguish intentional defaults (such as declared configuration precedence) from missing values. Optional fields use `T | None` and are handled explicitly by consumers.
- **Meaningful Errors**: Exceptions are caught where they can be meaningfully handled, logged with context, or converted into typed error responses.

## Subsystem Architecture

VoidCode enforces clean boundaries across its backend layers:

- **`src/voidcode/sdk/`**: The shared contract surface (plugin, tools, commands, services, provider, hooks, views, invocation, config). It may import only the standard library, `pydantic`, and `jsonschema` — never the core or a plugin.
- **`src/voidcode/core/`**: Governance, append-only persistence, plugin admission and activation, the in-process plugin runtime (`plugins.py`, `services.py`, `processes.py`), HTTP/SSE transport, and the TUI. It imports no concrete plugin.
- **`src/voidcode/capabilities/`**: Shared files, execution, and provider implementations. Imports the SDK and third-party libraries; never imports core or plugins.
- **`src/voidcode/plugins/`**: The bundled plugins, built on the public SDK and shared capabilities. Optional peer queries use SDK contracts rather than cross-plugin imports.

## Python Standards

- Target Python 3.14. Prefer standard library solutions and existing dependencies.
- Code formatting and linting are enforced by Ruff (`mise run lint`, `mise run format`).
- Static type checking is enforced by ty (`mise run typecheck`).
- **Tests**: Add or update tests when observable behavior changes. Assert meaningful outcomes, bounds, state transitions, and persisted effects. Use local loopback providers for real protocol paths, keeping routine verification deterministic and credential-free.

## Frontend Standards

- Built with Bun, Vite, and React 19 in `frontend/`.
- Validate with `bun run lint` and `bun run typecheck`.
- Core wire models are generated from the core transport's OpenAPI schema via `mise run frontend:api:generate`; do not hand-edit `frontend/src/lib/core/generated/api.d.ts`.
- Preserve dual language support (English and zh-CN) when modifying UI copy.

## Commit Conventions

VoidCode follows [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/):

```text
<type>[optional scope][!]: <description>

[optional body]

[optional footer(s)]
```

- **Types**: `feat`, `fix`, `docs`, `refactor`, `test`, `build`, `ci`, `chore`, `perf`.
- **Breaking Changes**: Indicated by a `!` before the colon (e.g., `feat(core)!: require explicit workspace`) or a `BREAKING CHANGE:` footer.
- **Descriptions**: Concise, imperative, and focused on the observable change.

Examples:
- `feat(core): persist sessions in sqlite`
- `fix(core): handle unknown session ids`
- `docs: add architecture overview guide`
- `feat(plugins)!: require state schema on owner state`
