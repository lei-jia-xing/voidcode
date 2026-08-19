# Subagent 委派灵活性设计（OMP 式演进）

## 状态

- 状态：proposed
- 范围：design-only（本文件只记录设计与决策，不包含实现代码；落地时以 `src/` 下实际代码为准）
- 目标仓库：`voidcode`
- 关联文档：`docs/contracts/background-task-delegation.md`（本设计修订它的 surface 契约）、`docs/oh-my-pi-comparison-priorities.md`（OMP 对比调研与缺口清单）、`docs/keep-alive-subagent-design.md`（idle/steer 前置设计）、`docs/agent-architecture.md`、`docs/architecture.md`

## 结论先行

**目标**：让 delegated subagent 委派获得 OMP 式的灵活性——任意结构化输出契约（invocation-level JSON Schema）、batch fan-out、可复活 agent 生命周期——**同时保持 voidcode 的 runtime-owned 治理不变量不变**（持久化状态机、通知去重、重启 reconcile、child ⊆ parent 的 delegation gate、完整 transcript 不复制）。

**核心判断**：OMP 与 voidcode 的差距不是"缺一个机制"，而是**契约位置不同**。但必须先拆开 `submit_result` 的两个职责——它同时是「完成提交点」和「固定结构契约」：

- **完成提交点（必须保留）**：等价于 OMP 的隐藏 `yield` 工具——OMP 并没有因为 `outputSchema` 就放弃 yield（child 仍必须经过 yield 结束，最多 3 次提醒，最后一次强制 `toolChoice=yield`）。voidcode 的完成判定（`child_terminal.py`）、interrupted 修复、keep-alive 判定、`summary_output` 渲染全部建立在「`submit_result` ok + 非空 `handoff.summary` + `graph.response_ready`」的 transcript 证据链上。去掉提交点等于拆掉状态机的判定锚点——**这不是兼容性问题，是架构一致性问题**。
- **固定结构契约（替换为 schema 声明）**：`submit_result` 的固定字段（`completed_work/files_touched/verification/...`）换成 parent 声明的任意 JSON Schema，确实更灵活。

因此本设计**不向后兼容**：`submit_result(summary, data?)` 中 `summary` 保留（完成证据 + parent 摘要，语义不变），固定字段删除，`data` 为任意 JSON 并由 invocation-level `outputSchema` 校验（permissive/strict）。无 `outputSchema` 时 `data` 不校验，`summary` 仍是完成判定的最小契约。

**分阶段范围**（详见各节）：

1. **Phase 1（推荐优先落地）**：`task` 工具增加 invocation-level `outputSchema` + `schemaMode`（permissive/strict）；`submit_result` 签名改为 `(summary, data?)`，固定字段删除，`data` 由 schema 校验（无 schema 不校验）。校验结果作为 runtime truth 持久化并进入 `BackgroundTaskResult`。提交点证据链（非空 `summary` + `graph.response_ready`）零改动。
2. **Phase 2**：`task` 工具 batch 形态（`context` + `tasks[]`），映射到现有 `parallel_group_id` / `parallel_group_size` + `runtime.background_task_group_completed` 语义，不新增并发模型。
3. **Phase 3**：可复活生命周期对齐——keep-alive 之外补 idle 资源回收（OMP `agentIdleTtlMs` 对应物）与 revive 语义文档化；续跑复用现有 `steer_task` 与 `task(session_id=<child>)` 两条已 shipped 路径。
4. **Phase 4（远期，不承诺）**：isolated workspace + patch/branch 合并（OMP `isolated`）。VoidCode 是 Python 运行时，无 native 隔离 PAL；此阶段只做设计评估，不进入 backlog。

**明示不引入**：任意拓扑 multi-agent、peer-to-peer agent bus、动态 agent marketplace、append-only session tree、跨进程 agent registry。

---

## 1. 背景与动机

`docs/oh-my-pi-comparison-priorities.md`（2026-08-15 调研，OMP HEAD `ad318c7`）把「task 支持 invocation-level JSON Schema output」列为真实缺口：

> P2：task 支持 invocation-level JSON Schema output —— 仍未落地（`task` 工具的 input_schema 不含 output schema），保持为真实缺口。

