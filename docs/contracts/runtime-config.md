# 运行时配置契约

来源 Issue：#16

## 目的

定义使 MVP 运行时具备可配置性所需的最小配置界面，同时确保系统是受控的而非过于宽泛。

## 状态

当前运行时从仓库本地的 `.voidcode.json` 中加载以下已实现领域的配置：

- `approval_mode`
- `model`
- `max_steps`
- `hooks`
- `tools`
- `skills`
- `lsp`
- `acp`

目前 hooks/config 的 MVP 收敛目标已经锁定：

- hooks 仅限运行时拥有（runtime-owned）的 `pre_tool` / `post_tool`
- 完整优先级仅适用于 `approval_mode` 和 `model`
- 单一可见检查面为 CLI：`voidcode config show --workspace <path> [--session <id>]`
- 恢复会话的配置覆盖仅存放在 `SessionState.metadata["runtime_config"]`

## MVP 配置领域

MVP 配置界面应仅覆盖以下区域：

- 工作区根目录（workspace root）
- 模型/供应商选择
- 审批模式
- 钩子（hook）的启用/默认值
- 工具发现/供应商默认值
- 技能发现默认值
- LSP 和 ACP 的扩展基础设施开关
- 恢复（resume）所需的客户端可见会话设置

## 计划的最小配置形状

MVP 契约应能够表示一个至少包含以下内容的运行时配置对象：

```json
{
  "workspace": "/workspace/project",
  "model": "opencode/gpt-5.4",
  "approval_mode": "ask",
  "max_steps": 4,
  "hooks": {
    "enabled": true,
    "pre_tool": [["python", "scripts/pre_tool.py"]],
    "post_tool": [["python", "scripts/post_tool.py"]]
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
  "lsp": {
    "enabled": false,
    "servers": {}
  },
  "acp": {
    "enabled": false
  }
}
```

字段意图：

- `workspace`：引导（bootstrap）字段，用于在发现仓库本地配置之前确定运行时工作区根目录，随后重用于工具执行和持久化
- `model`：OpenCode `provider/model` 格式的供应商/模型标识符
- `approval_mode`：由运行时治理的工具所使用的最小执行策略模式
- `max_steps`：execution engine 的最大 step budget，作用于 deterministic 与 single-agent engine
- `hooks`：运行时拥有的最小钩子配置对象，仅覆盖 pre/post tool 执行
- `tools`：内置工具启用的最小配置，以及额外的工具搜索路径
- `skills`：技能发现启用的最小配置，以及额外的技能搜索路径
- `lsp`：未来语言服务器（Language-server）集成的最小基础设施配置容器
- `acp`：未来 ACP 集成的最小基础设施启用开关

## 当前实现的仓库本地形状

当前的 `.voidcode.json` 解析器接受以下仓库本地形状：

- `approval_mode`：`allow`、`deny`、`ask` 之一
- `model`：字符串
- `max_steps`：大于等于 1 的整数
- `hooks.enabled`：布尔值
- `hooks.pre_tool`：命令数组的数组，每个命令在 workspace cwd 中执行
- `hooks.post_tool`：命令数组的数组，每个命令在 workspace cwd 中执行
- `tools.builtin.enabled`：布尔值
- `tools.paths`：字符串数组
- `skills.enabled`：布尔值
- `skills.paths`：字符串数组
- `lsp.enabled`：布尔值
- `lsp.servers`：对象
- `acp.enabled`：布尔值

所有扩展领域字段都是可选的。省略时，它们在领域级别解析为 `None`，并且数组字段在提供的领域对象内部默认回退为空元组。

## 关于 LSP 和 ACP 仅限基础设施的说明

在当前切片中，`lsp` 和 `acp` 仅作为配置领域存在。

- 它们的存在是为了让后续的运行时启动工作能够消费稳定的类型化配置。
- 它们**不**意味着 LSP 支持的工具或 ACP 传输在今天已处于激活状态。
- `lsp.servers` 目前仅是一个浅层对象容器；此处尚未实现服务器验证或启动行为。

## 工作区的引导规则

`workspace` 的解析不遵循与普通运行时配置字段相同的优先级阶梯。

它必须首先被确定，以便运行时发现该工作区下的任何仓库本地配置。在 MVP 中：

1. 显式的运行时/引导输入选择工作区根目录
2. 随后在该工作区内发现仓库本地配置
3. 普通运行时配置优先级适用于非引导字段，如 `model`、`approval_mode` 和 `hooks`

对于 hooks/config MVP，`approval_mode`、`model` 和 `max_steps` 的锁定优先级为：

1. 恢复会话中的 `SessionState.metadata["runtime_config"]`（仅恢复时）
2. 显式 CLI / 客户端覆盖
3. 仓库本地 `.voidcode.json`
4. 环境变量（`VOIDCODE_APPROVAL_MODE` / `VOIDCODE_MODEL`）
5. 内置默认值

其余领域仍保持浅层仓库本地配置语义，不在此轨道中获得完整优先级引擎。

对于 fresh run，`RuntimeRequest.metadata["max_steps"]` 可以作为窄范围的请求级覆盖；一旦会话开始，该值会被持久化到 `SessionState.metadata["runtime_config"]`，并在后续 resume 时优先于新的 runtime 默认值。

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
    "max_steps": 6
  }
}
```

这有意设计得很窄：在 MVP 中，`approval_mode`、`model` 和 `max_steps` 是恢复关键的字段；其中 `max_steps` 支持窄范围的请求级覆盖，并在会话启动后转化为持久化会话配置。

## 会话持久化设置

关键的恢复设置应随会话一起持久化，至少包括：

- 工作区（现有持久化字段）
- 审批模式
- 与确定性恢复行为相关的已选模型/供应商
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

成功输出必须是 JSON，且仅包含：

- `workspace`
- `session_id`
- `approval_mode`
- `model`
- `execution_engine`
- `max_steps`

失败契约锁定为：

- invalid workspace → 非零退出码，stderr 文本错误，无 JSON
- nonexistent session → 非零退出码，stderr 文本错误，无 JSON
- workspace/session mismatch → 非零退出码，stderr 文本错误，无 JSON

## 不变量

- 用户无需编辑代码即可更改运行时行为
- 优先级必须是确定性的
- 持久化会话必须携带足够的配置，以便进行有意义的重放或恢复
- MVP 配置界面必须专注于单智能体

## 当前限制

- hooks 在此轨道中仅限 pre/post tool 执行；不包含 session-start/session-end/render/message-transform 等阶段
- hooks 不得改变工具参数或结果，只能观察与失败中止
- 除 `approval_mode` / `model` / `max_steps` 外，其余扩展领域继续保持浅层仓库本地配置
- 仅 `approval_mode` / `model` / `max_steps` 在此轨道中具备恢复关键的优先级行为
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
