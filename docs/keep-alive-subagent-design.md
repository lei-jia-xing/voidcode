# Keep-Alive 子会话（可重入 worker）设计

## 状态

- 状态：proposed
- 范围：design-only（本文件只记录设计与决策，不包含实现代码；落地时以 `src/` 下实际代码为准）
- 目标仓库：`voidcode`
- 关联文档：`docs/contracts/background-task-delegation.md`（task 状态词汇与 surface 契约，本设计会修订它）、`docs/runtime-owned-scheduler-design.md`、`docs/mode-composition-design.md`（文档格式参照）

## 结论先行

**Session 层无差距（已实测，采信前序调查）**：child session 在 `completed`/`failed` 后可用 `run(child_session_id, prompt=新指令)` 重入并累积上下文，与主 agent 走完全相同的路径。keep-alive **不需要** session 层新增任何状态；差距全部在 task 层，加上 run_loop 一处最小改动。

本设计的选择（详见各决策节）：

1. **状态模型**：task 层新增 `idle`（awaiting_steer）状态，而非"running + 长活线程"。`idle` 不是 terminal，terminal 转移矩阵只扩三行。
2. **Worker 生命周期**：**per-turn 线程 + 持久化 idle 态**，不引入长活线程循环。`run_background_task_worker` 跑完一个 turn 后：有 handoff → 走现有 finalize（completed）；无 handoff（且任务为 keep-alive）→ `mark_background_task_idle` + 发 turn 完成事件 + 线程退出。下一 steer 重新派发一个全新线程——上下文全部在 SQLite child session 里，无需在线程间保活任何内存状态。
3. **submit_result 调和**：worker 在 keep-alive turn 的 internal request metadata 里打 `keep_alive_turn: true`；`run_loop.py:1750-1753` 对该标记跳过强制检查，且 final step 状态写 `interrupted`（复用"resumable child"语义：无 handoff 不 seal、不 terminalize）。现有 one-shot child 契约**完全不变**。
4. **steer 分发**：新增 runtime surface `steer_background_task(task_id, content)`，按 **task_id** 路由（child session id 由 task 行派生）。**不走 `queue_steering`**——turn 结束后 child 行处于封印态，`_queue_runtime_message` 会抛 `SessionSealedError`（已核实 `service.py:4700-4728` + `_sealed_session_status`）。CLI/HTTP/tool 各加一个 steer surface，与 status/output/cancel/retry/list 平级。
5. **持久化与恢复**：`background_tasks` 表 v8 迁移加 `keep_alive`/`steer_prompt` 两列。keep-alive 是**进程生命周期概念**：挂起时 child 行 `interrupted`（runtime 层封印、resumable）、task 行 `idle`；重启后 reconcile 将 keep-alive 任务 terminalize 为 `interrupted`（child 会话与完整 transcript 保留），续接走现有 `task` 工具 `session_id` 参数或 `tasks retry`。不改变 seal 机制。
6. **治理边界**：复用现有 delegation routing（`resolve_subagent_route`/worker preset）；spawn budget 天然正确——`_metadata_with_delegation_governance` 只在 `existing_session_id is None` 时扣减（`service.py:5697-5700`），keep-alive 重入同一 child session 不重复扣。

**必须动 session 层的唯一改动**：`run_loop.py` 的 final-step 检查/状态两行（D3），gated 在内部 metadata 上，对非 keep-alive 路径零影响。其余全部是 task 层。

---

## 1. 背景与目标

### 1.1 目标

让 delegated child（subagent）像主 agent 一样**反复重入同一个 session、跨 turn 累积上下文**，且 subagent 与主 agent 的 session 行为无差距（或差距很小）。keep-alive 是 vibe 式 director/worker 执行模型的地基：director（leader）给 worker 一条指令 → worker 执行一轮并挂起 → director 视进度发下一条指令 → …… → worker 提交最终结果。

### 1.2 非目标

- **不引入 append-only session tree**（OMP 那种）：复用现有 SQLite + session metadata 体系。
- **不扩成任意 multi-agent topology**：keep-alive 是 runtime-owned 的收敛能力，只服务"同一 leader 反复驱动同一 child"这一种关系。
- 不做跨进程/跨机器 worker 驻留、不做 worker 间通信、不做集群调度（参见 `docs/runtime-owned-scheduler-design.md` 的边界）。
- 不改变现有 one-shot 委托的 `submit_result` 契约、不改变 terminal task 不可变原则（`completed/failed/cancelled → frozenset()` 保持）。
- 不做 steer 流水线（同一时刻多个排队 steer）——v1 要求 leader 等 worker 回到 idle 再发下一条。

## 2. 现状（已核实）

### 2.1 session 层无差距（前序调查结论，实测验证）

