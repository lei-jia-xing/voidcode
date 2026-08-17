# 后台任务结果读取与 leader 通知契约

来源 issue：#139，#289

## 目的

定义由 runtime 拥有的契约，使后台任务和 delegated child execution 的完成结果能够被 leader session 可靠感知和消费。

当前 runtime 已经具备这层 shipped baseline：

- leader 可见的后台任务生命周期通知
- 结构化结果读取面
- 后台子任务 transcript 的恢复路径
- 通知的去重与重启恢复语义
- CLI / HTTP 的 task status、output、cancel、retry、list parity
- fake provider 与 fake MCP 覆盖的 delegated / MCP lifecycle 测试

## 状态

**状态：已实现的 runtime-owned baseline，仍不是任意拓扑 multi-agent 平台**

本文档描述当前已经落地的 background task / delegated child execution 契约，以及仍明确排除在外的能力。文中使用“应”描述当前实现需要继续保持的不变量，而不是表示这些能力尚未存在。

## 问题陈述

今天的 VoidCode 已经可以：

- 启动、加载、列出、取消和显式 retry 后台任务
- 在用户全局 XDG SQLite 中以 `workspace_id` scoped rows 持久化后台任务真相
- 让后台任务继续走现有 runtime 执行路径
- 通过 `task` 工具路由受支持的 child preset，并建立 parent / child session linkage
- 通过 runtime events 通知 parent session
- 通过 `load_background_task_result` / `background_output` 读取摘要或有界 child transcript
- 通过 CLI 与 HTTP 暴露 task status / output / cancel / retry / list surfaces

它支持的 async delegation 语义是收敛的：

1. leader 启动一个后台任务
2. leader 继续执行或稍后恢复
3. 后台任务完成、失败、取消，或进入 approval-blocked 状态
4. leader 被 runtime 可靠通知
5. leader 读取结构化结果摘要，或恢复完整的子任务 transcript

这层契约的目标是避免 async agent flow 退化成 prompt hack、轮询拼接或 client-local 推断，而不是扩展成任意拓扑 multi-agent runtime。

## 目标

本契约覆盖：

1. **后台结果读取**
2. **leader 通知**
3. **足够支撑恢复的 parent/child linkage**
4. **顺序与去重语义**
5. **让 hooks / clients 消费 runtime truth，而不是自行发明语义**

## 非目标

本契约 **不**扩展到：

- scheduler 或 scheduled runs 设计
- 完整 multi-agent orchestration 语义
- workspace-scoped MCP lifecycle
- MCP 生态市场式语义、provider marketplace 或 dynamic agents marketplace
- peer-to-peer agent bus
- 任意 dynamic agent discovery / runtime-generated agent topology
- #285 context assembly / compaction 的完整产品化语义
- 由 prompt 文本承载的伪通知模型
- 仅由客户端 toast/banner 构成的通知模型
- 替代现有 session replay / resume 契约
- 在 session persistence 之外再发明第二套 execution truth

## 当前基线

runtime 已有的基础能力：

- `BackgroundTaskState`，包含 `queued/running/idle/completed/failed/cancelled/interrupted` 状态（`idle` 表示 keep-alive 任务 turn 结束、等待 leader steer；它不是 terminal 状态）
- `start_background_task` / `load_background_task` / `list_background_tasks` / `cancel_background_task` / `retry_background_task` / `steer_background_task`
- 用户全局 XDG SQLite 中以 `workspace_id` scoped rows 保存的后台任务持久化
- 已有的 session truth，以及通过 `resume(session_id)` 恢复 transcript 的路径

## 并行性边界

当前实现需要区分 foreground tool execution 与 background/delegated execution：

