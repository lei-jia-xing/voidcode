# Configuration Guide

VoidCode is configured by exactly one file per workspace: `voidcode.json` at the workspace root, read by the core at startup. The core has no configuration CLI, no user-level configuration file, and no per-run override.

The file is optional only for the very first boot of a truly fresh state directory: with no database, no adopted revision, and no legacy file, the core starts with empty in-memory defaults (bundled plugins enabled, no model or provider) and never creates `voidcode.json`. Commands, tools, sessions, `/status`, and `/help` work in that state; model turns and governed model queries fail with configuration guidance until a valid file exists and `core.reload` is run. Every other start requires the strict file rules below.

Configuration is adopted as a *revision*: the first start of a workspace with an empty state directory adopts the live file, and a later restart restores that persisted revision (and its private snapshot) instead of silently re-reading the workspace. To apply an edited `voidcode.json`, run the reserved `core.reload` command on an idle session (`voidcode catalog` prints it; `voidcode invoke <session_id> core.reload --expected-revision <revision_id>`); it re-admits the live file and commits a new revision (refused while the session is busy, and refused entirely under `policy.read_only`). Pointing `--state-dir` at a new directory adopts the current file as a fresh first revision.

Plugins may additionally read their own resources (for example agent profiles under `$XDG_CONFIG_HOME/voidcode/agents`), but that never overrides `voidcode.json`.

## Configuration File

`voidcode.json` is required for every start except the providerless first boot described above, and is validated strictly whenever it exists: the payload must be a JSON object, must not exceed 256 KiB, and any unknown field inside `voidcode.json`, `model`, or `policy` is an error rather than a silently ignored key. A legacy `.voidcode.json` with no `voidcode.json` is always rejected explicitly, including on first boot.

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

The `my-linter` entry illustrates an external plugin: the directory must exist and export `create_plugin` from its `__init__.py`.

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
| `model` | string | `provider/model`, where `provider` must be a key in the root `providers` section *and* a name some admitted plugin declares as a provider. A model that names an unconfigured or undeclared provider is refused at admission. Optional only in the sense that the default is absent: the core then opens providerless and model turns fail with guidance until `model` and `providers` are configured and `core.reload` runs. |
| `reasoning_effort` | string | One of `low`, `medium`, `high`, `none`. |

### `policy`

| Field | Type | Description |
|---|---|---|
| `approval_mode` | string | `ask` (default), `write`, or `yolo`. |
| `read_only` | boolean | Default `false`. When true, `WRITE` and `EXECUTE` effects are refused outright; `READ`, `SESSION`, and `SPAWN` are not refused by this setting alone (see below). |

Policy semantics:

- **`ask`** — externally mutating work (`WRITE`, `EXECUTE`, `SPAWN`) waits for an explicit approval; reads and internal session state changes proceed.
- **`write`** — file mutations proceed; command execution and process spawning still wait.
- **`yolo`** — mutating work proceeds without a gate.
- **`read_only`** — checked before approval mode, so it is stronger than `yolo`: a tool that declares `WRITE` or `EXECUTE` is refused outright rather than prompted. It is not a blanket "no mutation" mode: `READ` and `SESSION` effects are never refused, and a `SPAWN`-only tool stays available because a restricted session may delegate only to a child that inherits the same restriction, which the core records on that child.

There is no per-invocation override (no `--model`, `--approval-mode`, or in-session policy command): the active policy comes from `voidcode.json` for the workspace.

### `providers`

Each key is a provider name matching `[a-z][a-z0-9_-]*`, and it must be declared by an admitted plugin — configuring a provider nobody declares is an error, not a no-op.

| Field | Type | Description |
|---|---|---|
| `api_key` | string | Optional. When omitted, the core falls back to `<PROVIDER>_API_KEY` in the environment, uppercased with `-` replaced by `_` (for example `openai` → `OPENAI_API_KEY`). |
| `base_url` | string | Required for every configured provider. |
| *any other field* | any JSON value | Passed to the declaring plugin verbatim and validated there against that plugin's own configuration schema. The core assigns no vendor meaning to them: for OpenAI-wire providers the plugin accepts `organization`, `project`, and `timeout_seconds`. `api_key` and `base_url` may not be repeated here. |

Credentials never reach events or revisions, which carry digests only: resolved credentials are written only into the private 0600 snapshot inside the state directory. The core never writes environment-resolved keys back to the workspace; an `api_key` written explicitly in `voidcode.json` stays where the user put it.

