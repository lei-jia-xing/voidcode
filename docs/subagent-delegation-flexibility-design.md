# Subagent 委派灵活性设计（OMP 式演进）

## 状态

- 状态：proposed
- 范围：design-only（本文件只记录设计与决策，不包含实现代码；落地时以 `src/` 下实际代码为准）
- 目标仓库：`voidcode`
- 关联文档：`docs/contracts/background-task-delegation.md`（本设计修订它的 surface 契约）、`docs/oh-my-pi-comparison-priorities.md`（OMP 对比调研与缺口清单）、`docs/keep-alive-subagent-design.md`（idle/steer 前置设计）、`docs/agent-architecture.md`、`docs/architecture.md`

> 文中 `submit_result` 是该 design-only 文档记录的历史完成工具名称；当前 child completion tool 统一称为 `yield`，当前运行时不再识别或接受 `submit_result`。本 design-only 文档不宣称 yield、peer bus 或其他增量协作能力已经实现。

## 结论先行

**目标**：让 delegated subagent 委派获得 OMP 式的灵活性——任意结构化输出契约（invocation-level JSON Schema）、batch fan-out、可复活 agent 生命周期——**同时保持 voidcode 的 runtime-owned 治理不变量不变**（持久化状态机、通知去重、重启 reconcile、child ⊆ parent 的 delegation gate、完整 transcript 不复制）。

- **完成提交点（必须保留）**：等价于 OMP 的隐藏 `yield` 工具——OMP 并没有因为 `outputSchema` 就放弃 yield（child 仍必须经过 yield 结束，最多 3 次提醒，最后一次强制 `toolChoice=yield`）。当前 VoidCode runtime 以 `yield` 作为唯一 child completion tool；`submit_result` 不再被识别或接受。当前 completion evidence、interrupted 修复、keep-alive 判定、`summary_output` 渲染均必须迁移到 `yield` 证据链；移除完成提交点会拆掉这些语义。
- **固定结构契约（替换为 schema 声明）**：`yield` 的结构化 handoff 由 parent 声明的任意 JSON Schema 约束。

因此本设计**不向后兼容**：`yield(summary, data?)` 中 `summary` 保留（完成证据 + parent 摘要，语义不变），固定字段删除，`data` 为任意 JSON 并由 invocation-level `outputSchema` 校验（无 schema 不校验）。

**分阶段范围**（详见各节）：

1. **Phase 1（已实现）**：`task` 委托支持 terminal `yield`（`summary`/`data`，可省略 `type` 或使用 `type: "result"`）、terminal `error`，以及不终止 child 的 bounded progress（其他非空 `type` string/list + `result` 或非空 `data`）。progress 通过 `runtime.background_task_progress` 与 parent outbox 投影，结果与校验作为 runtime truth 持久化。
2. **Phase 2**：`task` 工具 batch 形态（`context` + `tasks[]`），映射到现有 `parallel_group_id` / `parallel_group_size` + `runtime.background_task_group_completed` 语义，不新增并发模型。
3. **Phase 3**：可复活生命周期对齐——keep-alive 之外补 idle 资源回收（OMP `agentIdleTtlMs` 对应物）与 revive 语义文档化；续跑复用现有 `steer_task` 与 `task(session_id=<child>)` 两条已 shipped 路径。
4. **Phase 4（远期，不承诺）**：isolated workspace + patch/branch 合并（OMP `isolated`）。VoidCode 是 Python 运行时，无 native 隔离 PAL；此阶段只做设计评估，不进入 backlog。

**明示不引入**：任意拓扑 multi-agent、peer-to-peer agent bus、动态 agent marketplace、append-only session tree、跨进程 agent registry。

---

## 1. 背景与动机

`docs/oh-my-pi-comparison-priorities.md`（2026-08-15 调研，OMP HEAD `ad318c7`）曾把「task 支持 invocation-level JSON Schema output」列为真实缺口；该历史判断已由当前 runtime 实现覆盖。

> 历史调研记录：当时 `task` 工具的 input_schema 不含 output schema。当前实现已提供 invocation-level schema，并支持 bounded `yield` progress。

OMP 的委派模型（依据 `docs/tools/task.md` 与 `packages/coding-agent/src/task/`）：

