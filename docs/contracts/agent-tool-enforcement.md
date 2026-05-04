# Agent Tool Enforcement Contract

来源 Issue：#169

## 目的

定义 agent preset 中声明的 `tool_allowlist` / default tool set 如何进入 runtime execution truth。

这份契约要回答的不是“agent 想带什么工具”，而是：

> runtime 在真实执行路径中，如何决定一个 active agent 当前可以看见和调用哪些工具。

## 状态

当前仓库已经具备 runtime-owned per-agent tool enforcement：

- builtin `AgentManifest.tool_allowlist` 会进入 active agent 的工具边界计算；
- active `RuntimeAgentConfig.tools.allowlist` 会作为额外硬边界；
- active `RuntimeAgentConfig.tools.default` 只能进一步收窄默认暴露集合；
- provider-facing `available_tools` 与实际 tool lookup / invocation 使用同一个 filtered registry；
- approval resume 继续使用 session metadata 中持久化的 active agent config。

## 范围

这份契约只覆盖：

- active agent 的可见工具集合
- active agent 的可调用工具集合
- allowlist 与 default tool set 的 runtime 合并规则
- approval / resume / replay 下的一致行为

## 非目标

这份契约**不**定义：

- multi-agent delegation
- role routing
- tool calling prompt 文案
- client-side tool filtering
- TUI 特定行为
- MCP / skills 的完整能力治理 DSL

## 当前代码锚点

- `src/voidcode/agent/models.py`
- `src/voidcode/runtime/service.py`
- `src/voidcode/runtime/tool_provider.py`
- `src/voidcode/tools/contracts.py`

## 核心术语

### Active Agent

当前实际进入 execution path 的 agent preset。对于当前仓库，这意味着：

- 顶层 active run 接受 `leader`，也可显式选择 `product` 进入 planning 模式
- runtime-owned delegated child run 还可执行 `advisor`、`explore`、`researcher`、`worker`

这份契约不定义 delegation routing 本身，但一旦 child preset 已经被 runtime 选中进入真实执行路径，它与顶层 `leader` 一样要服从同一套 tool boundary enforcement。

### Tool Registry

runtime 当前构建出的完整可用工具注册表，包含：

- builtins
- optional tools
- runtime-managed MCP tools
- runtime-managed LSP / format tools（如存在）

### Tool Visibility

某工具是否会出现在 active agent 的 `available_tools` 中。

### Tool Invocation Enforcement

即使某工具名出现在请求或恢复路径里，runtime 是否允许实际解析并执行该工具。

## Contract 目标

runtime 对 tool boundary 的治理必须满足：

1. agent declaration 中的工具边界可以进入 execution truth；
2. `available_tools` 与实际可调用工具集合保持一致；
3. approval / resume / replay 不会绕过工具边界；
4. 工具过滤仍然是 runtime-owned，而不是客户端或 prompt-owned。

## 输入来源

runtime 在计算 active agent 工具集合时，允许消费以下输入：

1. runtime 当前构建出的 tool registry
2. builtin manifest 中的 `tool_allowlist`
3. active agent config 中的 `agent.tools.allowlist`
4. active agent config 中的 `agent.tools.default`

这些输入的角色不同：

- tool registry 负责表达“当前运行时实际上存在哪些工具”
- manifest `tool_allowlist` 负责表达“该 preset 最多可以使用哪些工具”
- `agent.tools.allowlist` 负责表达运行时配置/请求对 active agent 的额外硬边界
- `agent.tools.default` 负责表达“该 agent 默认暴露哪些工具”，且只能进一步收窄

## 合并规则

### Rule 1：Runtime registry 是上界

active agent 最终可见/可调用的工具必须永远是 runtime 当前注册表的子集。

如果 allowlist/default tool set 中引用了不存在的工具：

- runtime 不得虚构工具定义
- runtime 应忽略这些未知工具
- runtime 可以在后续实现中发出诊断或事件，但不能把未知工具当作可执行能力

### Rule 2：Allowlist 是硬边界

如果 active agent 存在 manifest `tool_allowlist` 或 `agent.tools.allowlist`，那么最终可见/可调用工具集合必须是这些边界的交集：

> runtime registry ∩ manifest.tool_allowlist ∩ agent.tools.allowlist

也就是说：

- 不在 allowlist 中的工具不得暴露给 active agent
- 不在 allowlist 中的工具不得通过 runtime resolve/invoke 成功执行

### Rule 3：Default tool set 只能进一步收窄，不得放宽 allowlist

`agent.tools.default` 只能在 allowlist 允许的范围内收窄默认暴露面，不能扩大工具权限。

### Rule 4：客户端不是 authority

CLI / Web / future clients 可以展示工具可见集合，但不能决定最终工具边界。

最终 authority 永远是 runtime。

## Execution Path 要求

### Fresh Run

在 fresh run 中：

1. runtime 先构建完整 tool registry
2. 再根据 active agent 计算最终工具边界
3. `available_tools` 只暴露最终过滤后的工具定义
4. tool lookup / invocation 只允许命中过滤后的集合

### Approval Resume

在 approval-resume 中：

- runtime 必须继续使用会话持久化的 active agent configuration
- 不能因为新的默认配置变化而放宽工具边界
- pending approval 对应的工具如果已不再属于该 agent 的可调用集合，应通过 runtime-owned failure path 明确失败，而不是静默执行

### Replay

replay 只重放历史 truth，不重新解释 agent 工具边界。

也就是说：

- replay 不应重新做工具授权决策
- replay 只展示已发生的事件序列

## 与 Approval 的关系

tool allowlist enforcement 发生在 approval 之前。

顺序应为：

1. active agent 工具边界判断
2. tool lookup
3. permission / approval 决策
4. tool invocation

如果工具已经不在 active agent 允许范围内，就不应进入 approval。

## 与 MCP / Optional Tools 的关系

这份契约同样适用于：

- MCP tools
- optional tools
- runtime-managed LSP / format tools

也就是说，`tool_allowlist` 的语义不能只覆盖 builtins。

如果某 MCP tool 在 registry 中存在，则它与 builtin tool 一样需要经过同样的可见性/调用性判断。Allowlist 支持 shell-style pattern，例如 `mcp/*`。

## 失败语义

当 active agent 请求了超出其工具边界的工具时：

- runtime 必须通过已有失败路径拒绝执行
- 不得静默降级为“工具不存在但继续猜测”
- 不得把该工具重新注入 `available_tools`

这类失败应被视为 runtime-owned governance failure，而不是 client rendering concern。

## 验收检查点

实现满足这份契约时，至少应能验证：

1. active agent 的 `available_tools` 已被 runtime 按边界过滤
2. 不在 allowlist 中的工具无法通过 runtime resolve/invoke 成功执行
3. MCP / optional tools 同样服从该边界
4. approval-resume 不会绕过该边界
5. replay 不会错误地重新做工具授权
