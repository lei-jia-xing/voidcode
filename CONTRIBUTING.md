# Contributing to VoidCode

Thanks for contributing to VoidCode. The project is still pre-MVP, so clear communication, small reviewable changes, and repeatable local verification matter more than raw speed.

## Development setup

Preferred local setup uses a uv-managed Python environment. Python 3.14 is the supported version.

```bash
mise install
uv sync --extra dev
uv run voidcode --help
```

Optional but recommended:

```bash
uv run pre-commit install
```

## Code style and quality gates

See [`docs/coding-standards.md`](./docs/coding-standards.md) for the repository coding standards.

VoidCode currently uses:

### Python
- **Ruff** for linting and formatting
- **basedpyright** for static type checking
- **pytest** for tests

### Frontend (Bun)
- **ESLint** for linting
- **Prettier** for formatting
- **TypeScript** for type checking

Run the standard checks with `mise`:

```bash
mise run lint
mise run format
mise run typecheck
mise run test
mise run check
mise run pre-commit
```

Direct `uv` commands are also available when needed:

```bash
uv run ruff check .
uv run ruff format .
uv run basedpyright --warnings src
uv run pytest
uv run pre-commit run --all-files
```

## Testing expectations

Please include or update tests for behavior changes whenever practical.

- Run `pytest` locally before opening a pull request.
- Keep type checking and linting clean.
- If you add or change CLI, runtime, graph, or tool behavior, include coverage for the new behavior when the test surface exists.

## Pull request process

1. Start from an up-to-date branch.
2. Keep changes focused and explain the reasoning in the PR description.
3. Run linting, type checking, tests, and pre-commit locally before requesting review.
4. Update user-facing documentation when behavior or workflows change.
5. Wait for review and address feedback with follow-up commits.

## Code of conduct

By participating in this project, you agree to follow the guidelines in [`CODE_OF_CONDUCT.md`](./CODE_OF_CONDUCT.md).

## Security issues

Please do not open public issues for security-sensitive reports. Follow the reporting guidance in [`SECURITY.md`](./SECURITY.md).
