# Coding Standards

VoidCode is still pre-MVP. Favor clarity, small changes, and repeatable verification over cleverness.

## General expectations

- Keep changes focused and reviewable.
- Avoid unrelated refactors in feature branches.
- Update documentation when behavior, workflows, or CLI surfaces change.
- Prefer explicit, typed code over implicit behavior.

## Python

- Match the existing runtime/graph/tools boundaries.
- Keep functions small and deterministic where practical.
- Use Ruff formatting/linting and keep basedpyright clean.
- Add or update tests when behavior changes.
- Avoid introducing new dependencies unless they are necessary.

## Frontend

- Keep the Bun/Vite/React stack consistent with the current shell.
- Preserve EN/zh-CN support when changing user-facing text.
- Keep state flows simple and explicit.
- Do not commit generated frontend artifacts.

## Pull requests

- Run the relevant checks before opening a PR.
- Include manual QA evidence for CLI or workflow changes.
- Keep commits atomic so they can be reviewed and reverted independently.

## Commits

- Follow the [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/) format.
- Use the structure `<type>[optional scope][!]: <description>`.
- `type` is required. `scope` is optional. Keep the description concise and imperative.
- Use `feat` for new features and `fix` for bug fixes. Common additional types include `docs`, `refactor`, `test`, `build`, `ci`, `chore`, `perf`, and `style`.
- Mark breaking changes with `!` before the colon, a `BREAKING CHANGE:` footer, or both.
- Add a body or footer only when extra context is useful.

Examples:

- `feat(runtime): persist sessions in sqlite`
- `fix(cli): handle unknown session ids`
- `docs: update development guide`
- `feat(api)!: remove deprecated response shape`