- provider-backed foreground loop 可接收同一 provider turn 返回的多个 tool calls；runtime 会按 provider 返回顺序把它们排入同一 foreground execution episode，并让每个 call 继续经过 `tool lookup → permission → hook → execute → result` 治理路径。
- foreground multi-tool calls 适合短小、独立的 read/search 类工作；它们不是绕过审批的并行写入通道，也不是 delegated child execution。
- foreground `shell_exec` 会在工具内部 worker thread 中运行，以便流式转发 progress events；这不是同一 turn 内的多工具并行执行。
- `task(..., run_in_background=true)` 会创建 persisted `queued` background task，runtime queue 会按 provider/model/default concurrency limit 启动多个 worker thread；默认 concurrency 由 `RuntimeBackgroundTaskConfig.default_concurrency` 控制，并可被 provider/model 级配置覆盖。
- background worker lifecycle、queued/running/completed/failed/cancelled/interrupted 状态、parent notification events 与 result retrieval 都持久化在同一 runtime truth 中；leader 不需要靠 prompt 文本或客户端本地状态推断完成情况。
- `background_output(block=true)` 是显式阻塞等待 surface；常规 agent flow 应优先继续其他安全工作，等待 parent-session notification event 或稍后读取 `background_output(task_id)`。

当前 shipped baseline 还必须保持以下限制：

- parent / child linkage 只表示 runtime-owned delegated lineage，不表示任意 agent graph topology
- result retrieval 不会把完整 child transcript 自动复制进 parent session
- retry 是显式 runtime operation，不能引入无限自动重试；旧 terminal task 保持不可变，retry 会创建新的 queued task handle
- MCP 只按 runtime/session scope 管理，不声明 workspace-scoped lifecycle
- `product` is a delegated read-only plan subagent (child preset). Top-level execution of `product` (e.g. `voidcode run --agent product ...` or a top-level request/runtime-config `agent=product` without delegation) must fail before run/session side effects with the stable `ValueError` message from runtime agent validation: `agent preset 'product' cannot be executed as the top-level active agent in the current runtime; executable agent presets are: leader`. Delegated execution via `task` (`subagent_type=product`) is allowed, and the child returns its plan to the leader via `submit_result`. This state remains visible through bounded runtime policy diagnostics and delegated/task error surfaces.

## 所有权边界

这层能力必须继续由 **runtime** 拥有。

这意味着：

- 生命周期真相属于 `runtime/`
- 通知投递语义属于 `runtime/`
- 结果读取语义属于 runtime contracts 与持久化状态
- hooks 与 clients 只能消费这层真相，而不能定义它

它**不能**由以下位置拥有：

- hook 脚本
- prompt 约定
- Web/TUI 的本地状态
- 未来的 scheduler 子系统

## 必需的 runtime truth

### 1. Parent / Child Linkage

凡是需要通知 leader 的 delegated/background task，都必须显式携带 leader session linkage。

最小要求：

- `parent_session_id: str | None`

解释：

- `None` 表示这是普通后台任务，没有 leader-notification target
- 非空值表示 runtime 必须把该 session 当作通知目标

当前实现中的 `BackgroundTaskState.session_id` 已经表示后台运行生成的结果 session。为了避免与现有 `session_id` 泛称冲突，本文档统一使用：

- `task_id`：后台任务 id
- `parent_session_id`：leader session id
- `child_session_id`：后台子任务对应的结果 session id

并明确约定：

- **在当前代码语境中，`child_session_id` 对应 `BackgroundTaskState.session_id`**

## 结果读取契约

issue #139 提供独立的结果读取面，而不是要求调用方自己拼接多个低层 API。

### 当前形状

```python
@dataclass(frozen=True, slots=True)
class BackgroundTaskResult:
    task_id: str
    parent_session_id: str | None
    child_session_id: str | None
    status: Literal[
        "queued",
        "running",
        "idle",
        "completed",
        "failed",
        "cancelled",
        "interrupted",
    ]
    approval_blocked: bool
    summary_output: str | None
    error: str | None
    result_available: bool
```

### 当前 runtime operation

```python
def load_background_task_result(task_id: str) -> BackgroundTaskResult: ...
```

### 语义说明

