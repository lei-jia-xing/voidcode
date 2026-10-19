# Configuration Guide

VoidCode is configured by exactly one file per workspace: `voidcode.json` at the workspace root, read by the kernel at startup. The kernel has no configuration CLI, no user-level configuration file, and no per-run override.

Configuration is adopted as a *revision*: the first start of a workspace with an empty state directory adopts the live file, and a later restart restores that persisted revision (and its private snapshot) instead of silently re-reading the workspace. To apply an edited `voidcode.json`, run the reserved `kernel.reload` command on an idle session (`voidcode catalog` prints it; `voidcode invoke <session_id> kernel.reload --expected-revision <revision_id>`); it re-admits the live file and commits a new revision (refused while the session is busy, and refused entirely under `policy.read_only`). Pointing `--state-dir` at a new directory adopts the current file as a fresh first revision.

Plugins may additionally read their own resources (for example agent profiles under `$XDG_CONFIG_HOME/voidcode/agents`), but that never overrides `voidcode.json`.

## Configuration File

`voidcode.json` is required — the kernel refuses to start without it — and is validated strictly: the payload must be a JSON object, must not exceed 256 KiB, and any unknown field inside `voidcode.json`, `model`, or `policy` is an error rather than a silently ignored key.

```json
{
  "schema_version": 1,
  "model": {
    "model": "openai/gpt-4o-mini",
    "reasoning_effort": "medium"
  },
  "policy": {
    "approval_mode": "ask",
    "read_only": false
  },
  "providers": {
    "openai": {
      "api_key": "sk-...",
      "base_url": "https://api.openai.com/v1",
      "timeout_seconds": 30
    }
  },
  "plugins": {
    "files": { "enabled": true },
    "providers": { "enabled": true },
    "effectiveness": { "enabled": false },
    "my-linter": {
      "enabled": true,
      "path": "../voidcode-plugins/my-linter",
      "config": { "strict": true }
    }
  }
}
```

The `my-linter` entry illustrates an external plugin: the directory must exist, and starting the kernel against it needs `--trust-plugin my-linter`.

### Top-Level Fields

| Field | Type | Description |
|---|---|---|
| `schema_version` | integer | Required to be `1` when present (the default). Any other value is refused. |
| `model` | object | Model selection: `model` and `reasoning_effort`. |
| `policy` | object | Execution policy: `approval_mode` and `read_only`. |
| `providers` | object | Endpoint configuration for provider names declared by admitted plugins. |
| `plugins` | object | Per-plugin enablement, configuration, and source override. |

No other top-level field is accepted.

### `model`

| Field | Type | Description |
|---|---|---|
| `model` | string | `provider/model`, where `provider` must be a key in the root `providers` section *and* a name some admitted plugin declares as a provider. A model that names an unconfigured or undeclared provider is refused at admission. |
| `reasoning_effort` | string | One of `low`, `medium`, `high`, `none`. |

### `policy`

| Field | Type | Description |
|---|---|---|
| `approval_mode` | string | `ask` (default), `write`, or `yolo`. |
| `read_only` | boolean | Default `false`. When true, externally mutating effects (`WRITE`, `EXECUTE`, `SPAWN`) are refused outright. |

Policy semantics:

- **`ask`** — externally mutating work (`WRITE`, `EXECUTE`, `SPAWN`) waits for an explicit approval; reads and internal session state changes proceed.
- **`write`** — file mutations proceed; command execution and process spawning still wait.
- **`yolo`** — mutating work proceeds without a gate.
- **`read_only`** — checked before approval mode, so it is stronger than `yolo`: a mutating tool is refused rather than prompted. `READ` and `SESSION` effects are never refused.

There is no per-invocation override (no `--model`, `--approval-mode`, or in-session policy command): the active policy comes from `voidcode.json` for the workspace.

### `providers`

Each key is a provider name matching `[a-z][a-z0-9_-]*`, and it must be declared by an admitted plugin — configuring a provider nobody declares is an error, not a no-op.