- `service.py`（`src/voidcode/runtime/service.py`，下同）`save_interrupted_checkpoint(create_if_missing=True)`（约 2118-2129）是解除封印的确切调用点：注释明说 `status in {completed, failed}` 是 **per-TURN** 而非 per-session，follow-up 重入同一 session_id 靠它 un-seal。
- `service.py:2061-2083`：existing_session 加载 + `_rehydrated_conversation_segments_for_existing_session` + `_rehydrated_tool_results_for_existing_session` 水合，无 terminal 检查。
- `service.py:2809`：终态 seal 守卫 `seal_terminal_status = ACTIVE_SESSION_REGISTRY.active_run_count(...) <= 1`（per-run 重叠保护）。
- 无任何针对 parent session 的封印/拒绝分支。

### 2.2 task 层三处差距（前序调查结论）

1. **one-shot worker 线程**：`background_tasks.py:2100 run_background_task_worker` 跑一次 `_run_with_persistence` → `2303 finalize_background_task_from_session_response` → `2304 return`；`2339 finally self._threads.pop(task_id, None)`。跑完即退，无"等待下一 steer"。
2. **terminal task 不可复活**：`task.py:72-79 _BACKGROUND_TASK_ALLOWED_TRANSITIONS`：`completed/failed/cancelled -> frozenset()`（空）；`interrupted -> {completed,failed,cancelled}`。`storage.py:3563-3608 mark_background_task_running` 对非 queued 是 no-op；`background_tasks.py:877` drain 只派发 queued；`background_tasks.py:523-546 retry_background_task` 拒绝 completed 且生成新 task_id。
3. **submit_result 契约**：`run_loop.py:1750-1753`：`if is_final_step and session.session.parent_id is not None: if not tool_results or tool_results[-1].tool_name != "submit_result" or tool_results[-1].status != "ok": raise ValueError("delegated child must call submit_result before completing")`。child 每个 final step 必须 submit_result，但 keep-alive 的中间 turn 不是最终 turn。

### 2.3 补充核实：steer/follow-up 机制与 seal 守卫（本轮核实）

- **steering 注入**：`service.py:2024-2039`（`_stream_chunks` 开头）`drain_runtime_messages(existing_session.session.metadata, kind="steering")` 把 queued steering 拼进本次 prompt（`"Runtime steering messages:\n..."`），并 `update_session_metadata` 落库。
- **follow-up 递归**：`service.py:1831-1849`——`final_session.status == "completed"` 时 drain `follow_up`，逐条以 `RuntimeRequest(prompt=followup.content, session_id=session_id, ...)` 递归 `_run_with_persistence`（同一 session）。
- **队列存储**：`interaction_queue.py` `enqueue_runtime_message`/`drain_runtime_messages`，消息放 session metadata 的 `pending_messages`（截断 50 条）。
- **seal 守卫（关键发现）**：`service.py:4700-4728 _queue_runtime_message` 先查 `_sealed_session_status(session_id)`；`service.py:7200-7247 _sealed_session_status` 对 `completed/failed` 以及**无活跃 run 的 `interrupted`** 一律返回 terminal 状态 → `queue_steering`/`queue_follow_up` 抛 `SessionSealedError`。**因此：turn 结束后（row 已 seal）leader 无法再往 child session metadata 排队 steer。** 这是"steer 必须走 task 层、不能走 session 队列"的硬证据。
- **sealed parent 通知例外**：`events.py:502-511 DELEGATED_BACKGROUND_TASK_EVENT_TYPES`（waiting_approval / idle_reminder / completed / failed / cancelled / interrupted / group_completed / delegated_result_available）是唯一允许附加到已封印 parent 行的事件集（`storage.py:104-119 _TERMINAL_ALLOWED_EVENT_TYPES`）。keep-alive 的 turn 完成通知必须加入该集合。
- **worker 现有 handoff 判定（可复用）**：`background_tasks.py:1815-1831 _child_terminal_status_from_response`——`interrupted` 行 + `_child_transcript_completed()`（`submit_result` ok + `graph.response_ready`，1833-1853）→ `completed`；无 handoff 的 interrupted → `None`（"Genuinely resumable interrupted children (no handoff) yield None and are never sealed or terminalized"——**这正是 keep-alive 挂起的现成语义**）。
- **并发 slot**：`background_tasks.py:624-639 _reserve_slot/_release_slot` 纯计数（provider/model 双维度）；drain 在派发 queued 任务时 reserve 一次，worker 线程经 start-gate 后任务行已是 `running`（走 else 分支，不重复 reserve）[推断：worker 的 `queued` 分支是防御路径]。
- **shutdown**：`background_tasks.py:188-247 shutdown`（置 `_shutdown_requested` → join → 超时 `_fail_unfinished_shutdown_threads:264-291` 标 failed）→ `_terminalize_queued_tasks_for_shutdown:242-262`（queued → interrupted）。
- **reconcile**：`background_tasks.py:2004-2073`（启动一次）——`fail_incomplete_background_tasks`（`storage.py:3915-3993`，只扫 queued/running）把失联 running 标 interrupted；drain 的孤儿扫描（`background_tasks.py:829-866`）terminalize "running 无线程"任务（waiting-approval 豁免）。
- **CLI/HTTP surface**：`cli/app.py:3221-3289` tasks 组（status/output/cancel/retry/list）；`http.py:79-160` RuntimeTransport 协议含 task 各 surface。
- **leader 工具 allowlist**：`agent/builtin.py:20-41 _LEADER_TOOL_ALLOWLIST` 含 `task`/`background_cancel`/`background_output`，**不含** submit_result（submit_result 仅 child 可用，`tools/submit_result.py:49-50` 要求 `parent_session_id is not None`）。
- **spawn budget**：`service.py:5661-5721 _metadata_with_delegation_governance`——`if existing_session_id is None: remaining_spawn_budget -= 1`；depth = parent_depth + 1（每 turn 从 parent 重算，不漂移）。