OMP 的委派模型（依据 `docs/tools/task.md` 与 `packages/coding-agent/src/task/`）：

- **任意结构化输出**：每个 task item 可声明 `outputSchema`（JSON Schema），优先级 per-call `outputSchema` → agent frontmatter `output` → 继承 parent session schema；`schemaMode` permissive/strict（默认 permissive）。
- **完成协议**：child 必须经过隐藏的 `yield` 工具结束（最多 3 次提醒，最后一次强制 `toolChoice=yield`）；`finalizeSubprocessOutput(...)` 把最终文本 + yield payload + schema 对账，产出 `SingleResult.structuredOutput{data, validation status/error}`。
- **batch fan-out**：`task.batch`（默认开）接受 `{context, tasks[]}`，一次调用多个 spawn，session-scoped `Semaphore` 限制并发（`task.maxConcurrency`）。
- **可复活生命周期**：进程内 registry `running | idle | parked | aborted`；success/failure 都进 `idle`，idle-TTL（默认 420s）后 `parked`（session disposed、JSONL 保留），`hub` 消息复活；isolated 完成即 teardown 不可复活；hard abort 是 `aborted` 终态。
- **隔离执行**：`isolated: true` → 隔离 workspace（apfs/btrfs/overlayfs 等 native PAL），完成捕获 patch 或提交 branch 后 merge。

voidcode 现状（已 shipped，详见 §2）是**治理更强、灵活性更弱**：`submit_result` 固定字段、固定 child presets、持久化 7 态状态机、通知去重与重启 reconcile。本设计的目标不是复制 OMP 的全部机制，而是**在 runtime-owned 治理框架内，把「结构化输出契约」和「委派形态」从固定形状升级为可声明形状**。

---

## 2. 现状核实（证据）

以下符号经实际代码核实（行号为核实位置，落地时以代码为准）。

### 2.1 `task` 工具（`src/voidcode/tools/task.py`）

- `_TaskArgs`：`prompt`（必填）、`run_in_background`、`load_skills`、`subagent_type`（必填）、`description`、`session_id`、`command`、`parallel_group_id`、`parallel_group_size`、`keep_alive`。
- `keep_alive=true` 要求 `run_in_background=true`（model_validator：`"keep_alive=true requires run_in_background=true (sync delegation has no suspend/resume semantics)"`）。
- `TaskRuntime` Protocol 只暴露：`run` / `start_background_task` / `load_background_task_result` / `cancel_background_task` / `list_background_tasks` / `session_result`。
- sync 模式 → `run(request)` 阻塞返回结果；background 模式 → `start_background_task(request)` 返回 `BackgroundTaskState`。
- `input_schema` **不含** output schema 字段（对比调研已确认，保持为缺口）。

### 2.2 `submit_result` 与 one-shot 强校验

- `src/voidcode/tools/submit_result.py`：`SubmitResultArgs{summary(必填, min_length=1), completed_work, files_touched, verification, open_questions, blockers}`；返回 `ToolResult(status="ok", data={"handoff": args}, reference="child-handoff:<session_id>")`；非 child session 调用 → `ValueError("submit_result is only available to delegated child sessions")`。
- `run_loop.py:2070`（`_finalize_step_state`）：final step 且 `parent_id is not None` 且非 `keep_alive_turn` → 最后结果必须是 `submit_result` ok，否则 `ValueError("delegated child must call submit_result before completing")` → 后台任务 `failed` / 同步委托变 tool error。
- `keep_alive_turn` 中间 turn 跳过该检查，final step 落 `interrupted`（run_loop.py:2088-2091）。

### 2.3 完成判定（`src/voidcode/runtime/child_terminal.py`，单一权威）