For the Google provider, `auth: {"method": "oauth", "access_token": "..."}` uses Vertex AI OAuth credentials and requires `project`; `region` is optional and follows the SDK default. It sends a Bearer token, does not use `x-goog-api-key`, and does not fall back to an environment API key. Supply the current access token; this configuration does not perform an interactive login or refresh-token exchange. API-key and service-account authentication remain separate methods.

### `plugins`

Each key is a plugin id matching `[a-z][a-z0-9_-]*`.

| Field | Type | Description |
|---|---|---|
| `enabled` | boolean | Default `true`. A disabled plugin is not admitted; its tools, commands, and views disappear from the catalog. |
| `path` | string | Optional. A source directory resolved relative to the workspace, for plugins that do not ship with the core. A non-bundled id without a `path` is refused. |
| `config` | object | Plugin-owned configuration, validated by the plugin itself. Provider credentials belong in the root `providers` section; the core injects those into every factory under the reserved `providers` key, which plugin-owned config must never use. |

Bundled plugin ids (those shipped in `src/voidcode/plugins/`) need no entry at all: they are enabled with an empty configuration by default, and an entry only exists to disable one, configure it, or point it at a different source.

Agent discovery and `/agents`/`agents.show` belong to `delegation`, along with
`task`/`task_batch`. Agent files use `.claude/agents/` and the supported Claude Code
Markdown format. See [agent definitions](contracts/agent-definitions.md) for the
supported fields and explicit runtime limitations.

### MCP servers

The bundled `mcp` plugin validates `plugins.mcp.config` strictly. Unknown fields,
incorrect types and non-positive/non-finite request timeouts are rejected before
connecting. For example:

```json
{
  "plugins": {
    "mcp": {
      "config": {
        "request_timeout_seconds": 30,
        "servers": {
          "local": {"command": ["python", "server.py"]},
          "remote": {"transport": "remote-http", "url": "http://127.0.0.1:8000/mcp"}
        }
      }
    }
  }
}
```

`stdio` is the default transport and requires a non-empty argv `command`; it may
also declare a string-to-string `env` map. `remote-http` requires an absolute
HTTP(S) `url` and does not accept `command` or `env`. A server entry has no other
fields. `/mcp` reads the currently known inventory without connecting; tools are
discovered when used. Discovery is remembered only for the current plugin runtime.
Tool schemas use JSON Schema Draft 2020-12; local references work, while external
schema references are never fetched during argument validation.

### LSP servers

`plugins.lsp.config` accepts `enabled` (boolean, default `true`) and `servers`.
Each server may declare `preset`, argv `command`, string arrays `extensions`,
`languages`, `root_markers`, and objects `settings` and `init_options`. Unknown
fields and invalid values are rejected rather than filtered out. A server needs
a command or a known preset; an empty server set retains workspace-based default
discovery. `enabled: false` disables LSP explicitly.

### Scheduled prompts

The bundled Cron tools accept `every <N><s|m|h|d>`, `at <ISO-8601>`, `once`, or
`cron <minute> <hour> <day> <month> <weekday>`. Cron expressions use croniter's
five-field syntax, including names such as `JAN`/`MON` and supported modifiers.
Six/seven-field expressions are refused. Calendar expressions follow the host's
local timezone. `/loop` accepts intervals; use `cron_delete` to cancel a schedule.

## API Environment Variables

Export API credentials in the environment of the process that starts VoidCode; no `.env` file is required.

### Model providers

For a configured provider, omit `providers.<name>.api_key` to use `<PROVIDER>_API_KEY`: uppercase the provider name and replace `-` with `_`. For example, `openai` uses `OPENAI_API_KEY`, `deepseek` uses `DEEPSEEK_API_KEY`, and `github-copilot` uses `GITHUB_COPILOT_API_KEY`. An explicit `api_key` in `voidcode.json` takes precedence.

```bash
export OPENAI_API_KEY="sk-..."
voidcode run "Summarize this repository"
```

The provider entry, its `base_url`, and the selected `model` still belong in `voidcode.json`; setting an API key alone does not configure a provider. Provider credentials are captured when a revision is admitted and restored from its private snapshot on restart. To adopt a changed key, export it before starting the core process and invoke `core.reload`; changing another shell's environment does not affect an already-running core.

### Web search APIs

The bundled `web_search` tool selects the first available backend in this order:

