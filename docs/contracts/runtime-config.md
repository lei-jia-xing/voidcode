# 运行时配置契约

来源 Issue：#16

## 目的

定义使 MVP 运行时具备可配置性所需的最小配置界面，同时确保系统是受控的而非过于宽泛。

## 状态

当前运行时从仓库本地的 `.voidcode.json` 中加载以下已实现领域的配置：

- `approval_mode`
- `model`
- `execution_engine`
- `max_steps`
- `tool_timeout_seconds`
- `reasoning_effort`
- `hooks`
- `tools`
- `skills`
- `context_window`
- `lsp`
- `mcp`
- `provider_fallback`
- `providers`
- `agent`
- `tui`

目前 hooks/config 的 MVP 收敛目标已经锁定：

- hooks 继续保持 runtime-owned，但当前已不再只限 `pre_tool` / `post_tool`，而是同时包含 `session_start`、`session_end`、`session_idle`、`background_task_registered`、`background_task_started`、`background_task_progress`、`background_task_completed`、`background_task_failed`、`background_task_cancelled`、`background_task_notification_enqueued`、`background_task_result_read` 与 `delegated_result_available` 等已解析的 lifecycle hook phases
- 显式 / 仓库本地 / 环境 / 默认值这条完整优先级链当前适用于 `approval_mode`、`model`、`execution_engine`、`max_steps` 和 `reasoning_effort`
- 单一可见检查面为 CLI：`voidcode config show --workspace <path> [--session <id>]`
- 当前 schema-backed 配置 UX 包含 `voidcode config schema` 与 `voidcode config init`
- 恢复会话的配置覆盖仍存放在 `SessionState.metadata["runtime_config"]`，并继续覆盖新的 runtime 默认值

## MVP 配置领域

MVP 配置界面应仅覆盖以下区域：

- 工作区根目录（workspace root）
- 模型/供应商选择
- 审批模式
- 钩子（hook）的启用/默认值
- 工具发现/供应商默认值
- 技能发现默认值
- LSP 的扩展基础设施开关，以及 ACP 的 runtime-managed 内部 capability 基线
- 恢复（resume）所需的客户端可见会话设置

## 计划的最小配置形状

MVP 契约应能够表示一个至少包含以下内容的运行时配置对象：

```json
{
  "workspace": "/workspace/project",
  "model": "opencode/gpt-5.4",
  "approval_mode": "ask",
  "execution_engine": "provider",
  "max_steps": 4,
  "hooks": {
    "enabled": true,
    "pre_tool": [["python", "scripts/pre_tool.py"]],
    "post_tool": [["python", "scripts/post_tool.py"]],
    "on_session_start": [["python", "scripts/session_start.py"]],
    "on_session_end": [["python", "scripts/session_end.py"]],
    "on_session_idle": [["python", "scripts/session_idle.py"]],
    "on_background_task_registered": [["python", "scripts/background_task_registered.py"]],
    "on_background_task_started": [["python", "scripts/background_task_started.py"]],
    "on_background_task_progress": [["python", "scripts/background_task_progress.py"]],
    "on_background_task_completed": [["python", "scripts/background_task_completed.py"]],
    "on_background_task_failed": [["python", "scripts/background_task_failed.py"]],
    "on_background_task_cancelled": [["python", "scripts/background_task_cancelled.py"]],
    "on_background_task_notification_enqueued": [["python", "scripts/background_task_notification.py"]],
    "on_background_task_result_read": [["python", "scripts/background_task_result_read.py"]],
    "on_delegated_result_available": [["python", "scripts/delegated_result.py"]]
  },
  "tools": {
    "builtin": {
      "enabled": true
    },
    "local": {
      "enabled": false,
      "path": ".voidcode/tools"
    }
  },
  "skills": {
    "enabled": true,
    "paths": [".voidcode/skills"]
  },
  "agent": {
    "preset": "leader",
    "model": "opencode/gpt-5.4",
    "execution_engine": "provider"
  },
  "lsp": {
    "enabled": false,
    "servers": {}
  }
}
```

字段意图：

