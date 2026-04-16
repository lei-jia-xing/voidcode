# `voidcode.agent`

这里是 VoidCode 已经落在仓库中的 agent 声明层目录。

## 定位

`voidcode.agent` 不负责执行，它负责描述**每一种 agent preset 是什么**，包括角色定位、默认权限、建议 skills、建议 hooks 与能力绑定方向。

它的目标不是替代 `runtime/`，而是把“agent 是什么、默认带什么组合”从运行时治理逻辑里拆出来，形成一个薄的、声明式的组合层。

当前这一层仍然是**文档化 / preset-intent 层**，不是独立的 agent runtime，也不是 multi-agent 已落地的证据。

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

其中只有 `leader` 对应今天真实存在的单 agent 主路径，其余角色都仍然是 post-MVP 的 preset 方向。

## 与 runtime 的边界

`voidcode.agent` 可以描述“这个角色默认希望带哪些工具/skills/hooks/MCP profile”，但不能决定系统最终如何执行、治理、审批、恢复和持久化它。

最终的执行真相仍然由 `voidcode.runtime` 持有。

这也意味着：本目录中出现的“建议 hooks / 建议能力”只是在描述未来 preset 希望依赖什么，不代表 runtime 今天已经支持对应的 lifecycle phase。以当前现实看，hooks 仍然只覆盖 runtime-owned 的 `pre_tool` / `post_tool`；background task、child-session、leader notification、result retrieval 等 async agent substrate 仍未落地。

## 相关文档

- [`docs/agent-architecture.md`](../../../docs/agent-architecture.md)
- [`docs/agent-boundary.md`](../../../docs/agent-boundary.md)
- [`docs/architecture.md`](../../../docs/architecture.md)
