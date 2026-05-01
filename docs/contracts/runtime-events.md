# 运行时事件模式（Schema）

来源 Issue：#13

## 目的

定义运行时为客户端渲染而发出的 MVP 事件词汇表。

## 状态

此模式记录了当前 MVP 的运行时事件契约。它有意地比任何未来的多角色 / multi-agent 协议更窄。
确定性的回退序列（fallback sequence）对于当前运行时仍然是规范的。未来的图模式（graph modes）可能会在现有阶段之间添加有序事件，而不会改变当前的回退行为。
对于全新运行和审批后的恢复运行，运行时会将图端的终结事件重新编号为活跃的运行时序列，从而避免图端固定的序列值与插入的运行时事件发生冲突。

## 规范信封

当前 `src/voidcode/runtime/events.py` 中的代码形状：

```python
EventEnvelope(
    session_id: str,
    sequence: int,
    event_type: str,
    source: Literal["runtime", "graph", "tool"],
    payload: dict[str, object],
)
```

## 字段规则

- `session_id`：必填；标识所属会话
- `sequence`：必填；在会话响应或重放中单调递增
- `event_type`：必填；事件类型的字符串标识符
- `source`：必填；`runtime`、`graph` 或 `tool` 之一
- `payload`：必填字段；可以是一个空对象

## MVP 不变量

- 事件以会话为作用域
- 事件按 `sequence` 排序
- 客户端在渲染轮次或重放时必须保持事件顺序
- 客户端必须能够容忍未知的 `event_type` 值，采用通用方式渲染而非报错
- 客户端必须将 `payload` 视为可扩展的

## 当前稳定事件词汇表

以下事件当前属于稳定的运行时事件契约，源自 `src/voidcode/runtime/service.py`、`src/voidcode/graph/deterministic_graph.py` 与 `src/voidcode/graph/provider_graph.py`。它们覆盖当前 deterministic 与 provider 两条 execution engine 路径：

- `runtime.request_received`
- `runtime.skills_loaded`
- `runtime.skills_applied`
- `runtime.acp_connected`
- `runtime.acp_disconnected`
- `runtime.acp_failed`
- `graph.loop_step`
- `graph.model_turn`
- `graph.tool_request_created`
- `runtime.tool_lookup_succeeded`
- `runtime.tool_started`
- `runtime.approval_requested`
- `runtime.approval_resolved`
- `runtime.permission_resolved`
- `runtime.tool_hook_pre`
- `runtime.tool_hook_post`
- `runtime.tool_progress`
- `runtime.tool_completed`
- `runtime.failed`
- `graph.response_ready`

在轮次中发出的所有事件（包括来自图端的事件）都会由运行时重新编号，变为每次响应或重放中单一的、单调递增的序列。
这确保了图端局部（graph-local）的序列值在跨审批恢复运行时，不会与运行时插入的事件发生冲突。

## 已交付的 delegated/background-task 事件

以下事件名称当前已经是 shipped delegated execution surface 的一部分：

- `runtime.background_task_waiting_approval`
- `runtime.background_task_completed`
- `runtime.background_task_failed`
- `runtime.background_task_cancelled`
- `runtime.delegated_result_available`

它们在 `src/voidcode/runtime/events.py` 中仍被归类在 `PrototypeAdditiveEventType`，这是代码内兼容性分组，而不是“尚未支持”的意思。CLI、HTTP、会话重放与 background-task result/output surfaces 都已经消费这些事件。

## 未来补充 / additive 词汇表

当前真正仍保持 additive/prototype 语义的共享事件名称至少包括：

- `runtime.memory_refreshed`

未来版本可以追加新的事件类型或为现有 payload 增加新字段；客户端必须继续容忍未知事件类型，并将 payload 视为可扩展结构。

## 当前 execution engine 循环的事件序列

运行时和集成测试断言了具有单个已审批工具调用的轮次的有序序列：

