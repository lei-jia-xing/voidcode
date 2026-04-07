# 面向客户端的运行时 API 契约

来源 Issue：#14

## 目的

定义客户端与无头运行时（Headless runtime）之间的 MVP 契约，用于运行请求、列出会话、加载会话状态、恢复会话以及订阅事件流。

## 状态

当前的方案已通过 CLI 和运行时方法实现了此契约，但尚未支持通过 HTTP 访问。

## 当前运行时请求/响应形状

源自 `src/voidcode/runtime/contracts.py`：

```python
RuntimeRequest(
    prompt: str,
    session_id: str | None = None,
    metadata: dict[str, object] = {},
)

RuntimeResponse(
    session: SessionState,
    events: tuple[EventEnvelope, ...] = (),
    output: str | None = None,
)
```

## 会话形状

源自 `src/voidcode/runtime/session.py`：

```python
SessionState(
    session: SessionRef(id: str),
    status: Literal["idle", "running", "waiting", "completed", "failed"],
    turn: int,
    metadata: dict[str, object],
)

StoredSessionSummary(
    session: SessionRef(id: str),
    status: SessionStatus,
    turn: int,
    prompt: str,
    updated_at: int,
)
```

## MVP 客户端操作

### 运行请求 (Run request)

输入：
- `prompt`
- 可选的 `session_id`
- 可选的客户端/运行时元数据

输出：
- 最终的 `session`
- 有序的 `events`
- 最终的 `output`

当前实现层面：
- 运行时：`VoidCodeRuntime.run(request)`
- CLI：`voidcode run <request> [--workspace] [--session-id]`

### 列出持久化会话 (List persisted sessions)

输出：
- `StoredSessionSummary` 的元组/列表

当前实现层面：
- 运行时：`VoidCodeRuntime.list_sessions()`
- CLI：`voidcode sessions list [--workspace]`

### 恢复持久化会话 (Resume persisted session)

输入：
- `session_id`

输出：
- 存储的该会话重放的 `RuntimeResponse`

当前实现层面：
- 运行时：`VoidCodeRuntime.resume(session_id)`
- CLI：`voidcode sessions resume <session_id> [--workspace]`

## 会话生命周期

MVP 生命周期：

1. 客户端提交一个运行请求
2. 运行时创建或重用一个会话 ID
3. 运行时在轮次中发出有序事件
4. 运行时终结一个响应
5. 运行时持久化会话摘要、事件和输出
6. 客户端后续可以列出或恢复会话

## 当前持久化会话行为

目前的实现可以持久化足以支持以下操作的数据：

- `sessions list` 返回 `StoredSessionSummary`
- `sessions resume <id>` 重放存储的响应

目前的集成测试验证了恢复（resume）会返回存储的输出和会话的存储事件序列。

## API 不变量

- 客户端必须将运行时视为系统边界
- 客户端不直接调用工具
- 客户端不创建与持久化的运行时状态相背离的私有会话状态
- 恢复（resume）返回可重放的、已存储的响应，而非根据 UI 状态推断出的重建版本
- 客户端必须按交付时的顺序保存运行时的有序事件，包括由后续图模式在现有阶段之间插入的补充事件类型
- 客户端必须能够容忍额外的有序补充事件，而不能假设确定性回退序列就是详尽无遗的

## 未来 HTTP/流式传输映射

当 HTTP 层存在时，它应保留相同的操作边界：

- 运行/创建会话
- 列出会话
- 加载/恢复会话
- 订阅或接收运行时的有序事件

本文档有意地定义了独立于 FastAPI/Starlette 路由详情的契约。
确定性的回退事件序列在今天仍是规范，未来的图模式可能会在现有阶段之间添加有序事件，而不会改变这些 API 边界。

## 非目标

- 完整的传输层实现
- post-MVP 的多智能体会话拓扑
- 特定于供应商的请求格式

## 验收检查点

- TUI 和 Web 客户端可以在不绕过运行时方法或概念的情况下实现
- 可以使用稳定的会话摘要和存储响应形状来列出和恢复持久化的会话
- 未来的 API 路由可以直接映射到这些操作上，而无需更改语义