- **任意结构化输出**：每个 task item 可声明 `outputSchema`（JSON Schema），优先级 per-call `outputSchema` → agent frontmatter `output` → 继承 parent session schema；`schemaMode` permissive/strict（默认 permissive）。
- **完成协议**：child 必须经过隐藏的 `yield` 工具结束（最多 3 次提醒，最后一次强制 `toolChoice=yield`）；`finalizeSubprocessOutput(...)` 把最终文本 + yield payload + schema 对账，产出 `SingleResult.structuredOutput{data, validation status/error}`。
- **batch fan-out**：`task.batch`（默认开）接受 `{context, tasks[]}`，一次调用多个 spawn，session-scoped `Semaphore` 限制并发（`task.maxConcurrency`）。
- **可复活生命周期**：进程内 registry `running | idle | parked | aborted`；success/failure 都进 `idle`，idle-TTL（默认 420s）后 `parked`（session disposed、JSONL 保留），`hub` 消息复活；isolated 完成即 teardown 不可复活；hard abort 是 `aborted` 终态。
- **隔离执行**：`isolated: true` → 隔离 workspace（apfs/btrfs/overlayfs 等 native PAL），完成捕获 patch 或提交 branch 后 merge。

voidcode 现状（已 shipped，详见 §2）是**治理更强、灵活性更弱**：固定 child presets、持久化 7 态状态机、通知去重与重启 reconcile，以及 runtime-owned bounded `yield` progress/terminal handoff。本设计的目标不是复制 OMP 的全部机制，而是在 runtime-owned 治理框架内保持可声明的结构化结果与受限进度观察。

---

## 2. 现状核实（证据）

以下符号经实际代码核实（行号为核实位置，落地时以代码为准）。

### 2.1 `task` 工具（`src/voidcode/tools/task.py`）

- `_TaskArgs`：`prompt`（必填）、`run_in_background`、`load_skills`、`subagent_type`（必填）、`description`、`session_id`、`command`、`parallel_group_id`、`parallel_group_size`、`keep_alive`。
- `keep_alive=true` 要求 `run_in_background=true`（model_validator：`"keep_alive=true requires run_in_background=true (sync delegation has no suspend/resume semantics)"`）。
- `TaskRuntime` Protocol 只暴露：`run` / `start_background_task` / `load_background_task_result` / `cancel_background_task` / `list_background_tasks` / `session_result`。
- sync 模式 → `run(request)` 阻塞返回结果；background 模式 → `start_background_task(request)` 返回 `BackgroundTaskState`。
- `input_schema` 现已包含 `outputSchema` / `schemaMode`，用于声明 terminal `yield` data 的校验契约；progress section 不要求满足完整终态 schema。

### 2.2 `yield` 与 one-shot 强校验

- 历史代码树中的 `src/voidcode/tools/submit_result.py` 与 `SubmitResultArgs{summary, completed_work, files_touched, verification, open_questions, blockers}` 仅作为旧实现证据保留；`submit_result` 不是当前 runtime 协议，也不提供兼容路径。当前 child completion tool 是 `src/voidcode/tools/yield_tool.py`：terminal success 使用非空 `summary` 与可选 `data`（`type` 省略或 `"result"`），terminal failure 使用非空 `error`（可用 `type: "error"`），其他非空 `type` string/list 搭配 `result` 或非空 `data` 是不终止 child 的 bounded progress。
- 历史 `run_loop.py:2070` 的 `submit_result` 强制检查仅描述旧代码树行为；当前校验对应 terminal `yield` handoff。progress yield 不满足 terminal handoff 证据，不会完成 task；terminal yield 才会沿既有 `graph.response_ready` 证据链完成后台任务，错误/取消/中断仍按当前 runtime 状态机处理。
- `keep_alive_turn` 中间 turn 可发送 progress，跳过终态 schema 校验；最终 terminal `yield` 正常完成终态校验。

### 2.3 完成判定（`src/voidcode/runtime/child_terminal.py`，单一权威）

- `child_terminal_outcome`：row `completed` → completed；`failed` → failed；`running` → failed（permission-denied tail）；`interrupted` + `child_transcript_proves_completed`（`runtime.tool_completed` for terminal `yield` ok + 非空 `handoff.summary`，**然后** `graph.response_ready`）→ completed；progress yield 不满足终态证据且不会 terminalize；否则 `None`（resumable，不 seal 不 terminalize）。