- `load_background_task(task_id)` 继续作为低层 task-status surface
- `load_background_task_result(task_id)` 作为 leader-facing retrieval surface
- `summary_output` 是面向 leader 的紧凑结果摘要，仅在可安全暴露时出现
- `child_session_id` 指向完整 child transcript；完整 transcript 继续通过已有的 `resume(child_session_id)` 路径读取
- `result_available` 只在 runtime 能安全暴露摘要结果或 child transcript pointer 时为 `true`
- `approval_blocked` 是**结果视图上的派生字段**，不是对 `BackgroundTaskState.status` 的扩展
- 当 child session 的 `SessionState.status == "waiting"` 时，`approval_blocked` 应为 `true`
- `background_output(full_session=true)` 可以返回 bounded child transcript metadata；`message_limit` 当前被限制在 1 到 100，避免把完整 session 无界塞回 leader context
- failed child result 只提供显式 user-request retry / continuation guidance，建议使用 `session_id=<child_session_id>` 继续；不能由工具自动无限重试
- interrupted child result 是确定性终态，表示 runtime 在重启/中断/超时类场景中无法证明 child 已完成；它与 failed 一样可读取错误摘要，但不能被 late child completion 回退或覆盖

## Transcript 恢复契约

完整 delegated/background history **不应**复制进 leader session。

正确模型应是：

- leader 收到一条 runtime-owned notification event，其中始终包含 `task_id`，并在 child session 已建立时包含 `child_session_id`
- 完整 child transcript 继续保持为 session-scoped truth
- 当 `child_session_id` 存在时，调用方通过现有 `resume(child_session_id)` 路径恢复 child transcript

这保持了当前 runtime 模型的一致性：

- parent session 拥有 leader 可见通知
- child session 拥有 delegated execution history

## Keep-alive 委托与 steer 契约

`task` 工具的 `keep_alive: true` 参数（要求 `run_in_background=true`）创建可重入 worker：child session 跨 turn 累积上下文，leader 反复驱动同一个 child，直到 worker 提交最终结果。

### 状态模型

- 新增 `idle`（awaiting_steer）状态：keep-alive turn 结束、无 handoff 时任务从 `running` 转入 `idle`，worker 线程退出。`idle` **不是 terminal**（`is_background_task_terminal("idle")` 为 false），不参与孤儿扫描（只扫 queued/running）。
- 允许转移：`running → idle`（turn 结束无 handoff）；`idle → running`（收到 steer）；`idle → cancelled|interrupted`（idle 期间 cancel / 进程 shutdown）；`interrupted → running`（keep-alive 断点续跑，仅 keep-alive 任务开放）。
- 并发 slot 只在 turn 在飞时占用；idle 期间释放，steer 派发时重新取 slot（drain 同款路径）。

### 生命周期语义（进程生命周期概念）

- 挂起时：child session 行以 `interrupted` 封印（resumable），task 行 `idle`——两层观察一致：会话可续、任务在等指令。
- 重启后：reconcile 把存活的 keep-alive 任务（`idle`/`running`）terminalize 为 `interrupted`，error 注明 runtime 在等待 steer 时退出；child session 与完整 transcript 保留。续接走现有 `task` 工具 `session_id=<child>` 或 `tasks retry`（keep-alive metadata 随 request metadata 复制）。
- shutdown：idle 的 keep-alive 任务 terminalize 为 `interrupted`（"runtime exited while awaiting steer"）；在飞 turn 标 `interrupted` 而非 `failed`。
- cancel：idle 任务无线程，cancel 直接 terminalize 为 `cancelled`。

### steer surface

```python
def steer_background_task(task_id: str, content: str) -> BackgroundTaskState: ...
```

- 按 `task_id` 路由；child session id 由 task 行派生，steer 方无需知道 session id。
- 校验：任务必须 `keep_alive == 1`；状态必须 `idle`（或 keep-alive 的 `interrupted`，视为断点续跑）；content 非空；`running`（turn 在飞）时拒绝——v1 无 steer 流水线。
- 派发：`mark_background_task_steered`（`idle|interrupted → running`，写入 `steer_prompt` 列）→ 取 slot → spawn 新 worker 线程；worker 以 `task.steer_prompt` 作为本 turn prompt，`session_id=task.session_id` 重入同一 child session，metadata 打内部 `keep_alive_turn: true`（run_loop 据此跳过 one-shot submit_result 强制检查并以 `interrupted` 结束 turn）。
- **不走 `queue_steering`**：turn 结束后 child 行处于封印态，session 队列无法投递；task 行是唯一不受 session seal 管辖的持久化面。
- 中间 turn 不强制 `submit_result`（run_loop gating 在内部 metadata `keep_alive_turn` 上，one-shot child 契约不变）；最终 turn 由 worker 主动 `submit_result`，transcript 有 handoff 证据 → 任务 `completed`、child row 修成 `completed`。

