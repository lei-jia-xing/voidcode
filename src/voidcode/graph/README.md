# `voidcode.graph`

这里是 VoidCode 的执行编排层。

## 定位

`voidcode.graph` 负责描述和驱动具体的执行循环，例如确定性只读循环、provider-backed 单智能体路径，或未来更复杂的 multi-agent orchestration path。它关注步骤如何推进，而不是产品级治理如何统一。

## 负责什么

- 执行循环与步骤推进逻辑
- graph/request/response 级别的编排契约
- engine 内部的状态流转
- 可能由 LangGraph-backed 或非 LangGraph-backed implementation 提供的 orchestration path

## 不负责什么

- 运行时配置优先级
- 权限、审批与 hooks 管理
- 本地持久化与会话恢复真相
- 客户端传输与 UI 语义

## 边界关系

`voidcode.runtime` 负责选择和调用 graph，并为 graph 提供 resolved config、session state、tool metadata 和执行治理。graph 不应反向成为系统控制面。

未来如果引入 `voidcode.agent`，agent 定义与 agent preset/configuration 也应归属该边界，而不是让 `graph/` 直接承载命名 agent 的 prompt、hook、skill、MCP 或 tool 配置。

## 当前状态

当前这里仍以确定性执行切片为主，是 runtime 驱动下的编排层，而不是独立产品边界。未来 multi-agent 扩展可以增加 graph complexity，但不会改变 runtime-owned governance 的基本前提。