### 2.4 关键约束提炼

1. terminal task 不可复活——keep-alive 任务**永不进入 terminal** 直到最终 handoff/cancel。
2. turn 结束后 child 行被封印——steer 必须绕过 session metadata 队列，走 task 行。
3. 中间 turn 不能被 run_loop 强制 submit_result，也不能 seal 成 `completed`（否则 reconcile/孤儿扫描会误判任务完成）。
4. 并发 slot 是 per-运行资源，idle 期间不应占坑；但也因此 steer 派发需要走"取 slot → spawn"路径。

## 3. 设计决策

### D1 状态模型：新增 `idle`（awaiting_steer）

**选 `idle`，不选"running + 长活循环"**：

- `running` 在现有代码里的不变量是"该行必须被一个活着的 worker 线程拥有"（drain 孤儿扫描 `background_tasks.py:829-866`、`fail_incomplete_background_tasks`、shutdown 的 `_fail_unfinished_shutdown_threads` 全都建立在这个不变量上）。让 `running` 表示"挂起等 steer"会同时击穿三条扫描路径，恢复逻辑必须逐处打补丁。
- 一个独立、非 terminal 的 `idle` 态让"活着、等指令"成为一等公民：孤儿扫描不碰它（只扫 running/queued）、shutdown 单独处理它、observability/CLI 能给它专属文案。`is_background_task_terminal("idle")` 为 False，`_BACKGROUND_TASK_TERMINAL_STATUSES` 不变。

**转移矩阵精确修改**（`task.py:72-79`）：

```python
_BACKGROUND_TASK_ALLOWED_TRANSITIONS: dict[BackgroundTaskStatus, frozenset[BackgroundTaskStatus]] = {
    "queued": frozenset({"running", "completed", "failed", "cancelled", "interrupted"}),  # 不变
    "running": frozenset({"completed", "failed", "cancelled", "interrupted", "idle"}),  # +idle
    "idle": frozenset({"running", "completed", "failed", "cancelled", "interrupted"}),  # 新增
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "interrupted": frozenset({"completed", "failed", "cancelled", "running", "idle"}),  # +running（keep-alive 断点续跑）、+idle
}
```

要点：

- `running → idle`：turn 结束、无 handoff、任务为 keep-alive。
- `idle → running`：收到 steer（派发 turn）。
- `idle → cancelled|interrupted`：idle 期间 leader cancel / 进程 shutdown。idle → completed/failed 只经 `idle → running → terminal` 到达（不做"无 turn 直接完成"的 surface）；矩阵里保留它们仅为 `mark_background_task_terminal` 的通用性（idle 不是 terminal，terminalize 只能从 running 派生的状态发起——见 D5）。
- `interrupted → running`：keep-alive 任务中断后（进程重启/崩溃）可以**同 task_id 续跑**（复用同一 child session），这是对现有 retry（新 task_id）的补充，仅对 keep-alive 任务开放。
- `BackgroundTaskStatus` literal（`task.py`/`contracts.py`）加入 `"idle"`；`_BACKGROUND_TASK_TERMINAL_STATUSES` 不加。

### D2 worker 生命周期：per-turn 线程 + 持久化 idle 态

**不引入长活线程**。理由：

