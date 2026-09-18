# 执行生命周期不变式（execution lifecycle invariants）

## 目的

本文档是 **execution lifecycle 的唯一权威表述**：一次执行（foreground run，或 runtime 派发的 background execution）在「取消如何传入」「何时允许提交」「何时失去所有权」「何时可以释放资源」四个问题上的规范，以及每条规范的**执行点**（enforcement point，注明 `file:line`）。

术语：

- **execution**：由 runtime 拥有的一次执行主体——前台 run 或 `background_task` 派发的一次 worker turn。两者共享同一套提交与真相规则。
- **run 级信号**：一次 run 的 `abort_signal`，由 `ACTIVE_SESSION_REGISTRY` 在 run 注册时创建，`interrupt_active_run` 会取消它。
- **invocation**：一次工具调用。它有独立的取消视图，不是 run 信号本身。
- **执行点**：源码是行为真相。下面的行号引用的是实现该规范的那一处代码；改动该处必须同步本文。

非目标：不定义工具如何实现自己的行为（见 `agent-tool-calling.md`），不定义 delegated 任务的 parent/child 契约细节（见 `background-task-delegation.md`），不定义多进程（跨进程对同一 SQLite 的并发写入属于既有 SQLite 并发模型）。

## (a) 取消如何传入正在运行的执行 / 工具

规范：

1. 只有一个取消槽位对工具可见：`context.abort_signal`。用户中断取消 **run 级信号**；runtime 超时只取消 **该次 invocation**，不得把 run 变成 `interrupted`。两者在同一个槽位上以 `abort_signal.cancelled` / `abort_signal.reason` 呈现。
2. 工具必须轮询该槽位并在观察到后停止；不轮询的工具不可被中断，runtime 只能如实报告 `side_effect_state="unknown"`，不得伪装成普通失败。
3. 超时路径先置位取消信号，**再**停止等待，然后做有界回收（见 (b)）。

执行点：

| 规范 | 执行点 |
| --- | --- |
| run 级信号创建 | `src/voidcode/runtime/active_session.py:88`（`ActiveSessionRegistry.register`） |
| run 入口注册 / 注销 | `src/voidcode/runtime/service.py:1436`（注册）、`:1564`（注销） |
| 用户中断取消 run 信号 | `src/voidcode/runtime/active_session.py:184` → `:220`（`set_cancelled`） |
| request 级 abort flag | `src/voidcode/graph/provider_graph.py:264` → `:277` |
| invocation 取消视图 | `src/voidcode/runtime/tool_execution.py:99`（`_InvocationCancelSignal`），每次调用创建于 `:286`，经 `_invoke_tool` 绑入工具上下文 |
| 超时置位（停止等待之前） | `src/voidcode/runtime/tool_execution.py:367`、`:378` |
| 工具侧读取该槽位 | `src/voidcode/tools/runtime_context.py:88` |
| 超时不得把 run 变成 interrupted | run 信号的写点只有 `active_session.py:220` 与 `graph/provider_graph.py:277`；工具超时只写 invocation 信号 |

## (b) 何时允许把结果提交为真相

规范：

1. 一次 invocation 的结果只有在 runtime 确认其执行已结束之后才允许提交：超时路径先置取消、再有界回收（`_TOOL_TIMEOUT_REAP_SECONDS`），据回收结果给出 `cancellation_signalled` / `execution_stopped` / `side_effect_state`（`settled` 当且仅当 `execution_stopped`）；未确认停止时 `error` 必须说明执行可能仍在运行。
2. 在已记录的调用结果之后到达的完成结果**永不**作为该调用的结果提交，只记入 runtime 日志用于诊断。
3. 所有 runtime 真相写入都必须经过唯一的 storage 写网关；已封印的 session 拒绝任何 late event。
4. 同一 session 上并发 run 共享事件流：终态封印属于**最后一个**活跃 run，先结束的 run 不得封印。
5. 工具结果提交后立即清除该调用的 pending intent；未清除的 pending intent 只是**未结算意图**，不等于结果。
6. **进程崩溃后的可恢复性**：一次执行一旦物化了它要复现的绑定（capability binding + skill snapshot），它的 checkpoint 就必须记录这些绑定，使「执行中崩溃」的 session 可被 resume；resume 只从 checkpoint 的**已完成** tool_results 重新进入循环，丢弃 orphan tail，因此崩溃时在飞的调用**既不会被报告为完成、也不会被自动重放**——模型若仍需要它，会发出一次全新的调用（新的 `tool_call_id`）。
7. 若 checkpoint 完全没有记录绑定（执行在物化绑定之前就被中断），resume 必须在**改写任何真相之前**以具名原因拒绝（`RuntimeRequestError`），而不是在更深处抛出内部缺字段错误。

