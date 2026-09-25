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

以下事件当前属于稳定的运行时事件契约，与 `src/voidcode/runtime/events.py` 的 `KnownEventType`（即 `KNOWN_EVENT_TYPES`，`EMITTED_EVENT_TYPES` 与 `RUNTIME_EVENT_TYPES` 的无交并集）一致。它们覆盖当前 deterministic 与 provider 两条 execution engine 路径：

- 请求与技能：`runtime.request_received`、`runtime.skills_loaded`、`runtime.skills_applied`、`runtime.hook_presets_loaded`
- provider 治理：`runtime.provider_fallback`、`runtime.provider_transient_retry`、`runtime.provider_context_policy`。当被重启的那次尝试已经向客户端流出可见内容（assistant/reasoning 文本、tool-call 预览）时，事件 payload 增加 `discarded_streamed_output: true`，客户端据此丢弃该次尝试的实时投影（实时投影不进入持久化 transcript）；未流出任何内容的尝试不带此字段
- provider 终态语义：无法识别的 `done_reason`（含缺失原因）按已完成的 stop 等价终态处理；`error` / `cancelled` 仍为失败终态
- provider 终态诊断：`graph.response_ready` payload 记录 `finish_reason`（规范终态）与 `finish_reason_reported`。`finish_reason_reported` 为 `false` 表示上游声明终态但未给出可读取的原因，此时运行时以 `warning` 记录，使被截断但仍"正常结束"的流可被 `sessions debug` 之类检查发现；正常 `stop` 为 `true`
- ACP：`runtime.acp_connected`、`runtime.acp_disconnected`、`runtime.acp_failed`、`runtime.acp_delegated_lifecycle`
- LSP：`runtime.lsp_server_started`、`runtime.lsp_server_reused`、`runtime.lsp_server_startup_rejected`、`runtime.lsp_server_stopped`、`runtime.lsp_server_failed`
- MCP：`runtime.mcp_server_started`、`runtime.mcp_server_reused`、`runtime.mcp_server_acquired`、`runtime.mcp_server_released`、`runtime.mcp_server_stopped`、`runtime.mcp_server_idle_cleaned`、`runtime.mcp_server_failed`
- graph 阶段：`graph.loop_step`、`graph.model_turn`、`graph.tool_request_created`
- 工具执行边界：`runtime.tool_lookup_succeeded`、`runtime.tool_started`、`runtime.tool_progress`、`runtime.tool_completed`、`runtime.tool_timeout`、`runtime.tool_input_processed`、`runtime.tool_hook_pre`、`runtime.tool_hook_post`
- 权限 / 审批 / 提问：`runtime.permission_resolved`、`runtime.approval_requested`、`runtime.approval_resolved`、`runtime.question_requested`、`runtime.question_answered`
- 终结：`graph.response_ready`、`runtime.failed`

Runtime hook surface 与其事件名称的内部对应关系由
`src/voidcode/hook/surfaces.py::HOOK_SURFACE_DESCRIPTORS` 统一描述；该 catalog
不改变本节既有事件名称、source、payload 或序列规则。

在轮次中发出的所有事件（包括来自图端的事件）都会由运行时重新编号，变为每次响应或重放中单一的、单调递增的序列。
这确保了图端局部（graph-local）的序列值在跨审批恢复运行时，不会与运行时插入的事件发生冲突。

## RuntimeEventType 扩展事件

以下事件在 `src/voidcode/runtime/events.py` 中归类为 `RuntimeEventType`，按需发射：

- 会话：`runtime.session_started`、`runtime.session_ended`、`runtime.session_idle`
- 技能绑定：`runtime.skill_loaded`、`runtime.skills_binding_mismatch`
- 任务状态与推理：`runtime.todo_updated`、`runtime.reasoning_part`、`runtime.reasoning_diagnostic`、`runtime.turn_progress`、`runtime.stuck_detected`
- 策略物化：`runtime.policy_materialized`
- 上下文变换：`runtime.context_compacted`、`runtime.context_transform_applied`（正式事件，由 `run_loop.py` 发射；payload 见下文）
- 每轮提醒：`runtime.reminder_injected`（terminal 回合发出的 reminder 已通过 per-call 通道注入；payload 见下文）
- provider context 恢复：`runtime.provider_context_recovery`（`context_limit` 的一次性本地裁剪/升级决策；payload 见下文）

## 已交付的 delegated/background-task 事件

以下事件名称当前已经是 shipped delegated execution surface 的一部分：

