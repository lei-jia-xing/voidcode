# 运行时配置契约

来源 Issue：#16

## 目的

定义使 MVP 运行时具备可配置性所需的最小配置界面，同时确保系统是受控的而非过于宽泛。

## 状态

当前运行时从仓库本地的 `.voidcode.json` 中加载以下已实现领域的配置：

- `approval_mode`
- `model`
- `hooks`
- `tools`
- `skills`
- `lsp`
- `acp`

目前仅 `approval_mode` 具备多源优先级逻辑。此切片中的扩展领域仅支持配置模式（Config-schema）。

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
  "hooks": {
    "enabled": true
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
- `hooks`：运行时钩子行为的最小开关/配置对象
- `tools`：内置工具启用的最小配置，以及额外的工具搜索路径
- `skills`：技能发现启用的最小配置，以及额外的技能搜索路径
- `lsp`：未来语言服务器（Language-server）集成的最小基础设施配置容器
- `acp`：未来 ACP 集成的最小基础设施启用开关

## 当前实现的仓库本地形状

当前的 `.voidcode.json` 解析器接受以下仓库本地形状：

- `approval_mode`：`allow`、`deny`、`ask` 之一
- `model`：字符串
- `hooks.enabled`：布尔值
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

对于当前实现的加载器，`model`、`hooks`、`tools`、`skills`、`lsp` 和 `acp` 的仓库本地值直接从 `.voidcode.json` 加载，而 `approval_mode` 保持其“显式 > 仓库本地 > 环境变量 > 默认”的优先级行为。

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

对于恢复的会话，在运行时选择支持可覆盖字段的情况下，应允许全新的显式客户端或 CLI 输入覆盖持久化的会话设置。持久化的会话设置是恢复时的基准，而非对全新显式输入的绝对覆盖。

## 计划的会话覆盖形状

会话作用域的覆盖应能与仓库默认值分开表示。最小的覆盖形状应支持：

```json
{
  "session_id": "session-123",
  "overrides": {
    "model": "opencode/gpt-5.4-pro",
    "approval_mode": "ask"
  }
}
```

这有意设计得很窄：在 MVP 中，只有实质性影响运行时行为或恢复语义的设置才应是会话可覆盖的。

## 会话持久化设置

关键的恢复设置应随会话一起持久化，至少包括：

- 工作区
- 审批模式
- 与确定性恢复行为相关的已选模型/供应商
- 任何会改变客户端解释会话方式的运行时模式

## 当前代码映射

代码库中当前的具体存储/映射点包括：

- `VoidCodeRuntime(workspace=...)` 提供活跃的工作区根目录
- `RuntimeRequest.metadata` 是当前的灵活请求作用域容器
- `SessionState.metadata` 在内存中存储运行时/会话元数据
- SQLite 会话存储将 `SessionState.metadata` 作为持久化会话 payload 的一部分进行保存
- SQLite 会话存储还将 `workspace` 持久化为 `sessions.workspace` 中的一等公民列，并将其用于会话列出和查找

今天，上述仓库本地模式已实现，而更广泛的会话覆盖和特定于恢复的配置行为仍有意保持在较窄范围，且仅部分实现。

## 不变量

- 用户无需编辑代码即可更改运行时行为
- 优先级必须是确定性的
- 持久化会话必须携带足够的配置，以便进行有意义的重放或恢复
- MVP 配置界面必须专注于单智能体

## 当前限制

- 对于扩展领域，仓库本地配置有意保持浅层，且尚未接入运行时行为
- 目前仅 `approval_mode` 记录了环境变量支持 (`VOIDCODE_APPROVAL_MODE`)
- 当前的请求元数据是灵活的，但尚不属于稳定的公共模式（Schema）

## 非目标

- 高级的多智能体配置
- 特定于供应商的机密管理详情
- 完整的策略 DSL

## 验收检查点

- 存在一份配置文档，供后续实现直接遵循
- 持久化会话契约显式指出了哪些设置在恢复后依然有效
- 配置优先级已被记录，并被 TUI/Web 实现工作所复用
- 配置文档包含仓库/运行时默认值和会话级覆盖的最小具体形状