- 上下文不在线程里，在 SQLite child session 里（整个前提）。线程间无需保活任何内存状态 → 不需要 threading.Event/queue 信号机、没有唤醒竞态、没有线程泄漏面。
- 现有 `_threads` dict、孤儿扫描、shutdown join/terminalize 的"线程寿命 ≈ 一个 turn"假设全部原样成立。
- 长活循环方案（线程等 Event）需要处理：wake 竞态（steer 到达瞬间 turn 恰好结束）、shutdown 时阻塞在 Event 上的线程如何唤醒、线程崩溃后如何自愈——三处新机制，都是现有代码刻意避免的复杂度。

**形状**：`run_background_task_worker` 主体（`background_tasks.py:2100-2347`）保持：load task → 校验 → 取 slot（queued 分支）→ `while True` rate-limit 重试循环内跑 `_run_with_persistence` → cancel 轮询。**只改 finalize 一步**（原 2303-2304）：

```
turn_response = RuntimeResponse(session=final_session, events=..., output=...)
if self._response_has_rate_limit_error(turn_response) and retry_count < MAX:
    ...  # 现有 rate-limit 重试，不变

task = load(task_id)
if task.request.metadata.get("keep_alive") is True:          # keep-alive 分支
    if turn_response.session.status == "failed":
        finalize (现有路径 → failed)
    elif turn_response.session.status == "waiting":
        existing waiting 处理（idle reminder + return，task 保持 running）   # 审批/提问暂停
    elif self._child_transcript_completed(turn_response):     # submit_result ok + response_ready
        finalize_background_task_from_session_response(turn_response)   # → completed（含 _seal_child_session_from_response 修 row）
    else:
        store.mark_background_task_idle(task_id)              # running → idle
        emit awaiting-steer 事件到 parent（见 D4）
        return                                               # 线程退出，slot 在 finally 释放
else:
    finalize_background_task_from_session_response(turn_response)   # 现有 one-shot，不变
```

worker 的 finally（`self._threads.pop(task_id, None)` + `_release_slot` + drain）不变。

**为什么中间 turn 用 `_child_transcript_completed` 而不是 `_child_terminal_status_from_response`**：后者对 row status == "completed" 直接返回 completed（无 transcript 证据）。keep-alive 中间 turn 被 D3 强制成 `interrupted`，所以两者等价；但防御上 keep-alive 分支只信 transcript 证据，防止任何残留路径把无 handoff 的 turn 误判为完成。

**并发 slot**：idle 期间不占 slot；steer 派发时经 drain 同款路径重新取 slot（见 D4）。`idle` 行不参与 `list_running_background_tasks`（只扫 running），孤儿扫描天然不碰。

### D3 submit_result 契约调和（唯一 session 层改动）

**机制**：worker 在 keep-alive turn 的 internal request metadata 里带 `keep_alive_turn: true`（与现有 `background_task_id`/`background_run` 同层，走 `allow_internal_metadata=True`；需在 `_metadata_with_delegation_governance` 的 `allow_internal_fields` 白名单与 `validate_runtime_request_metadata` 校验中放行 [推断：位置在 `service.py:5520-5540` 附近，实现时核对]）。

**run_loop.py 两处，均 gated 在 `session.metadata.get("keep_alive_turn") is True`**：

1. `1750-1753` 的强制检查改为：`if is_final_step and session.session.parent_id is not None and not session.metadata.get("keep_alive_turn"):` —— keep-alive 中间 turn 不再强制 submit_result；**one-shot child 完全不受影响**（metadata 无此键）。
2. final step 的状态（约 1785-1795，`SessionState(..., status="completed", ...)`）改为：keep-alive turn → `status="interrupted"`（否则 `completed`）。

**为什么 interrupted**：`_child_terminal_status_from_response` 对"interrupted + 无 handoff"返回 None（"resumable child"）→ `finalize_background_task_from_session_response` 不 seal、不 terminalize——这正是挂起语义；有 handoff 时同一函数把 row 修成 completed 并 terminalize 任务（现有 `_seal_child_session_from_response` 已实现）。顺带收益：`_run_with_persistence` 的 follow-up drain 只在 `completed` 触发，keep-alive 中间 turn 不会误 drain session 队列。

**契约矩阵**：

| 场景 | turn 结束 row 状态 | run_loop 检查 | 结果 |
|---|---|---|---|
| one-shot child（现状） | completed | 强制 submit_result | 不变 |
| keep-alive 中间 turn | interrupted | 跳过 | worker → idle，不 seal 任务 |
| keep-alive 最终 turn（child 主动 submit_result） | interrupted | 跳过 | transcript 有 handoff → 任务 completed，row 修成 completed |
| keep-alive turn 失败 | failed | — | 任务 failed（现有路径） |
| keep-alive turn 卡审批 | waiting | — | 现有 waiting 机制（idle reminder），任务保持 running |

### D4 leader → worker steer 分发

