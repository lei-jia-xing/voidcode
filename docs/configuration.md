# Configuration Guide

VoidCode is configured by exactly one file per workspace: `voidcode.json` at the workspace root, read by the product layer at startup. There is no configuration CLI, no user-level configuration file, and no per-run override.

A fresh state directory (no database or adopted revision) starts with bundled plugins enabled and no selected model; supported environment credentials can supply providers without a file. It never creates `voidcode.json`. Restarts restore the recorded configuration and private snapshot without reading new environment keys or the live file. The live file is required for explicit `/core reload`, or when a database exists without an adopted revision. `/status` and `/help` work without a model; model turns require a selected `model.model` and an available provider.

Configuration is adopted as a *revision*. To apply an edited `voidcode.json` or changed environment credentials, run `/core reload` on an idle session (`voidcode invoke <session_id> core.reload --expected-revision <revision_id>` from the CLI). It re-admits the live file and commits a new revision; busy sessions and `policy.read_only` refuse the reload. A new `--state-dir` adopts the current file and environment as a fresh first revision.
Before upgrading from a build that bundles `effectiveness`, set `plugins.effectiveness.enabled` to `false` in `voidcode.json`, then run `/core reload` and confirm the revision commits while still on the old build. Editing the file alone is insufficient: restart restores the recorded revision, not the live file.

Plugins may additionally read their own resources (for example agent profiles under `$XDG_CONFIG_HOME/voidcode/agents`), but that never overrides `voidcode.json`.

## Configuration File

`voidcode.json`, when it is read, is validated strictly: the payload must be a JSON object, must not exceed 256 KiB, and any unknown field is an error rather than a silently ignored key. Only `voidcode.json` is ever read; any other filename (including a legacy `.voidcode.json`) is ignored, not detected or converted.

```json
{
  "schema_version": 1,
  "model": {
    "model": "openai/gpt-4o-mini",
    "reasoning_effort": "medium"
  },
  "policy": {
    "approval_mode": "ask",
    "read_only": false,
    "max_root_children": 4
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
| `policy` | object | Execution policy: `approval_mode`, `read_only`, and `max_root_children`. |
| `sandbox` | object | Optional transparent exec backend: `mode`, `network`, `writable_roots`. Absent means unconfined host exec. |
| `providers` | object | Endpoint configuration for provider names declared by admitted plugins. |
| `plugins` | object | Per-plugin entries: `enabled`, `path`, `config`. |
| `tui` | object | Optional local TUI chrome prefs: `border`, `footer`, `show_session`, `banner`. Display-only: never admitted, never snapshotted, zero revision churn. |

No other top-level field is accepted.

### `tui`

Optional. Read by the terminal client only (embedded: the workspace file; remote `--server`: the client's local cwd file best-effort, never the server's block). CLI flags win per-field over the file, which wins over defaults. A malformed file never refuses TUI launch: prefs degrade to defaults.

| Field | Type | Description |
|---|---|---|
| `border` | string | `lines` (default, horizontal rules), `box`, or `borderless` (`--no-border` forces borderless). |
| `footer` | string | `full` (default: workspace · model · effort), `compact` (model · effort), or `off` (context row hidden; gates/notices still show). `--footer` overrides. |
| `show_session` | boolean | Default `false`. `true` adds the short session id to the full footer. `--show-session` forces true. |
| `banner` | object or null | Empty-session welcome artwork and palette; `null` disables it. Default: a Unicode V in blue-violet/cyan. |

The banner uses a centered header layout inspired by [dashboard-nvim](https://github.com/nvimdev/dashboard-nvim), without a dashboard framework or theme dependency. It appears only on an empty session, never over replayed conversation.

```json
{
  "tui": {
    "banner": {
      "text": "██╗ ██╗\n╚███╔╝\n ╚═╝",
      "colors": ["#8b5cf6", "#06b6d4"]
    }
  }
}
```

`text` is optional; omitted or `null` uses the built-in V. Leading spaces and internal blank lines preserve the drawing's geometry. `colors` is a non-empty palette of `#RRGGBB` or the SDK's 16 ANSI color names, sampled from first to last across rows; one color is monochrome. Terminal controls are escaped, wide text is cell-clipped, and `NO_COLOR` removes color without replacing Unicode. These settings affect only the banner, not chat or composer styling.

