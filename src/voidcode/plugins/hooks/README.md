# hooks

User-configured argv commands gating tools and observing session lifecycle.

## Surface

No tools, commands, views, or queries. Registers `pre_tool` (gate), `post_tool` (note), `session_start`, and `session_end` callbacks only for surfaces that have commands configured. Notes are bounded to 2000 characters; hook commands inherit `VOIDCODE_RUNNING_TOOL_HOOK` as a recursion guard.

## Configuration

Plugin-only keys (strict: unknown keys are rejected):

- `enabled` (bool, default `true`): `false` disables every surface.
- `timeout_seconds` (default `30.0`): per-command timeout.
- `pre_tool`, `post_tool`, `session_start`, `session_end`: lists of argv commands (`[[argv...], ...]`, each a non-empty array of non-empty strings).
- `pre_tool_match`, `post_tool_match`: tool-name glob filters (stdlib `fnmatch`, case-sensitive); empty matches every tool (config default). The registration falls back to `("*",)` so every tool reaches the config filter.

See [configuration](../../../../docs/configuration.md) for global config. The product-injected `providers` key (product bootstrap hands every factory the resolved map) is accepted and ignored.

## Behavior and limits

- `pre_tool` fails closed: a broken gate blocks the call, and a `cancel` action blocks with the hook's diagnostic (or a default message). `post_tool` fails open: a broken note never fails a write that already succeeded. Lifecycle hooks are fail-open side effects whose only record is the returned note.
- A surface with no commands is a fast no-op — no subprocess is spawned. The plugin reads nothing through its service lease; hook commands receive workspace, session, tool, and phase context.