- `runtime.background_task_registered`
- `runtime.background_task_started`
- `runtime.background_task_progress`
- `runtime.background_task_idle_reminder`
- `runtime.background_task_waiting_approval`
- `runtime.background_task_awaiting_steer`
- `runtime.background_task_completed`
- `runtime.background_task_failed`
- `runtime.background_task_cancelled`
- `runtime.background_task_interrupted`
- `runtime.background_task_group_completed`
- `runtime.background_task_notification_enqueued`
- `runtime.background_task_result_read`
- `runtime.delegated_result_available`

它们在 `src/voidcode/runtime/events.py` 中归类为 `RuntimeEventType`（终态 / 通知类事件同时属于 `DelegatedBackgroundTaskEventType`）。CLI、HTTP、会话重放与 background-task result/output surfaces 都已经消费这些事件；`runtime.acp_delegated_lifecycle`（CoreEventType）用于 ACP 侧的 delegated observability，payload 与 background-task 事件一致。

`runtime.background_task_progress` 的 payload 至少包含 `task_id`、`parent_session_id`、`child_session_id`、`status: "running"`、`progress` 与 `progress_event_sequence`。`progress` 是 child `yield` 产生的有界非终态 section，包含 `type`、runtime 分配的 `ordinal`，以及 `result` 或非空 `data`。每段最多 4096 字符，每个 child 最多 100 段、累计最多 65536 字符；不可序列化或超限内容按 runtime 的 bounded 规则拒绝/截断，不得扩展成无界 transcript 流。

该 parent event 以 `task_id + child_event.sequence` 去重，使用 parent session sequence 空间，并可同时进入 runtime outbox/interaction projection。它只表示 progress 观察，不改变 queued/running/idle/completed/failed/cancelled/interrupted 状态，不替代 terminal result；客户端应通过 `background_output` 读取有界 progress/result projection。

## 未来补充 / additive 词汇表

当前不再有仍保持 additive/prototype 语义的具名共享事件：`runtime.context_transform_applied` 已转为正式事件（见上文），memory 观测统一由上文 memory / context 事件表达。

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

### `runtime.context_transform_applied`
- source: `runtime`
- 当前 payload:
  - `provider_id: str`
  - `failure_policy: str`
  - `tool_result_count: int`（本次 transform 实际看到的 retained tool result 数量，而不是完整历史数量）
  - `status: str`（可选）
  - `priority: int`（可选）
  - `execution_index: int`（可选）
  - `injection_count: int`（可选）
  - `provider_order: list[str]`（可选）
  - `sources: list[str]`（可选）
  - `diagnostics: list[str]`（可选）
- 该事件描述 runtime-owned context transform 已经应用或记录的有界 trace。它不执行 hook、不允许客户端改写 provider context，也不携带被注入的完整 system prompt / rule / skill 内容。
- 同一 run 内，如果某个 transform trace 未发生变化，runtime 不应重复发出该事件；只有首次观察到或 trace 发生变化时才发出新的 event。
- `hook_preset_guidance` transform 已通过 `runtime.hook_presets_loaded` 表达，不重复发出该事件。

## 当前 Payload 预期

### `runtime.request_received`
- source: `runtime`
- 当前 payload:
  - `prompt: str`
  - `runtime_policy: object`（可选；当会话已 materialize Runtime Harness Policy v1 时出现）
- `runtime_policy` 是 persisted `RuntimePolicySnapshot` 的有界、redacted projection。它包含 `schema_version`、`policy_version`、`mode`、`read_only`、agent ids、`materialization`、neutral `intent`、`tool_policy`、`delegation_policy`、`hook_policy`、`prompt_activation`、`precedence_trace` 与 `diagnostics`。
- `runtime_policy.materialization.kind == "runtime_policy_materialized"` 表示 runtime control plane 已完成 policy materialization；这不是可由客户端写回或覆盖的第二控制面。
- `tool_policy.denied` / `delegation_policy.denied` 必须携带稳定 denial reason；product child delegation 的稳定 reason 是 `delegation_denied_product_top_level_only`。
- `hook_policy` 只描述 named event-scoped hook decisions。Hooks are non-authoritative: they can observe/report/cancel/guidance within runtime policy, but cannot grant tool/delegation authority.
- `prompt_activation.activated_this_turn` 是 run-local projection；replay/resume surfaces must project it conservatively and never imply fresh activation unless the current run actually activated it.
- This projection never contains raw prompt bodies, skill bodies, secret-like values, env values, or unbounded policy inputs. Strings/lists/traces/diagnostics are bounded by runtime-owned helpers.
- Replay and debug require stored policy truth. Missing snapshots and unsupported policy/schema versions are version errors.

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
### `runtime.background_task_idle_reminder`
### `runtime.background_task_completed`
### `runtime.background_task_failed`
### `runtime.background_task_cancelled`
### `runtime.background_task_group_completed`
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