- `workspace`：引导（bootstrap）字段，用于在发现仓库本地配置之前确定运行时工作区根目录，随后重用于工具执行和持久化
- `model`：OpenCode `provider/model` 格式的供应商/模型标识符
- `approval_mode`：由运行时治理的工具所使用的最小执行策略模式
- `execution_engine`：当前接受 `deterministic` 或 `provider`。`provider` 是产品默认主路径；`deterministic` 保留为显式 test/dev/no-key harness（参考/debug）。
- `max_steps`：execution engine 的最大 step budget，作用于 deterministic 与 provider-backed engine
- `hooks`：运行时拥有的最小钩子配置对象，覆盖 pre/post tool 与当前已解析的 lifecycle hook phases
- `tools`：内置工具启用、仓库本地自定义 tool manifest 发现，以及 provider 可见工具收窄的最小配置；所有 tool 都通过 runtime registry / allowlist / permission 路径治理
- `skills`：技能发现启用的最小配置，以及额外的技能搜索路径
- `context_window`：provider-backed 路径的上下文窗口与 tool-result retention 配置
- `lsp`：当前 runtime-managed 语言服务器（Language-server）能力的最小配置容器
- `mcp`：当前已解析的 runtime-managed MCP 配置容器
- `provider_fallback`：provider fallback 链的配置入口
- `providers`：provider 级配置对象；当未提供仓库本地 `providers` block 时，runtime 也会从标准 provider 凭据环境变量构造最小 provider 配置（例如 `OPENCODE_API_KEY`）
- `agent`：agent preset 的 runtime 消费入口。当前顶层 active run 默认使用 builtin `leader`，也可显式选择 builtin `product` 或本地自定义 `mode: primary` markdown manifest；runtime-owned delegation path 上的 child run 可执行 builtin child preset 或本地自定义 `mode: subagent` manifest。
- `agents`：按 agent preset 配置 model / fallback defaults。这里是“已发现 preset 的配置覆盖/别名入口”，不是 manifest 定义入口；内置 preset key 与已发现本地 manifest key 可省略 `preset`，其他 alias key 必须显式声明 `preset`。
- `categories`：按 task category 配置 delegated child model override。
- `reasoning_effort`：可选的 runtime-owned reasoning-effort hint（例如 `low` / `medium` / `high`），透传给当前 active provider；当前 model metadata 显式 `supports_reasoning_effort=false` 时 runtime 会 fail-fast，未知能力按 best-effort 透传。

### Execution engine 生命周期决策

当前生命周期约定为：

- `provider`：产品默认主路径；执行前必须配置 `model = "provider/model"`（或等价环境变量），否则 runtime 会在 run preflight 阶段返回清晰的 provider/model 配置错误。
- `deterministic`：保留为显式支持的 test/dev/no-key harness，并继续承担 graph harness 与确定性回归测试。
- `voidcode config init` 默认不写入 `execution_engine`，避免 repo-local config 锁死 provider/no-model 状态；若显式写入 `execution_engine = "provider"`，应同时写入 `model`。
- 历史会话 replay/resume 的兼容语义由持久化 runtime config 元数据维持，不应被新默认值静默覆盖。

## 当前实现的仓库本地形状

当前的 `.voidcode.json` 解析器接受以下仓库本地形状：

- `approval_mode`：`allow`、`deny`、`ask` 之一
- `permission.external_directory_read`：对象，key 为路径 pattern（支持 absolute / `~` / glob），value 为 `allow|deny|ask`
- `permission.external_directory_write`：对象，key 为路径 pattern（支持 absolute / `~` / glob），value 为 `allow|deny|ask`
- `permission.rules`：有序数组，每条规则可包含 `tool`、`path`、`command` 与必填 `decision`，用于 runtime-owned 的工具/路径/命令 pattern 权限匹配
- `model`：字符串
- `max_steps`：大于等于 1 的整数
- `reasoning_effort`：非空字符串；通常为 `low` / `medium` / `high` / `none`，但 runtime 不强制 enum，由 provider adapter 翻译
- `hooks.enabled`：布尔值
- `hooks.pre_tool`：命令数组的数组，每个命令在 workspace cwd 中执行
- `hooks.post_tool`：命令数组的数组，每个命令在 workspace cwd 中执行
- hook 命令必须是 argv 数组，不是 shell 字符串；runtime 不隐式启动 `sh` / `cmd.exe`。需要 shell 功能时必须显式写出解释器，例如 `["python", "script.py"]` 或平台专属的 `["bash", "-lc", "..."]`。
- `hooks.on_session_start`：命令数组的数组
- `hooks.on_session_end`：命令数组的数组
- `hooks.on_session_idle`：命令数组的数组
- `hooks.on_background_task_registered`：命令数组的数组
- `hooks.on_background_task_started`：命令数组的数组
- `hooks.on_background_task_progress`：命令数组的数组
- `hooks.on_background_task_completed`：命令数组的数组
- `hooks.on_background_task_failed`：命令数组的数组
- `hooks.on_background_task_cancelled`：命令数组的数组
- `hooks.on_background_task_notification_enqueued`：命令数组的数组
- `hooks.on_background_task_result_read`：命令数组的数组
- `hooks.on_delegated_result_available`：命令数组的数组
- `hooks.formatter_presets`：对象，用于覆盖或扩展 formatter preset
- `tools.builtin.enabled`：布尔值
- `tools.local.enabled`：布尔值；显式为 `true` 时 runtime 才会发现仓库本地自定义 tool manifest
- `tools.local.path`：workspace-relative 目录，默认 `.voidcode/tools`，包含 `*.json` tool manifest
- `tools.allowlist`：字符串数组，用于 active agent tool boundary 的硬边界
- `tools.default`：字符串数组，用于 active agent 默认可见工具集合，只能在 allowlist 内进一步收窄
- `skills.enabled`：布尔值
- `skills.paths`：字符串数组
- `context_window.auto_compaction`：布尔值
- `context_window.max_tool_results`：大于等于 1 的整数
- `context_window.max_tool_result_tokens`：大于等于 1 的整数
- `context_window.max_context_ratio`：大于 0 的数字
- `context_window.model_context_window_tokens`：大于等于 1 的整数
- `context_window.reserved_output_tokens`：大于等于 1 的整数
- `context_window.minimum_retained_tool_results`：大于等于 1 的整数
- `context_window.recent_tool_result_count`：大于等于 1 的整数
- `context_window.recent_tool_result_tokens`：大于等于 1 的整数
- `context_window.default_tool_result_tokens`：大于等于 1 的整数
- `context_window.per_tool_result_tokens`：对象，value 为大于等于 1 的整数
- `context_window.tokenizer_model`：字符串
- `lsp.enabled`：布尔值
- `lsp.servers`：对象

