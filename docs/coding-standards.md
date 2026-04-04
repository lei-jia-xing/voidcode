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
- Use Ruff formatting/linting and keep mypy clean.
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