### `runtime.context_compacted`
- source: `runtime`
- 当前 payload:
  - `reason: str`，`token_budget_exceeded:usage_tokens_before=…:threshold_tokens=…:pruned_tool_results=…:usage_tokens_after=…`（另有 `no_prunable_tool_content` / `already_pruned_view` / `compaction_unsized` 三种边界 reason）
  - `compacted: bool`，仅在本次调用**实际发生缩减**时为 true
  - `original_tool_result_count: int`、`retained_tool_result_count: int`（pairing 保留，因此两者相等）、`dropped_tool_result_count: int`（content 被占位文本替换的结果数）、`truncated_tool_result_count: int`
  - `usage_tokens_before: int | None`：判定数字 = 实测锚点 + 增量估算（inexact）
  - `usage_tokens_after: int | None`：裁剪后**实际发出的 segments** 上的同一口径数字，由 `assemble_provider_context` 实测写入
  - `usage_tokens_estimated: bool`：恒为 true，提醒消费方这不是 provider usage
  - `measured_anchor_tokens: int | None` / `estimated_delta_tokens: int | None`：这个数字里哪部分来自实测锚点（最近一次 provider usage 的 `input+cache_read+cache_write+output`）、哪部分是按 UTF-8 字节 / 4 估算的增量；锚点不可用时前者为 null
  - `pruned_savings_tokens: int`
  - `summary_anchor` / `projection_id` / `summary_source` / `summary_strategy` / `projection`
- 该事件描述 runtime 对 provider view 做的**有界裁剪**：只替换最旧 tool 结果的 content，system/instruction 段与消息 pairing 不变；被裁内容带 artifact 时同时出现 `runtime_context_artifact_reference` 段，模型可经 `voidcode://artifact/<id>` 取回。计数与 usage 估算都必须真实（见 `docs/contracts/runtime-config.md` 的 `context_window.compaction`）。

### `runtime.provider_context_recovery`
- source: `runtime`
- 当前 payload:
  - `mode: str`：`prune`（本地有界裁剪后重试同一次调用）或 `promote`（本地裁剪已用尽/不可行，改用既有 fallback 链升级）
  - `window_tokens_before: int | None`：失败模型的有效输入窗口（catalog `max_input_tokens`，缺失且不可比较时为 null）
  - `window_tokens_after: int | None`：被选中的升级 target 的有效窗口（未选出候选时为 null）
  - `candidate_count: int`：参与窗口比较的链上候选数
  - `promotion_reason: str`：`larger_window`（选到严格更大的窗口）、`no_larger_candidate`（无严格更大的候选，保持链顺序）或 `unavailable`（不可 fallback / 链已穷尽）
  - `outcome: str`（仅 `prune`）：`retry`（已重试）或 `unavailable`（无可裁剪内容，直接进入升级/失败）
  - `reason: str`：触发 kind，当前为 `context_limit`
  - `provider: str`、`model: str`：触发恢复的 provider/model
  - `tool_result_count: int`、`dropped_tool_result_count: int`
  - `usage_tokens_before` / `usage_tokens_after` 与 `measured_anchor_tokens` / `estimated_delta_tokens`：与 `runtime.context_compacted` 同义（实测锚点 + 字节/4 增量估算，inexact；after 为裁剪后重新组装的实际 segments 数字），并带 `usage_tokens_estimated: true`
  - `compaction_reason: str | None`：该次恢复组装的 compaction reason（含 `compaction_unsized` 等边界）
  - `provider_error_details: object`（可选，已 redact）
- 升级是**窗口感知**的：候选仍来自既有 fallback 解析（同一 target 链、同一事件形状），但 `context_limit` lane 会按解析出的有效窗口（catalog `max_input_tokens`）跳过同窗/更小的候选，只挑**严格更大**的那个；没有严格更大的候选时保持既有链顺序，并在 `promotion_reason` 里如实标注 `no_larger_candidate`，不把「升了级」说成成功。该偏好只属于 `context_limit` lane，其它 provider 错误的 fallback 顺序不变。
- 恢复语义：`context_limit` 是 runtime-owned lane（解析层不再硬判终态）。每个 turn 最多一次本地裁剪重试、最多一次 fallback 升级；`mode=promote` 之后若仍失败，以 `runtime.failed`（payload 含 `provider_error_kind=context_limit`、`resumable=true`、`context_limit_recovery`、`guidance`）结束，且该失败保持可 resume。