1. `runtime.request_received`
2. `runtime.skills_loaded`
3. `runtime.acp_connected`（仅在 ACP 已启用且 startup/handshake 成功时出现）
4. `runtime.skills_applied`（仅在本次 run 存在已启用 skill 时出现）
5. `graph.loop_step`
6. `graph.model_turn`
7. `graph.tool_request_created`
8. `runtime.tool_lookup_succeeded`
9. 对于 `ask` 策略发出 `runtime.approval_requested`；或者对于 `allow`/`deny` 策略发出 `runtime.approval_resolved`；或者对于只读操作发出 `runtime.permission_resolved`
10. `runtime.approval_resolved`（仅在 `ask` 后恢复运行时）
11. `runtime.tool_started`
12. `runtime.tool_progress`（仅支持增量输出的工具在执行中可发出 0 次或多次）
13. `runtime.tool_completed`
14. `graph.loop_step`
15. `graph.response_ready`
16. `runtime.acp_disconnected`（仅在 ACP 已启用且本次 run 结束时出现）

当前已实现的最小 hooks 路径会在非只读工具的成功执行周围插入：

- `runtime.tool_hook_pre`
- `runtime.tool_started`
- `runtime.tool_progress`（可选，0 次或多次）
- `runtime.tool_completed`
- `runtime.tool_hook_post`

如果 pre-hook 失败，工具调用必须在执行前中止，并通过已有失败路径对外可见。

### `runtime.tool_progress`
- source: `tool`
- 当前 payload:
  - `tool: str`
  - `tool_call_id: str`，与同一次工具调用的 `runtime.tool_started` / `runtime.tool_completed` 一致
  - `stream: str`，当前用于 `shell_exec` 的 `stdout` 或 `stderr`
  - `chunk: str`，本次增量输出的有界文本片段
  - `chunk_char_count: int`，本次原始文本片段长度
  - `truncated: bool`，表示本次 progress payload 是否因事件大小上限而截断
  - `dropped_progress_events: int`（可选），表示 runtime 为保护内存而合并/丢弃的 progress 事件数量
- 该事件只描述正在运行工具的观测进度；最终、模型可见且可重放的工具结果仍以 `runtime.tool_completed` 为准。
- 客户端必须将 progress 当作增量输出流，不能用它替代 terminal tool result，也不能假设每个输出字节都会对应一个 progress 事件。

此序列是目前实现的、最具体的、客户端可见的 MVP 事件流。
未来的图模式可能会在这些阶段之间添加有序事件，但此回退顺序仍为规范的确定性序列。

## 当前 Payload 预期

### `runtime.request_received`
- source: `runtime`
- 当前 payload:
  - `prompt: str`

### `runtime.skills_loaded`
- source: `runtime`
- 当前 payload:
  - `skills: list[str]` 按技能名称升序排列
- 每次新运行都会发出，包括未发现技能的情况（`{"skills": []}`）

### `runtime.skills_applied`
- source: `runtime`
- 当前 payload:
  - `skills: list[str]` 本次 run 真正启用并注入执行语义的 skill 名称
  - `count: int`
- 仅在存在已启用 skill 时发出

### `runtime.acp_connected`
### `runtime.acp_disconnected`
### `runtime.acp_failed`
- source: `runtime`
- 当前 payload:
  - `status: str`
  - `available: bool`
  - `error: str`（仅 `runtime.acp_failed` 时出现）
- 这些事件由 runtime-owned ACP lifecycle 发出，并在响应/重放中按会话序列重新编号
- 相关 ACP 运行态会写入 session metadata 的 `runtime_state.acp`，而不是用户主配置快照 `runtime_config`

### `runtime.acp_delegated_lifecycle`
### `runtime.background_task_waiting_approval`
### `runtime.background_task_completed`
### `runtime.background_task_failed`
### `runtime.background_task_cancelled`
### `runtime.delegated_result_available`
- source: `runtime`
- 当前 payload 同时保留：
  - 旧的顶层关联字段（如 `task_id`、`parent_session_id`、`child_session_id`、`approval_request_id`、`question_request_id`、routing fields、`status`、`summary_output`、`error`）
  - 新的嵌套类型化字段：
    - `delegation: {...}`
    - `message: {...}`
- `delegation` 当前至少可携带：
  - `parent_session_id`
  - `requested_child_session_id`
  - `child_session_id`
  - `delegated_task_id`
  - `approval_request_id`
  - `question_request_id`
  - `routing`
  - `selected_preset`
  - `selected_execution_engine`
  - `lifecycle_status`
  - `approval_blocked`
  - `result_available`
  - `cancellation_cause`