### 事件

- turn 完成（进入 idle）时向 parent 发 `runtime.background_task_awaiting_steer`，payload 含 `task_id`/`child_session_id`/turn 摘要；该事件在 `DELEGATED_BACKGROUND_TASK_EVENT_TYPES` 集合内，可附加到已封印的 parent 行，去重键为 per-turn（child session 最后一个事件的 sequence）。
- hook surface：通过现有 `background_task_notification_enqueued` hook 投递，`extra_payload` 带 `notification_event_type=runtime.background_task_awaiting_steer` 与事件 sequence。

### 工具 / CLI / HTTP surface（与 status/output/cancel/retry/list 平级）

- leader 工具 `steer_task(task_id, prompt)`：调用方必须是任务的 parent（`context.session_id == task.parent_session_id`，session_id 取自 runtime tool context，不信任客户端传入）；`keep_alive=true` 时 `task` 工具在 request metadata **顶层**加 `"keep_alive": true`（不放 delegation 子对象）。
- CLI：`voidcode tasks steer <task_id> "<prompt>" [--json]`；`tasks status` 对 `idle` 状态给出 `tasks steer` / `tasks cancel` 指引。
- HTTP：`POST /api/tasks/<task_id>/steer`，body `{"prompt": "..."}`，返回 steered task state。

## 通知契约

leader notification 必须表现为**附加到 parent session 上的 runtime events**。

### 需要稳定化的事件

- `runtime.background_task_completed`
- `runtime.background_task_failed`
- `runtime.background_task_cancelled`
- `runtime.background_task_waiting_approval`
- `runtime.background_task_awaiting_steer`（keep-alive 任务 turn 完成、进入 idle）

这五类事件已经足够覆盖 issue #139 的最小 leader-notification 需求，以及 keep-alive 委托的 leader 驱动需求。

当前 runtime 同时发出 `runtime.delegated_result_available`，用于表达 delegated/background result 已作为 runtime truth 可被 leader-facing retrieval 消费。background-task lifecycle 事件仍承载 `result_available` 与 `summary_output` 等字段。

### parent-session payload 基线

所有 leader-notification events 至少应包含：

```json
{
  "task_id": "task-123",
  "parent_session_id": "session-leader",
  "status": "completed",
  "result_available": true
}
```

如果某个事件对应的 child session 已经建立，则该 payload 可以额外包含：

```json
{
  "child_session_id": "session-worker"
}
```

### 各事件 payload

#### `runtime.background_task_completed`

- `task_id`
- `parent_session_id`
- `child_session_id`
- `status: "completed"`
- `summary_output`（可选）
- `result_available: true`

#### `runtime.background_task_failed`

- `task_id`
- `parent_session_id`
- `child_session_id`（可选；如果失败发生在 child session 建立之前可为空）
- `status: "failed" | "interrupted"`
- `error`
- `result_available: true`

#### `runtime.background_task_cancelled`

- `task_id`
- `parent_session_id`
- `child_session_id`（可选；若 child session 尚未创建可为空）
- `status: "cancelled"`
- `error`（可选）
- `result_available: false`

#### `runtime.background_task_waiting_approval`

- `task_id`
- `parent_session_id`
- `child_session_id`
- `status: "running"`
- `approval_blocked: true`

这里的 `status: "running"` 明确表示：approval-blocked 是 child session lifecycle 的派生观察结果，而不是对 task status 词汇表的扩展。

对于最小契约，`runtime.background_task_waiting_approval` 不再单独引入新的 `approval_session_id` 标识符；当前语义直接使用 `child_session_id` 指向进入 `waiting` 的 child session。

#### `runtime.background_task_awaiting_steer`

- `task_id`
- `parent_session_id`
- `child_session_id`
- `status: "idle"`
- `result_available`（可选）
- `delegation` / `message`（可选；`_delegated_lifecycle_payloads` 结构）

