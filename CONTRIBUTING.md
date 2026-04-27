# Contributing to VoidCode

Thanks for contributing to VoidCode. The project is still pre-MVP, so clear communication, focused changes, and repeatable local verification matter more than speed.

## Development setup

The recommended local environment uses `uv` for Python and Bun for the frontend. Supported Python version: **3.13**.

```bash
mise install
uv sync --extra dev
mise run frontend:install
uv run voidcode --help
```

Optional but recommended:

```bash
uv run pre-commit install
```

## Code quality and verification

See [`docs/coding-standards.md`](./docs/coding-standards.md) for repository coding standards.

Current toolchain:

### Python
- **Ruff** for linting and formatting
- **basedpyright** for static type checking
- **pytest** for tests

### Frontend
- **Bun** as the package manager and task runner
- **ESLint** for linting
- **TypeScript** for type checking
- **Vitest** for tests

Run the standard checks with `mise`:

```bash
mise run lint
mise run format
mise run typecheck
mise run test:fast
mise run test
mise run test:coverage
mise run build
mise run frontend:lint
mise run frontend:typecheck
mise run frontend:test
mise run frontend:e2e
mise run check
mise run ci
mise run pre-commit
```

When needed, you can also invoke the Python tooling directly:

```bash
uv run ruff check .
uv run ruff format .
uv run basedpyright --warnings src
uv run pytest -n auto
uv run pytest -n auto --cov=voidcode --cov-report=term-missing
uv run pre-commit run --all-files
```

## Testing expectations

Please add or update tests when behavior changes.

- Run the relevant local checks before opening a pull request.
- Prefer `mise run test:fast` while iterating, then run the relevant full or coverage-bearing task before asking for review.
- Keep linting and type checking clean.
- If you change CLI, runtime, graph, tool, or transport behavior, add test coverage where an existing test surface already exists.
- For frontend changes, run the relevant Bun-based checks as well.

## Documentation language policy

- Public-facing documents in the repository root should be written in English.
- Internal design and planning documents under `docs/` may remain in Chinese.
- If behavior or workflow changes, update the affected user-facing documentation.

## Pull request process

1. Start from an up-to-date branch.
2. Keep the change focused and explain the motivation in the PR description.
3. Run the relevant lint, typecheck, test, and pre-commit checks locally before requesting review.
4. Update documentation when behavior, workflow, or contributor expectations change.
5. Address review feedback with follow-up commits.

## Commit messages

Use Conventional Commits as described in [`docs/coding-standards.md`](./docs/coding-standards.md).

Examples:

- `feat(runtime): persist sessions in sqlite`
- `fix(cli): handle unknown session ids`
- `docs: refresh contribution guide`

## Code of conduct

By participating in this project, you agree to follow [`CODE_OF_CONDUCT.md`](./CODE_OF_CONDUCT.md).

## Security

Do not open public issues for security-sensitive reports. Follow the reporting instructions in [`SECURITY.md`](./SECURITY.md).
