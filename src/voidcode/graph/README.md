# `voidcode.graph`

这里是 VoidCode 的执行编排层。

## 定位

`voidcode.graph` 负责描述和驱动具体的执行循环，例如确定性只读循环、provider-backed 单智能体路径，或未来更复杂的 multi-agent orchestration path。它关注步骤如何推进，而不是产品级治理如何统一。

## 负责什么

- 执行循环与步骤推进逻辑
- graph/request/response 级别的编排契约
- engine 内部的状态流转
- 由仓库内 plain-Python implementations 提供的 orchestration path

## 不负责什么

- 运行时配置优先级
- 权限、审批与 hooks 管理
- 本地持久化与会话恢复真相
- 客户端传输与 UI 语义

## 边界关系

`voidcode.runtime` 负责选择和调用 graph，并为 graph 提供 resolved config、session state、tool metadata 和执行治理。graph 不应反向成为系统控制面。

provider-facing transcript、segment 和结果 view 由 `voidcode.core.transcript` 定义；graph 直接消费这些中立类型，不导入 runtime context。runtime 的预算、continuity projection、历史 replay 和授权不进入这些 lower contracts。命令交付事件仍由 `runtime.events` 定义，command package 不反向 re-export 它。

未来如果引入 `voidcode.agent`，agent 定义与 agent preset/configuration 也应归属该边界，而不是让 `graph/` 直接承载命名 agent 的 prompt、hook、skill、MCP 或 tool 配置。

## 当前状态

P1 已完成 lower contracts 的中立化，现有 deterministic/provider graph 步骤可在无 runtime/SQLite/UI 的进程运行。完整会话循环仍由 runtime 驱动；P3 将切换到实际 turn engine，不把当前单 agent 循环包装成通用 DAG/workflow。