| Backend | Environment variables |
|---|---|
| Exa | `EXA_API_KEY` |
| Tavily | `TAVILY_API_KEY` |
| Brave | `BRAVE_API_KEY` |
| DeepSeek | `DEEPSEEK_API_KEY` |
| TinyFish | `TINYFISH_API_KEY` |
| Kagi | `KAGI_API_KEY` |
| Firecrawl | `FIRECRAWL_API_KEY`; `FIRECRAWL_BASE_URL` (or `FIRECRAWL_API_URL`) selects a self-hosted endpoint and also enables keyless use. |
| SearXNG | `SEARXNG_ENDPOINT`; optional `SEARXNG_BASIC_USERNAME` + `SEARXNG_BASIC_PASSWORD`, or `SEARXNG_TOKEN` for bearer authentication. Basic authentication takes precedence when both fields are set. |
| Jina | Optional `JINA_API_KEY`; the final fallback supports keyless use at low volume. |

These are read directly from the core process environment by the web plugin, independently of the model provider credentials in the revision snapshot.

## Plugins are trusted code

There is no trust flag, no manifest, and no sandbox: configuring a plugin id
means loading that source as code equal to the application, in the core process
process. A restart imports the *current* source with the *recorded*
configuration; `core.reload` re-admits the *live* configuration as a new
generation in-session.

Changed configuration, a changed SDK, or a different core image is never
admitted implicitly: a restart against a revision whose SDK protocol or image
pin no longer matches fails with an explicit "controlled core upgrade"
error instead of silently running new code under an old revision. The
explicit path is `voidcode upgrade --candidate <install>` (preflight before
downtime, fenced drain, one atomic adoption transaction, health-checked
election); see `docs/contracts/core-upgrade.md`.

## State Directory

Core state lives outside the workspace, at:

```text
$XDG_STATE_HOME/voidcode/<sha256-of-the-workspace-path>/
```

(default `$XDG_STATE_HOME` is `~/.local/state`). It contains:

- `voidcode.db` — sessions, append-only events, checkpoints, revisions, and confirmation records;
- `private/<revision>.json` — the 0600 configuration snapshot for one revision, the only place provider credentials are stored.

`--state-dir PATH` overrides the derived directory. The state directory must not be inside the workspace: the core refuses a state directory that the workspace's file tools could write to.

Sessions are per-workspace: `voidcode sessions` and `voidcode show` only report sessions whose stored workspace matches, and a session belonging to another workspace is refused.

## Legacy Configuration and State

Legacy configuration is rejected, never converted:

- A workspace with a legacy `.voidcode.json` and no `voidcode.json` fails with an explicit error (`legacy .voidcode.json is not supported; create voidcode.json explicitly`). Nothing is read from the legacy file, and no converter exists.
- Legacy session/state databases from the pre-core architecture are neither imported nor auto-deleted. The core never reads them. Removing that historical data is a manual user action with `rm`; there is no cleanup command.

## Built-in capabilities with plugins disabled

The core always supplies file read/write/edit/search, `shell_exec`, and configured model
adapters. Optional plugins may replace those names or add other capabilities. Disabling
`files`, `exec`, or `providers` removes that adapter, but leaves the core defaults available;
use the policy and session tool scope to restrict execution or writes.

`plugins: {}` keeps the bundled default set; it does not request zero plugins. To admit none,
set `enabled: false` for each bundled id listed in `core/activation.py` (`BUNDLED_PLUGIN_IDS`).
The `model` and `providers` configuration remains necessary for model calls, even with no
plugins. Core defaults use the same provider configuration and authentication as the adapters.

## Model catalog updates

Model specifications and base rates are generated from the official
[models.dev API](https://models.dev/api.json), rather than edited model by model.
Run `uv run python scripts/generate_model_catalog.py` to refresh the packaged
snapshot; `--source api.json` accepts a saved official response for reproducible
offline generation. Missing source facts remain unknown. The application reads
the packaged snapshot and does not need network access for catalog lookup.

`.github/workflows/update-model-catalog.yml` runs daily and can also be triggered
manually. It validates the generated data and provider behavior, then creates or
updates one catalog refresh PR. GitHub Actions must be allowed to create PRs in
the repository settings. No automatic merge is configured. Protocol identifiers also come from upstream model/provider metadata.
Thinking effort/none/interleaved fields are also generated upstream; only SDK
parameter spellings and budget conversions remain as local protocol defaults. Long-context price tiers are generated
from the same models.dev response as each model's base rates.