### `runtime.reminder_injected`
- source: `runtime`
- 当前 payload:
  - `reminder_type: str`：`todo`（terminal 回合的完成提醒）或 `todo_mid_run`（回合进行中的停滞提醒）
  - `attempt: int`，本 cycle 内第几次提醒（从 1 开始）
  - `max_attempts: int`，本 cycle 的提醒上限
  - `incomplete_todo_count: int`，本次被提醒的未完成 todo 条目数
  - `mutation_count: int`，仅 `reminder_type = "todo_mid_run"` 出现：本次 nudge 依据的变更类工具调用计数
- 该事件表示运行时在一个 terminal assistant 回合（无待执行 tool call）通过 **per-call reminder 通道**注入了一条提醒：reminder 作为 provider context 的尾部 segment 只对本次 provider 调用可见，不写入 SQLite transcript，也不进入 per-call cache hash（`hook/percall.py` 的 `PerCallMessage(per_call=True)` 语义）。reminder 文本本身不是持久化 truth，客户端不得把它当作会话历史或用户输入回显。
- 触发点、触发阈值、每 cycle 预算、抑制条件与计数器持久化（含 `reminder_type = "todo_mid_run"` 的 mid-run nudge，对齐 upstream pi-coding-agent 的 todo tracker）由 `docs/contracts/runtime-config.md` 的 `reminders` 节定义，该节是这些语义的唯一 owner；本条只描述本事件的 payload 与「运行时注入了一条提醒」这一事实。

### `graph.loop_step`
- source: `graph`
- 当前稳定的 payload 字段：
  - `step: int`
  - `phase: str`（当前为 `plan` 或 `finalize`）

### `graph.model_turn`
- source: `graph`
- 当前稳定的 payload 字段：
  - `turn: int`
  - `mode: str`
  - `prompt: str`
- 当前可追加的 payload 字段：
  - `provider: str`
  - `model: str`
  - `streaming: bool` (如果为 true，reasoning 通道内容以 `runtime.reasoning_part` 事件表达；原始 provider stream 分片是 live-only 传输细节，不持久化、不属于 `KnownEventType`)

> 说明：`graph.provider_stream` 是 live-only 的客户端传输细节（运行时将其转换为 `runtime.reasoning_part` 后才持久化），它不是 `KnownEventType` 成员，不属于本词汇表。

### live-only 传输细节（非词汇表成员）

`graph.provider_stream` 分片只在 provider streaming 开启时向客户端实时投射，不进入持久化 transcript，也不属于 `KnownEventType`。reasoning 通道内容以 `runtime.reasoning_part` 事件表达与持久化（见 `runtime_reasoning_part_from_provider_stream`）。

### `graph.tool_call_start` / `graph.tool_call_delta` / `graph.tool_call_end`
- source: `graph`
- 非写工具保持既有参数增量字段（包括 `arguments_delta`，以及 end 时的 `parsed_arguments`）。
- `write`、`edit`、`multi_edit`、`apply_patch` 的 live payload 使用 `diff_preview` 作为
  参数观察的 canonical 字段，并省略原始 `arguments_delta` / `parsed_arguments`，避免
  任意文件内容或秘密直接流向客户端。
- `diff_preview` 是有界、只读 projection：`schema_version: 1`、`phase: "partial"`、
  `live_only: true`、`status: "ready" | "degraded"`、`bounded: true`、
  `truncated: bool`，以及安全目标路径、统计、hash 和有界 unified diff（apply_patch
  可使用 `paths` 与 patch 格式 diff）。失败或缺字段时保持事件生命周期并返回稳定
  `reason`，不得抛出破坏执行主循环的异常。
- 该 preview 不写入 session truth、checkpoint 或 replay。完整的
  `graph.tool_request_created` 可附带 `phase: "final"` 的 `diff_preview`；最终
  `runtime.tool_completed` 数据仍是实际执行 diff 的权威来源。

### `graph.tool_request_created`
- source: `graph`
- 当前 payload:
  - `tool: str`
  - `arguments: dict[str, object]`
  - `path: str`（可选；仅在 `arguments` 中存在 `path` 时包含）
  - `diff_preview: object`（可选；写工具完整参数的最终只读预览）

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

### `runtime.tool_timeout`
- source: `runtime`
- 当前 payload:
  - `tool: str`
  - `timeout_seconds: int | null`，生效的 runtime 超时
  - `cancellation_signalled: bool`，runtime 是否在停止等待前取消了该次调用
  - `execution_stopped: bool`，runtime 是否在有界回收窗口内确认执行已经停止
  - `side_effect_state: "settled" | "unknown"`，`settled` 当且仅当 `execution_stopped` 为 `true`
- 同一组执行事实同时出现在该次调用的 `runtime.tool_completed`（顶层与 `diagnostics.details`，`diagnostics.kind="tool_timeout"`）和终结该 run 的 `runtime.failed` payload 上；语义与验收规则见 `agent-tool-calling.md` 的「取消与超时」。

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