### 2.4 结果读取面（`src/voidcode/tools/background_output.py`）

- `BackgroundTaskResult{task_id, parent_session_id, child_session_id, status, approval_blocked, summary_output, error, result_available}`（`runtime/contracts.py`）。
- `full_session=true` 返回有界 transcript（`message_limit` 1–100）；`block=true` 显式阻塞等待（50ms 轮询至 deadline，超时返回 `block_timed_out` 不误标失败）；`emit_result_read_hook` 把「结果被读取」作为 runtime truth 事件（stop idle reminder 等）。
- 完整 transcript 永不自动复制进 parent；通过 `resume(child_session_id)` 恢复。

### 2.5 生命周期与通知（`src/voidcode/runtime/background_tasks.py`、`task.py`）

- 7 态：`queued/running/idle/completed/failed/cancelled/interrupted`；terminal = {completed, failed, cancelled, interrupted}，**completed/failed/cancelled 完全不可变**；`interrupted` 是唯一可升级 terminal。
- 通知去重键：terminal `类型:task_id`；waiting_approval `task_id:approval_request_id`；awaiting_steer `task_id:turn_sequence`（per-turn）；`session_event_deliveries` 表持久化 delivery state，重启 reconcile 补投不重投。
- 并发：`RuntimeBackgroundTaskConfig.default_concurrency=5`，provider/model 级覆盖；slot 在 worker `finally` 恰好释放一次。
- keep-alive：`running → idle`（turn 结束无 handoff）→ `steer_background_task`（`idle|interrupted → running`）；idle 非 terminal、不参与孤儿扫描。
- 续跑另一条已 shipped 路径：`task` 工具 `session_id=<child>` 重入同一 child session（follow-up 语义）。

### 2.6 治理边界

- child preset 校验：`_agent_registry.executable_subagent_ids()`（manifest 驱动），fallback `CALLABLE_SUBAGENT_PRESETS = ("advisor","explore","researcher","worker","product")`（`runtime/task.py`）。
- `RuntimePolicySnapshot.delegation_policy`（`runtime/policy.py`）：任何 child-session 分配 / task 行创建 / queueing / hook 通知前必须通过；child snapshot 只能是 parent 的子集。
- `worker` 默认不获得 `task` 工具（防无控制 nested delegation）。

---

## 3. OMP 对照结论

| 维度 | voidcode（shipped） | OMP（调研） | 差距与设计取向 |
|---|---|---|---|
| 结构化输出契约 | `yield` terminal handoff + bounded progress；执行期强制 + transcript 证据链 | `outputSchema` 任意 JSON Schema，派发期声明 + 完成时对账 | Phase 1 已支持 terminal `summary`/`data`/`error` 与非终态 progress；terminal data 由 `outputSchema` 校验 |
| 完成判定 | row + transcript 证据（`child_terminal_outcome`） | `finalizeSubprocessOutput` 对账 raw text + yield + schema | voidcode 更严格（可修复 interrupted、可审计）；schema 校验加在 finalize 路径不改变判定 |
| 委派形态 | 单任务（`parallel_group_id/size` 已有组语义） | `task.batch` `{context, tasks[]}` | Phase 2：batch 映射到 parallel_group，不新增并发模型 |
| 可复活 | keep-alive `idle` + `steer`；`task(session_id=<child>)` 续跑 | registry `idle/parked` + `hub` 复活（idle-TTL 420s） | 续跑能力已等价；Phase 3 补 idle 资源回收与语义文档化 |
| 隔离执行 | 无（对比调研标记为真实缺口） | `isolated` + patch/branch merge（native PAL） | Phase 4 远期，仅设计评估 |
| 并发 | `default_concurrency=5` + provider/model 覆盖，持久化任务行 | session-scoped `Semaphore`，实时 resize | voidcode 更强（跨进程语义）；不迁移 |
| 失败语义 | 缺 terminal handoff → 确定性 `failed`；terminal schema 无效按 permissive/strict 处理；progress 是非终态且有界 | 缺 yield → 警告不判死；schema 无效 permissive 接受 / strict 失败 | Phase 1 已采用 OMP 的 permissive/strict 词汇，并支持 bounded yield progress |

