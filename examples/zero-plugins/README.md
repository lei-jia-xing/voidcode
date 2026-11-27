# Zero-plugin coding agent

Edit `voidcode.toml` with an OpenAI-compatible endpoint, its model id, and credentials.
All bundled plugins are explicitly disabled. The core still supplies model calls,
file read/write/edit/search, and `shell_exec`; mutations use the default approval policy.

From the repository root:

```bash
uv run voidcode --workspace examples/zero-plugins
```

For your own project, copy this `voidcode.toml` into its workspace root and update
the provider configuration. Optional plugin commands, views, background-process management,
LSP, skills, and delegated agents are absent. The core's file/model/shell implementations
are the same ones the optional adapters use.