执行点：

| 规范 | 执行点 |
| --- | --- |
| 有界回收 + 事实判定 | `src/voidcode/runtime/tool_execution.py:402`（`join(_TOOL_TIMEOUT_REAP_SECONDS)`）、`:35`（窗口常量）、`:402-406`（late item 只诊断、不提交）、`:420`（按回收结果判定事实） |
| 提交超时结果（含诚实措辞） | `src/voidcode/runtime/run_loop.py:1040`（`runtime.tool_completed`）、`:1017`（`runtime.tool_timeout`）、`:381`（事实投影） |
| 提交正常工具结果 + 清除 intent | `src/voidcode/runtime/run_loop.py:1187` → `:1193`（`clear_tool_execution_intent`） |
| 唯一 storage 写网关 | `src/voidcode/runtime/storage/sqlite.py:576`（`_write_connect` 内的 `assert_writes_allowed`，位于 `BEGIN IMMEDIATE` 之前） |
| 封印：storage 层 | `src/voidcode/runtime/storage/shared.py:108`，由 `storage/sessions.py:367`、`storage/sessions.py:460` 两个 append 入口调用 |
| 封印：runtime 层（含无活跃 run 的 `interrupted`） | `src/voidcode/runtime/service.py:6414`（`_sealed_session_status`） |
| 只有最后一个活跃 run 可封印 | `src/voidcode/runtime/service.py:2643`（`active_run_count <= 1`） |
| 崩溃后仍可 resume：checkpoint 记录绑定 | `src/voidcode/runtime/service.py:1883`（`_refresh_run_checkpoint` 调用，位于 ACP/skill 绑定物化之后、任何工具调用之前）、`:2195`（helper） |
| checkpoint 的版本要求 | `src/voidcode/runtime/execution/resume_checkpoint.py:206-208`（`version = checkpoint.get("version")` 后 `version != 1` 即拒绝；每个 checkpoint 都带 `kind` + `version`，未知版本不得被当作可续跑） |
| safe boundary 才捕获 checkpoint | `src/voidcode/runtime/run_loop.py:1650`（`len(tool_results) > checkpoint_tool_result_count and self._at_safe_boundary(graph)`）→ `:1651`（`_capture_interrupted_checkpoint`），因此批次中未完成的调用不会进入 checkpoint 的 `tool_results` |
| 无绑定记录时具名拒绝 | `src/voidcode/runtime/resume.py:1266`（拒绝点，先于 tail 截断）、`:95`（`_require_recorded_capability_snapshot`） |

## (c) 何时失去所有权，前 owner 还能做什么

规范：

> 每个 background execution 在开始前取得一份 `ExecutionLease`；只有当前**未撤销**的 lease 能让它写 runtime 真相与 session 事件。撤销按 lease 身份（`task_id` + `generation`）生效。