**新 runtime surface**（`RuntimeBackgroundTaskSupervisor` + `VoidCodeRuntime` + transport 协议）：

```
steer_background_task(task_id: str, content: str) -> BackgroundTaskState
```

- **路由按 task_id**：child session id 由 task 行派生（`task.session_id`），steer 方无需知道 session id。task_id 是现有唯一 ID 空间，天然避免"同 session 多任务"歧义。
- **校验**：任务必须 `keep_alive == 1`；状态必须 `idle`（或 keep-alive 的 `interrupted`，视为断点续跑，走 `interrupted → running`）；content 非空。`running`（turn 在飞）时拒绝并给出明确错误（v1 无流水线）。
- **派发**：`mark_background_task_steered(task_id, steer_prompt)`（新 storage op：`idle|interrupted → running`，写入 `steer_prompt` 列）→ 复用 drain 的 spawn 路径（取 slot → start-gate 线程 → `run_background_task_worker`）。**建议把 drain 里 935-945 的线程创建抽成可复用 helper**，steer 与 drain 共用，slot 计数保持"dispatch 时 reserve 一次、worker finally release 一次"的既有平衡 [推断：worker 的 `queued` 分支为防御路径，steer 派发时任务行已是 running，走 else 分支]。
- **worker 读 steer**：turn 的 `RuntimeRequest.prompt = task.steer_prompt`（原始 prompt 已在 transcript 里，不需要重放；语义同主 agent 的 follow-up）；`session_id = task.session_id`；`allocate_session_id=False`；metadata = 原始 request.metadata + `background_task_id` + `background_run` + `keep_alive_turn`。
- **为什么不复用 `queue_steering`**：D2.3 已证——turn 结束后 child 行封印，`_queue_runtime_message` 抛 `SessionSealedError`。task 行是唯一不受 session seal 管辖的持久化面。顺带：`queue_steering` 的"delivered before next provider turn"语义对挂起 worker 也不成立（没有 in-flight turn）。

**事件**：turn 完成（进入 idle）时向 parent 发新事件 `runtime.background_task_awaiting_steer`（命名待定），payload 含 `task_id`/`child_session_id`/turn 摘要（复用 `_delegated_lifecycle_payloads` 结构）；**必须加入 `DELEGATED_BACKGROUND_TASK_EVENT_TYPES`**（`events.py:502-511`）才能在 sealed parent 行上落库。hook surface 同步新增 `background_task_awaiting_steer`（对照 `background_task_started/progress/...`）。

**工具与 CLI/HTTP surface**（与 status/output/cancel/retry/list 平级）：

- leader 工具：新 `steer_task`（`tools/steer_task.py`），arg `task_id` + `prompt`；调用方须是任务的 parent（`context.session_id == task.parent_session_id`，在工具内校验，runtime surface 保持无状态）。加入 `agent/builtin.py _LEADER_TOOL_ALLOWLIST`；`agent_capability.py` snapshot 版本号 +1（`AGENT_CAPABILITY_SNAPSHOT_VERSION = 3`）。
- CLI：`voidcode tasks steer <task_id> "<prompt>" [--json]`；`cli/app.py:3221-3289` tasks 组新增子命令；`_background_task_next_steps`（约 1229-1280）加 `idle` 分支（提示 `tasks steer`/`tasks cancel`）。
- HTTP：`POST /api/tasks/<task_id>/steer`（`http.py` transport + 路由）。
- `task` 工具（`tools/task.py`）新增 `keep_alive: bool = False` 参数：`keep_alive=true` 时请求 metadata 顶层加 `"keep_alive": true`（**不放 delegation 子对象**，避免污染 `DelegatedRoutingPayload`/`subagent_routing_identity_from_metadata` 解析 [推断]）；校验 `keep_alive=true` 要求 `run_in_background=true`（sync 委托是阻塞调用，无挂起语义）。

### D5 持久化与恢复

**Schema**（`storage.py`，`PRAGMA user_version` v7 → v8，迁移模式见 803-809）：

```sql
ALTER TABLE background_tasks ADD COLUMN keep_alive INTEGER NOT NULL DEFAULT 0;
ALTER TABLE background_tasks ADD COLUMN steer_prompt TEXT;
```

- `create_background_task` 写入 `keep_alive`（从 request metadata `keep_alive` 派生）；`BackgroundTaskState`/`StoredBackgroundTaskSummary`（`task.py:307-372`）加 `keep_alive: bool`、`steer_prompt: str | None` 字段。
- 新 storage op：`mark_background_task_idle(task_id)`（`running → idle`，按转移矩阵校验，清 `steer_prompt`）、`mark_background_task_steered(task_id, steer_prompt)`（`idle|interrupted → running`）。`mark_background_task_running` 保持 queued-only 不变。

