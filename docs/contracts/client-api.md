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
    title: str | None = None,
    # fork 血缘：本会话从其复制的来源会话 id 与复制边界序号；
    # 非 fork 会话为 None。
    forked_from_session_id: str | None = None,
    forked_at_sequence: int | None = None,
)
```

`title` 是用户可设置的显示标签（`voidcode sessions rename <session_id> <title>` / `VoidCodeRuntime.rename_session`），
`None` 表示尚未命名、客户端按 `prompt` 自行推导标签。运行时只在设置时校验它：
去除首尾/连续空白后必须非空，且不超过 `SESSION_TITLE_MAX_LENGTH`（120 字符）；
为 `None` 的行不会落成空字符串，`title` 的推导规则（截断、CJK 处理）始终只在客户端。

**用户设置的 `title` 与客户端推导出的标签是两种东西，有各自的长度上限：**

- **运行时上限 120。** 这是 `title` 的**唯一**校验点；超出即拒绝（不静默截断）。用户写的标签是完成态文本，
  runtime 只保证它单行、非空、有界。
- **客户端推导上限（参考实现 56 字符 / 7 词）。** 只作用于从 `prompt` **推导**出来的标签——那是"从一段话里
  取出一个标签"的启发式，需要压缩。它 MUST NOT 用于用户设置的 `title`：客户端不得把推导规则回灌到用户
  已定稿的文本上，否则会出现"服务端接受 120、界面静默改成 56"的漂移。
- **溢出是布局问题，不是模型问题。** 用户设置的 `title` 按原样渲染（仅折叠空白），由界面自行处理超长：
  侧边栏行用 CSS 截断（`truncate`），TUI 选择器换行并裁到面板宽度。
- 客户端可以保留一个**防御性**的 120 上限以防服务端不守约（参考实现 `MAX_EXPLICIT_TITLE_LENGTH`），
  但该值与运行时上限绑定，不是另一个独立的显示契约。

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

### 派生持久化会话 (Fork persisted session)

输入：
- `session_id`：要复制的来源会话
- `at_sequence`（可选）：复制事件 `1..N`；缺省为来源会话当前的 `last_event_sequence` 水位

输出：
- 新会话的 `StoredSessionSummary`，其 `forked_from_session_id` 为来源会话 id、`forked_at_sequence` 为复制边界序号

保证（由代码提供，客户端可以依赖）：

- **来源会话永不被修改。** fork 只读来源行与事件日志，新会话是一个全新的会话行；来源的 status、事件、水位、metadata 全部保持不变。
- **复制的事件保留其原始序号**（`1..N` 连续），新会话的水位是自己的 `N`，不是来源的水位。
- **复制是一个事务**（`BEGIN IMMEDIATE`），事件日志与 provenance 列一起落盘，不会出现半个 fork。
- **边界不得劈开一次交互。** 若 `at_sequence` 使某个 `runtime.tool_started` 与其 `runtime.tool_completed`（按 `tool_call_id` 配对），或某个 `runtime.approval_requested` / `runtime.question_requested` 与其 resolved/answered 对端（按 `request_id` 配对）分离，则拒绝并抛出 `RuntimeSessionForkBoundaryError`（`src/voidcode/runtime/contracts.py`，`code = "fork_boundary_splits_interaction"`）；错误信息给出可安全 fork 的序号。未知会话抛 `UnknownSessionError`。
- 新会话状态为 `interrupted`，并写入一个 replay-only 的终端 resume checkpoint，使后续 `sessions resume` 走存储重放而非截断重跑。

当前实现层面：
- 运行时：`VoidCodeRuntime.fork_session(session_id=..., at_sequence=...)`
- CLI：`voidcode sessions fork <session_id> [--at-sequence N] [--workspace] [--json]`

### 查询 fork 血缘 (Session lineage / tree)

输入：
- `session_id`（可选）：从该会话沿 `forked_from_session_id` 向上走到最老祖先；缺省时返回 workspace 内参与 fork provenance 的会话行（delegated background-task child 不参与，已排除），供客户端自行摆放整片森林

输出：
- `tuple[StoredSessionLineageEntry, ...]`：每项为 `{session_id, forked_from_session_id, forked_at_sequence, updated_at}`，**从最老祖先到 fork 本身**排列。`updated_at` 仅用于森林对**根**排序，血缘走查本身不按它排序

血缘查询是**只读**的：它不修改任何会话、不触发 resume，也不改变 fork 结构。

### 摆放 workspace fork 森林 (Session forest)

输入：无（整片森林）。

输出：
- `tuple[StoredSessionForestEntry, ...]`：在线性血缘行之上追加拓扑 `depth`（`{session_id, forked_from_session_id, forked_at_sequence, depth}`），并**按展示顺序排列**

森林是**唯一**的展示顺序权威：CLI `sessions tree`、TUI resume picker、web sidebar 都渲染这个投影，任何客户端都**不得**再按 `updated_at` 重排（重排会把子会话排到父会话之上）。

森林的摆放规则（由代码提供，客户端可以依赖）：

- **根按最近活跃排序**：`updated_at` 降序（`session_id` 升序破平），最近活跃的会话在最前。
- **子节点紧跟其父节点**，兄弟保持森林的确定性排序 `(forked_at_sequence 升序、NULL 在前，session_id 升序)`；`depth` 由拓扑遍历得出，恒为父节点 depth + 1。**depth 不依赖行的 `updated_at` 顺序**：一个 fork 了子会话、之后又被续接的父会话拥有更新的 `updated_at`，若每一层都按它排，子会话会跑到父会话之上、depth 也塌掉。
- **根的定义**：`forked_from_session_id` 为空，**或**其指向的会话不在结果集内（fork 了已删除/停用的会话）——后者仍作为根出现，不会被丢弃或成环。
- **delegated 子会话不参与**：`parent_session_id` 非空的会话是 delegated background-task child，不是 fork 节点；整片森林（以及缺省 `session_id` 的 `session_lineage()`）排除它们，与 `sessions list` / `GET /api/sessions` 一致。一个 *fork of* delegated child 因父节点被排除而按上面的孤儿规则作为根出现。
- **环检测**：持久化的 provenance 若成环（无根），抛出 `SessionLineageCycleError`（`code = "session_lineage_cycle"`），不吞掉也不死循环。

当前实现层面：
- 运行时：`VoidCodeRuntime.session_lineage(session_id=...)`（单链）、`VoidCodeRuntime.session_forest()`（整片森林）
- CLI：`voidcode sessions tree [session_id] [--workspace] [--json]`；缺省 `session_id` 时渲染森林（每行 `depth` 空格缩进、可选 `title`、以及 `<- parent@sequence` 溯源标注）；给出 `session_id` 时渲染该会话的单条祖先链。`--json` 的 `lineage` 行在本节原有四个键（`session_id` / `forked_from_session_id` / `forked_at_sequence` / `depth`）之上**新增** `title`。

### 从某个 entry 续接 (Continue from an entry / checkout)

输入：
- `session_id`
- `sequence`：目标 event 的 `sequence`

输出：新的 leaf `sequence`（`int`）。

checkout 是一个**位置变更**：把会话的 `leaf_sequence` 指向 `sequence`，之后该会话可继续续接的起点就是这里。它**不写、不移动、不删除任何 event**——`sequence` 之后的事件仍留在 `session_events`，成为当前路径之外的**被放弃分支**，之后可以再 checkout 回去。

- 下一次 run / resume 的 provider context 是 `sequence` 处的 **root→leaf 路径**投影，被放弃分支不再进入模型上下文。
- 被放弃分支的位置状态（`runtime_state`、`resume_checkpoint_json`、pending approval/question）在同一次事务里丢弃或替换，下一次 run 重新推导。
- **checkout 永不把会话留在终态**：即使行原本是 `completed`，checkout 后其状态变为 `interrupted`（可续接的断点状态）。这是刻意的——checkout 是「从这里继续」的显式意图，若仍为 `completed`，`sessions resume` 会**重放**刚被 checkout 作废的那次响应，而不是从新 leaf 续接。
- 目标若不存在，或 root→target 路径**拆开**了某个 tool call / approval / question 的调用与结果，运行时以 `ValueError` 拒绝（`RuntimeSessionCheckoutBoundaryError`，`code = "checkout_boundary_splits_interaction"`），位置不变。

选择目标的只读清单：`session_entries(session_id)` 返回**升序**的全部 entry `SessionEntrySummary`：
- `{sequence, event_type, parent_sequence, on_current_path, preview}`；`parent_sequence` 为祖先边（首条为 `None`），`on_current_path` 表示该 entry 是否在当前 root→leaf 路径上（`False` 即被放弃分支），`preview` 是该事件的单行短文本。
- CLI 行形如 `<seq>  <event_type>  parent=<seq|->  <on-path|abandoned>  <preview>`。

`undo` / `revert` 是 checkout 之上的便捷入口，**没有**独立的 marker 机制（线性 revert marker 已删除：被放弃分支现在可见、可再次选中，"撤销我的撤销" 不再有意义，`sessions unrevert` 与 `POST /api/sessions/{id}/unrevert` 一并移除）：
- `sessions undo <session_id>`：定位当前路径上最后一个 `runtime.request_received`，checkout 到它**之前**的最新 entry（即那一轮开始的位置）。
- `sessions revert <session_id> --to <sequence>`：checkout 到当前路径上 `sequence < S` 的最新 entry。
- 两者都只是 `checkout_session` 的目标计算，不写、不删、不隐藏任何 event。
- HTTP 不再提供 undo/revert/unrevert 路由；位置变更统一走 checkout（`checkout_session`）。

当前实现层面：
- 运行时：`VoidCodeRuntime.checkout_session(session_id, sequence)`、`VoidCodeRuntime.session_entries(session_id)`
- CLI：`voidcode sessions checkout <session_id> <sequence> [--workspace] [--json]`、`voidcode sessions entries <session_id> [--workspace] [--json]`
- 与 `sessions tree` **不同**：`tree` 渲染跨会话的 fork 森林（provenance），`entries` 渲染**单个会话内部**的事件树；两者不可互相替代。

### fork 语义的诚实边界 (Fork semantics — the honest limits)

VoidCode 的会话事件日志承载**一个可变 leaf pointer**（`sessions.leaf_sequence`）与 entry 之间的祖先边（`session_events.parent_sequence`），所以会话内部的「树」是可表达、可选择的：`sessions checkout` 在一个会话 id 内把续接点移到任意 entry，被放弃分支留在日志里可再次 checkout 回来，下一次运行的 provider context 只走 root→leaf 路径。

fork 与 checkout 仍是两件事：fork 产出**新的会话 id**（复制前缀 + provenance 列），用于建立一条独立血缘；checkout 不改变会话身份，只改变同一会话内的续接位置。因此：

- 同一会话内可以有**多条分叉的续接**（entry tree）：它们共享一个会话 id 与一段日志，靠 `leaf_sequence` 选择当前路径；`fork` 仍是跨会话血缘的唯一工具。
- fork provenance 刻意使用**独立列**（`forked_from_session_id` / `forked_at_sequence`），而不是复用 `parent_session_id`。`parent_session_id` 在**所有**读取点都表示 *delegated background-task child*（列表过滤、委派路由、父会话终结检查、孤儿清理），fork 若写进该列会被误当成 delegated child；用独立列后，**fork 永不进入 delegated-child 行为**。
- fork 迁移 `runtime_config` / `runtime_policy`（会话级有效配置与策略），但丢弃 `runtime_state`（context 投影与 todos，属于被复制那轮的位置状态）；fork 的下一轮自行重新推导运行位置。

### 重命名持久化会话 (Rename persisted session)

输入：
- `session_id`
- `title`

输出：
- 更新后的 `StoredSessionSummary`（含新 `title`）

`title` 的规范化与长度上限由运行时唯一的校验点负责（见上文 `StoredSessionSummary`）；
未知会话或 workspace 之外的会话返回 `UnknownSessionError`。
重命名只写 `title` 列并推进 `updated_at`，不改动会话元数据、事件或 resume checkpoint。

当前实现层面：
- 运行时：`VoidCodeRuntime.rename_session(session_id=..., title=...)`
- CLI：`voidcode sessions rename <session_id> <title> [--workspace] [--json]`

### 恢复持久化会话 (Resume persisted session)

输入：
- `session_id`

输出：
- 一个 `RuntimeResponse`。**它有两种模式，由该会话自己持久化的 resume checkpoint 决定，调用方无法选择**：
  - **存储重放（stored replay）**：checkpoint 的 kind 不是 `interrupted` / `provider_failure_retryable`（最常见的是 `terminal`，也包括没有 checkpoint）时，运行时不执行任何图步骤，直接返回已持久化的 session / events / output（`VoidCodeRuntime.resume(session_id)` → `_load_replay_response`，`runtime/service.py:3241-3265`）。此时响应就是该会话落盘真相的重放。
  - **重跑（truncate-and-rerun）**：checkpoint 为 `interrupted` 时，运行时先把持久化事件尾部截断到 checkpoint 记录的 `last_event_sequence`（丢弃 checkpoint 之后、那批未完成调用留下的孤儿事件），再重新进入 graph loop、重新执行 provider（`runtime/resume.py:1282-1286` 截断；`:1381` 起 `execute_graph_loop`）。checkpoint 为 `provider_failure_retryable` 时同样重新进入 graph loop 并重新执行 provider（`runtime/resume.py:1149-1167`、`:1350`），但**不截断**已有事件，而是把本轮新事件追加在其后。两种重跑模式下响应都是「保留的存储事件 + 本次重跑产生的新事件」，`output` 是本次重跑的输出（重跑未产生输出时回退为存储输出）；持久化的会话日志也随之改写。

因此客户端 MUST NOT 假设 resume 总是返回逐字节等于既有持久化内容的响应；需要纯只读重放时使用只读的会话加载 surface（见下文 `GET /api/sessions/{id}`）。

checkout 之后该会话的 checkpoint 一定是可续接的 `interrupted`（见上文 checkout 一节），所以此处的 `resume` 走**重跑**分支、从 checkout 后的 leaf 继续，而不是重放刚被 checkout 作废的响应。

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
- 未完成会话（`interrupted` / `provider_failure_retryable`）持久化足够的 checkpoint（prompt、session metadata、tool results、最后安全事件序号），使 `sessions resume <id>` 能重跑该轮；已终结会话则由 `sessions resume <id>` 重放存储的响应

目前的集成测试验证了两条路径：`tests/integration/test_read_only_slice.py::test_cli_lists_and_resumes_persisted_session`（重放存储的输出与事件序列）与 `tests/integration/test_read_only_slice.py::test_runtime_resume_truncates_orphaned_tail_after_interrupted_checkpoint`（`interrupted` 重跑并丢弃孤儿 tail）。

## API 不变量

- 客户端必须将运行时视为系统边界
- 客户端不直接调用工具
- 客户端不创建与持久化的运行时状态相背离的私有会话状态
- 恢复（resume）返回的两类响应都是 runtime 拥有的真相，而非根据 UI 状态推断出的重建版本：无 checkpoint 可续时是**已存储响应的重放**；checkpoint 为 `interrupted` / `provider_failure_retryable` 时是**运行时重跑**（`interrupted` 先截断孤儿事件尾部）后的新响应；客户端不得把后者当作已存储内容或当作纯 UI 重建
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
- `GET /api/sessions` — 列出持久化主会话摘要；成功 `200`；错误 `405`。行按运行时森林的展示顺序（根按最近活跃降序，父节点先于子节点）排列，客户端**不得**重排；每行在 `StoredSessionSummary` 字段之上带一个可空的 `depth`（拓扑 fork 深度），取自运行时的 `session_forest()` 投影；不在森林中的会话为 `null`，客户端按 depth 0 渲染。
- `GET /api/sessions/{id}` — 只读加载/重放持久化会话（不得触发 resume）；成功 `200`；错误 `404`、`405`
- `GET /api/sessions/{id}/events` — 订阅会话有序事件流，支持 `after_sequence` / `follow` 查询参数（SSE 帧信封、`session` 字段交付规则与 `follow` 增量读取语义见 `stream-transport.md`）；成功 `200`（SSE 流）；错误 `400`（`after_sequence` 非整数或为负）、`404`、`405`
- `GET /api/sessions/{id}/result` — 读取会话终态结果视图；成功 `200`；错误 `404`、`405`
- `GET /api/sessions/{id}/debug` — 读取会话调试快照；成功 `200`；错误 `404`、`405`
- `GET /api/sessions/{id}/delegated-context` — 读取 delegated 子会话上下文；成功 `200`；错误 `404`（`code=delegated_context_missing`）、`405`
- `POST /api/sessions/{id}/approval` — 提交审批决策并继续执行，返回下一次暂停或执行结束时的会话快照；成功 `200`；错误 `400`、`409`（`code=no_pending_approval`）、`405`
- `POST /api/sessions/{id}/question` — 回答等待中的问题，返回恢复后的 `RuntimeResponse`；成功 `200`；错误 `400`、`404`、`409`（`code=no_pending_question`）、`405`
- `POST /api/sessions/{id}/cancel` — 按 run identity 取消/中断会话；成功 `200`；错误 `400`、`405`
- `POST /api/sessions/{id}/steer` — 向会话排队一条 steer 消息；成功 `200`；错误 `400`、`404`、`409`（`code=session_sealed`）、`405`
- `POST /api/sessions/{id}/resume` — 显式恢复 interrupted / failed-retryable 会话（会重新进入 graph loop 并重新执行 provider）；成功 `200`；错误 `404`、`405`
- `GET /api/sessions/{parent}/tasks` — 按 parent session 列出 background tasks；成功 `200`；错误 `404`、`405`
- `GET /api/tasks` — 列出 background tasks（workspace 全局视图）；成功 `200`；错误 `405`
- `POST /api/tasks` — 创建 background task；成功 `201`；错误 `400`、`405`
- `GET /api/tasks/{id}` — 读取单个 task 状态；成功 `200`；错误 `404`、`405`
- `GET /api/tasks/{id}/output` — 读取 task 输出/结果视图；成功 `200`；错误 `404`、`405`
- `POST /api/tasks/{id}/cancel` — 取消 task；成功 `200`；错误 `404`、`405`
- `POST /api/tasks/{id}/retry` — 重试 terminal task（复用旧请求创建新的 queued task handle）；成功 `201`；错误 `400`、`404`、`405`
- `POST /api/tasks/{id}/steer` — 向 keep-alive task 派发下一 worker turn；成功 `200`；错误 `400`、`404`、`405`
- `GET /api/settings` — 读取运行时设置；成功 `200`；错误 `405`
- `POST /api/settings` — 更新运行时设置；成功 `200`；错误 `400`、`405`
- `GET /api/workspaces` — 列出 workspace registry 快照；成功 `200`；错误 `405`
- `POST /api/workspaces/open` — 打开/切换 workspace；成功 `200`；错误 `400`（`code=invalid_workspace`）、`404`、`409`（`code=workspace_busy`）、`405`
- `GET /api/providers` — 列出 providers 及 configured/current 状态；成功 `200`；错误 `405`
- `GET /api/providers/{name}/models` — 列出 provider 模型与 catalog metadata；成功 `200`（已配置）/ `409`（未配置，body 仍是同一个 models 载荷）；错误 `405`
- `GET /api/providers/{name}/inspect` — 读取 provider 端点/配置检查视图；成功 `200` / `409`；错误 `400`、`405`
- `POST /api/providers/{name}/validate` — 校验 provider 凭据/就绪状态；成功 `200` / `409`（body 仍是同一个 validation 载荷）；错误 `400`、`405`
- `GET /api/agents` — 列出可用 agents；成功 `200`；错误 `405`
- `GET /api/skills` — 列出 skills；成功 `200`；错误 `405`
- `GET /api/commands` — 列出命令；成功 `200`；错误 `405`
- `GET /api/status` — 读取运行时状态快照（git / lsp / mcp / background_tasks）；成功 `200`；错误 `405`
- `POST /api/status/mcp/retry` — 重试 MCP 连接并返回状态快照；成功 `200`；错误 `400`、`405`
- `GET /api/review` — 读取 workspace review 快照；成功 `200`；错误 `405`
- `GET /api/review/diff/{path}` — 读取单个文件 diff（`{path}` 为 path-safe 多段路径）；成功 `200`；错误 `400`、`404`、`405`
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

### 重复的标量查询参数

当同一个标量查询参数（例如 `show_thinking=true&show_thinking=false`）重复出现时，传输层采用**最后一个取值生效（last-wins）**：FastAPI/Starlette 的查询解析按此约定处理，与 HTTP 客户端（浏览器 `URLSearchParams`、`requests` 等）的普遍行为一致。这是有意选择的契约，而不是偶然行为：迁移前的手写传输层取的是第一个取值，与 HTTP 惯例相悖，现已统一为 last-wins。需要保留多个取值的语义时，路由 MUST 使用显式的重复参数/列表形状，而不是依赖单值参数的多次出现。

## 响应模型（authoritative response surface）

每一条 route 的成功响应体都由 `src/voidcode/runtime/transport/http_models.py` 中的
pydantic 模型描述，并通过 `response_model=` 接到路由上，因此
`/api/openapi.json` 现在会为每个 operation 给出真实的响应 schema（生成客户端类型时
从这里生成，而不是在客户端手写第二份形状）。

需要区分的两件事：

- **模型是描述，不是运行时校验。** 所有 handler 都返回自己渲染的 `Response`
  （`http_contract.JsonResponse`），FastAPI 因此**不会**把响应体过一遍
  `response_model`：字节级渲染（`sort_keys=True`、显式 charset）与每帧成本都不变。
  正因如此，模型必须**完整**覆盖序列化器真正发出的字段，而这一点由
  `tests/integration/test_http_response_schema.py` 用 fixture runtime 驱动**每一条**
  route 来钉住：body 必须能通过模型校验（`extra="forbid"`，未知字段即失败），并且
  再 dump 回来必须与 body 完全一致（缺字段、多余字段、`null`/缺失语义漂移都会失败）。
- **`null` 与“缺失”是两种线格式。** 一部分序列化器在值未设置时**整个省略 key**
  （`SessionRef.parent_id`、event 的 `delegated_lifecycle`、agent/skill 的来源字段、
  debug 快照的 `runtime_policy`、provider model 的每个 capability），另一部分则明确
  输出 `null`（`BackgroundTaskState.child_session_id`、错误信封的 `code`、`kind` 之外的
  帧字段）。模型用 `_absent_when_none` 记录前者，因此 dump 结果与线上 body 逐字节形状
  一致；生成的客户端类型可以据此区分 `field?: T` 与 `field: T | null`。

### 有意保持动态的部分

以下字段是 runtime 拥有的自由形状，按 `dict[str, object]`（JSON object）声明，而不是
伪装成固定字段；其余字段一律完整建模：

| 模型 | 动态字段 | 原因 |
|---|---|---|
| `EventBody` / `SessionDebugEventBody` | `payload` | 每个事件类型拥有自己的 payload 形状（见 `runtime-events.md`），客户端按 `event_type` 路由 |
| `SessionStateBody` / `BackgroundTaskRequestSnapshotBody` / `BackgroundTaskResultBody.hook_reminder` | `metadata` / `payload` / `hook_reminder` | 持久化的 runtime/session 元数据 blob，由 runtime 自己投影与约束 |
| `RuntimePolicyBody` | `diagnostics`、`precedence_trace` 条目、以及 `schema_version`/`policy_version`/`mode`/`read_only`/`agent_preset`/`agent_manifest_id`/`intent.label`/`intent.confidence` | runtime policy 快照的 bounded 投影；标量的类型来自其唯一写入者（`runtime/policy.py`），传输层不再做二次校验 |
| `ProviderContextBody.context_window` | `context_window` | `runtime/context` 拥有的窗口预算投影 |
| `ProviderReadinessBody.reasoning_controls` | `reasoning_controls` | provider 特有的 reasoning 控制项 |
| `CapabilityStatusBody.details` | `details` | 每个 capability manager 自己的明细 |
| `BackgroundTaskStateBody.output_schema` / `structured_output` | 同上 | 由委派调用方提供的 JSON Schema / 实例 |
| 事件内的工具参数 | `arguments`、`artifact`、`tool_arguments`、`tool_calls` 条目、`details` | 工具输入/产物形状由工具自身拥有 |

`model_metadata`（provider 模型目录）不是动态 map：其值类型是完整的
`ProviderModelMetadataBody`，key 为 model id。所有 23 个 capability 字段都已建模，
未知名不会出现（序列化器直接丢弃未设置项）。

### 校验位置

JSON route 与 SSE 帧都在**测试期**校验，热路径不做逐帧/逐响应 pydantic 校验：

- handler 自己渲染 body，FastAPI 的 `response_model` 在这个仓库里只是文档；
- 运行流的帧率是 provider delta 级（每帧可能只是一小段文本），而为每帧跑一次完整
  pydantic 校验会把热路径成本与模型规模挂钩，却只换来“漂移被推迟到线上”的收益；
- 漂移检测的价值来自覆盖全部 route/帧形状的契约测试（加上 CI），不是来自运行期；
- 需要真实请求体校验时，那是**请求**边界（`_HttpBoundaryModel`、`extra="forbid"`）的
  职责，那里本来就有。

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
| `no_pending_question` | 对没有待回答问题的会话提交回答 | `409` | `NoPendingQuestionError`（`runtime/contracts.py`） |
| `delegated_context_missing` | 该会话没有 delegated 子会话上下文（`/api/sessions/{id}/delegated-context`） | `404` | 传输层（runtime 返回 `None` 即该语义） |

`null` 覆盖其余全部错误：未知会话/task（`404`）、方法不允许（`405`）、未匹配路径
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
- 可以使用稳定的会话摘要和存储响应形状来列出持久化会话，并按上文的两模式语义恢复（能续则重跑，否则重放）
- 未来的 API 路由可以直接映射到这些操作上，而无需更改语义
