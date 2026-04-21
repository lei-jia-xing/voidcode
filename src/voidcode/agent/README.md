# `voidcode.agent`

这里是 VoidCode 已经落在仓库中的 agent 声明层目录。

## 定位

`voidcode.agent` 不负责执行，它负责描述**每一种 agent preset 是什么**，包括角色定位、默认权限、建议 skills、建议 hooks 与能力绑定方向。

它的目标不是替代 `runtime/`，而是把“agent 是什么、默认带什么组合”从运行时治理逻辑里拆出来，形成一个薄的、声明式的组合层。

当前这一层仍然是**声明式 preset / preset-intent 层**，不是独立的 agent runtime，也不是 multi-agent 已落地的证据。

## 负责什么

- agent 角色说明
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

其中只有 `leader` 对应今天真实存在的单 agent 主路径，其余角色都仍然是 post-MVP 的 future preset。Runtime 当前会解析这些 future preset 以验证声明层 shape，但会拒绝把它们作为 active execution agent 运行。

## Preset intent vs runtime truth

本目录描述的是“一个角色默认希望带什么组合”，不是“runtime 今天已经能怎样执行它”。

- `leader`：当前唯一映射到真实执行路径的角色；`preset` / `prompt_profile` / `model` / `execution_engine` / `tools` / `skills` / `provider_fallback` 会进入 runtime config truth 并随 session 持久化
- `worker`：future focused executor preset，不代表今天已经有 delegated runtime
- `advisor`：future advisory preset，不代表今天已有独立审查/判断 runtime
- `explore`：future local-code exploration preset，不代表今天已有独立 explore session model
- `researcher`：future external research preset，不代表今天已有独立 researcher orchestration
- `product`：future scope/alignment preset，不代表今天已有需求对齐 gate runtime

当前 `leader` 是第一阶段 runtime-managed agent slice：active agent 的 manifest allowlist 会收窄 provider 可见的 `available_tools`，并且同一边界也会约束实际 tool lookup / invocation。`leader` 的 `prompt_profile` 会进入 provider system message，`model` / `execution_engine` / `provider_fallback` 会决定 provider-backed single-agent 主路径，manifest `skill_refs` 会作为默认 skill selection 进入 runtime skill application，`agent.skills` 会覆盖本次运行使用的 runtime-managed skill discovery / application policy。

## 与 runtime 的边界

`voidcode.agent` 可以描述“这个角色默认希望带哪些工具/skills/hooks/MCP profile”。其中 `leader` 的 prompt profile、工具边界、skills、model、execution engine 与 provider fallback 已进入 runtime enforcement / persistence；其他 preset 的能力绑定仍不能决定系统最终如何执行、治理、审批、恢复和持久化它。

最终的执行真相仍然由 `voidcode.runtime` 持有。

这也意味着：本目录中出现的“建议 hooks / 建议能力”只是在描述未来 preset 希望依赖什么，不代表 runtime 今天已经支持对应的 lifecycle phase。以当前现实看，hooks 仍然只覆盖 runtime-owned 的 `pre_tool` / `post_tool`；background task、child-session、leader notification、result retrieval 等 async agent substrate 仍未落地。

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