对于内置 LSP server，推荐的用户配置路径是直接使用内置 server 名作为 key，例如：

```json
{
  "lsp": {
    "enabled": true,
    "servers": {
      "pyright": {},
      "gopls": {},
      "clangd": {}
    }
  }
}
```

只有在需要自定义 server 名、复用内置 preset 或声明完全自定义 server 时，才需要提供 `command` 或显式 `preset` 字段。
- `mcp.enabled`：布尔值
- `mcp.servers`：对象
- `provider_fallback`：对象
- `providers`：对象
- provider 凭据环境变量可作为 first-run fallback：设置 `VOIDCODE_MODEL=opencode-go/<model>` 与 `OPENCODE_API_KEY` 时，即使 `.voidcode.json` 没有 `providers.opencode-go` block，runtime 也会构造最小 OpenCode Go provider 配置。等价 fallback 也覆盖现有标准变量：`OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`GOOGLE_API_KEY`、`GITHUB_COPILOT_TOKEN`、`LITELLM_API_KEY` / `LITELLM_PROXY_API_KEY`、`GLM_API_KEY`、`MINIMAX_API_KEY`、`KIMI_API_KEY` 与 `DASHSCOPE_API_KEY`。这些值只进入运行时配置对象；`config show` 与 persisted runtime metadata 不会输出 secret。
- `agent.preset`：agent preset id。可解析 builtin `leader`、`worker`、`advisor`、`explore`、`researcher`、`product`，以及本地发现的 markdown manifest id（见下方“本地 markdown agent manifest”）。
- `agent.prompt_profile`：字符串；省略时从内置 manifest 回填
- `agent.model`：字符串；对 active agent 覆盖顶层 `model`
- `agent.execution_engine`：`deterministic` 或 `provider`；`leader` 省略时从内置 manifest 回填为 `provider`
- `agent.tools`：与顶层 `tools` 相同的配置 shape；当前作为 active agent tool boundary 解析、序列化和持久化
- `agent.tools.allowlist`：字符串数组；与 manifest allowlist 一起收窄 active agent 可见/可调用工具集合
- `agent.tools.default`：字符串数组；在 allowlist 允许范围内进一步收窄默认暴露工具
- `agent.skills`：与顶层 `skills` 相同的配置 shape；对 active agent 覆盖本次运行使用的 runtime-managed skill discovery / application policy
- `agent.provider_fallback`：与顶层 `provider_fallback` 相同的配置 shape；对 active agent 覆盖顶层 provider fallback
- `agent.fallback_models`：agent-scoped shorthand；必须同时配置 `agent.model`，runtime 会把 `agent.model` 作为 `provider_fallback.preferred_model`，并把该数组作为 fallback chain。不能与同一 agent 的 `provider_fallback` 同时出现。
- `agents.<preset>`：按 preset 配置 delegated child / primary agent defaults；builtin key 与已发现本地 manifest key 可省略 `preset`，alias key 必须显式声明 `preset`。
- `agents.<preset>.fallback_models`：与 `agent.fallback_models` 相同的 shorthand；delegation path 会把选中 preset 的 fallback chain 持久化到 child session metadata，category model override 只替换 preferred model，不丢弃 preset fallback chain。
- `categories.<category>.model`：字符串；对该 task category 的 delegated child 覆盖 preferred model，优先级高于 agent preset model、低于 request agent model。
- `tui.leader_key`：字符串
- `tui.keymap`：对象，值当前仅允许 `command_palette`、`session_new`、`session_resume`
- `tui.preferences.theme.name`：字符串
- `tui.preferences.theme.mode`：`auto`、`light`、`dark` 之一
- `tui.preferences.reading.wrap`：布尔值
- `tui.preferences.reading.sidebar_collapsed`：布尔值

### external directory permission 语义

- `permission.external_directory_read` 与 `permission.external_directory_write` 是 runtime-owned 的外部目录权限面。
- workspace 内路径保持现有治理：只读工具自动 `allow`，非只读工具遵循 `approval_mode`。
- workspace 外路径使用 external permission 决策：
  - read-like tool calls 使用 `external_directory_read`
  - write-like tool calls 使用 `external_directory_write`
- 当前默认值：
  - `external_directory_read = {"*": "ask"}`
  - `external_directory_write = {"*": "deny"}`
- rule matching 使用按顺序匹配（first-match-wins）；路径在匹配前会做 canonicalization。

### pattern-based permission rules 语义

- `permission.rules` 是 runtime 在工具执行前评估的通用 permission rule 面；客户端、agent、command 与 custom tool 不能绕过它。
- 每条规则的形状为：

```json
{
  "tool": "write_file",
  "path": ".github/**",
  "decision": "ask"
}
```

- 字段语义：
  - `tool`：工具名 glob；省略时等同 `*`。
  - `path`：workspace-relative 或 canonical path glob；对文件系统工具与 `shell_exec` 中已识别的显式输出路径生效。
  - `command`：`shell_exec` command 字符串 glob，例如 `pytest*`、`mise run test` 或 `rm -rf *`。
  - `decision`：必填，值为 `allow`、`ask` 或 `deny`。
- 匹配语义是确定性的 first-match-wins；只有第一条匹配的 pattern rule 生效。
- `permission.rules` 不能扩大 hard boundary：agent/tool allowlist 仍先收窄可见与可调用工具；外部目录规则仍先保护 workspace 外路径，且 pattern rule 只能把 external decision 收紧（例如 `allow -> ask/deny` 或 `ask -> deny`），不能把 external `deny` 降级为 `allow`。
- workspace 内只读工具仍默认允许；如果需要让某个只读工具进入审批或拒绝路径，可用 `permission.rules` 显式 `ask` 或 `deny`。

常见策略示例：

```json
{
  "permission": {
    "rules": [
      {"tool": "read_file", "path": "src/**", "decision": "allow"},
      {"tool": "grep", "path": "src/**", "decision": "allow"},
      {"tool": "glob", "path": "src/**", "decision": "allow"},
      {"tool": "write_file", "path": ".github/**", "decision": "ask"},
      {"tool": "edit", "path": ".github/**", "decision": "ask"},
      {"tool": "shell_exec", "command": "pytest*", "decision": "allow"},
      {"tool": "shell_exec", "command": "mise run test", "decision": "allow"},
      {"tool": "shell_exec", "command": "rm -rf *", "decision": "deny"}
    ],
    "external_directory_write": {"*": "ask"}
  }
}
```

所有扩展领域字段都是可选的。省略时，它们在领域级别解析为 `None`，并且数组字段在提供的领域对象内部默认回退为空元组。

### 仓库本地自定义 Tools

`tools.local` 是一个 opt-in 的本地优先扩展点，用于把仓库内声明的命令包装成 typed tool。它不是 marketplace、不是客户端执行路径，也不是 workspace-scoped MCP；runtime 负责发现 manifest、注册 tool、执行命令、注入 session context，并继续使用现有 `tools.allowlist` / `agent.tools.allowlist` / permission 默认策略治理可见性和调用。

当 `tools.local.enabled=true` 时，runtime 会读取 `tools.local.path`（默认 `.voidcode/tools`）下的 `*.json` manifest。最小 manifest 形状：

```json
{
  "name": "local/echo",
  "description": "Echo JSON arguments",
  "input_schema": {
    "type": "object",
    "properties": {
      "message": {"type": "string"}
    }
  },
  "command": ["python", "${manifest_dir}/echo.py"],
  "read_only": true
}
```

执行语义：

- `name` 必须稳定且不能与内置、MCP 或其他 runtime tool 重名；重名会 fail-fast，而不是覆盖。
- `description` 与 `input_schema` 直接进入 `ToolDefinition`，供 provider/tool boundary 使用。
- `read_only` 是 provider-visible mutability hint；本地自定义 tool 仍按 command execution 治理，默认走审批/拒绝策略，不能仅凭 manifest 声明绕过 runtime permission。
- `command` 由 runtime 在 workspace cwd 中执行；它必须是 argv 数组，不是 shell 字符串，runtime 不隐式启动 `sh` / `cmd.exe`。tool arguments 仅以 JSON 写入 stdin，不会通过环境变量暴露完整参数 payload。
- runtime 环境变量只携带有界执行上下文，例如 `VOIDCODE_WORKSPACE`、`VOIDCODE_TOOL_NAME`、`VOIDCODE_TOOL_CALL_ID`（如有）、`VOIDCODE_SESSION_ID`、`VOIDCODE_PARENT_SESSION_ID`（如有）和 `VOIDCODE_DELEGATION_DEPTH`，tool 作者不应从客户端获得这些上下文字段。

## Agent preset runtime consumption 边界

当前 runtime 已经能够解析内置 agent preset，但会区分“顶层 active agent”与“delegated child agent”两条执行边界：

- 顶层 active run 默认使用 builtin `leader`，也可显式选择 builtin `product` 或本地自定义 `mode: primary` manifest
- runtime-owned delegation path 上的 child run 还可执行 builtin `advisor`、`explore`、`product`、`researcher`、`worker`，以及本地自定义 `mode: subagent` manifest

这个限制是有意的：除 `leader` 与 `product` 外，child preset 不是任意可选的顶层 active agent，只有在 delegation path（例如带 `parent_session_id` 且通过 subagent routing 校验的请求）中才会进入真实执行路径。

### 本地 markdown agent manifest

MVP 支持 true local manifest，而不是 marketplace / plugin distribution。发现路径为：

- project scope：`<workspace>/.voidcode/agents/*.md`
- user scope：Linux/macOS 使用 `$XDG_CONFIG_HOME/voidcode/agents/*.md`，未设置 `XDG_CONFIG_HOME` 时为 `~/.config/voidcode/agents/*.md`；Windows 使用 `%APPDATA%\voidcode\agents`，缺失时回退到 `%LOCALAPPDATA%\voidcode\agents`。

每个 markdown 文件必须以 YAML-like frontmatter 开头，随后正文作为 prompt material：

```markdown
---
name: Review Helper
description: Read-only reviewer for focused code quality checks.
mode: subagent
tool_allowlist: [read_file, glob, grep]
skill_refs: [code-review]
preset_hook_refs: [role_reminder]
prompt_append: |
  Always include severity and exact file paths in findings.
---
You are a focused reviewer. Stay within the runtime-provided tools and report risks clearly.
```

必需字段：`name`、`description`、`mode`（`primary` 或 `subagent`）。支持字段：`id`、`name`、`description`、`mode`、`model`、`fallback_models`、`tool_allowlist`、`skill_refs`、`preset_hook_refs`、`mcp_binding`、`prompt_append`。正文是 primary prompt；`prompt_append` 会作为附加本地 guidance 单独持久化，并在 provider context 中追加一次。未声明 `id` 时，runtime 使用 `name` 的 lowercase-kebab 形式作为稳定 id；显式 / 派生 id 必须匹配现有 agents key 风格 `^[a-z][a-z0-9_-]*$`。

发现优先级：同一 custom id 下 project scope 覆盖 user scope；同一 scope 内重复 id 会 fail-fast；custom manifest 不允许使用 builtin id（例如 `leader` 或 `worker`）替换 builtin preset。错误会包含具体文件路径，便于修复。

本地 manifest 只声明 prompt 和默认 capability intent。它不会绕过 runtime tool allowlist、approval、MCP lifecycle、hook execution、skill loading 或 delegated child session contract。正文 prompt 与可选 `prompt_append` 会在 runtime config / `agent_capability_snapshot` 中以 `prompt_materialization.source = "custom_markdown"` 持久化，因此 resume / replay 不会因 manifest 文件后续变更而静默改变历史 session。

`.voidcode.json` 的 `agent` / `agents.<key>` 也可声明 `prompt` 与 `prompt_append`：`prompt` 明确替换/定义 profile text，`prompt_append` 在 resolved base prompt 后追加本地 guidance。runtime snapshot 会把 resolved base prompt 与 append 分开持久化，渲染时只追加一次。

`voidcode run --agent <id>` 不再使用 argparse 静态 choices；它会在加载 runtime config 与本地 manifest 后验证 `<id>` 是否为可顶层执行的 builtin/custom primary agent。

`voidcode agents list --workspace <path> [--json]` 会列出 builtin 与本地 custom primary agents，并在 custom agents 上显示 `source_scope` / `source_path`。

`leader` 与 `product` preset 当前进入 runtime truth 的字段是：

- `preset`
- `prompt_profile`：注入 provider turn 的 agent profile system message
- `model`：覆盖本次运行的 resolved provider model
- `execution_engine`：`leader` 与 `product` 默认进入 runtime-managed `provider` 路径
- `tools`：收窄 provider 可见工具与实际 tool lookup / invocation 边界；manifest allowlist、`agent.tools.allowlist`、`agent.tools.default` 按交集生效，`agent.tools.builtin.enabled=false` 只移除内置工具名集合，仍保留已通过 allowlist 的 runtime-managed MCP / 注入工具
- `skills`：覆盖本次运行的 skill registry discovery 与 applied skill payload / prompt context；active manifest 的 `skill_refs` 会作为默认 skill selection 进入 application，并与 request metadata `skills` 去重合并
- `provider_fallback`：覆盖本次运行的 fallback model chain
- `fallback_models`：`provider_fallback` 的简写形式；仅在同一 agent 配置了 `model` 时有效

这些字段会影响当前 runtime-managed provider 主路径，并持久化到 `SessionState.metadata["runtime_config"]["agent"]`，以保证 resume / replay 不被新的 runtime 默认值污染。

以下字段当前仍只作为声明层 metadata 保留，不代表 runtime 已经实现相应能力语义：

- `AgentManifest.routing_hints`：仍属于声明层 metadata，不是执行治理 truth。
- 把 `worker` / `advisor` / `explore` / `researcher` 作为任意顶层 active preset 的 config intent：runtime 仍不会把它们当作普通顶层会话直接执行。

如果运行时在顶层 active run 中收到非 top-level-selectable preset，例如：

```json
{"agent": {"preset": "worker"}}
```

则 runtime 会拒绝执行，而不是把 child role 悄悄映射到普通顶层 deterministic/provider 路径。这保持了 `agent/` declaration layer 与 `runtime/` execution truth 的边界；这些 preset 只能通过 runtime-owned delegation path 进入真实 child execution。

## TUI 偏好优先级与持久化语义

TUI 偏好与其他多数领域不同，拥有一条单独的双层优先级链：

1. workspace override（仓库本地 `.voidcode.json`）
2. global default（`~/.config/voidcode/config.json`）
3. built-in defaults

其中第一阶段已实现的 built-in defaults 为：

- `tui.leader_key` -> `alt+x`
- `tui.preferences.theme.name` -> `textual-dark`
- `tui.preferences.theme.mode` -> `auto`
- `tui.preferences.reading.wrap` -> `true`
- `tui.preferences.reading.sidebar_collapsed` -> `false`

### 重要语义

- workspace override 仍然是“局部覆盖”，不是完整快照。
- 但当前 TUI 产品默认不会把普通偏好修改写回 workspace。
- 普通 theme / theme mode / wrap / sidebar 修改默认写回 global default。
- workspace 中未覆盖的字段继续继承 global default；workspace override 只用于显式的项目级覆盖语义。

### 当前已实现的全局配置路径

用户级全局 TUI 默认配置路径为：

`~/.config/voidcode/config.json`

workspace 本地覆盖路径保持为：

`<workspace>/.voidcode.json`

## 关于 LSP 和 ACP 基础设施状态的说明

在当前切片中，`acp` 已进入最小的 runtime-managed transport/lifecycle 路径；`lsp` 也已经拥有最小 runtime-managed 基线，但两者都仍保持严格收敛的 MVP 范围。

- 它们的存在是为了让运行时消费稳定的类型化配置，并为更强的 capability 管理保留边界。
- `acp` 作为 runtime 内部 capability 继续存在，但不再属于 repo-local `.voidcode.json` 的用户配置领域；它的运行结果通过 session metadata 中的 `runtime_state` 暴露，而不是进入用户主配置快照 `runtime_config`。
- `acp` 当前只支持 runtime-owned `memory` transport。启用后，运行时会在 run / approval-resume 启动阶段执行 connect + handshake，并在该次运行结束时 disconnect。
- ACP contract 现在已经是 delegation-aware：request / response / event envelope 会携带 `parent_session_id` 与 `AcpDelegatedExecution`，runtime 也会发出 `runtime.acp_delegated_lifecycle` 来对齐 delegated child lifecycle observability。
- 如果 `acp` startup / handshake 失败，运行时会将 ACP 状态标记为 `failed`，发出 `runtime.acp_failed`，并使本次运行通过已有失败路径结束；不会静默降级为 disconnected 继续执行。
- `lsp` 已支持最小的 runtime-managed server 启动与只读工具访问，并且 `lsp.servers` 已可消费内置 preset、extension/language 映射、root markers 与默认 command/preset override merge。
- 对用户来说，内置 LSP server 的规范配置面是 `lsp.servers.{builtin_name}: {}`；显式 `preset` 仅用于自定义 server 名复用内置 preset，而不是主配置入口。
- 当 repo-local `.voidcode.json` 未显式提供 `lsp` 配置时，运行时现在会为高置信度 workspace 自动推导最小默认值：当前覆盖 Python (`pyright`)、TypeScript/JavaScript (`tsserver`)、Go (`gopls`)、Rust (`rust-analyzer`)、C/C++ (`clangd`)、Java (`jdtls`)、Lua (`lua_ls`)、Zig (`zls`) 与 C# (`csharp-ls`)，并且只有在对应语言服务器可执行文件存在时才会启用。
- 显式的 repo-local `lsp` 配置（包括 `enabled: false`）继续具有最高优先级；自动推导默认值不会覆盖用户已声明的 server 列表或关闭语义。

## 工作区的引导规则

`workspace` 的解析不遵循与普通运行时配置字段相同的优先级阶梯。

它必须首先被确定，以便运行时发现该工作区下的任何仓库本地配置。在 MVP 中：

1. 显式的运行时/引导输入选择工作区根目录
2. 随后在该工作区内发现仓库本地配置
3. 普通运行时配置优先级适用于非引导字段，如 `model`、`approval_mode` 和 `hooks`

对于 hooks/config MVP，`approval_mode`、`model`、`execution_engine`、`max_steps` 和 `reasoning_effort` 的锁定优先级为：

1. 恢复会话中的 `SessionState.metadata["runtime_config"]`（仅恢复时）
2. 请求级覆盖（`RuntimeRequest.metadata["max_steps"]` / `RuntimeRequest.metadata["reasoning_effort"]`）
3. 显式 CLI / 客户端覆盖
4. 仓库本地 `.voidcode.json`
5. 环境变量（`VOIDCODE_APPROVAL_MODE` / `VOIDCODE_MODEL` / `VOIDCODE_EXECUTION_ENGINE` / `VOIDCODE_MAX_STEPS` / `VOIDCODE_TOOL_TIMEOUT_SECONDS` / `VOIDCODE_REASONING_EFFORT`）
6. 内置默认值

其余领域仍保持浅层仓库本地配置语义，不在此轨道中获得这条完整优先级引擎。

对于 fresh run，`RuntimeRequest.metadata["max_steps"]` 与 `RuntimeRequest.metadata["reasoning_effort"]` 可以作为窄范围的请求级覆盖；一旦会话开始，这些值会被持久化到 `SessionState.metadata["runtime_config"]`，并在后续 resume 时优先于新的 runtime 默认值。`execution_engine` 同样会进入显式 / 仓库本地 / 环境 / 默认值解析，并在会话恢复时优先采用持久化的会话配置。

`reasoning_effort` 的 capability-aware 校验由 runtime 在请求处理早期完成：当解析后的 `provider/model` 的 `ProviderModelMetadata.supports_reasoning_effort` 显式为 `False` 时，runtime 抛出 `RuntimeRequestError`；未知能力按 best-effort 透传。

## 当前代码锚点

- `VoidCodeRuntime(workspace=...)`
- `RuntimeRequest(prompt, session_id, metadata)`
- `SessionState.metadata`
- SQLite 存储的会话持久化元数据

## 推荐优先级

对于 MVP，非引导配置字段应按此顺序解析：

1. 显式的会话覆盖（session override）
2. 显式的客户端或 CLI 标志
3. 仓库本地配置文件
4. 环境变量
5. 内置默认值

对于恢复的会话，持久化在 `SessionState.metadata["runtime_config"]` 中的 `approval_mode` / `model` 就是会话覆盖，并且优先级高于新的 CLI / 客户端覆盖。

## 计划的会话覆盖形状

会话作用域的覆盖应能与仓库默认值分开表示。锁定的 MVP 形状为：

```json
{
  "runtime_config": {
    "model": "opencode/gpt-5.4-pro",
    "approval_mode": "ask",
    "execution_engine": "provider",
    "max_steps": 6,
    "reasoning_effort": "high"
  }
}
```

这有意设计得很窄：在 MVP 中，`approval_mode`、`model`、`execution_engine`、`max_steps` 与 `reasoning_effort` 是恢复关键字段；其中 `max_steps` 与 `reasoning_effort` 支持窄范围的请求级覆盖，并在会话启动后转化为持久化会话配置。

## 会话持久化设置

关键的恢复设置应随会话一起持久化，至少包括：

- 工作区（现有持久化字段）
- 审批模式
- 与确定性恢复行为相关的已选模型/供应商
- execution engine
- execution engine 的 step budget

## 当前代码映射

代码库中当前的具体存储/映射点包括：

- `VoidCodeRuntime(workspace=...)` 提供活跃的工作区根目录
- `RuntimeRequest.metadata` 是当前的灵活请求作用域容器
- `SessionState.metadata` 在内存中存储运行时/会话元数据
- SQLite 会话存储将 `SessionState.metadata` 作为持久化会话 payload 的一部分进行保存
- SQLite 会话存储还将 `workspace` 持久化为 `sessions.workspace` 中的一等公民列，并将其用于会话列出和查找

锁定的 CLI 检查路径为：

```bash
voidcode config show --workspace <path> [--session <id>]
voidcode config schema
voidcode config init --workspace <path> [--force] [--print] [--with-examples]
```

`config show` 成功输出必须是 JSON，且当前至少包含：

- `workspace`
- `session_id`
- `approval_mode`
- `model`
- `execution_engine`
- `max_steps`
- `reasoning_effort`（仅当配置或会话覆盖该字段时出现）
- `agent`
- `agents`
- `categories`
- `provider_fallback`
- `resolved_provider`

`config schema` 成功输出必须是 JSON Schema 文档，用于描述仓库本地 `.voidcode.json` 的当前公共形状。schema 的稳定 `$id` 为：

```text
https://voidcode.dev/schemas/runtime-config.schema.json
```

`config init` 生成不含 secrets 的 starter workspace 配置。默认写入 `<workspace>/.voidcode.json`，并在文件已存在时失败；`--force` 显式覆盖，`--print` 只输出 JSON 而不写入文件，`--with-examples` 会加入最小 `tools` / `skills` 示例块。生成配置默认包含 `$schema` 与 `approval_mode: "ask"`，不会生成 `providers` 或任何 `api_key` / token 字段。

失败契约锁定为：

- invalid workspace → 非零退出码，stderr 文本错误，无 JSON
- nonexistent session → 非零退出码，stderr 文本错误，无 JSON
- workspace/session mismatch → 非零退出码，stderr 文本错误，无 JSON

## 不变量

- 用户无需编辑代码即可更改运行时行为
- 优先级必须是确定性的
- 持久化会话必须携带足够的配置，以便进行有意义的重放或恢复
- MVP 配置界面必须专注于运行时驱动的确定性执行路径

## 当前限制

- hooks 在此轨道中已包含 `pre_tool` / `post_tool` 以及 `session_start`、`session_end`、`session_idle`、`background_task_completed`、`background_task_failed`、`background_task_cancelled`、`delegated_result_available` 这些 lifecycle hook phases；但仍不包含 render/message-transform 一类更宽的展示层阶段
- hooks 不得改变工具参数或结果，只能观察与失败中止
- 除 `approval_mode` / `model` / `execution_engine` / `max_steps` / `reasoning_effort` 外，其余扩展领域继续保持浅层仓库本地配置
- 仅 `approval_mode` / `model` / `execution_engine` / `max_steps` / `reasoning_effort` 在此轨道中具备恢复关键的优先级行为
- 当前的请求元数据是灵活的，但尚不属于稳定的公共模式（Schema）；将其收紧为稳定 runtime request metadata schema 的后续实现工作由 `#175` 跟踪

## 非目标

- 高级的多智能体配置
- 特定于供应商的机密管理详情
- 完整的策略 DSL
- 丰富的 OpenCode 风格 hooks 框架
- HTTP config inspection endpoint

## 验收检查点

- 存在一份配置文档，供后续实现直接遵循
- 持久化会话契约显式指出了哪些设置在恢复后依然有效
- 配置优先级已被记录，并被 TUI/Web 实现工作所复用
- 配置文档包含仓库/运行时默认值和会话级覆盖的最小具体形状