该事件在每个 keep-alive turn 结束时投递一次（去重键 per-turn），表示 worker 已挂起、等待 leader 用 `steer_task(task_id, prompt)` / `tasks steer` / `POST /api/tasks/<id>/steer` 派发下一 turn。

## 顺序规则

通知事件以 **parent session** 为作用域，并继续服从现有 session event ordering 模型。

要求：

1. 通知事件使用 parent session 自己的 sequence 空间
2. parent session 内 sequence 继续单调递增
3. 通知顺序反映 runtime 提交生命周期真相的顺序，而不是客户端收到事件的时机
4. 如果某个通知同时带有 `result_available`，则该字段不得早于其所依赖的生命周期真相被持久化

## 终端封印与关闭排空（terminal seal / shutdown drain）

session 一旦进入终端状态（`completed` / `failed`，以及没有活跃 run 的 `interrupted`），其持久化真相即被封印：**任何随后到达的 late event（provider delta、tool result、background-task completion、steer/follow-up）如果会改写 session truth，必须被拒绝或丢弃，而不是应用**。

封印由单一权威 guard 执行，分两层：

- **storage 层**（`storage.py::_assert_terminal_session_events_allowed`）：`append_session_event` 与 `append_session_events` 是 `session_events` 仅有的两个写入口，两者都必须经过同一检查；对 `completed` / `failed` 行，非 lifecycle 事件一律抛 `SessionSealedError`。`interrupted` 行在 storage 层**不**封印——该行状态正是运行中 session 的在飞状态；`interrupted` 的封印由 runtime 层完成。
- **runtime 层**（`VoidCodeRuntime._sealed_session_status`）：把封印扩展到「没有活跃 run 的 `interrupted` 行」，并 gate 交互队列（`queue_steering` / `queue_follow_up`）。活跃 run 在 `ACTIVE_SESSION_REGISTRY` 中注册期间，其自身 append 永远不算 late event；只有显式 re-entry（fresh run / follow-up / approval / question resume 通过 `save_interrupted_checkpoint` 或 `save_run` 解除封印）才能重新打开 session。

对 background/delegated 子任务的具体语义：

1. **child/task 真相独立于 parent 封印**：child session 行与 `background_tasks` 行是 child 自己的 truth；parent 封印不影响 child 完成结果的持久化（task 标记 terminal、child session 落盘照常进行）。
2. **parent 通知是唯一 sanctioned 的例外**：`DELEGATED_BACKGROUND_TASK_EVENT_TYPES` 仍允许附加到已封印的 parent 行（parent 需要感知 delegated result 的完成），但它们只是通知，永远不会改变 parent 的 status / sequence 顺序 / resume checkpoint。若未来某通知类型落入封印范围，worker 侧必须捕获 `SessionSealedError` 并丢弃通知，而不是崩溃或回退 child 真相。
3. **关闭排空顺序（enforced）**：`VoidCodeRuntime.__exit__` 必须先 `shutdown_background_tasks()`（join 每个 worker——worker 在退出前已把 task 终态、child session 真相、parent 通知与 lifecycle hooks 全部落盘；超时未完成的 worker 被标记 `failed` 以兜底），再停掉进程管理器，最后才关闭 ACP/MCP/LSP 适配器。session 封印侧：run 产生的所有事件先增量 append，`save_run` 只封存行快照且绝不回退 `last_event_sequence`，封印先于活跃 run 注销发生，从而把「封印后到达的 late event」精确地暴露给 runtime guard 并被拒绝/丢弃。

## v1 delegated/background idle reminder

v1 的 delegated/background idle reminder 只能由 runtime 拥有，并且只能基于已经存在的 runtime truth 来判定是否需要轻量提醒。

它是一个 soft nudge 机制，不是 scheduler，也不是覆盖所有 session 的全局 reminder system。它只面向带有 `parent_session_id` 的 delegated/background child session，并且只在 runtime 已经观察到真实的 `session_idle` / waiting 语义后才进入可投递状态。

约束如下：