**设计原则**：灵活性必须落在「契约声明」与「结果校验」上，不能落在「绕过治理」上。所有新 surface 继续走现有 runtime 路径（`task` 路由 → `start_background_task` → worker → finalize → 通知 → 读取）。

---

## 4. Phase 1：`yield` terminal handoff 与 bounded progress（已实现）

### 4.1 形状

`yield` 同时承载终态 handoff 与非终态 progress：
```jsonc
// terminal success
{
  "summary": "human-readable 摘要，同时是完成判定证据（非空必填）",
  "data": { ... },
  "type": "result" // 可省略
}

// terminal error
{
  "type": "error", // 可省略；error 本身也标识终态失败
  "error": "human-readable failure"
}

// nonterminal progress
{
  "type": "progress", // 也可为任意其他非空 string 或 string[]
  "result": "短进度文本",
  "data": { ... }
}
```

- 仅 delegated child 可调用 `yield`；未知字段拒绝。
- 终态成功要求非空 `summary`；`type` 省略或为 `"result"`，`data` 可选。
- 终态错误要求非空 `error`，可用 `type: "error"`；不得与 `summary` 或 `result` 混用。
- 非终态 progress 的 `type` 必须是除 `result`/`error` 外的非空字符串或非空字符串数组；必须提供 `result` 或非空 `data`，不得提供 `summary`。它不完成 child。
- runtime 为 progress 分配 `ordinal` 并持久化 bounded section：每段最多 4096 字符，每个 child 最多 100 段、累计最多 65536 字符；`type` 最多 100 项且每项最多 64 字符。
- 这些约束是显式 breaking contract 的当前形状；不恢复旧 `submit_result` 字段或兼容路径。

`task` 工具仍可声明 `outputSchema` / `schemaMode`，用于终态 `data` 的结果校验；progress 是独立的有界观察面，不要求每个 section 满足完整终态 schema。

`outputSchema` 随 request metadata 持久化到 task 行（新列 `output_schema_json` + `schema_mode`，storage 迁移 `_SCHEMA_VERSION 11 → 12`，迁移风格与 v10→v11 一致）。

### 4.2 校验时机（关键决策）

**在 worker finalize 时校验并持久化终态 data，不在 parent 读取时惰性校验**——符合「runtime 先持久化 lifecycle truth 再让客户端消费」的既有原则。progress section 在 child `runtime.tool_completed` 时按 bounded 规则校验/编号，并由 runtime 投影给 parent。

具体位置：`finalize_background_task_from_session_response` 在 terminal `yield` 的 `child_terminal_outcome` 判定之后、`mark_background_task_terminal` 之前，若 task 行带 `output_schema`：

1. 取 child transcript 中最后一次成功 terminal `yield` 的 `handoff`，提取 `data`。
2. 对 terminal `handoff.data` 做 JSON Schema 校验。
3. 结果写入 task 行：`structured_output_json` + `schema_validation`（`{schema_source, schema_mode, valid, error}`）。
4. 校验失败：permissive → 照常 `completed`，`structured_output` 附 validation error；strict → task 走 `failed`，child row 仍按 transcript 证据 seal。

progress 不改变 `queued/running/idle/completed/failed/cancelled/interrupted` 状态；它通过 `runtime.background_task_progress` 以 child event sequence 去重，并进入 parent outbox 的 bounded interaction projection。

### 4.3 结果面

`BackgroundTaskResult` 同时可携带 `structured_output`、`schema_validation` 与 bounded `progress` sections。`background_output` 返回这些有界字段；聚合 selector 不返回 child transcript，完整 transcript 仍走显式 session recovery。

CLI/HTTP 的 JSON 与 SSE 可消费 progress event / projection；客户端不得把 progress 当作 terminal result，也不得把它解释为 peer bus。

### 4.4 keep-alive 交互

- 中间 turn（`keep_alive_turn`，无 terminal handoff）：progress 可以发送；不做终态 schema 校验，任务保持原有 keep-alive 语义。
- 最终 turn（terminal `yield` + `response_ready`）：正常校验；strict 失败时任务 `failed`，child row 仍按 transcript 证据封存。

### 4.5 Breaking change 影响面