- `child_terminal_outcome`：row `completed` → completed；`failed` → failed；`running` → failed（permission-denied tail）；`interrupted` + `child_transcript_proves_completed`（`runtime.tool_completed` for `submit_result` ok + 非空 `handoff.summary`，**然后** `graph.response_ready`）→ completed；否则 `None`（resumable，不 seal 不 terminalize）。

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
| 结构化输出契约 | `submit_result` 固定字段，执行期强制 + transcript 证据链 | `outputSchema` 任意 JSON Schema，派发期声明 + 完成时对账 | Phase 1：固定字段删除，`data` 由 `outputSchema` 校验；提交点（`summary` 证据）保留 |
| 完成判定 | row + transcript 证据（`child_terminal_outcome`） | `finalizeSubprocessOutput` 对账 raw text + yield + schema | voidcode 更严格（可修复 interrupted、可审计）；schema 校验加在 finalize 路径不改变判定 |
| 委派形态 | 单任务（`parallel_group_id/size` 已有组语义） | `task.batch` `{context, tasks[]}` | Phase 2：batch 映射到 parallel_group，不新增并发模型 |
| 可复活 | keep-alive `idle` + `steer`；`task(session_id=<child>)` 续跑 | registry `idle/parked` + `hub` 复活（idle-TTL 420s） | 续跑能力已等价；Phase 3 补 idle 资源回收与语义文档化 |
| 隔离执行 | 无（对比调研标记为真实缺口） | `isolated` + patch/branch merge（native PAL） | Phase 4 远期，仅设计评估 |
| 并发 | `default_concurrency=5` + provider/model 覆盖，持久化任务行 | session-scoped `Semaphore`，实时 resize | voidcode 更强（跨进程语义）；不迁移 |
| 失败语义 | 缺 handoff → 确定性 `failed`；schema 无效（现无 schema） | 缺 yield → 警告不判死；schema 无效 permissive 接受 / strict 失败 | Phase 1 采用 OMP 的 permissive/strict 词汇，但**只在有 schema 时生效** |

**设计原则**：灵活性必须落在「契约声明」与「结果校验」上，不能落在「绕过治理」上。所有新 surface 继续走现有 runtime 路径（`task` 路由 → `start_background_task` → worker → finalize → 通知 → 读取）。

---

## 4. Phase 1：`submit_result` payload schema 化（无兼容期）

### 4.1 形状

`task` 工具 `input_schema` 新增（声明消费契约）：

```jsonc
{
  "outputSchema": {            // 任意 JSON Schema（object），校验 child 提交的 data
    "type": "object",
    "properties": { ... },
    "required": [...]
  },
  "schemaMode": "permissive"   // "permissive" | "strict"，默认 permissive
}
```

`submit_result` 工具签名变更（**breaking change**，删除固定字段）：

```jsonc
{
  "summary": "human-readable 摘要，同时是完成判定证据（非空必填）",
  "data": { ... }              // 任意 JSON object；形状由 parent 的 outputSchema 声明
}
```

- `submit_result` 固定字段（`completed_work/files_touched/verification/open_questions/blockers`）**删除**；需要这些字段的调用方在 `outputSchema` 里声明。
- `summary` 语义不变：`child_transcript_proves_completed` 仍检查非空 `handoff.summary` + `graph.response_ready`（child_terminal.py），`_child_handoff` 仍渲染 parent 摘要，`_submit_result_terminal` 仍发射 `graph.response_ready`。**提交点证据链零改动**。
- `data` 缺失时视为空对象；无 `outputSchema` 时 `data` 不校验。
- 只允许 `run_in_background=true`（sync 模式结果直接返回，无校验时机问题，但为行为一致也允许——由实现决定 [推断]；建议 v1 限制 background，与 keep_alive 同风格）。
- `outputSchema` 随 request metadata 持久化到 task 行（新列 `output_schema_json` + `schema_mode`，storage 迁移 `_SCHEMA_VERSION 11 → 12`，迁移风格与 v10→v11 一致）。

### 4.2 校验时机（关键决策）

**在 worker finalize 时校验并持久化，不在 parent 读取时惰性校验**——符合「runtime 先持久化 lifecycle truth 再让客户端消费」的既有原则（对比 `result_available` 不得早于其所依赖的真相）。

具体位置：`finalize_background_task_from_session_response`（background_tasks.py:2046）在 `child_terminal_outcome` 判定 terminal 之后、`mark_background_task_terminal` 之前，若 task 行带 `output_schema`：