### `model`

| Field | Type | Description |
|---|---|---|
| `model` | string | `provider/model`, where the provider is explicitly configured or discovered from its environment credential, and an admitted plugin declares it. Unconfigured or undeclared providers are refused at admission. No model is selected automatically; set this field and run `/core reload` to enable model turns. |
| `reasoning_effort` | string or integer | One of `off`, `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, or a non-negative token budget integer. |

### `policy`

| Field | Type | Description |
|---|---|---|
| `approval_mode` | string | `ask` (default), `write`, or `yolo`. |
| `read_only` | boolean | Default `false`. When true, `WRITE` and `EXECUTE` effects are refused outright; `READ`, `SESSION`, and `SPAWN` are not refused by this setting alone (see below). |
| `max_root_children` | integer | Default `4`, within `1..64`. The single governed *lifetime* quota for how many child sessions one chain root may create, including completed children; exhausted spawns fail visibly instead of queueing. Wide or repeated workflows need a larger value or a fresh session. |

Policy semantics:

- **`ask`** — externally mutating **tool** work (`WRITE`, `EXECUTE`, `SPAWN`) waits for an explicit approval; reads and internal session state changes proceed.
- **`write`** — file mutations proceed; tool command execution and process spawning still wait.
- **`yolo`** — mutating tool work proceeds without a gate.
- **`read_only`** — checked before approval mode, so it is stronger than `yolo`: a tool that declares `WRITE` or `EXECUTE` is refused outright rather than prompted. It is not a blanket "no mutation" mode: `READ` and `SESSION` effects are never refused, and a `SPAWN`-only tool stays available because a restricted session may delegate only to a child that inherits the same restriction, which the core records on that child.

Commands are a separate, client-invoked surface: they follow the same `read_only` effect refusal, but they have no approval-mode gate, no pre-tool-hook gate, and no tool-allowlist check. A human invoking a command is the consent boundary; automation with client credentials inherits that trust. The `user_shell` `!` escape is therefore available under `ask`/`write`/`yolo` and is only refused by `read_only`.

There is no per-invocation override (no `--model`, `--approval-mode`, or in-session policy command): the active policy comes from `voidcode.json` for the workspace.

### `sandbox`

Optional. When absent, registry children run unconfined on the host — today's behavior
exactly, with no observed effects. When present, every registry child (shell commands,
background jobs, user-shell escapes, LSP service processes) runs under OS-enforced
confinement, and the core merges supervisor-observed file changes into tool results
transparently: plugins make zero sandbox changes.

| Field | Type | Description |
|---|---|---|
| `mode` | string | `workspace-write` (default), `read-only`, or `danger-full-access`. |
| `network` | boolean | Default `false` (`true` under `danger-full-access`). `false` = private network namespace with no route; `true` = unfiltered direct egress, labeled as such. `danger-full-access` with explicit `false` is refused as contradictory. |
| `writable_roots` | list of strings | Default `[]`. Extra absolute writable roots beyond workspace+tmp. Non-empty under `read-only` is refused; any confinement option under `danger-full-access` is refused. |

No other field is accepted. A configured sandbox with a missing native backend (no
helper binary, no `bwrap` on Linux) refuses `open_core` with a clear error naming the
missing piece — there is no silent degrade to unconfined. `danger-full-access` runs the
supervisor unconfined: its observations are labeled best-effort, not ground truth, and no
isolation is claimed.

V1 boundary (honest, not hidden): hooks executor commands, review git diagnostics, and
MCP stdio servers stay host-side with zero plugin changes and are NEVER claimed
confined. Hook commands are trusted host-side code with the core process environment:
they run unconfined, outside the sandbox supervisor and approval gate, and declare no
tool effects (see the [hooks protocol](../src/voidcode/plugins/hooks/README.md)).
File tools stay host-side and self-report; the engine dedups overlap with
supervisor observations. Reload carries no gate: new spawns use the current policy, old
jobs keep the enforcement fixed at their spawn. Model-editing-config plus `yolo` reload
is out of threat model.

The bundled `web` tools run in the Core process, not as registry children: their egress
is not covered by `sandbox.network`, `NETWORK` is not approval-gated, and they remain
available under `read_only`/plan mode. Disable the `web` plugin (or install a pre-tool
gate) when that egress is not wanted.

### `providers`

Each key is a provider name matching `[a-z][a-z0-9_-]*`, and it must be declared by an admitted plugin — configuring a provider nobody declares is an error, not a no-op.

With the bundled `providers` plugin enabled, recognized non-blank credentials such as `OPENROUTER_API_KEY` and `DEEPSEEK_API_KEY` make those providers available without a `providers` entry. Names, credential aliases, and default endpoints come from the shipped provider table. This discovery is disabled when the bundled provider plugin is disabled or replaced; other plugins keep explicit provider configuration. Unknown environment variable names are not scanned.

| Field | Type | Description |
|---|---|---|
| `api_key` | string or null | Optional. Explicit values win, including `null` (no environment credential). Otherwise built-ins use their table-declared credential variables; explicitly configured names also accept `<PROVIDER>_API_KEY`, uppercased with `-` replaced by `_`. |
| `base_url` | string | Optional for built-ins, which use their own vendor endpoint. Custom provider names still require it. Explicit URLs override defaults; the `endpoint` provider also accepts `ENDPOINT_BASE_URL`. |
| *any other field* | any JSON value | Passed to the declaring plugin verbatim and validated there against that plugin's own configuration schema. The product layer assigns no vendor meaning to them: for OpenAI-wire providers the plugin accepts `organization`, `project`, and `timeout_seconds`. `api_key` and `base_url` may not be repeated here, and a nested `extra` object is forbidden — configure provider fields flat. |

Credentials never reach events or revisions, which carry digests only: resolved credentials are written only into the private 0600 snapshot inside the state directory. The core never writes environment-resolved keys back to the workspace; an `api_key` written explicitly in `voidcode.json` stays where the user put it.

`/model deepseek` reads the shipped catalog. `/model openrouter` reads current model IDs from the configured OpenAI-compatible `/models` endpoint because OpenRouter has no shipped catalog. Discovery is explicit, uses the recorded credential and endpoint, and makes no inference request. A failed listing is an error, not an empty catalog. An unknown provider id is refused. A known provider with a shipped catalog is listed without configuration; one with no shipped catalog must be configured in this workspace, otherwise it is refused instead of reported as an empty catalog.

For the Google provider, `auth: {"method": "oauth", "access_token": "..."}` uses Vertex AI OAuth credentials and requires `project`; `region` is optional and follows the SDK default. It sends a Bearer token, does not use `x-goog-api-key`, and does not fall back to an environment API key. Supply the current access token; this configuration does not perform an interactive login or refresh-token exchange. API-key and service-account authentication remain separate methods. A nested `auth` block is authoritative: table-declared environment credentials are not injected as a flat `api_key` alongside it. Google credentials must be configured explicitly (API key, OAuth token, or service-account file); ambient `GOOGLE_API_KEY` and application-default credentials are not read after admission.

### `plugins`

Each key is a plugin id matching `[a-z][a-z0-9_-]*`.

| Field | Type | Description |
|---|---|---|
| `enabled` | boolean | Default `true`. A disabled plugin is not admitted; its tools, commands, and views disappear from the catalog. |
| `path` | string | Optional. A source directory resolved relative to the workspace, for plugins that do not ship with the product. A non-bundled id without a `path` is refused. |
| `config` | object | Plugin-owned configuration, validated by the plugin itself. Provider credentials belong in the root `providers` section; the product layer injects those into every factory under the reserved `providers` key, which plugin-owned config must never use. |

Bundled plugin ids (those shipped in `src/voidcode/plugins/`) need no entry at all: they are enabled with an empty configuration by default, and an entry only exists to disable one, configure it, or point it at a different source.

Subagent discovery and monitoring belong to `subagents`, along with
`task`/`task_batch`. Agent files use `.claude/agents/` and the supported Claude Code
Markdown format. See the [subagents](../src/voidcode/plugins/subagents/README.md)
README for the supported fields and explicit runtime limitations.

### Scripted workflows

The bundled `workflows` plugin runs explicitly configured trusted `.py` scripts:
`plugins.workflows.config` accepts exactly `workflows: {name: workspace-relative
.py path}` (plus the reserved injected `providers` key). Unknown fields, absolute
paths, `..`, and non-`.py` files are refused. An empty mapping admits the plugin
with nothing runnable. See the [workflows](../src/voidcode/plugins/workflows/README.md)
README and the runnable `examples/workflows/` parallel-review setup.

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
also declare a string-to-string `env` map. The child inherits the core process
environment, with configured entries overriding it. `remote-http` requires an
absolute HTTP(S) `url` and does not accept `command` or `env`; it has no header
or authentication configuration. A server entry has no other fields. `/mcp` reads
the currently known inventory without connecting; tools are discovered when used.
Discovery reads every `tools/list` page up to fixed page/tool caps and is
remembered only for the current plugin runtime. Server-side tool changes require
a restart or `/core reload`. Tool schemas use JSON Schema Draft 2020-12; local
references work, while external schema references are never fetched during
argument validation.

Inner `plugins.mcp.config.enabled: false` keeps the plugin admitted but reports
MCP support as disabled; use `plugins.mcp.enabled: false` to remove the tool and
command. MCP is tools-only: resources and prompts are not exposed. Stdio servers
are host-side and unconfined. Normal close/reload stops them; a core SIGKILL can
leave a server that ignores stdin EOF, with no parent-death mechanism. Calls
observe the invocation abort signal; post-send failures/timeouts/cancellation are
reported as `UnconfirmedStop`, while pre-send failures are confirmed.

### LSP servers

`plugins.lsp.config` accepts `enabled` (boolean, default `true`) and `servers`.
Each server may declare `preset`, argv `command`, string arrays `extensions`,
`languages`, `root_markers`, and objects `settings` and `init_options`. Unknown
fields and invalid values are rejected rather than filtered out. A server needs
a command or a known preset; an empty server set retains workspace-based default
discovery. `enabled: false` keeps the plugin admitted but disables its runtime: the
`lsp` tool still appears and fails visibly when called, and `workspace.document_version`
answers nothing. Use the outer `plugins.lsp.enabled: false` to remove the plugin's
tool, command, and query entirely.

### Scheduled prompts

The bundled Cron tools accept `every <N><s|m|h|d>`, `at <ISO-8601>`, `once`, or
`cron <minute> <hour> <day> <month> <weekday>`. Cron expressions use croniter's
five-field syntax, including names such as `JAN`/`MON` and supported modifiers.
Six/seven-field expressions are refused. Calendar expressions follow the host's
local timezone. Recurring schedules expire 7 days after creation; an `every`
interval must be at least 1 minute and shorter than that horizon, and a
recurring schedule whose next fire is at/after its expiry is refused or retired.
`/loop` accepts intervals and sends each prompt as ordinary user text (it does
not dispatch slash commands). Cancel a schedule with `/loop delete <id>` (or the
`cron_delete` tool); a run already in flight is not interrupted.

## API Environment Variables

Export API credentials in the environment of the process that starts VoidCode; no `.env` file is required.

### Model providers

For a configured provider, omit `providers.<name>.api_key` to use the provider's table-declared credential variables, then the generic `<PROVIDER>_API_KEY` fallback: uppercase the provider name and replace `-` with `_`. For example, `openai` uses `OPENAI_API_KEY`, `deepseek` uses `DEEPSEEK_API_KEY`, and `github-copilot` uses `GITHUB_COPILOT_TOKEN` (its generic fallback is `GITHUB_COPILOT_API_KEY`). An explicit `api_key` in `voidcode.json` takes precedence over discovery, and an explicit nested `auth` block suppresses both discovered and generic keys. A non-null `api_key` and a nested `auth` block are mutually exclusive.

```bash
export OPENAI_API_KEY="sk-..."
voidcode run "Summarize this repository"
```

A supported environment credential can make its provider available without a `providers` entry (see provider discovery above). It does not select a model, and it does not pin a non-default `base_url` or explicit auth: put those in `voidcode.json`. Provider credentials are captured when a revision is admitted and restored from its private snapshot on restart. To adopt a changed key, export it before starting the core process and invoke `core.reload`; changing another shell's environment does not affect an already-running core.

The normal turn path streams and is single-attempt. Once a provider dispatch has started, a failure is reported rather than retried or rerouted to another provider/model; the runtime retry policy applies to non-streaming provider implementations. No fallback provider or model is configured automatically.

### Web search APIs

The bundled `web_search` tool selects the first available backend in this order (`web_fetch` snapshots reuse the same process environment; no new config keys):

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
means loading that source as code equal to the application, in the core
process. A restart imports the *current* source with the *recorded*
configuration; `core.reload` re-admits the *live* configuration as a new
generation in-session.

Changed configuration is never admitted implicitly: a restart restores the
persisted revision's recorded configuration and private snapshot. To move to
different code, stop the process and start the new installation against the
same workspace and state directory; to move to different configuration, edit
`voidcode.json` and run `core.reload`.

## State Directory

Core state lives outside the workspace, at:

```text
$XDG_STATE_HOME/voidcode/<sha256-of-the-workspace-path>/
```

(default `$XDG_STATE_HOME` is `~/.local/state`). It contains:

- `voidcode.db` — sessions, append-only events, checkpoints, revisions, and confirmation records (schema v6; any other version is refused read-only);
- `private/<revision>.json` — the 0600 configuration snapshot for one revision, the only place provider credentials are stored;
- `artifacts/` — content-addressed tool-output artifacts owned by the core.

`--state-dir PATH` overrides the derived directory. The state directory must not be inside the workspace: the core refuses a state directory that the workspace's file tools could write to.

Sessions are per-workspace: `voidcode sessions` and `voidcode show` only report sessions whose stored workspace matches, and a session belonging to another workspace is refused.

## Legacy State

Legacy session/state databases from the pre-core architecture are neither imported nor auto-deleted. The core never reads them. Removing that historical data is a manual user action with `rm`; there is no cleanup command. A database at any schema other than v6 (including legacy v5) is refused read-only: nothing is migrated, nothing is deleted automatically.

## Plugin-only architecture

The core has no built-in tools or fallback providers: files, execution, and model providers are ordinary plugins. Disabling `files`, `exec`, or `providers` genuinely disables those model-facing capabilities without fallback. `user_shell` is an independent human shell surface: disabling `exec` removes the model-facing shell/background tools but not the `!` escape; disable both to remove shell execution.

`plugins: {}` keeps the bundled default set; it does not request zero plugins. To admit none, set `enabled: false` for each bundled id listed in `bootstrap.py` (`BUNDLED_PLUGIN_IDS`). A zero-plugin core manages sessions and SQLite persistence only, with no assumption of file, shell, or model execution.

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