1. **授予**：唯一分发点在 worker 线程 `start()` 之前授予并递增 generation，worker 在整个执行期内把 lease 绑定到自身线程。
2. **夺取式撤销先于状态变更**：runtime 从执行者手中夺取所有权（shutdown 超时、孤儿扫描、被新 generation 取代）时，先 revoke，再改 task 行状态。
3. **撤销后可提交的内容：无。** 任务状态提交、生命周期通知、以及任何 session / background-task 存储写入都会被拒；工具自己的提交同样被拒——工具执行线程继承调用方的 lease。
4. **前 owner 仍可做**：把每次拒绝记录为 `late_write` 诊断并输出一条 `logger.warning`。诊断按 `task + generation + operation + 拒绝线程` 去重计数，因此条目里的线程与计数同每条 warning 一致（绝不把一次拒绝归因到没做这次写入的线程）；诊断只记录事实，绝不变更 truth；它不是 resume。
5. **`interrupted` 仍是可恢复断点**：resume 由 runtime 拥有（`interrupted -> running`）并授予**新** generation；旧 lease 永久撤销，后来者取得同一 task 不会重新授权旧 execution。
6. **边界（可检查）**：lease 绑定在线程上，网关只校验当前线程的绑定。工具执行线程（`tool-executor`）**在**保证范围内——它继承调用方的 lease（`tool_execution.py:334-337`），因此工具自身的 runtime-owned 提交同样被拒；不在保证范围内的是**工具自己 spawn 的**线程/进程（工具派生并自行持久化的写者）。新增这类会自行持久化的线程前，必须把 lease 传播给它，或在 execution 线程上提交。
7. 该保证是**进程内**的：执行体是活线程，registry 与之同生命周期。

执行点：

| 规范 | 执行点 |
| --- | --- |
| 授予 + generation 递增 | `src/voidcode/runtime/background/supervisor.py:1457` |
| 绑定 / 释放 worker 线程 | `src/voidcode/runtime/background/supervisor.py:1476`（`bind`）、`:1484`（`revoke_if_current`） |
| shutdown 超时夺取 | `src/voidcode/runtime/background/supervisor.py:434` → `:447` |
| 孤儿行夺取 | `src/voidcode/runtime/background/supervisor.py:1327` |
| dispatch 后、启动前 shutdown | `src/voidcode/runtime/background/supervisor.py:1511` |
| 被新 execution 取代 | `src/voidcode/runtime/execution_ownership.py:133`（`grant` 撤销旧 lease） |
| 任务状态提交被拒 | `src/voidcode/runtime/background/supervisor.py:2549`、`:3352` |
| 生命周期通知被拒 | `src/voidcode/runtime/background/supervisor.py:2738` |
| 存储写入被拒（抛 `ExecutionOwnershipRevokedError`） | `src/voidcode/runtime/storage/sqlite.py:576` |
| 工具线程继承 lease（工具提交同被拒） | `src/voidcode/runtime/tool_execution.py:334` → `:337` |
| 拒绝被记录为诊断（每条诊断含拒绝线程） | `src/voidcode/runtime/execution_ownership.py:269`（`_record_late_write`）、`:277-278`（线程属于诊断身份）、`:264`（`late_writes`） |
| 新 generation（resume） | `src/voidcode/runtime/execution_ownership.py:184`（`acquire`） |

## (d) 何时可以释放一次执行依赖的资源

规范：

1. `VoidCodeRuntime` 关闭时的排空顺序是**固定且不可交换**的：① 先 drain background execution（join / revoke / terminalize）→ ② 停 background process manager → ③ 最后才关 ACP/MCP/LSP 适配器。
2. 第 ① 步内，join 超时的 worker 先被 revoke，再被标记 `interrupted`（keep-alive）/`failed`；因此「关闭完成」的含义是：每个已派发 task 行 terminal、按时完成的真相已落盘、未能完成的 execution 已失去写资格。
3. 适配器排在最后，是因为一次 run 的 per-session release 事件由 run loop 在 run 结束时增量落盘；释放适配器不得与这些持久化写入竞争。
4. 工具超时导致的资源回收不是「释放执行依赖」，而是 (a)/(b) 的调用级收尾：它不得触发 run 级资源释放，也不得改变 run 的终态。

执行点：

