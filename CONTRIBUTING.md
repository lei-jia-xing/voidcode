# Contributing to VoidCode

Welcome, and thank you for your interest in contributing to VoidCode! VoidCode is an open-source, local-first coding agent runtime, and we welcome contributions ranging from bug fixes and documentation improvements to new features and test coverage.

## Getting Started

### Prerequisites

- **Python**: Version 3.14
- **uv**: Fast Python package installer and resolver
- **Bun**: JavaScript runtime and package manager for frontend assets
- **mise**: (Recommended) Polyglot toolchain and task runner

### Environment Setup

1. Clone the repository:
   ```bash
   git clone https://github.com/lei-jia-xing/voidcode.git
   cd voidcode
   ```

2. Install toolchain versions and Python dependencies:
   ```bash
   mise install
   uv sync --extra dev --extra syntax
   ```

3. Install frontend dependencies:
   ```bash
   mise run frontend:install
   ```

4. Install pre-commit hooks (optional but recommended):
   ```bash
   uv run pre-commit install
   ```

## Development Workflow

### Useful Commands

Common development tasks are configured in `mise.toml`:

```bash
# Code Quality
mise run lint            # Check Python code with Ruff
mise run format          # Format Python code with Ruff
mise run typecheck       # Static typecheck with ty

# Testing
mise run test            # Run unit and integration tests
mise run test:coverage   # Run tests with terminal coverage

# Frontend
mise run frontend:dev    # Start Vite dev server
mise run frontend:lint   # Lint frontend code
mise run frontend:test   # Run Vitest tests

# Full Verification
mise run check           # Run all lint, typecheck, schema, test, and frontend checks
mise run pre-commit      # Run all pre-commit hooks
```

## Contribution Guidelines

- **Architecture & Design**: Before making structural changes, read the [Architecture Overview](./docs/architecture.md) to understand ownership boundaries between runtime, core engine, tools, and common utilities.
- **Coding Standards**: See [Coding Standards](./docs/coding-standards.md) for detailed style rules, typing expectations, and error handling principles.
- **Testing**: Review the [Testing Policy](./docs/testing.md). Add or update tests for any observable behavior changes.
- **Commit Messages**: Follow the [Conventional Commits](./docs/coding-standards.md#commit-conventions) specification (`feat(runtime): ...`, `fix(cli): ...`).

## Pull Request Process

1. Create a descriptive branch from `master` (`git checkout -b feat/my-feature`).
2. Implement your changes with accompanying tests and documentation.
3. Verify that `mise run check` and `mise run pre-commit` pass cleanly.
4. Submit a Pull Request clearly explaining the motivation, changes made, and verification steps exercised.