- 旧 `submit_result` 固定字段不恢复；当前 child completion protocol 是 `yield`，其 terminal `summary` / `data` 形状保持显式 breaking 语义。
- 不传 `outputSchema` 的委托：终态 `data` 不校验；progress 仍受 bounded section 上限约束；状态机、通知、去重、CLI/HTTP 读取路径保持 runtime-owned。
- progress notification 不是 transcript 复制、不是任意 streaming，也不是 peer-to-peer agent bus。


---

## 5. Phase 2：batch fan-out

### 5.1 形状

`task` 工具新增 batch 形态（与单任务形态互斥校验）：

```jsonc
{
  "context": "shared background for all spawns",
  "tasks": [
    {"name": "A", "subagent_type": "explore", "prompt": "...", "outputSchema": {...}},
    {"name": "B", "subagent_type": "researcher", "prompt": "...", "outputSchema": {...}}
  ]
}
```

### 5.2 实现取向（复用而非新增）

- 每个 item → 独立 `start_background_task`（独立 task 行、独立 child session lineage），共享同一 `parallel_group_id`（runtime 生成）；`parallel_group_size` = items 数。
- 组完成通知复用已 shipped 的 `runtime.background_task_group_completed`（dedupe key `{parent_session_id}:{group_id}`，`_emit_parallel_group_terminal_event`）。
- 并发仍由 `RuntimeBackgroundTaskConfig` 控制——**不引入 OMP 的 session-scoped Semaphore**（voidcode 的 slot 计数已跨线程正确，且持久化语义更强）。
- 返回：各 task_id 列表 + `background_task_registered`；parent 后续用 `background_output(task_id)` / 组完成事件消费。

### 5.3 边界

- `context` 只进入 child system prompt 的共享段（对齐 OMP `CONTEXT` section），不进入 child transcript 的事件 payload（保持 bounded observability）。
- batch 内的 item 共享 parent delegation gate 校验（每个 item 单独过 `RuntimePolicySnapshot.delegation_policy`）。

---

## 6. Phase 3：可复活生命周期对齐

### 6.1 现状等价性（已 shipped，先文档化）

- keep-alive：`running → idle`（turn 结束无 handoff）→ `steer_task(task_id, prompt)` 续跑同一 child session——等价于 OMP `idle` + `hub` 复活。
- 任意 child：`task(session_id=<child>, prompt=...)` 重入——等价于 OMP revive + follow-up。
- 与 OMP 的差异：voidcode 无进程内 registry；复活完全基于 SQLite 持久真相 + 新 worker 线程，跨重启可用（OMP registry 是 process-global，跨进程重启丢失）。

### 6.2 新增（仅一项）：idle 资源回收

- OMP `agentIdleTtlMs`（默认 420s）对应物：`RuntimeBackgroundTaskConfig.idle_release_ttl_ms`（默认 0 = 不启用，保持现状）。
- 启用时：`idle` 任务超过 TTL 无 steer → runtime 执行 release（**不改 task 行状态、不 terminalize**；只回收进程内资源——worker 已退出，实际是 registry/observability 清理 + 可选的 child session 内存释放 [推断：当前 idle 任务在 worker 退出后已无内存驻留，此 TTL 主要价值是「明确释放信号」与 hook 通知 `background_task_released`]）。
- child session 行保持 `interrupted`（resumable），续跑路径不变（steer 或 `task(session_id=...)` 都会解除封印）。
- 不做 OMP 的 `parked`（session disposed）语义：voidcode 的 session 是 SQLite 持久真相，dispose 无意义；保留 JSONL/row 即保留可复活性。

### 6.3 明示不做

- 不把 `completed/failed` 改为可复活（terminal 不可变是 voidcode 一致性基石，与 OMP `success → idle` 的根本差异保留并文档化——需要「复用上下文」时用 keep-alive 或 `task(session_id=...)`，而不是放开 terminal）。

---

## 7. Phase 4（远期，设计评估）：isolated workspace

- 目标：child 在隔离 workspace 执行，完成捕获 patch（或提交分支）后合并回 parent workspace，避免 child 写坏共享树。
- 约束：VoidCode 是 Python 运行时，无 OMP `pi-natives` 隔离 PAL（apfs/btrfs/overlayfs/projfs）。可行候选 [推断]：git worktree（最简、纯 git 依赖）、`git stash` + 临时目录、`copy-on-write` 不可用时的 rcopy 兜底。
- 合并语义：patch 模式（`git diff` 捕获 + `canApplyText` 校验，失败留 `.patch` artifact 供手动处理）或 branch 模式（`omp/task/<id>` 等价物 + cherry-pick，stash 冲突单独 surface）。
- **本阶段不进入 backlog**；只有当 Phase 1/2 落地后真实任务数据表明「child 写坏共享树」是高频失败时才评估。