1. 取 child transcript 中最后一次成功 `submit_result` 的 `handoff`（复用 `_child_handoff`，background_tasks.py:1486），提取 `data`。
2. 对 `handoff.data` 做 JSON Schema 校验（仓库已有 pydantic / jsonschema 依赖面，用 jsonschema 或手写校验器 [推断]）。
3. 结果写入 task 行：`structured_output_json`（通过校验的 data）+ `schema_validation`（`{schema_source, schema_mode, valid, error}`）。
4. 校验失败：permissive → 照常 `completed`，`structured_output` 附 validation error；strict → 视为 child 未满足契约，task 走 `failed`（error 携带校验失败详情），child row 仍按 transcript 证据 seal（两层的既有分离不变）。

**不影响 `child_terminal_outcome`**：完成判定仍只信 transcript 证据（handoff + response_ready）；schema 校验是契约层面的附加判定，不改变「row 是否完成」的推导。`strict` 失败是 task 层终态选择，不是 child session 行状态回退。

### 4.3 结果面

`BackgroundTaskResult` 新增字段（可选）：

```python
structured_output: dict[str, object] | None  # 通过校验的 data
schema_validation: SchemaValidation | None  # {schema_source, schema_mode, valid, error}
```

`background_output` 返回 payload 增加这两项；`summary_output` 渲染逻辑不变（仍由 `_child_handoff` 提取 `summary`）。CLI `tasks output --json` / HTTP 同步暴露（与既有 delegated correlation 字段平级）。

### 4.4 keep-alive 交互

- 中间 turn（`keep_alive_turn`，无 handoff）：**不校验**——契约只作用于最终 handoff。
- 最终 turn（submit_result + response_ready）：正常校验。strict 失败时任务 `failed`，keep-alive 语义不受影响（child row 已 completed，续跑走 `task(session_id=<child>)` 需要重新派发，与现状 failed 一致）。

### 4.5 无兼容期影响面（显式 breaking change）

- `submit_result` 固定字段删除：旧调用方（模型 prompt 依赖 `completed_work` 等字段的）需在 `outputSchema` 中声明等价结构；builtin child preset prompt 与 `_child_handoff` 渲染标签同步更新。
- 不传 `outputSchema` 的现有委托：`data` 不校验、`structured_output` 为 null，其余路径（状态机、通知、去重、CLI/HTTP 形状）不变——**证据链与生命周期语义无兼容问题，只有工具签名 breaking**。
- `child_terminal.py`、run_loop.py:2070 强校验、keep-alive 判定、`_submit_result_terminal` **零改动**。
- 契约测试更新：固定字段用例改写为 schema 用例；`test_background_task_tools.py` 中依赖固定字段 payload 的断言同步迁移。

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
7. `submit_result` 签名变更是显式 breaking（固定字段删除）；`summary` 证据语义与完成判定链（`child_transcript_proves_completed` / `_submit_result_terminal` / keep-alive 判定）保持零改动。
8. 失败/中断只给显式 user-request retry/continue guidance；无无限自动重试。

---

## 9. 验收检查点

Phase 1 落地后以下条件全部成立：

1. `task(..., run_in_background=true, outputSchema={...})` 创建带 schema 的 task 行；child `submit_result(summary, data={...})` 后 `BackgroundTaskResult.structured_output` 为通过校验的 data。
2. permissive 模式下 schema 无效 → task 仍 `completed`，`schema_validation.valid=false` 且带 error。
3. strict 模式下 schema 无效 → task `failed`，error 含校验失败详情；child row 仍按 transcript 证据 seal。
4. `submit_result` 固定字段删除后，builtin child preset prompt / `_child_handoff` 渲染标签 / 契约测试同步迁移，无遗留固定字段引用。
5. keep-alive 中间 turn 不触发校验；最终 turn 正常校验；`summary` 证据链（非空 summary + `graph.response_ready`）行为不变。
6. `background_output` / CLI `tasks output --json` / HTTP 暴露 `structured_output` + `schema_validation` 字段。

Phase 2 落地后：

7. batch 调用创建 N 个独立 task 行 + 共享 `parallel_group_id`；组完成事件恰好一次。
8. 单个 item 校验失败不影响其他 item 的独立完成（组事件聚合，非整体回滚）。

Phase 3 落地后：

9. `idle_release_ttl_ms` 启用时 idle 任务到期发 `background_task_released`，task 行保持 `idle`，child 行保持 `interrupted`，steer 仍可续跑。

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
