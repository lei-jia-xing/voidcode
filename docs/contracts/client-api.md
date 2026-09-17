# 面向客户端的运行时 API 契约

来源 Issue：#14

## 目的

定义客户端与无头运行时（Headless runtime）之间的 MVP 契约，用于运行请求、列出会话、加载会话状态、恢复会话以及订阅事件流。

## 状态

当前的契约已经通过 CLI、运行时方法以及本地 HTTP 层落地。除会话列表、会话重放、流式运行和审批处理外，当前还已经交付 delegated/background-task 的 status/output/cancel/list surfaces。

## 当前运行时请求/响应形状

源自 `src/voidcode/runtime/contracts.py`：

```python
RuntimeRequest(
    prompt: str,
    session_id: str | None = None,
    parent_session_id: str | None = None,
    metadata: RuntimeRequestMetadataPayload = {},
    allocate_session_id: bool = False,
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
    session: SessionRef(id: str, parent_id: str | None = None),
    status: Literal["idle", "running", "waiting", "completed", "failed", "interrupted"],
    turn: int,
    metadata: dict[str, object],
)

StoredSessionSummary(
    session: SessionRef(id: str, parent_id: str | None = None),
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
- 可选的 `parent_session_id`（delegated child run / background task lineage）
- 可选的客户端/运行时元数据

输出：
- 最终的 `session`
- 有序的 `events`
- 最终的 `output`

当前实现层面：
- 运行时：`VoidCodeRuntime.run(request)`
- CLI：`voidcode run <request> [--workspace] [--session-id]`
- HTTP：`POST /api/runtime/run/stream`

### Delegated / background task 操作

当前实现已经暴露 runtime-owned background-task lifecycle surfaces：

- 创建 background task：`VoidCodeRuntime.start_background_task(request)` / `POST /api/tasks`
- 查看 task status：`VoidCodeRuntime.load_background_task(task_id)` / `voidcode tasks status <id>` / `GET /api/tasks/{id}`
- 查看 task output：`VoidCodeRuntime.load_background_task_result(task_id)` / `voidcode tasks output <id>` / `GET /api/tasks/{id}/output`
- 取消 task：`VoidCodeRuntime.cancel_background_task(task_id)` / `voidcode tasks cancel <id>` / `POST /api/tasks/{id}/cancel`
- 列出 tasks：`VoidCodeRuntime.list_background_tasks()` / `voidcode tasks list` / `GET /api/tasks`
- 按 parent session 列出 tasks：`VoidCodeRuntime.list_background_tasks_by_parent_session(parent_session_id)` / `voidcode tasks list --parent-session <id>` / `GET /api/sessions/{parent}/tasks`

这些结果当前会暴露 runtime-owned delegated correlation 字段，包括：

- `parent_session_id`
- `requested_child_session_id`
- `child_session_id`
- `approval_request_id`
- `question_request_id`
- `routing`
- `delegation`
- `message`

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

### 回答等待中的问题 (Answer pending question)

输入：
- `session_id`
- `question_request_id`
- `responses`: 一个或多个 `{header, answers}` 结构；CLI 简单文本模式会把 `--response` 归一化为 header 为 `response` 的单项回答

输出：
- 恢复后的 `RuntimeResponse`

当前实现层面：
- 运行时：`VoidCodeRuntime.answer_question(session_id, question_request_id=..., responses=...)`
- CLI：`voidcode sessions answer <session_id> --question-request-id <id> --response <text> [--workspace]`
- CLI 多问题/精确 header 形态：`--response-json '[{"header":"Confirm","answers":["yes"]}]'`

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
- 客户端必须按交付顺序处理运行时事件，即使未来的图模式在现有阶段之间插入额外事件
- 客户端必须能够容忍新增的有序事件，而不能假设当前的确定性事件序列已经穷尽所有情况

## 当前 HTTP/流式传输映射

现有 HTTP 层保留了相同的操作边界：

- 运行/创建会话
- 列出会话
- 加载/恢复会话
- 订阅或接收运行时的有序事件
- delegated/background-task create/status/output/cancel/list

当前已交付的本地 HTTP routes 包括：

- `POST /api/runtime/run/stream` — 运行请求并以 SSE 交付有序事件与最终输出（SSE 帧信封与 `session` 字段交付规则见 `stream-transport.md`）；成功 `200`（SSE 流）；错误 `400`（请求无效）、`500`（内部错误）、`405`（方法不允许）
- `GET /api/sessions` — 列出持久化主会话摘要；成功 `200`；错误 `405`
- `GET /api/sessions/{id}` — 只读加载/重放持久化会话（不得触发 resume）；成功 `200`；错误 `404`、`405`
- `GET /api/sessions/{id}/events` — 订阅会话有序事件流，支持 `after_sequence` / `follow` 查询参数（SSE 帧信封、`session` 字段交付规则与 `follow` 增量读取语义见 `stream-transport.md`）；成功 `200`（SSE 流）；错误 `400`（`after_sequence` 非整数或为负）、`404`、`405`
- `GET /api/sessions/{id}/result` — 读取会话终态结果视图；成功 `200`；错误 `404`、`405`
- `GET /api/sessions/{id}/debug` — 读取会话调试快照；成功 `200`；错误 `404`、`405`
- `GET /api/sessions/{id}/delegated-context` — 读取 delegated 子会话上下文；成功 `200`；错误 `404`（`code=delegated_context_missing`）、`405`
- `POST /api/sessions/{id}/approval` — 提交审批决策并继续执行，返回下一次暂停或执行结束时的会话快照；成功 `200`；错误 `400`、`409`（`code=no_pending_approval`）、`405`
- `POST /api/sessions/{id}/question` — 回答等待中的问题，返回恢复后的 `RuntimeResponse`；成功 `200`；错误 `400`、`404`、`405`
- `POST /api/sessions/{id}/cancel` — 按 run identity 取消/中断会话；成功 `200`；错误 `400`、`405`
- `POST /api/sessions/{id}/steer` — 向会话排队一条 steer 消息；成功 `200`；错误 `400`、`404`、`409`（`code=session_sealed`）、`405`
- `POST /api/sessions/{id}/resume` — 显式恢复 interrupted / failed-retryable 会话（会重新进入 graph loop 并重新执行 provider）；成功 `200`；错误 `404`、`405`
- `POST /api/sessions/{id}/undo` — 撤销（回退）会话；成功 `200`；错误 `404`、`405`
- `POST /api/sessions/{id}/revert` — 写入 revert marker；成功 `200`；错误 `400`、`404`、`405`
- `POST /api/sessions/{id}/unrevert` — 清除 revert marker；成功 `200`；错误 `404`、`405`
- `GET /api/sessions/{parent}/tasks` — 按 parent session 列出 background tasks；成功 `200`；错误 `405`
- `GET /api/tasks` — 列出 background tasks（workspace 全局视图）；成功 `200`；错误 `405`
- `POST /api/tasks` — 创建 background task；成功 `201`；错误 `400`、`405`
- `GET /api/tasks/{id}` — 读取单个 task 状态；成功 `200`；错误 `404`、`405`
- `GET /api/tasks/{id}/output` — 读取 task 输出/结果视图；成功 `200`；错误 `404`、`405`
- `POST /api/tasks/{id}/cancel` — 取消 task；成功 `200`；错误 `404`、`405`
- `POST /api/tasks/{id}/retry` — 重试 terminal task（复用旧请求创建新的 queued task handle）；成功 `201`；错误 `400`、`405`
- `POST /api/tasks/{id}/steer` — 向 keep-alive task 派发下一 worker turn；成功 `200`；错误 `400`、`405`
- `GET /api/notifications` — 列出通知；成功 `200`；错误 `405`
- `POST /api/notifications/{id}/ack` — 确认通知；成功 `200`；错误 `404`、`405`
- `GET /api/settings` — 读取运行时设置；成功 `200`；错误 `405`
- `POST /api/settings` — 更新运行时设置；成功 `200`；错误 `400`、`405`
- `GET /api/workspaces` — 列出 workspace registry 快照；成功 `200`；错误 `405`
- `POST /api/workspaces/open` — 打开/切换 workspace；成功 `200`；错误 `400`、`404`、`405`
- `GET /api/providers` — 列出 providers 及 configured/current 状态；成功 `200`；错误 `405`
- `GET /api/providers/{name}/models` — 列出 provider 模型与 catalog metadata；成功 `200`（已配置）/ `409`（未配置，body 仍为 JSON）；错误 `400`、`405`
- `GET /api/providers/{name}/inspect` — 读取 provider 端点/配置检查视图；成功 `200` / `409`；错误 `400`、`405`
- `POST /api/providers/{name}/validate` — 校验 provider 凭据/就绪状态；成功 `200` / `409`；错误 `405`
- `GET /api/agents` — 列出可用 agents；成功 `200`；错误 `405`
- `GET /api/skills` — 列出 skills；成功 `200`；错误 `405`
- `GET /api/commands` — 列出命令；成功 `200`；错误 `405`
- `GET /api/status` — 读取运行时状态快照（git / lsp / mcp / background_tasks）；成功 `200`；错误 `405`
- `POST /api/status/mcp/retry` — 重试 MCP 连接并返回状态快照；成功 `200`；错误 `400`、`405`
- `GET /api/review` — 读取 workspace review 快照；成功 `200`；错误 `405`
- `GET /api/review/diff/{path}` — 读取单个文件 diff（`{path}` 为 path-safe 多段路径）；成功 `200`；错误 `400`、`405`
- `GET /api/openapi.json` — 返回本路由表对应的 OpenAPI 文档（JSON）；成功 `200`；错误 `405`

所有 route 在方法不匹配时返回 `405`（`{"error": "method not allowed", "code": null}`）；未知 `/api/*` 路径返回 `404`（`{"error": "not found", "code": null}`）。`/api/openapi.json` 是传输层自己渲染的 route，因此 SPA fallback 不会遮蔽它，未知 `/api/*` 路径也不会落到 `index.html`。

`GET /api/sessions/{parent}/tasks`（parent 作用域）与 `GET /api/tasks`（workspace 全局）共用同一套 task summary 序列化，但作用域不同；两者都是 shipped surface，调用方按需要选择：会话详情用前者，跨会话 roster 用后者。

### `show_thinking` 查询参数

`show_thinking`（布尔，缺省 `false`）控制响应中的 reasoning/thinking 内容是否脱敏：

- 缺省（`false`）时 reasoning payload 只保留脱敏后的占位字段；`true` 时返回未脱敏内容。
- 接受的写法：`true` / `1` / `yes` / `on`（大小写不敏感）；其他取值一律视为 `false`，不会产生校验错误。
- 唯一拼写是 `show_thinking`（下划线）。
- honour 该参数的 route：`POST /api/runtime/run/stream`、`GET /api/sessions/{id}/events`、`GET /api/sessions/{id}`、`GET /api/sessions/{id}/result`、`GET /api/sessions/{id}/debug`、`GET /api/sessions/{id}/delegated-context`、`GET /api/tasks/{id}/output`、`POST /api/sessions/{id}/resume`、`POST /api/sessions/{id}/approval`、`POST /api/sessions/{id}/question`；其余 route 忽略该参数。
- 该参数只影响客户端可见的展示内容，不写入 session truth（`runtime.reasoning_part` 等事件仍按原样持久化；脱敏发生在交付给客户端的投影层）。

本文档仍然有意地将契约定义与具体框架实现细节解耦；但这些路由边界与 delegated/task surfaces 本身已经属于当前 shipped API，而不是 future work。

## HTTP 错误信封

本地 HTTP 层的所有错误响应共用同一个信封：

```json
{"error": "<面向用户的错误消息>", "code": "<稳定的机器可读原因，或 null>"}
```

- `error` 始终存在，内容就是面向用户的句子（例如 `not found`、`method not allowed`、
  `request body must be valid JSON`、`prompt must be a non-empty string`）。
- `code` 只在失败操作本身携带稳定原因时给出；其余错误为 `null`。客户端应当按 `code`
  分流，不要匹配 `error` 文本。
- 请求体校验失败统一返回 `400`，错误消息保持按字段定位的文本，不使用框架默认的
  `422` 与错误字典列表。
- 未匹配的路径返回 `404 {"error": "not found", "code": null}`；方法不匹配返回
  `405 {"error": "method not allowed", "code": null}`。
- `/api/*` 上任何未被处理的服务端异常返回 `500 {"error": "internal server error",
  "code": null}`；服务端仍会把该异常的 traceback 记入日志（响应与日志都会发生）。
- JSON 响应固定为 `content-type: application/json; charset=utf-8`，键按字典序输出。
- 该信封只覆盖 `/api/*`：非 API 路径（前端静态资源与 SPA fallback）在服务端异常时保持
  `500 text/plain: Internal Server Error`。

当前稳定的 `code` 取值（由 runtime 的错误类型拥有，传输层只负责携带）：

| `code` | 触发条件 | 状态 | 拥有者 |
|---|---|---|---|
| `workspace_busy` | 有活跃 run/approval/task 时打开其它 workspace | `409` | `WorkspaceOpenError`（`runtime/workspace.py`） |
| `invalid_workspace` | workspace 路径不存在/不可用 | `400` | `WorkspaceOpenError` |
| `session_sealed` | 对已封印（terminal 且无活跃 run）的会话 steer | `409` | `SessionSealedError`（`runtime/storage/shared.py`） |
| `no_pending_approval` | 对没有待审批请求的会话提交审批决策 | `409` | `NoPendingApprovalError`（`runtime/contracts.py`） |
| `delegated_context_missing` | 该会话没有 delegated 子会话上下文（`/api/sessions/{id}/delegated-context`） | `404` | 传输层（runtime 返回 `None` 即该语义） |

`null` 覆盖其余全部错误：未知会话/task/通知（`404`）、方法不允许（`405`）、未匹配路径
（`404`）、请求体与查询参数校验（`400`）、provider 未配置或校验失败（`409`）、凭据类
`ValueError`（`400`）等。这些是"输入/状态不对"的统一拒绝，不构成需要客户端区分语义的
机器可读原因。

## Provider context 与 provider identity 可见性

Provider-visible prompt 是一次请求的受限上下文投影，不是 provider、gateway 或上游模型的事实证明。客户端和调试/导出工具 MUST 将以下语义分开：

- `requested model` / `model selector`：runtime 请求的 `provider/model` 字符串。它描述选择意图；例如 `openrouter/free` 表示 OpenRouter Free Models Router，而不是某个具体上游模型。
- `resolved provider`：runtime 选择的 provider adapter/config。它描述请求如何发出，不代表 gateway 最终采用的 upstream。
- `observed upstream model`：仅当 provider response metadata 明确返回时才成立的事实。OpenRouter 的 `response.model` 才能证明本次请求实际处理的模型；不得从 prompt、配置 slug 或能力声明推断。

因此，provider-visible prompt MUST NOT 承诺 host/model/upstream identity，也不得将 router id 渲染成具体模型名称、厂商、能力或版本。Host、模型和上游路由的结构化信息 MAY 出现在 `/api/sessions/{id}/debug`、provider inspect 或导出视图中，但必须标注来源（requested/resolved/observed）并遵守现有脱敏边界；原始 provider credentials、secret-like values 和未脱敏环境值不得进入这些视图。

对于 OpenRouter，`openrouter/free` 的实际 upstream 只能从该次响应 metadata 读取；若 metadata 缺失，客户端应显示 unknown/未观测，而不是回退为“free model”或某个 catalog entry。固定的 `provider/model:free` 也只表示 OpenRouter 的免费 variant，仍不应在 prompt 中声称 host 已实际选中该模型。

### 环境信息的选择性原则

环境卡片应优先提供模型完成当前任务真正需要的最小事实：workspace/cwd（文件和工具定位）、平台/终端（命令或交互差异），以及 runtime 明确启用的工具和约束。日期、分支和 Git 状态属于动态上下文，应与稳定前缀分层；CPU、GPU、发行版、kernel 等 workstation 字段仅在任务或工具能力相关时注入，不能作为模型身份或 provider 能力的替代品。debug/UI/export 可以展示更完整的结构化卡片，但应保持同样的字段来源和不确定性标记。

## 非目标

- 完整的传输层实现
- post-MVP 的多智能体会话拓扑
- 特定于供应商的请求格式

## 验收检查点

- TUI 和 Web 客户端可以在不绕过运行时方法或概念的情况下实现
- 可以使用稳定的会话摘要和存储响应形状来列出和恢复持久化的会话
- 未来的 API 路由可以直接映射到这些操作上，而无需更改语义