| 规范 | 执行点 |
| --- | --- |
| 关闭顺序 | `src/voidcode/runtime/service.py:697`（drain）→ `:698`（process manager）→ `:699-701`（ACP/MCP/LSP） |
| drain 内部：revoke 再 terminalize | `src/voidcode/runtime/background/supervisor.py:434`（`_fail_unfinished_shutdown_threads`）→ `:447` |
| run 结束时的 MCP release 事件 | `src/voidcode/runtime/service.py:2572`、`src/voidcode/runtime/resume.py:1518` |

## 并发与 re-entry（同一 session）

| 场景 | 结果 | 执行点 |
| --- | --- | --- |
| 同一 session 上的第二个 fresh run（第一个仍在飞行中） | **允许**：并发追加，各次 append 在同一写网关内串行；终态封印属于最后一个活跃 run | `src/voidcode/runtime/service.py:2643` |
| steering / follow-up | **排队**：写入 session outbox（不产生 mid-run 事件），在下一个 turn 边界或下一次 run 生效 | `src/voidcode/runtime/run_loop.py:1314`、`src/voidcode/runtime/service.py:4606` |
| checkpoint resume（interrupted / provider-failure） | **拒绝**：session 有**其它**活跃 run 时抛 `RuntimeRequestError`，不改写任何真相。streaming resume 会先为自己注册一个 run（`service.py:4734`/`:4757`），该自身 handle 被排除——排除它才不会把自己的注册误判成竞争 owner；blocking resume 不注册，因此任何已注册 run 都会拒绝它 | `src/voidcode/runtime/resume.py:1244`（`exclude_run_id=run_id`）、`src/voidcode/runtime/active_session.py:138`（`contains(..., exclude_run_id=...)`）、`src/voidcode/runtime/service.py:4734`/`:4757`（streaming resume 注册自身 run） |
| resume 的前置校验 | 绑定记录（capability + skill snapshot）与只读准备先于 tail 截断；无绑定记录的 resume 在**截断之前**按名拒绝，无法兑现的 resume 在截断之前失败 | `src/voidcode/runtime/resume.py:1266`（绑定记录）→ `:1279`（准备）→ `:1313`（截断）→ `:1321`（重载） |

多条 run 共享一个 session 时，「取消/超时/所有权」规则依然逐 execution 生效：一次 invocation 的超时只影响该 invocation；一次 background execution 的撤销只影响该 execution；session 的历史重写（resume 的 tail 截断）是唯一要求独占所有权的操作。

## 与详细章节的关系

- 工具调用侧的载荷与工具作者指引：`agent-tool-calling.md` → 「取消与超时（execution lifecycle）」与 `runtime-events.md` → `runtime.tool_timeout`。
- delegated / background 侧的 parent/child 契约细节：`background-task-delegation.md` → 「执行所有权与 late write」「终端封印与关闭排空（terminal seal / shutdown drain）」。

两节只保留各自领域的细节（载荷字段、工具作者要求、delegated 生命周期），生命周期规范本身以本文为准，不再各自重述。

## 相关代码与测试

| 主题 | 代码 | 测试 |
| --- | --- | --- |
| 工具超时取消 / 回收 / 事实 | `runtime/tool_execution.py`、`tools/contracts.py` | `tests/unit/runtime/test_tool_execution_timeout.py` |
| 执行所有权 / late write（含诊断按拒绝线程归属） | `runtime/execution_ownership.py`、`runtime/background/supervisor.py`、`runtime/storage/sqlite.py`、`runtime/tool_execution.py` | `tests/unit/runtime/test_tool_execution_ownership.py`、`tests/unit/runtime/test_late_writes_after_seizure.py`、`tests/integration/test_keep_alive_subagent.py` |
| 进程崩溃 / resume（含首个工具调用中崩溃、无绑定记录时的具名拒绝） | `runtime/resume.py`、`runtime/run_loop.py`、`runtime/service.py` | `tests/integration/test_process_crash_tool_resume.py`（4 个用例）、`tests/integration/test_read_only_slice.py`（crash / orphan tail） |
| 同 session re-entry | `runtime/resume.py`、`runtime/service.py` | `tests/unit/runtime/test_same_session_reentry.py`、`tests/unit/runtime/test_session_lifecycle_seal.py` |