**挂起时 child session 状态**：row `interrupted`（runtime 层封印、显式 re-entry 可开）；transcript/tool_results/last_event_sequence 全部持久化（per-turn 事实）。`queue_steering`/`queue_follow_up` 对挂起 child 被拒——刻意如此（steer 走 task 层）。从外部读 `session_result(child_session_id)` 得到 interrupted（resumable），与 task `idle` 是**一致的两层观察**：会话可续、任务在等指令。

**与 terminal seal 的关系**：seal 机制零改动。每 turn 结束 child row 以 `interrupted` 封印（`save_run`，`seal_terminal_status` 守卫不变）；下一 turn 经 `save_interrupted_checkpoint(create_if_missing=True)` 解除；turn 完成通知走 `DELEGATED_BACKGROUND_TASK_EVENT_TYPES` 例外写入 sealed parent。

**重启恢复（决策：keep-alive 是进程生命周期概念）**：

- 进程重启后无 worker 线程、无 leader 上下文。reconcile（`background_tasks.py:2004-2073`）对 keep-alive 任务：`idle` 行不在 `fail_incomplete_background_tasks` 扫描范围（只扫 queued/running）→ 天然保留；`running` 行（turn 在飞时崩溃）被现有逻辑标 `interrupted`（正确）。随后**显式把存活的 keep-alive 任务（idle/running）terminalize 为 `interrupted`**，error 注明"runtime exited while keep-alive worker was awaiting steer"。
- child session 与完整 transcript 保留；续接走两条现有路径：(a) leader 用 `task` 工具 `session_id=<child>` 重新委托（同 child、新 task，`_metadata_with_delegation_governance` 见 existing session 不扣 budget）；(b) `tasks retry`（keep-alive metadata 随 `previous_task.request.metadata` 复制，新任务仍是 keep-alive、同 child session）。
- `interrupted → running`（D1）是可选增强：同 task_id 断点续跑（先 `steer` 即可触发），v1 不承诺、文档记录。
- 备选（不采纳）：idle 任务跨重启存活 + 重启后 `steer_task` 直接唤醒。代价是 parent 校验失效（旧 leader session 已封存）、孤儿 keep-alive 泄漏面变大，与"runtime-owned 收敛能力"的边界冲突。

**Shutdown**（`background_tasks.py:188-247`）：新增一步——`_terminalize_queued_tasks_for_shutdown` 之外，把 `idle` 的 keep-alive 任务 terminalize 为 `interrupted`（"runtime exited while awaiting steer"）；在飞 turn 沿用 `_fail_unfinished_shutdown_threads`（keep-alive 任务标 `interrupted` 而非 `failed`，一行分支）。`cancel`（`background_tasks.py:1056-1128`）：idle 任务无线程，`request_background_task_cancel` 后须直接走 terminal 分支（现有代码只对 `status == "running"` 做 child 处理；idle → 直接 `mark_background_task_terminal(cancelled)`，补一个分支）。

**Observability/CLI 文案**：`_waiting_reason`（约 486-490）加 `idle → "awaiting_steer"`；`tasks status` 的 `next_steps` 加 idle 分支（见 D4）。

### D6 治理边界

- **Routing**：完全复用现有 delegation routing。keep-alive 与 `subagent_type`/preset 正交——`task` 工具仍要求 `subagent_type`（`resolve_subagent_route`、`_CALLABLE_SUBAGENT_PRESETS` 不变），任意 callable preset 都可 keep-alive。不新增路由面。
- **工具作用域**：keep-alive child 每 turn 从持久化 session metadata + delegation routing 重新 materialize worker preset，`submit_result` 始终在 child allowlist 内（已知的"真实 provider 下 leader allowlist 不含 submit_result"只影响 leader 侧，与 child 无关）。每 turn 的 re-entry 携带的 delegation routing metadata 由 task 行 `request.metadata` 原样复制——无需新增 metadata 通道。
- **Spawn budget**：`_metadata_with_delegation_governance` 仅在 `existing_session_id is None` 时扣减。首 spawn 扣 1；后续 steer turn 以 `task.session_id`（已存在）进入 → 不扣。depth 每 turn 从 parent 重算（parent_depth + 1）→ 不漂移。**无需改动，文档固化该不变量即可**（防未来实现把 steer turn 误建成新 session）。
- **生命周期归属**：keep-alive 任务的生命周期 = 进程生命周期（D5），不与 leader 的 per-turn `completed` 绑定——与现有 background task 语义一致（child 独立完成后经 `DELEGATED_BACKGROUND_TASK_EVENT_TYPES` 通知已封存的 parent）。

## 4. 时序示例

