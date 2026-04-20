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
- `hooks`
- `tools`
- `skills`
- `lsp`
- `mcp`
- `provider_fallback`
- `providers`
- `plan`
- `agent`
- `tui`

目前 hooks/config 的 MVP 收敛目标已经锁定：

- hooks 继续保持 runtime-owned，但当前已不再只限 `pre_tool` / `post_tool`，而是同时包含 `session_start`、`session_end`、`session_idle`、`background_task_completed`、`background_task_failed`、`background_task_cancelled` 与 `delegated_result_available` 等已解析的 lifecycle hook phases
- 显式 / 仓库本地 / 环境 / 默认值这条完整优先级链当前适用于 `approval_mode`、`model`、`execution_engine` 和 `max_steps`
- 单一可见检查面为 CLI：`voidcode config show --workspace <path> [--session <id>]`
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
  "execution_engine": "deterministic",
  "max_steps": 4,
  "hooks": {
    "enabled": true,
    "pre_tool": [["python", "scripts/pre_tool.py"]],
    "post_tool": [["python", "scripts/post_tool.py"]],
    "on_session_start": [["python", "scripts/session_start.py"]],
    "on_session_end": [["python", "scripts/session_end.py"]],
    "on_session_idle": [["python", "scripts/session_idle.py"]],
    "on_background_task_completed": [["python", "scripts/background_task_completed.py"]],
    "on_background_task_failed": [["python", "scripts/background_task_failed.py"]],
    "on_background_task_cancelled": [["python", "scripts/background_task_cancelled.py"]],
    "on_delegated_result_available": [["python", "scripts/delegated_result.py"]]
  },
  "tools": {
    "builtin": {
      "enabled": true
    },
    "paths": [".voidcode/tools"]
  },
  "skills": {
    "enabled": true,
    "paths": [".voidcode/skills"]
  },
  "agent": {
    "preset": "leader",
    "model": "opencode/gpt-5.4",
    "execution_engine": "single_agent"
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
- `max_steps`：execution engine 的最大 step budget，作用于 deterministic 与 provider-backed engine
- `hooks`：运行时拥有的最小钩子配置对象，覆盖 pre/post tool 与当前已解析的 lifecycle hook phases
- `tools`：内置工具启用的最小配置，以及额外的工具搜索路径
- `skills`：技能发现启用的最小配置，以及额外的技能搜索路径
- `lsp`：当前 runtime-managed 语言服务器（Language-server）能力的最小配置容器
- `mcp`：当前已解析的 runtime-managed MCP 配置容器
- `provider_fallback`：provider fallback 链的配置入口
- `providers`：provider 级配置对象
- `plan`：计划/plan provider 的配置入口
- `agent`：agent preset 的 runtime 消费入口。当前只有 `leader` 可以进入真实执行路径，其余内置 preset 仍是 declaration-only。

## 当前实现的仓库本地形状

当前的 `.voidcode.json` 解析器接受以下仓库本地形状：

- `approval_mode`：`allow`、`deny`、`ask` 之一
- `model`：字符串
- `max_steps`：大于等于 1 的整数
- `hooks.enabled`：布尔值
- `hooks.pre_tool`：命令数组的数组，每个命令在 workspace cwd 中执行
- `hooks.post_tool`：命令数组的数组，每个命令在 workspace cwd 中执行
- `hooks.on_session_start`：命令数组的数组
- `hooks.on_session_end`：命令数组的数组
- `hooks.on_session_idle`：命令数组的数组
- `hooks.on_background_task_completed`：命令数组的数组
- `hooks.on_background_task_failed`：命令数组的数组
- `hooks.on_background_task_cancelled`：命令数组的数组
- `hooks.on_delegated_result_available`：命令数组的数组
- `hooks.formatter_presets`：对象，用于覆盖或扩展 formatter preset
- `tools.builtin.enabled`：布尔值
- `tools.paths`：字符串数组
- `tools.allowlist`：字符串数组，用于 active agent tool boundary 的硬边界
- `tools.default`：字符串数组，用于 active agent 默认可见工具集合，只能在 allowlist 内进一步收窄
- `skills.enabled`：布尔值
- `skills.paths`：字符串数组
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

只有在需要自定义 server 名、复用内置 preset 或声明完全自定义 server 时，才需要继续提供 `command` 或兼容性的 `preset` 字段。
- `mcp.enabled`：布尔值
- `mcp.servers`：对象
- `provider_fallback`：对象
- `providers`：对象
- `plan.provider`：字符串
- `plan.module`：字符串
- `plan.factory`：字符串
- `plan.options`：对象
- `agent.preset`：内置 agent preset id，当前可解析 `leader`、`worker`、`advisor`、`explore`、`researcher`、`product`
- `agent.<preset>`：兼容的 nested preset 配置形式，例如 `{"agent": {"leader": {...}}}`
- `agent.prompt_profile`：字符串；省略时从内置 manifest 回填
- `agent.model`：字符串；对 active agent 覆盖顶层 `model`
- `agent.execution_engine`：`deterministic` 或 `single_agent`；`leader` 省略时从内置 manifest 回填为 `single_agent`
- `agent.tools`：与顶层 `tools` 相同的配置 shape；当前作为 agent config intent 解析、序列化和持久化
- `agent.tools.allowlist`：字符串数组；与 manifest allowlist 一起收窄 active agent 可见/可调用工具集合
- `agent.tools.default`：字符串数组；在 allowlist 允许范围内进一步收窄默认暴露工具
- `agent.skills`：与顶层 `skills` 相同的配置 shape；当前作为 agent config intent 解析、序列化和持久化
- `agent.provider_fallback`：与顶层 `provider_fallback` 相同的配置 shape；对 active agent 覆盖顶层 provider fallback
- `tui.leader_key`：字符串
- `tui.keymap`：对象，值当前仅允许 `command_palette`、`session_new`、`session_resume`
- `tui.preferences.theme.name`：字符串
- `tui.preferences.theme.mode`：`auto`、`light`、`dark` 之一
- `tui.preferences.reading.wrap`：布尔值
- `tui.preferences.reading.sidebar_collapsed`：布尔值

所有扩展领域字段都是可选的。省略时，它们在领域级别解析为 `None`，并且数组字段在提供的领域对象内部默认回退为空元组。

## Agent preset runtime consumption 边界

当前 runtime 已经能够解析内置 agent preset，但只有 `leader` preset 会进入真实执行路径。这个限制是有意的：`worker`、`advisor`、`explore`、`researcher` 与 `product` 仍是声明层 / future preset，不能通过 runtime config 或 request metadata 激活成实际执行 agent。

`leader` preset 当前进入 runtime truth 的字段是：

- `preset`
- `prompt_profile`
- `model`
- `execution_engine`
- `provider_fallback`

这些字段会影响 provider-backed single-agent 主路径，并持久化到 `SessionState.metadata["runtime_config"]["agent"]`，以保证 resume / replay 不被新的 runtime 默认值污染。

以下字段当前只作为 parsed config intent 保留，不代表 runtime 已经实现相应能力语义：

- `agent.tools`：不等同于已落地的 per-agent tool allowlist 或动态工具过滤。
- `agent.skills`：不等同于已落地的 per-agent skill activation policy。
- `AgentManifest.tool_allowlist` / `skill_refs` / `routing_hints`：仍属于声明层 metadata，不是执行治理 truth。

如果运行时收到 declaration-only preset 作为 active agent，例如：

```json
{"agent": {"preset": "worker"}}
```

则 runtime 会拒绝执行，而不是把 future role 悄悄映射到当前 deterministic 或 single-agent 路径。这保持了 `agent/` declaration layer 与 `runtime/` execution truth 的边界。

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
- 如果 `acp` startup / handshake 失败，运行时会将 ACP 状态标记为 `failed`，发出 `runtime.acp_failed`，并使本次运行通过已有失败路径结束；不会静默降级为 disconnected 继续执行。
- `lsp` 已支持最小的 runtime-managed server 启动与只读工具访问，并且 `lsp.servers` 已可消费内置 preset、extension/language 映射、root markers 与默认 command/preset override merge。
- 对用户来说，内置 LSP server 的规范配置面是 `lsp.servers.{builtin_name}: {}`；显式 `preset` 继续保留为兼容/alias 路径，而不是主配置入口。
- 当 repo-local `.voidcode.json` 未显式提供 `lsp` 配置时，运行时现在会为高置信度 workspace 自动推导最小默认值：当前覆盖 Python (`pyright`)、TypeScript/JavaScript (`tsserver`)、Go (`gopls`)、Rust (`rust-analyzer`)、C/C++ (`clangd`)、Java (`jdtls`)、Lua (`lua_ls`)、Zig (`zls`) 与 C# (`csharp-ls`)，并且只有在对应语言服务器可执行文件存在时才会启用。
- 显式的 repo-local `lsp` 配置（包括 `enabled: false`）继续具有最高优先级；自动推导默认值不会覆盖用户已声明的 server 列表或关闭语义。

## 工作区的引导规则

`workspace` 的解析不遵循与普通运行时配置字段相同的优先级阶梯。

它必须首先被确定，以便运行时发现该工作区下的任何仓库本地配置。在 MVP 中：

1. 显式的运行时/引导输入选择工作区根目录
2. 随后在该工作区内发现仓库本地配置
3. 普通运行时配置优先级适用于非引导字段，如 `model`、`approval_mode` 和 `hooks`

对于 hooks/config MVP，`approval_mode`、`model`、`execution_engine` 和 `max_steps` 的锁定优先级为：

1. 恢复会话中的 `SessionState.metadata["runtime_config"]`（仅恢复时）
2. 显式 CLI / 客户端覆盖
3. 仓库本地 `.voidcode.json`
4. 环境变量（`VOIDCODE_APPROVAL_MODE` / `VOIDCODE_MODEL` / `VOIDCODE_EXECUTION_ENGINE`）
5. 内置默认值

其余领域仍保持浅层仓库本地配置语义，不在此轨道中获得这条完整优先级引擎。

对于 fresh run，`RuntimeRequest.metadata["max_steps"]` 可以作为窄范围的请求级覆盖；一旦会话开始，该值会被持久化到 `SessionState.metadata["runtime_config"]`，并在后续 resume 时优先于新的 runtime 默认值。`execution_engine` 同样会进入显式 / 仓库本地 / 环境 / 默认值解析，并在会话恢复时优先采用持久化的会话配置。

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
    "max_steps": 6
  }
}
```

这有意设计得很窄：在 MVP 中，`approval_mode`、`model`、`execution_engine` 和 `max_steps` 是恢复关键字段；其中 `max_steps` 支持窄范围的请求级覆盖，并在会话启动后转化为持久化会话配置。

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
```

成功输出必须是 JSON，且当前至少包含：

- `workspace`
- `session_id`
- `approval_mode`
- `model`
- `execution_engine`
- `max_steps`
- `provider_fallback`
- `resolved_provider`

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
- 除 `approval_mode` / `model` / `execution_engine` / `max_steps` 外，其余扩展领域继续保持浅层仓库本地配置
- 仅 `approval_mode` / `model` / `execution_engine` / `max_steps` 在此轨道中具备恢复关键的优先级行为
- 当前的请求元数据是灵活的，但尚不属于稳定的公共模式（Schema）

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