- 触发源以 `session_idle` 这类真实 lifecycle signal 为准，而不是客户端空闲、prompt 文本或定时调度
- reminder 的目标是提示 leader 继续查看、恢复或显式处理 child session，而不是替代 task/session truth
- reminder 不能把自己变成新的 authority layer，也不能把 hook、client 或 prompt 文本变成 truth source

### cooldown 与 dedupe

同一 eligible idle episode 只应发送一次 reminder。

这里的 eligible idle episode 指的是：runtime 已经观察到 child session 进入 idle/waiting 语义，但该 episode 还没有被新的 progress、result、retry、cancel 或 terminal transition 终结。

episode 级 dedupe 的意思是：只要当前 episode 已经发过一次 reminder，后续再看到同一个 episode 的 idle/waiting 事实，也不能重复投递。

如果 child session 重新进入新的 idle episode，runtime 才可以再次产生新的 reminder。

### stop conditions

一旦出现以下任一情况，runtime 必须停止对当前 eligible idle episode 继续发 reminder：

- result 已被读取，包括 `background_output(task_id)`、`load_background_task_result(task_id)` 或通过 `resume(child_session_id)` 消费 child transcript 的路径
- 显式 retry
- cancel
- terminal completion
- terminal failure
- interruption
- 当前 eligible idle episode 的 reminder 已经发出过一次

也就是说，stop condition 既包括对结果的读取，也包括对 episode 的终止、重试或取消，不允许 runtime 围绕同一 episode 持续补发 reminder。

## Background process guardrail scope

background process 相关 guardrail 只覆盖当前 v1 已落地的有限范围，不延伸为模糊去重或连续监视系统。

约束如下：

- `background_process_start.py` 中的 live process reuse 仍然以精确匹配为准，只有 trimmed command 相同且 workspace 相同才算同一个可复用进程
- `background_process_logs.py` 提供的是有界的日志状态检查，读取的是 retained stdout/stderr，不是持续 watch 或订阅流
- v1 不做 fuzzy duplicate-process suppression，也不做基于启发式的进程身份推断
- v1 不把日志读取改造成 scheduler、watcher 或跨进程协调层

这意味着，当前背景进程护栏只负责 exact-match reuse 和 bounded log read 两件事，重复读取日志只是有界的状态检查，不是连续监视或自动去重决策。

## 去重规则

leader notification 必须满足：**对同一语义转换至多投递一次**。

最小要求：

- runtime 为每种通知语义持久化内部 delivery markers

例如：

- completed 通知仅发出一次
- failed 通知仅发出一次
- cancelled 通知仅发出一次
- approval-blocked 通知在当前 waiting 状态下仅发出一次

重启后，runtime 必须依据持久化的 notification delivery state，避免把同一条通知再次写入 parent session。

> 这里的 delivery state 属于 runtime 内部持久化真相，不要求出现在 `BackgroundTaskResult` 这样的 leader-facing retrieval payload 中。

## 恢复语义

恢复是必需的，因为 parent 或 runtime 可能在 child 完成后、leader 消费前重启。

要求：

1. child task 先持久化自己的 lifecycle truth
2. 通知投递状态也作为 runtime truth 持久化，而不是依赖内存回调
3. 重启后 runtime 能判断 leader 是否已经被通知
4. 如果 lifecycle truth 已存在、但通知尚未完成提交，则 runtime 在 reconciliation 中补全通知投递
5. parent session 的 `resume(session_id)` 必须像其他 session event 一样暴露这些已投递通知

## Approval-blocked 语义

approval-blocked 不是 terminal task status，但它对 leader 是重要事件。

要求：

- 当 child session 进入 `waiting` 时，runtime 向 parent session 发出 `runtime.background_task_waiting_approval`
- leader 可通过 `child_session_id` 检查进入 `waiting` 的 child session
- 通知不得通过普通文本输出注入来伪装实现

## Routing、工具与 skill guardrails

Delegated child execution 必须先经过 runtime-owned routing 与 tool scope enforcement：