```
leader                 supervisor/worker                     storage(SQLite)
  | task(keep_alive=true)                                      |
  |─────────────────────>|  create(queued, keep_alive=1)  ────>|  row: queued
  |                      |  drain → running, spawn thread      |  row: running
  |                      |  turn1: _run_with_persistence       |
  |                      |    (child row 每 turn interrupted)  |  row: interrupted(child)
  |  runtime.background_task_progress (events)                 |
  |  ←──────────────────|  turn1 无 handoff → mark idle        |  row: idle
  |                      |  emit awaiting_steer → thread exits |
  |  awaiting_steer 事件  ←───────────────────                 |
  | steer_task(task_id, "继续：检查 X")                         |
  |─────────────────────>|  idle→running, steer_prompt=X       |  row: running
  |                      |  spawn thread, turn2 (prompt=X)     |  row: interrupted(child, turn2)
  |                      |  turn2 无 handoff → idle            |  row: idle
  |  (重复多轮...)                                              |
  | steer_task(task_id, "提交最终结果")                          |
  |─────────────────────>|  turnN: child 调 submit_result       |
  |                      |  handoff 证据 → finalize completed  |  row: completed(task+child)
  |  runtime.background_task_completed ←───────────────────    |
  | background_output / tasks output <task_id>                 |
```

## 5. 落地影响面（文件级，分 Phase）

### Phase 0 — 契约与存储（无行为变化）

| 文件 | 改动 |
|---|---|
| `src/voidcode/runtime/task.py` | `BackgroundTaskStatus` + `"idle"`；`_BACKGROUND_TASK_ALLOWED_TRANSITIONS`（D1 矩阵）；`is_background_task_terminal` 不变 |
| `src/voidcode/runtime/storage.py` | v8 迁移（`keep_alive`/`steer_prompt` 列）；`create_background_task` 写 keep_alive；row↔state 映射；新 op `mark_background_task_idle` / `mark_background_task_steered` |
| `src/voidcode/runtime/contracts.py` | `BackgroundTaskState`/`StoredBackgroundTaskSummary` 加字段；`BackgroundTaskStatus` literal |

### Phase 1 — worker 循环 + submit_result 调和

| 文件 | 改动 |
|---|---|
| `src/voidcode/runtime/run_loop.py` | 1750-1753 跳过 keep_alive_turn 强制检查；final step 状态 keep-alive → `interrupted`（D3，**唯一 session 层改动**） |
| `src/voidcode/runtime/background_tasks.py` | `run_background_task_worker` finalize 分支（D2）；`steer_background_task`（D4）；spawn helper 抽取复用；drain/孤儿扫描豁免 `keep_alive + steer_prompt 在途` 的 running 行；`shutdown` idle→interrupted、`_fail_unfinished_shutdown_threads` keep-alive→interrupted；`cancel_background_task` idle 分支；`reconcile` keep-alive terminalize 逻辑；observability `_waiting_reason` |
| `src/voidcode/runtime/service.py` | 暴露 `steer_background_task`；`allow_internal_fields` 放行 `keep_alive_turn`（约 5520 附近）；`_metadata_with_delegation_governance` 不变量加注释 |
| `src/voidcode/runtime/events.py` | 新事件 `runtime.background_task_awaiting_steer`（命名待定）加入 `DELEGATED_BACKGROUND_TASK_EVENT_TYPES` |

### Phase 2 — 工具与 surface

| 文件 | 改动 |
|---|---|
| `src/voidcode/tools/task.py` | `keep_alive` 参数（校验 run_in_background=true）；metadata 顶层 `keep_alive: true` |
| `src/voidcode/tools/steer_task.py` | 新工具：`task_id` + `prompt`；parent 校验 |
| `src/voidcode/agent/builtin.py` | `_LEADER_TOOL_ALLOWLIST` + `steer_task` |
| `src/voidcode/runtime/agent_capability.py` | `AGENT_CAPABILITY_SNAPSHOT_VERSION` + 1 |
| `src/voidcode/cli/app.py` | `tasks steer <task_id> "<prompt>" [--json]`；idle next_steps |
| `src/voidcode/runtime/http.py` | `POST /api/tasks/<task_id>/steer`（transport + handler） |
| `docs/contracts/background-task-delegation.md` | 状态词汇 + steer 契约 + idle 语义修订 |

### Phase 3 — 测试

- unit：转移矩阵（含 `running→idle`、`idle→running`、idle terminalize）；`steer_background_task` 校验（非 keep-alive/running/空 content 拒绝）；storage 新 op。
- integration（用 `graph/deterministic_graph.py`）：两轮 steer 后 child session transcript 累积；中间 turn 不强制 submit_result；最终 turn submit_result → 任务 completed、child row 修成 completed；idle 期间 cancel；shutdown 后任务 interrupted、child 可续接。
- 回归：one-shot child 的 submit_result 契约测试不变（run_loop gating 的负向用例补一条 keep_alive_turn 跳过）。