| Field | Type | Description |
|---|---|---|
| `api_key` | string | Optional. When omitted, the kernel falls back to `<PROVIDER>_API_KEY` in the environment, uppercased with `-` replaced by `_` (for example `openai` → `OPENAI_API_KEY`). |
| `base_url` | string | Required for every configured provider. |
| *any other field* | any JSON value | Passed to the declaring plugin verbatim and validated there against that plugin's own configuration schema. The kernel assigns no vendor meaning to them: for OpenAI-wire providers the plugin accepts `organization`, `project`, `timeout_seconds`, and `transient_retry`. `api_key` and `base_url` may not be repeated here. |

Credentials never reach events, revisions, or the workspace: they are written only into the private 0600 snapshot inside the state directory, while revisions and events carry a digest.

### `plugins`

Each key is a plugin id matching `[a-z][a-z0-9_-]*`.

| Field | Type | Description |
|---|---|---|
| `enabled` | boolean | Default `true`. A disabled plugin is not admitted; its tools, commands, and views disappear from the catalog. |
| `path` | string | Optional. A source directory resolved relative to the workspace, for plugins that do not ship with the kernel. A non-bundled id without a `path` is refused. |
| `config` | object | Plugin-owned configuration, validated against the plugin manifest's `config_schema`. Provider credentials belong in the root `providers` section; a plugin `config` that repeats a `providers` key is refused. |

Bundled plugin ids (those shipped in `src/voidcode/plugins/`) need no entry at all: they are enabled with an empty configuration by default, and an entry only exists to disable one, configure it, or point it at a different source.

## Plugin Trust

Trust is a startup decision about a concrete source path, never something a running session can grant itself:

- **Bundled ids** are trusted by construction — they ship with the kernel, so trusting them says nothing about user code.
- **External sources** (`plugins.<id>.path`) require `--trust-plugin <id>` on the command line that starts the kernel:

  ```bash
  uv run python -m voidcode.kernel run "summarize the workspace" --trust-plugin my-linter
  ```

  The flag authorizes the *canonical path the configuration resolves to for that id*, and that authorization is recorded in the revision's private snapshot. A restart of that revision restores the pinned artifact from the snapshot without needing the flag again; pointing the id at a different directory is a different path and needs a new explicit trust decision (and, because the persisted revision is restored on restart, a reload or a fresh state directory to take effect).
- **`kernel.reload`** re-admits the live configuration, but only trusts source paths already authorized in the current generation. Adding a brand-new external plugin is therefore a restart with `--trust-plugin`, not a reload.
- Trust is about source bytes, not sandboxing: an admitted plugin runs in its own host process with the capability set the kernel grants (`workspace.read`, `workspace.write`, `network`), and a manifest whose requests exceed that set is refused at admission.

Changed plugin source, changed configuration, or a changed SDK is never admitted implicitly: an edited plugin file produces a new identity, and a restart against a revision whose SDK hash no longer matches fails with an explicit "controlled kernel upgrade" error instead of silently running new code under an old revision.

## State Directory

Kernel state lives outside the workspace, at:

```text
$XDG_STATE_HOME/voidcode/kernel-v1/<sha256-of-the-workspace-path>/
```

(default `$XDG_STATE_HOME` is `~/.local/state`). It contains:

- `kernel-v1.sqlite3` — sessions, append-only events, checkpoints, capability revisions, and confirmation records;
- `artifacts/<identity>/` — the pinned copies of admitted plugin sources;
- `private/<revision>.json` — the 0600 configuration snapshot for one revision, the only place provider credentials are stored.

`--state-dir PATH` overrides the derived directory. The state directory must not be inside the workspace: the kernel refuses a state directory that the workspace's file tools could write to.

Sessions are per-workspace: `voidcode sessions` and `voidcode show` only report sessions whose stored workspace matches, and a session belonging to another workspace is refused.

## Legacy Configuration and State

Legacy configuration is rejected, never converted:

- A workspace with a legacy `.voidcode.json` and no `voidcode.json` fails with an explicit error (`legacy .voidcode.json is not supported; create voidcode.json explicitly`). Nothing is read from the legacy file, and no converter exists.
- Legacy session/state databases from the pre-kernel architecture are neither imported nor auto-deleted. The kernel never reads them. Removing that historical data is a manual user action with `rm`; there is no cleanup command.