- `message` 当前至少可携带：
  - `kind`
  - `status`
  - `summary_output`
  - `error`
  - `approval_blocked`
  - `result_available`
- `runtime.acp_delegated_lifecycle` 用于对齐 ACP 侧 delegated observability；background-task 事件用于父会话/任务结果/重放等 runtime-owned delegated lifecycle surfaces。

### `graph.loop_step`
- source: `graph`
- 当前稳定的 payload 字段：
  - `step: int`
  - `phase: str`（当前为 `plan` 或 `finalize`）
  - `max_steps: int`

### `graph.model_turn`
- source: `graph`
- 当前稳定的 payload 字段：
  - `turn: int`
  - `mode: str`
  - `prompt: str`
- 当前可追加的 payload 字段：
  - `provider: str`
  - `model: str`
  - `streaming: bool` (如果为 true，后续可能跟随 `graph.provider_stream` 事件)

### `graph.provider_stream`
- source: `graph`
- 当前 payload:
  - `kind: str` (事件类型: `delta`, `content`, `error`, `done`)
  - `channel: str` (数据通道: `text`, `tool`, `reasoning`, `error`)
  - `text: str` (可选; 流片段文本)
  - `error: str` (可选; 错误描述)
  - `error_kind: str` (可选; 错误分类)
  - `done_reason: str` (可选; 完成原因)

### `graph.tool_request_created`
- source: `graph`
- 当前 payload:
  - `tool: str`
  - `arguments: dict[str, object]`
  - `path: str`（可选；仅在 `arguments` 中存在 `path` 时包含）

### `runtime.tool_lookup_succeeded`
- source: `runtime`
- 当前 payload:
  - `tool: str`

### `runtime.tool_started`
- source: `runtime`
- 当前 payload:
  - `tool: str`
- 该事件只在 runtime 真正跨入工具执行边界时发出：
  - 权限已经被 resolve
  - pre-hook（如果启用）已经成功
  - 紧接着会进入真实 `tool.invoke(...)` / `invoke_with_runtime_timeout(...)`
- 该事件不会出现在以下路径：
  - 审批仍在等待中
  - 权限被拒绝
  - pre-hook 失败导致工具未启动

### `runtime.permission_resolved`
- source: `runtime`
- 当前 payload:
  - `tool: str`
  - `decision: str`

### `runtime.tool_completed`
- source: `tool`
- 当前 payload:
  - 工具定义的结果数据

## 工具执行阶段区分

当前稳定契约中，工具相关阶段的语义边界如下：

1. `graph.tool_request_created`：graph 已经决定要请求哪个工具，以及请求参数是什么。
2. `runtime.tool_lookup_succeeded`：runtime 已经把该工具名称解析到真实的 tool definition / implementation。
3. `runtime.tool_started`：runtime 已经通过 permission 与 pre-hook 边界，真实工具执行现在开始。
4. `runtime.tool_completed`：工具执行已经返回结果（成功或失败结果都通过该事件 payload 暴露）。

这四个阶段允许 transcript 消费方区分“计划了工具”、“找到了工具”、“真正开始执行”与“工具已经返回”，避免把审批等待、hook 开销与实际工具执行混在同一个阶段里。

### `runtime.tool_hook_pre`
### `runtime.tool_hook_post`
- source: `runtime`
- 当前稳定的 payload 字段：
  - `phase`
  - `tool_name`
  - `session_id`
  - `status`
  - `error`（仅失败时出现）

## 客户端渲染要求

- CLI 可以将事件渲染为格式化的行
- TUI 和 Web 客户端应将有序流渲染为时间线/活动数据
- 当事件数据可用时，客户端不应仅从文本输出推断审批、失败或工具完成状态

## 非目标

- 多智能体事件语义
- Token/成本遥测模式
- 特定于供应商的模型推理事件

## 验收检查点

- 客户端可以仅使用存储的事件序列和输出来重放持久化的会话
- 事件顺序足以展示 请求 → 加载技能 → 工具请求 → 权限 → 工具完成 → 响应就绪
- 添加新的事件类型不会破坏使用通用回退渲染的旧版客户端