## 6. 风险与未决

| 风险 | 等级 | 缓解 |
|---|---|---|
| run_loop 改动触及所有 delegated child 的终态判定 | 低 | 严格 gated 在内部 metadata `keep_alive_turn`；非 keep-alive 路径字节级不变；Phase 3 回归覆盖 |
| 中间 turn 以 `interrupted` 结束是行为面变化（外部读 child session 看到 interrupted） | 中 | 语义诚实（"会话可续、任务在等指令"），文档/CLI 文案明示；`tasks status` idle 分支给 steer 指引 |
| keep-alive worker 在 leader 放弃/会话出错后无限 idle 泄漏 | 中 | v1：cancel surface + shutdown terminalize；可选 follow-up：idle TTL 自动 interrupted（`idle_since_unix_ms` 列已列入 schema 候选） |
| turn 卡 `waiting`（审批/提问）后任务停在 running，steer 被拒 | 中 | 现有 waiting 机制已处理（idle reminder、resume/answer）；leader 必须先 resume/answer；文档写明契约 [推断：resume 完成后的 finalize 归属需实现时验证——现有 one-shot 下 resume 后由 reconcile/drain 兜底] |
| 并发 slot 在 idle 期间释放、steer 时重取——burst steer 可能排队 | 低 | drain FIFO + `_slot_available` notify 现有机制；keep-alive 数量受 spawn budget 限制，天然有界 |
| `steer_prompt` 单列 = 无流水线，重复 steer 需等 idle 事件 | 低 | v1 明确契约（leader 事件驱动）；follow-up：`pending_steers` JSON 队列列 |
| 事件名/字段名未定 | 低 | `[推断]` 命名以落地为准 |

## 7. 附录：已核实符号与行号索引

| 符号 | 位置 |
|---|---|
| `save_interrupted_checkpoint(create_if_missing=True)` un-seal 调用点 | `service.py` ~2118-2129 |
| existing_session 水合（segments/tool_results） | `service.py` ~2061-2083 |
| `seal_terminal_status = active_run_count <= 1` | `service.py` ~2809 |
| follow-up 递归 `_run_with_persistence(session_id=same)` | `service.py` ~1831-1849 |
| steering drain 注入 prompt | `service.py` ~2024-2039 |
| `_queue_runtime_message` seal 守卫（SessionSealedError） | `service.py` ~4700-4728 |
| `_sealed_session_status`（interrupted 亦封印） | `service.py` ~7200-7247 |
| `_metadata_with_delegation_governance`（existing_session 不扣 budget） | `service.py` ~5661-5721 |
| `run_background_task_worker`（one-shot 循环） | `background_tasks.py` ~2100-2347 |
| `finalize_background_task_from_session_response` | `background_tasks.py` ~1748-1830 |
| `_child_terminal_status_from_response` / `_child_transcript_completed` | `background_tasks.py` ~1815-1853 |
| `_drain_background_task_queue`（queued-only 扫描 + spawn） | `background_tasks.py` ~785-975 |
| shutdown / `_fail_unfinished_shutdown_threads` / queued terminalize | `background_tasks.py` ~188-291 |
| `reconcile_background_tasks_if_needed` | `background_tasks.py` ~2004-2073 |
| `retry_background_task`（新 task_id） | `background_tasks.py` ~523-546 |
| `cancel_background_task` | `background_tasks.py` ~1056-1128 |
| `_BACKGROUND_TASK_ALLOWED_TRANSITIONS` / `resolve_subagent_route` | `task.py` ~70-95 |
| `mark_background_task_running`（queued-only UPDATE） | `storage.py` ~3563-3608 |
| `fail_incomplete_background_tasks`（只扫 queued/running） | `storage.py` ~3915-3993 |
| background_tasks 表 schema / v6→v7 迁移模式 | `storage.py` ~679-711 / ~803-809 |
| submit_result 强制检查 / final step 状态 | `run_loop.py` ~1750-1753 / ~1785-1795 |
| `submit_result` 仅 child 可用 | `tools/submit_result.py` ~49-50 |
| leader allowlist（含 task/background_cancel，无 submit_result） | `agent/builtin.py` ~20-41 |
| `task` 工具 `_TaskArgs`（含 `session_id` 续接） | `tools/task.py` ~23-105 |
| `DELEGATED_BACKGROUND_TASK_EVENT_TYPES` | `events.py` ~502-511 |
| CLI tasks 组 / next_steps | `cli/app.py` ~3221-3289 / ~1229-1280 |
| transport task surface | `http.py` ~79-160 |
