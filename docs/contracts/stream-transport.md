# 流传输契约

来源 Issue：#17

## 目的

定义向 CLI、TUI 和 Web 客户端交付运行时事件及最终输出的 MVP 传输预期。

## 状态

当前代码已经通过进程内运行时响应、CLI 打印以及极简 HTTP/SSE 路径公开有序事件和最终输出。

CLI 仍是目前最完整的流消费者；TUI 和 Web 已具备最小可用的运行时传输路径，但交互层仍在继续完善。

对当前 MVP/runtime 架构而言，服务端到客户端的实时事件交付基线是 **HTTP + SSE**。审批解析等客户端到运行时的动作继续通过普通 HTTP 请求完成。除非后续进入 PTY 或其他明确的双向流场景，WebSocket 不是当前主路径的必需项。

## 传输职责

传输层必须交付：

- 有序的运行时事件
- 最终输出
- 足以将会话流与持久化和恢复相关联的会话标识

它不得：

- 绕过运行时治理
- 创建无法恢复的私有客户端专有状态

## MVP 交付语义

- 流以会话为作用域
- 事件排序遵循 `EventEnvelope.sequence`
- 客户端必须能够在最终输出产生前渲染阶段性进展
- 持久化重放必须保留与实时交付相同的可观测排序模型
- 运行时内部可以为恢复维护 checkpoint / resume anchor，但这不会改变客户端可见的完整事件重放契约

### Tool-call 参数增量与写入预览

Provider streaming 可发送 `graph.tool_call_start`、`graph.tool_call_delta` 和
`graph.tool_call_end` 事件。事件按 `tool_call_id`（并保留 ordinal）关联；
`arguments_delta` 是可拼接的原始 JSON 片段，不能被当作完整工具输入执行。

这些参数增量是 **live-only** 观察数据，不写入持久化 session truth，也不出现在
replay 中。对于 `write`、`edit`、`multi_edit`、`apply_patch`，runtime/graph 会
在相应 lifecycle payload 中提供有界的 `diff_preview`；写工具事件不会把
`arguments_delta` 或 `parsed_arguments` 直接交给客户端。`diff_preview` 仅由
workspace 当前快照读取生成，不执行工具、不创建目录、不写入临时 patch；它带有
`schema_version`、`phase`、`status`、`bounded`、`truncated`、目标路径（或 paths）
以及有界 `diff`/统计/hash。路径不安全、快照不可读、输入不完整或超限时，
`status` 为 `degraded` 并给出稳定 `reason`，不会中断运行主循环。

最终完整参数仍必须走现有 hash/permission/approval/tool execution 边界。完整的
`graph.tool_request_created` 可以带 `diff_preview`（`phase=final`）；真实执行的
`runtime.tool_completed` 结果是最终 diff 的权威来源，可覆盖/对齐任何 live preview。
partial preview 不参与 session truth、checkpoint 或 replay；provider 不支持参数
增量时继续使用 completed-only tool-call 兼容路径。

## 客户端预期

### CLI
- 消费完整的响应，并按顺序打印事件

### TUI
- 将有序事件作为当前轮次的活动时间线进行消费
- 能够在实时流与持久化重放之间切换，而不会产生语义偏差

### Web 客户端
- 消费与 TUI 相同的事件语义
- 根据运行时提供的数据渲染事件进展、工具活动、审批以及最终输出

## 推荐的传输抽象

运行时应公开一个传输中立（transport-neutral）的事件流契约。对于当前仓库，已落地并应被视为主路径的绑定包括：

- 针对本地客户端的进程内迭代
- HTTP 分块或 SSE 风格的交付

未来如果出现明确的双向流需求，可以再增加 WebSocket 等额外绑定；但这不是当前 MVP 契约的隐含要求。

本文档定义行为，而非最终的有线协议（Wire protocol）实现。

## 不变量

- 实时交付与重放共享相同的事件词汇表
- 客户端无需解析面向人类的文本输出即可显示进展
- 最终输出不能取代对有序事件可见性的需求
- 内部 retention / checkpoint 优化不得改变客户端观察到的有序 replay 语义，除非契约文档显式更新

## 非目标

- 为当前主路径之外的未来双向传输方案背书
- post-MVP 的多智能体多路复用语义
- Token/成本遥测传输要求

## 验收检查点

- 契约足以实现一个实时流消费者和一个重放消费者
- 后续可以更改传输方案，而无需更改事件模式（Schema）本身
- 运行时持久化仍是重放行为的权威来源
