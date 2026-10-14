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

- **`src/voidcode/runtime/`**: Owns session lifecycle, permissions, approvals, SQLite persistence, and hooks.
- **`src/voidcode/core/`**: Host-agnostic turn progression through `TurnEngine`.
- **`src/voidcode/tools/`**: Built-in tool implementations.
- **`src/voidcode/common/`**: Shared data modeling, snapshotting, and template parsing.
- **`src/voidcode/provider/`**: Provider adapters for upstream LLM APIs.

## Python Standards

- Target Python 3.14. Prefer standard library solutions and existing dependencies.
- Code formatting and linting are enforced by Ruff (`mise run lint`, `mise run format`).
- Static type checking is enforced by ty (`mise run typecheck`).
- Add or update tests whenever observable behavior changes.

## Frontend Standards

- Built with Bun, Vite, and React 19 in `frontend/`.
- Validate with `bun run lint` and `bun run typecheck`.
- Backend wire models are generated from OpenAPI schemas via `mise run frontend:api:generate`; do not hand-edit `src/lib/runtime/generated/api.d.ts`.
- Preserve dual language support (English and zh-CN) when modifying UI copy.

## Commit Conventions

VoidCode follows [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/):

```text
<type>[optional scope][!]: <description>

[optional body]

[optional footer(s)]
```

- **Types**: `feat`, `fix`, `docs`, `refactor`, `test`, `build`, `ci`, `chore`, `perf`.
- **Breaking Changes**: Indicated by a `!` before the colon (e.g., `feat(runtime)!: require explicit workspace`) or a `BREAKING CHANGE:` footer.
- **Descriptions**: Concise, imperative, and focused on the observable change.

Examples:
- `feat(runtime): persist sessions in sqlite`
- `fix(cli): handle unknown session ids`
- `docs: add architecture overview guide`
- `feat(api)!: require schema v2 in request body`