- `task` 工具会校验 `subagent_type`，只允许受支持的 child presets。
- 当前顶层 active preset 是 `leader`；delegated child presets 是 `advisor`、`explore`、`researcher`、`worker`、`product`（`product` 是只读 plan subagent）。
- provider 可见工具 schema 会被 agent manifest allowlist 和 request tool config 收窄。
- 即使 provider 伪造 raw tool call，runtime tool lookup 仍必须拒绝 denied built-in tools。
- `worker` 当前不默认获得再次 delegation 的 `task` 工具；这避免形成无控制的 nested delegation。
- manifest `skill_refs` 作为 catalog/default selection 进入 runtime skill application；`force_load_skills` 与 delegated `load_skills` 只在目标 run 或 child session 注入完整 skill body，parent full-body skill context 不应泄漏给 child。
- hook guardrails 是 runtime lifecycle 的观察/干预层，不是 session truth 的替代品；background terminal hook failure 只记录警告，不改写已持久化的 terminal truth。

## Runtime Harness Policy delegation gate

Before any child-session allocation, background-task row creation, queueing, lifecycle hook notification, or provider/tool execution side effect, delegated execution must pass the parent `RuntimePolicySnapshot.delegation_policy`. The child snapshot is derived from the parent snapshot plus the selected child manifest and can only be a subset of parent tools, skills, hooks, MCP bindings, prompt activations, and delegation rights.

The product plan preset is a delegated read-only child preset at this gate. Direct `subagent_type="product"` and configured aliases that resolve to product route to the product subagent, which returns its plan to the leader via `submit_result`. Top-level execution of `product` (e.g. `voidcode run --agent product ...` or a top-level request/runtime-config `agent=product` without delegation) must fail before run/session side effects with the stable `ValueError` message `agent preset 'product' cannot be executed as the top-level active agent in the current runtime; executable agent presets are: leader`. This contract does not add product delegation helpers beyond the existing `task` routing, and the product subagent stays read-only (no task/shell/write). Delegation allow/deny state is visible through the bounded `runtime.request_received.payload.runtime_policy` projection and through delegated/background lifecycle or explicit runtime failure payloads; clients must not infer delegation authority from prompt text.

## MCP scope 与测试立场

MCP 当前是 runtime-managed capability，不是 workspace-scoped marketplace：

- runtime-scoped MCP server 可被同一 runtime 复用。
- session-scoped MCP server 按 owner session key 管理，并在 session 完成、释放或 idle cleanup 时关闭。
- MCP tool discovery 与 tool call 仍通过 runtime-managed lifecycle 和 tool registry 暴露。
- 当前不声明 workspace-scoped MCP lifecycle、MCP 生态市场式语义、dynamic MCP install flow 或 skill marketplace。
- 自动化测试使用 fake MCP / fake stdio manager 覆盖 lifecycle、concurrency、session release 与 failure paths；CI 不需要真实 `npx @playwright/mcp` 或外部 MCP server。

## Result、retry 与 cancel flow

Leader 读取结果时应遵守以下规则：

- `background_output(task_id)` 默认返回紧凑结果视图。
- `background_output(task_id, full_session=true)` 返回 bounded transcript payload，并带 child session id 与 transcript metadata。
- CLI `voidcode tasks status/output/list/cancel/retry/steer --json` 返回 machine-readable payload，并保留 readable 默认输出；结构化字段应包含 `task_id`、`parent_session_id`、`requested_child_session_id`、`child_session_id`、approval / question request id、`result_available`、`error_type` 与 `next_steps`。
- CLI readable 默认输出先暴露 `TASK ...` correlation record，并在 waiting / running / idle / failed / completed 等状态下打印 concrete next-step commands（例如 `sessions resume <child_session_id>`、`tasks output <task_id>`、`tasks steer <task_id> "<prompt>"`、`tasks cancel <task_id>`）。
- `block=true` 等待超时时返回 `block_timed_out`，同时保留当前 task state，而不是把任务误标为失败。
- failed/cancelled/interrupted task 可以通过 runtime 方法 `retry_background_task(task_id)`、CLI `voidcode tasks retry <task_id>` 或 HTTP `POST /api/tasks/<task_id>/retry` 显式重试。retry 必须复用旧 task 持久化的 request prompt、requested child session id、parent session id、metadata、routing 与 `allocate_session_id`，并创建新的 queued task handle；不得改写旧 terminal task。
- idle keep-alive task 通过 `steer_background_task(task_id, prompt)`（CLI `voidcode tasks steer <task_id> "<prompt>"`、HTTP `POST /api/tasks/<task_id>/steer`、工具 `steer_task(task_id, prompt)`）派发下一 worker turn；keep-alive 的 `interrupted` 任务可视为断点续跑同样 steer。steer 只对 keep-alive 任务开放，且任务必须处于 `idle`/`interrupted`（turn 在飞时拒绝）。
- failed/interrupted child 输出可以提示用户显式请求 retry/continue，并优先使用 runtime-owned `retry_background_task` 返回的新 task id；工具本身不得自动进入无限 retry loop。
- repeated child failure 应升级给 leader / user，而不是继续隐藏在后台循环里。
- `background_cancel` 对 unknown task 返回稳定 `status="unknown"` payload；对 running task 标记 cancel requested；对 completed/cancelled 等 terminal task 返回其 terminal state，不描述成新取消。
- `background_output` 的 payload 应包含 `retrieval_instruction` 与 compact `handoff_summary`，至少表达 objective、completed work、open questions、files touched、verification、blocked/error reason；未知项用空值或空列表表达，不能要求客户端推断。