---

## 8. 不变量清单（任何阶段不得破坏）

1. `completed/failed/cancelled` 完全不可变；`interrupted` 是唯一可升级 terminal。
2. 通知 dedupe 键与 `session_event_deliveries` 持久化 delivery state 不变；重启 reconcile 不重投。
3. parent seal 例外白名单（`DELEGATED_BACKGROUND_TASK_EVENT_TYPES`）不变；child/task 真相独立于 parent 封印。
4. 完整 transcript 永不自动复制进 parent；`resume(child_session_id)` 是唯一恢复路径。
5. child 能力 ⊆ parent（`RuntimePolicySnapshot.delegation_policy`）；`worker` 默认无 `task` 工具。
6. 顶层 active preset 仅 `leader`；child presets 仍由 `executable_subagent_ids()` 校验。
7. `yield` terminal/progress 契约是当前 completion protocol；旧 `submit_result` 签名变更是显式 breaking（固定字段不恢复），terminal `summary` 证据语义与完成判定链保持不变。
8. 失败/中断只给显式 user-request retry/continue guidance；无无限自动重试。

---

## 9. 验收检查点

Phase 1 落地后以下条件全部成立：

1. `task(..., run_in_background=true, outputSchema={...})` 创建带 schema 的 task 行；child terminal `yield(summary, data={...})` 后 `BackgroundTaskResult.structured_output` 为通过校验的 data。
2. permissive 模式下 terminal schema 无效 → task 仍 `completed`，`schema_validation.valid=false` 且带 error。
3. strict 模式下 terminal schema 无效 → task `failed`，error 含校验失败详情；child row 仍按 transcript 证据 seal。
4. 旧 `submit_result` 固定字段仅作为历史 source-tree evidence；当前 builtin child preset prompt / `_child_handoff` / 契约均使用 `yield`，无遗留兼容路径。
5. progress yield 使用其他非空 `type` string/list，并要求 `result` 或非空 `data`；每个 child 最多 100 段、累计最多 65536 字符，按 child event sequence 去重并投影到 `runtime.background_task_progress` / parent outbox，不改变 task 状态。
6. keep-alive 中间 turn 可发送 progress 且不触发终态 schema 校验；最终 turn 正常校验；terminal `summary` 证据链（非空 summary + `graph.response_ready`）行为不变。
7. `background_output` / CLI `tasks output --json` / HTTP 暴露 `structured_output` + `schema_validation` 与 bounded `progress` projection。

Phase 2 落地后：

8. batch 调用创建 N 个独立 task 行 + 共享 `parallel_group_id`；组完成事件恰好一次。
9. 单个 item 校验失败不影响其他 item 的独立完成（组事件聚合，非整体回滚）。

Phase 3 落地后：

10. `idle_release_ttl_ms` 启用时 idle 任务到期发 `background_task_released`，task 行保持 `idle`，child 行保持 `interrupted`，steer 仍可续跑。

## 10. 验证命令（维护该契约时至少运行）

```bash
uv run pytest tests/unit/runtime/test_runtime_events.py tests/unit/interface/test_cli_delegated_parity.py
uv run pytest tests/unit/tools/test_background_task_tools.py -k "background or cancel or output or steer"
mise run check
```

契约测试继续使用 fake provider / fake MCP；不引入 live provider 或真实 MCP server 作为 CI 前提。

## 11. 超出本设计的后续工作

- 任意拓扑 multi-agent orchestration、peer-to-peer agent bus、动态 agent 发现 / marketplace（保持非目标）。
- 更丰富的 child lineage / topology 设计。
- schema 驱动的 agent frontmatter `output` 默认契约（OMP 有 per-agent output 声明；本设计只做 invocation-level，agent 级默认 schema 可后续叠加）。
- isolated workspace 的真实实现（Phase 4 评估后另行设计）。
- JSON Schema 校验器的选型与错误消息归一化（与 `_pydantic_args.format_validation_error` 风格对齐）。
