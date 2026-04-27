# `voidcode.agent`

这里是 VoidCode 已经落在仓库中的 agent 声明层目录。

## 定位

`voidcode.agent` 不负责执行，它负责描述**每一种 agent preset 是什么**，包括角色定位、builtin manifest、builtin prompt materialization、默认权限、建议 skills、建议 hooks 与能力绑定方向。

它的目标不是替代 `runtime/`，而是把“agent 是什么、默认带什么组合”从运行时治理逻辑里拆出来，形成一个薄的、声明式的组合层。

当前这一层仍然是**声明式 preset / preset-intent 层**，不是独立的 agent runtime，也不是 multi-agent 已落地的证据。

## 负责什么

- agent 角色说明
- builtin manifest 校验
- builtin prompt/profile 文本与 materialization
- preset 级别的职责边界
- 默认权限倾向
- 建议 skill 绑定
- 建议 hook 关注点
- 与 runtime 的边界说明

## 不负责什么

- session 持久化与恢复
- approval / permission 决策
- runtime event truth
- tool 实际执行
- hook 执行时机
- MCP / LSP / ACP lifecycle
- 当前 multi-agent orchestration

## 当前角色集

- [`leader`](./leader/README.md)
- [`worker`](./worker/README.md)
- [`advisor`](./advisor/README.md)
- [`explore`](./explore/README.md)
- [`researcher`](./researcher/README.md)
- [`product`](./product/README.md)

当前 builtin manifest 已覆盖以下角色：

- `leader`
- `worker`
- `advisor`
- `explore`
- `researcher`
- `product`

其中 `leader` 是默认的顶层执行/编码 preset；`product` 现在也是可显式选择的顶层规划 preset，用于需求讨论、范围收敛、验收标准与 issue shaping。`worker`、`advisor`、`explore`、`researcher` 仍主要作为 runtime-owned delegated child presets 执行，不是任意可选的顶层 active agent。

## Preset intent vs runtime truth

本目录描述的是“一个角色默认希望带什么组合”，不是“runtime 今天已经能怎样执行它”。

- `leader`：默认的顶层执行/编码角色；`preset` / `prompt_profile` / `model` / `execution_engine` / `tools` / `skills` / `provider_fallback` 会进入 runtime config truth 并随 session 持久化
- `product`：可显式选择的顶层规划角色，用于需求讨论、范围收敛、验收标准与 issue 草拟；它不是新的 orchestrator，也不执行代码修改
- `worker`：delegated focused executor preset；可进入 child execution，但不作为任意顶层 active agent 直接运行
- `advisor`：delegated advisory preset；可进入 child execution，但不作为任意顶层 active agent 直接运行
- `explore`：delegated local-code exploration preset；可进入 child execution，但不作为任意顶层 active agent 直接运行
- `researcher`：delegated external research preset；可进入 child execution，但不作为任意顶层 active agent 直接运行

当前 builtin preset 都有 agent-owned prompt profile；active / delegated agent 的 manifest allowlist 会收窄 provider 可见的 `available_tools`，并且同一边界也会约束实际 tool lookup / invocation。builtin `prompt_profile` 由 `src/voidcode/agent/` 统一 materialize 后进入 provider system message，`model_preference` / `execution_engine` 会作为 manifest live defaults 被 runtime 解析，manifest `skill_refs` 会作为默认 skill selection 进入 runtime skill application，`agent.skills` 会覆盖本次运行使用的 runtime-managed skill discovery / application policy。

`prompt_materialization` 是 prompt 审计元数据：它声明 builtin prompt profile、materialization version、source/format，以及可选的 `model_family_overrides`。当前 builtin agents 仍共享各自默认 profile，但这个结构允许后续在不改变执行拓扑的前提下，为特定模型族选择不同 profile。profile 选择规则属于 agent declaration 层；最终 provider system message 的组装仍由 runtime/provider 路径负责。

`top_level_selectable` 显式声明一个 manifest 是否允许作为顶层 active agent 被选择。当前 `leader` 与 `product` 为 `true`：`leader` 是默认执行/编码模式，`product` 是显式规划/需求模式。`worker`、`advisor`、`explore`、`researcher` 仍是 delegated/internal presets，不能作为任意顶层 active agent 选择。runtime 仍通过自己的 `_EXECUTABLE_AGENT_PRESETS` 做执行时 enforcement，测试会校验该 allowlist 与 manifest 声明保持一致。

此外，builtin prompt 文本 ownership 现在明确归属 `src/voidcode/agent/`：agent 层负责 builtin prompt 内容、profile materialization 与 manifest 校验，provider 层只负责 message assembly 和模型调用，不再持有 role-specific persona 源码副本。

## 与 runtime 的边界

`voidcode.agent` 可以描述“这个角色默认希望带哪些工具/skills/hooks/MCP profile”。其中 builtin preset 的 prompt profile、工具边界、skills、model、execution engine 与 provider fallback 都会被 runtime 按当前执行边界消费；但这些声明仍不能决定系统最终如何执行、治理、审批、恢复和持久化它。

当前 agent manifest 内部也区分了两类语义：

- **live defaults**：`prompt_profile`、`prompt_materialization`、`top_level_selectable`、`execution_engine`、`model_preference`、`tool_allowlist`、`skill_refs`。这些字段要么已经被 runtime 直接消费，要么作为 active agent 的默认值进入 runtime config truth。`top_level_selectable` 是 declaration，runtime enforcement 仍由 `_EXECUTABLE_AGENT_PRESETS` 持有；`prompt_materialization` 是 declaration，runtime/provider materialization 仍使用 agent 层导出的 prompt rendering helper。
- **intent metadata**：`routing_hints`。它仍属于声明层元数据，不是 runtime execution governance truth。

最终的执行真相仍然由 `voidcode.runtime` 持有。

这也意味着：本目录中出现的“建议 hooks / 建议能力”只是在描述 preset intent，不代表 runtime 会把治理权让渡给 agent 层。当前现实里，background task、child-session、notification、result retrieval、tool enforcement、approval 与持久化仍全部由 runtime 持有。

从 OMO/OMOA 的经验看，更值得借鉴的是以下结构判断，而不是直接照搬执行语义：

- 角色要有清晰的 responsibility boundary，而不是只有 prompt 名称
- 窄职责角色应当配更窄的工具权限与更明确的 skill 附着
- `explore` 与 `researcher` 应分开：前者面向本地仓库，后者面向外部资料
- `product` 更像 pre-plan / acceptance 对齐角色，而不是新的 orchestrator
- hook 应按 event 类型表达系统干预点，而不是混成角色自己的执行权

## 相关文档

- [`docs/agent-architecture.md`](../../../docs/agent-architecture.md)
- [`docs/agent-boundary.md`](../../../docs/agent-boundary.md)
- [`docs/architecture.md`](../../../docs/architecture.md)