## Lifecycle hooks

runtime 公开以下 background/delegation hook surfaces，全部由 runtime truth 触发，hook 不能改写 task truth：

- `background_task_registered`
- `background_task_started`
- `background_task_progress`
- `background_task_completed`
- `background_task_failed`
- `background_task_cancelled`
- `background_task_notification_enqueued`
- `background_task_result_read`
- `delegated_result_available`

## Polling 与 Notification 的关系

Polling 可以存在，但不能成为唯一模型。

契约拆分：

- `load_background_task_result(task_id)` = pull surface
- parent-session notification events = push surface

clients、hooks 与未来 async orchestration 可以选择其一或同时使用，但它们都必须建立在同一层 runtime truth 之上。

## Hook 与 Client 的含义

本 issue 不应把 hooks 变成真相源。

正确顺序应是：

- runtime 先发出并持久化通知真相
- hooks 作为可选观察者消费这些生命周期时刻
- Web / TUI / SSE 在 parent session 上直接渲染这些 runtime events，而不是引入自己的并行通知模型

## 验收检查点

这层 baseline 只有在以下条件全部成立时才算保持有效：

1. leader 能启动一个带 notification target 的后台任务。
2. 当 child task 完成、失败、取消或进入 approval-blocked 状态时，parent session 会收到且只收到一条对应的 runtime-owned notification event。
3. 调用方可以读取结构化后台任务结果，而不是靠 prompt 文本 scraping。
4. 调用方可以通过 `resume(child_session_id)` 恢复完整 child transcript。
5. 重启 / reconciliation 不会重复投递 leader 通知。
6. parent session 中的通知顺序是确定性的、可 replay 的。
7. CLI 和 HTTP task surfaces 暴露一致的 delegated correlation 字段。
8. fake provider 与 fake MCP 测试覆盖 routing、tool guardrail、skill force-load、MCP lifecycle、result、explicit retry 和 cancel paths。

## 验证命令

维护该契约时至少运行：

```bash
uv run pytest tests/unit/runtime/test_runtime_events.py tests/unit/interface/test_cli_delegated_parity.py
uv run pytest tests/unit/tools/test_background_task_tools.py tests/unit/runtime/test_mcp.py -k "background or cancel or output or mcp"
mise run check
```

如果 `mise run check` 失败，必须记录是新失败还是预先存在的失败。契约测试应继续使用 fake provider / fake MCP；除非任务明确要求，不应把 live provider 或真实 MCP server 作为 CI 前提。

## 超出本 issue 的后续工作

本文档只覆盖 runtime-owned delegated baseline 的 routing、retrieval、notification、retry/cancel 与测试立场。以下内容仍然属于单独 follow-up：

- 更丰富的 child-session lineage / topology 设计
- 完整的 async leader/worker orchestration 语义
- scheduler integration
- UI-specific presentation choices
- workspace-scoped MCP、marketplace、dynamic agents、peer-to-peer agent bus
- #285 context assembly / compaction 的完整产品化语义
