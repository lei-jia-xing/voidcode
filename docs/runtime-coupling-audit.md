# Runtime 架构耦合审计报告

> ⚠️ 本文档描述 2026-08-17 之前的代码树；此后 storage.py 已拆 10 mixin、execute_graph_loop 已切片、19 个 proxy 已删（child_terminal.py 单权威）、child preset 枚举已收敛；方法数/行号已过时。

> 审计日期：2026-08-17 · 范围：`src/voidcode/runtime/`（只读调查，未改代码）
> 基准：`docs/oh-my-pi-comparison-priorities.md` + `docs/architecture.md` 提炼的 OMP 设计原则（分层清晰 / 协议不变量 / 单一职责边界）
> 方法：AST 方法清点、跨模块属性访问统计、magic-string metadata key 统计、状态/语义重复实现比对。全部结论附 `文件:行号` 代码证据。

---

## 1. 结论先行：耦合严重度排序

| 排序 | 耦合点 | 证据量级 | 严重度 | 一句话 |
|---|---|---|---|---|
| 1 | `VoidCodeRuntime` God class + 三个 collaborator 对它的 448 处 `runtime._xxx` 私有访问 | service.py 324 方法（86 public / 238 private）；background_tasks.py 195/198 次访问是私有、run_loop.py 118/118、resume.py 135/136 | **高** | 拆分出了 collaborator，但契约仍是"属性名约定"，不是类型化接口；`runtime-architecture-refactor-plan.md` 规则 7 被 19 个残留 proxy 方法违反 |
| 2 | `session.metadata` / `request.metadata` 的 magic-string 隐式契约 | 76 个 string key 在 ≥2 个文件间共享语义，无单一 TypedDict 权威定义 | **高** | `dict[str, object]` 携带 `runtime_state`/`delegation`/`runtime_config` 等跨 5 文件语义，改一个 key 四处编译不过（无静态检查） |
| 3 | `RuntimeRunLoopCoordinator.execute_graph_loop` 单方法 1397 行 | run_loop.py:1100-2497；同类 `execute_approved_tool_call` 437 行、`_execute_invoked_tool` 413 行 | **高** | 事件循环、hook、fallback graph 切换、final-step 判定、checkpoint、abort 全部在一个方法体内顺序展开 |
| 4 | child preset 枚举平行推导 | 4+ 处硬编码同一 5 元组，其中 `service.py:393` 是死代码，靠测试看守 | **中** | 新增/改名一个 child preset 必须同步 4-6 处，`policy.py` 的过滤副本会静默吞掉 manifest 新增 preset |
| 5 | background-task 终态集合重复硬编码 | `storage.py:3661`、`background_tasks.py:1637`、`service.py:4208` 各一份 | 中 | 状态转移矩阵已收敛到 `task.py`，但 3 处终态集合仍绕过权威在本地再写一遍 |

**已收敛、无需处理的（对照确认）**：`read_only`/mode 推导已全部收敛到 `mode.py:resolve_mode`（见 §3.1）；background task 状态转移校验已收敛到 `task.py` 矩阵并由 `storage.py` 强制（见 §3.2）；无运行时循环 import（§4 只存在 hub-and-spoke 回引）。

---

## 2. 维度一：God class（单体类承载多职责）

### 2.1 `VoidCodeRuntime` — service.py:617，324 方法（86 public / 238 private），7363 行

按方法语义归类，至少 **20 个可分离职责簇**：

| 职责簇 | 代表符号（service.py 行号） | 状态 |
|---|---|---|
| 执行入口/replay | `run` 1858、`run_stream` 1985、`_run_with_persistence` 1766、`_stream_chunks` 2000（**542 行**）、`_persist_response` 2758 | 堆在 service.py |
| 运行流与持久化 | `_persist_emitted_event` 1924 / `_persist_emitted_chunk` 1946 / `_persist_emitted_chunks` 1958 | 堆在 service.py |
| runtime config 组合 | `_effective_runtime_config_from_metadata` 6932（**181 行**）、`_runtime_config_for_request` 1163、`_config_with_request_agent_override` 6569、`_runtime_config_metadata` 6540 | 已有 `config_materializer.py`，组合决策仍在 service |
| agent 校验/registry | `_runtime_agent_registry` 1631、`_validate_runtime_agent_for_execution` 6666、`_metadata_with_resolved_subagent_route` 6691、`list_agent_summaries` 3990 | 堆在 service.py |
| 工具构造与 scoping | `_build_base_tool_registry` 843、`_build_lsp_tool` 1242、`_build_mcp_tools*` 1249/1341、`_tool_registry_for_effective_config` 1362、`_tool_policy_denial` 1423 | 已有 `tool_provider.py`/`tool_scope.py`/`tool_materializer.py`，构造与 refresh 时机仍在 service |
| skills 快照/装配 | `_build_skill_registry` 1214、`_skill_snapshot*` 6189/6240/6491、`_applied_skill_contexts` 6157、`_selected_skill_names_for_agent` 6498 | 堆在 service.py |
| context 装配 | `_assemble_provider_context` 5955（102 行）、`_prepare_provider_context_window` 5830 | 已有 `provider_context.py`，装配编排在 service |
| LSP/MCP/ACP 生命周期门面 | `request_lsp` 1477、`request_mcp_tool` 1513、`connect_acp` 1583、`request_delegated_acp` 1600、`_emit_acp_events` 1022 | 门面留在 service 合理，但 ACP 事件编排与 delegated 生命周期（1640-1758）是独立语义 |
| 权限/审批 | `_resolve_permission` 2819（133 行）、`_approval_resolution_outcome` 2953、`_pending_approval_*` 5087-5277 | 已有 `resume.py`，但 `_resolve_permission` 主路径仍在 service |
| hook 执行 | `_run_lifecycle_hooks` 2568、`_run_tool_hooks` 2625、`_hook_execution_policy_from_metadata` 2668 | 已有 `hook/` 模块，编排在 service |
| background task 门面 | `start_background_task` 3146 … `steer_background_task` 3218 | 已委托 supervisor（合理门面） |
| session 查询/撤销 | `replay_session` 3242、`revert/undo/unrevert_session` 3261-3305、`session_result` 3227 | 薄门面（合理） |
| resume/question | `resume` 4828、`resume_stream` 4868、`answer_question*` 5032-5262、`_resume_*` 5318-5452 | 已有 `RuntimeResumeCoordinator`，但 service 保留约 25 个 resume 相关私有方法 |
| memory | `add_memory` 3026 … `memory_event_payload` 3107（约 10 方法） | 薄门面 + 事件 payload 投影（可分离） |
| provider inspection | `provider_models*` 3737-3750、`provider_readiness` 3793、`inspect_provider` 3895、`validate_provider_credentials` 3920、`list_provider_summaries` 3775 | 已有 `provider_inspection.py`/`provider_catalog_query.py`，组合仍在 service |
| 状态/能力快照 | `current_status` 4094（122 行）、`web_settings` 4337、`review_snapshot` 4257 | 混合 git 子进程（4266）与 7 类 capability 投影 |
| debug/observability | `session_debug_snapshot` 3404（148 行）、`_debug_*` 4444-4602（约 12 方法） | 已有 `runtime_debug.py`，快照组合在 service |
| 存储管理 | `prune_runtime_storage` 3651、`reset_runtime_storage` 3665、`export/import_session_bundle*` 3571-3620 | 薄门面（合理） |
| active session 注册表 | `_sealed_session_status` 7215、`_is_active_session_id` 7259、`_register/_unregister` 7262-7287 | 已有 `active_session.py`，guard 在 service |
| 工具类静态转发 | `_optional_*` 3960-3984、`_coerce_*` 模块级 408-425、`_contract_metadata_from_payload` 3987 | 转发噪音 |

**已有独立 collaborator 但仍堆在 service.py 的**：config 组合决策（materializer 只做 parse/serialize）、permission 主路径、context 装配编排、agent/skills 快照、debug 快照组合——这些正是 `runtime-service-decomposition-plan.md` 里"保留在 service.py"的清单，说明拆分计划有意让 service 保留组合职责；问题是组合点过多且方法体过大（`_stream_chunks` 542 行）。

### 2.2 `RuntimeBackgroundTaskSupervisor` — background_tasks.py:148，82 方法（35 public / 47 private）

职责簇（均可分离）：
- **worker 生命周期/队列调度**：`run_background_task_worker` 2404（289 行）、`_drain_background_task_queue` 928（172 行）、`_spawn_worker_thread` 1101、`drain_queued_background_tasks` 891
- **并发 slot 管理**：`_reserve_slot` 767、`_release_slot` 771、`_can_start_task` 758、`_concurrency_identity_*` 691-756
- **重试/退避**：`_wait_for_rate_limit_backoff_or_cancel` 804、`_rate_limit_backoff_seconds` 2705、`retry_background_task` 582
- **cancel/steer**：`cancel_background_task` 1240（84 行）、`steer_background_task` 607（83 行）、`_task_cancel_requested` 784
- **事件 emit/通知**：`emit_background_task_parent_terminal_event` 1514（83 行）、`emit_background_task_waiting_approval` 1706、`emit_background_task_awaiting_steer` 1784、`_emit_parallel_group_terminal_event` 1598、idle reminder 家族 1868-2007
- **reconcile/shutdown**：`reconcile_background_tasks_if_needed` 2281（72 行）、`shutdown` 189、`_terminalize_*` 235-349、`_terminalize_queued_orphans_with_terminal_parent` 2354
- **observability 投影**：`task_observability` 351、`summaries_with_observability` 394、`status_counts` 426
- **child session 封口/终态修复**：`finalize_background_task_from_session_response` 2025、`_seal_child_session_from_response` 2136、`repair_interrupted_task_from_child_terminal_session` 1211、`_child_terminal_status_from_response` 2092
- **lifecycle hook**：`run_background_task_lifecycle_hook` 2210、`run_background_task_lifecycle_surface` 2237

其中 **child session 终态修复**（封口、terminal 推导、`submit_result` 证据判定）在职责上属于 run-loop/finalize 语义而非"队列调度"，与 §3.5 的双 truth 问题相关。

### 2.3 `SqliteSessionStore` — storage.py:407，157 方法（50 public / 107 private），5275 行

职责簇：
- **schema/连接治理**（约 40 私有方法）：`_ensure_schema` 644（**204 行**）、`_connect` 564、`_write_connect` 635、`_assert_*` 936-1103、`_ensure_storage_sequences` 863
- **session 行/快照/事件**：`save_run` 1618、`append_session_event(s)` 2268/2375、`_write_session_snapshot` 1279、`load_session*` 3046-3204、`truncate_session_events_after` 2606
- **revert/undo 家族**：3351-3398
- **pending approval/question + resume checkpoint**（约 15 方法）：1931-2266、2756-2922
- **background task 持久化**（约 25 方法）：`create_background_task` 3422、`mark_background_task_*` 3605-4040、`fail_incomplete_background_tasks` 4043、idle reminder 3886-4040、`_background_task_*` 行解析/序列化 1451-1600
- **memory**（约 12 方法）：1771-1928
- **notifications**（约 6 方法）：3400-3420、4283-4327、`_sync_notifications` 4942（125 行）
- **tool effectiveness**：`tool_effectiveness_report` 2956
- **diagnostics/prune/reset**（约 15 方法）：4330-4732
- **todo 投影**：1183-1277（与 `todos.py` 协作）

`SessionStore` Protocol（storage.py:174，45 个方法）和 `SessionEventAppender`（385）已经给外部消费方提供了类型化边界——这是本次审计中最接近 OMP"类型化契约"的现有设施；问题在 SqliteSessionStore 内部（7+ 领域合一的实现类）。

### 2.4 `RuntimeRunLoopCoordinator` — run_loop.py:477，22 方法 + 24 个模块函数

方法数少但**单个方法极大**：`execute_graph_loop` 1100-2497（1397 行）、`execute_approved_tool_call` 662-1099（437 行）、`_execute_invoked_tool` 2580-2993（413 行）。`execute_graph_loop` 内部顺序混合：turn-progress hook → checkpoint 捕获 → submit_result 终态 → provider 调用 → fallback 决策/图切换/事件 → 工具执行 → abort 检查 → final-step 判定 → 事件重编号 → context-compacted 判定。fallback 的**纯决策**已抽到 `provider_fallback.py`（`decide_provider_error_policy`），但"何时换图、如何重排 metadata、发什么事件、终态怎么映射"的编排仍在 1397 行方法体内（1533-1690 是 fallback 段，1749-1800 是 final-step 段）。

---

## 3. 维度二/三：跨模块隐式依赖与平行推导

### 3.1 read_only / mode 推导 —— **已收敛**（Phase 1 完成）

`mode.py:69-96 resolve_mode` 是唯一权威；所有消费者经 `runtime_read_only_from_metadata`（mode.py:99）或其 contracts 包装（contracts.py:162-171）读取。证据：
- `permission.py:99-103 is_plan_mode_blocked` 只接收 `read_only` 布尔（由调用方解析）
- `tool_scope.py:53-54` 明确注释"不再保留 private copy"，走 contracts 包装
- `policy.py:364-365`、`session.py:214-215`、`service.py:2672-2673 / 5623-5626 / 5969-5972` 全部经 `runtime_read_only_from_metadata` / `resolve_mode`

全仓库未发现 `mode == "plan"` 直接推导 read_only 的漏网副本。**无漂移风险。**

### 3.2 background task 状态转移矩阵 —— **基本收敛，3 处终态集合残留**

权威：`task.py:72-76`（`_BACKGROUND_TASK_TERMINAL_STATUSES` + `_BACKGROUND_TASK_ALLOWED_TRANSITIONS` + `is_background_task_terminal`/`is_background_task_transition_allowed`）。`storage.py` 在 running/idle/steered/cancel/terminal 各 mark 方法中强制校验转移（3619-3623、3670-3674、3730-3733、3773-3776、3817、3859、3903、4166）——**这是正确的 enforcement 位置**。

残留硬编码（绕过权威）：
- `storage.py:3661` `mark_background_task_terminal` 内联校验 `status not in ("completed", "failed", "cancelled", "interrupted")`——与 `task.py:72` 终态集合重复，新增终态（如有）时两处都要改
- `background_tasks.py:1637` parallel group 计数 `for status in ("completed", "failed", "cancelled", "interrupted")`——同集合第三次出现
- `service.py:4208` `current_status` 中 `terminal_count` 对同一集合求和——第四次
- `storage.py:1132 _parse_background_task_status` 硬编码 6 个状态字符串——解析器必须枚举，可接受，但未引用 `task.py` 的类型定义（有字符串→类型漂移的隐性风险）
- `background_tasks.py:590/634` retry/steer 的允许集合（"failed/cancelled/interrupted"、"idle/interrupted"）是控制流判断，未进矩阵——属 supervisor 的合法局部判定，但语义与矩阵重叠

### 3.3 delegated routing / child preset 校验 —— **收敛路径清晰，但枚举仍 4+ 处平行推导**

实际 enforcement 链已收敛：`contracts.py:337-363 runtime_subagent_route_from_metadata` → `task.py:84-98 resolve_subagent_route`，service.py 三处调用点（1452-1455、6698-6701、6946-6950）都传 `agent_registry.executable_subagent_ids()`（agent/registry.py:63-64，manifest 驱动）。

但同一 5 元组（advisor/explore/researcher/worker/product）仍有 4+ 处硬编码副本：
1. `task.py:71` `_CALLABLE_SUBAGENT_PRESETS`（resolver 的默认回退——保留为默认值可接受）
2. `service.py:393` `_EXECUTABLE_SUBAGENT_PRESETS` —— **死代码**：全仓库（含测试）无生产引用，仅 `tests/unit/agent/test_builtin.py:186` 与文档引用；是"用测试看守的平行副本"
3. `policy.py:12` `_ALLOWED_CHILD_PRESETS`，且 `policy.py:641-643` 用它**过滤** child preset 列表——这是真正的漂移风险：manifest 新增合法 child preset 会被该过滤静默剔除，而 runtime 的 `executable_subagent_ids()` 已允许它
4. `agent_capability.py:137/139` 两处内联同一元组
5. 文档面：`tools/task.py:163` schema description、`agent/prompts.py:12`、`docs/mode-composition-design.md:91`（该文档已承认此枚举分散现状）

`_EXECUTABLE_AGENT_PRESETS`（service.py:392，{"leader"}）与 manifest `top_level_selectable` 的一致性由 `test_builtin.py:165-166` 看守——同一"测试维护平行副本"模式，但该常量仍被生产使用（文档称 runtime 持 enforcement）。

### 3.4 terminal seal / sealed status —— **双层设计（有意），两处硬编码但语义已文档化**

- 存储层：`storage.py:122-138 _assert_terminal_session_events_allowed` 只对 `{"completed", "failed"}` 封口（行 138），并列出允许的迟到事件白名单（行 89-120 注释）
- 运行时层：`service.py:7215-7257 _sealed_session_status` 用 `session.py:23-27 SESSION_TERMINAL_STATUSES`/`is_session_status_terminal` 把 `interrupted`（无活跃 run）一并封口
- `session.py:26-30` 注释明确这是"single source of truth"的两半：存储层窄、运行时层宽

评估：语义分层是**刻意设计**且有文档锚点，但 `storage.py:138` 的 `{"completed", "failed"}` 字面量与 `session.py:23` 的 frozenset 仍是两个独立字面量（`session.py` 未导出 `{"completed","failed"}` 子集供 storage 引用）。风险：若将来调整终态集合，storage 层需要手工同步。属于**低风险的有意耦合**，但可以低成本消除（storage 引用 session.py 导出的窄集合常量）。

### 3.5 双 truth 风险面：task 状态 vs child session 状态

supervisor 维护 task `status`（SQLite `background_tasks` 行），同时 child 是独立 session（`sessions` 行）。二者终态必须对齐，桥接逻辑集中在 `finalize_background_task_from_session_response`（2025）、`_child_terminal_status_from_response`（2092-2113，含"transcript 证据"推导：session 行可能滞后于事件流）、`_seal_child_session_from_response`（2136）。这套桥接是有意的（durable 双行模型），但：
- `_child_terminal_status_from_response` 里 `status == "running"` → `"failed"`、`"interrupted"` + transcript 证据 → `"completed"` 的映射是**第二套终态推导**，与 run_loop 的 final-step 判定（run_loop.py:1749-1800）语义重叠——两处各自实现"什么算 completed"
- keep-alive 的 `task.idle` ↔ 子 session `interrupted`（可恢复）对应关系散落在 supervisor 多个方法（243、634、2625-2629），无单一表

### 3.6 事件类型注册 —— 一权威 + 多处局部副本（脆弱模式）

`events.py` 是权威：`EMITTED_EVENT_TYPES`/`RUNTIME_EVENT_TYPES`（215-260）、`KNOWN_EVENT_TYPES`（505）、`DELEGATED_BACKGROUND_TASK_EVENT_TYPES`（509）、`_DELEGATED_EVENT_STATUS_BY_TYPE`（549）。但存在多处需手工同步的副本：
- `event_envelopes.py:47-52 / 85-92 / 145-152` 每个 surface（LSP/ACP/MCP）本地构造 `known_event_types` set——值从 events.py import（值不漂移），但**成员列表**是手工挑选的子集，新增事件类型时若忘加，envelope 转换静默丢弃
- 事件类型字符串同时出现在 `storage.py`（如 `RUNTIME_TOOL_COMPLETED` 引用、`"runtime.todo_updated"` 字面量 1269）、`background_tasks.py`（`RUNTIME_FAILED` 1294）、`run_loop.py`（`"graph.provider_stream"` 202、`"runtime.provider_fallback"` 1641 等）——多数经 events.py 常量引用（好），但 `storage.py:1269` 的 `"runtime.todo_updated"` 与 `run_loop.py:1641` 的 `"runtime.provider_fallback"` 是**裸字符串字面量**，与 events.py 的 Literal 定义无编译期绑定
- `events.py:505-508` 的 `KNOWN_EVENT_TYPES` 是"EMITTED ∪ RUNTIME"手工并集，非自动推导

### 3.7 metadata magic-string 隐式契约（§2 量化）

`session.metadata` / `request.metadata` 均为 `dict[str, object]`。统计 14 个核心 runtime 文件，**76 个 string key 在 ≥2 个文件间共享语义**，无单一权威定义：

| key | 跨文件数 | 主要文件 |
|---|---|---|
| `tool` | 6 | background_tasks/events/resume/service/session/storage |
| `runtime_state` | 5 | resume/run_loop/service/session_metadata_helpers/storage |
| `delegation` | 5 | agent_capability/contracts/events/service/task |
| `runtime_config` / `runtime_policy` / `prompt_activation` | 3-4 | policy/session/session_metadata_helpers/service/config_materializer |
| `provider_error_kind` / `error_kind` / `retry_guidance` | 3 | background_tasks/resume/service/storage |
| `skill_snapshot` / `plan_state` / `context_window` / `todos` 等 | 2-3 | 多文件 |

已存在的类型化锚点：`RuntimeRequestMetadata` TypedDict（contracts.py:52-76）、`RuntimeSubagentRoutingMetadata`（78）、`RuntimeCommandMetadata`（43）。但**持久化 session metadata 没有对应 TypedDict**；`config_materializer.py` 的 `parse_persisted_runtime_config()` 只覆盖 `runtime_config` 子集，`runtime_state`/`plan_state`/`skill_snapshot`/`delegation` 等仍是无类型 dict 约定。`session_metadata_helpers.py` 是"共享 helper 模块"（缓解符号重复），但 helper 间没有类型化 payload 定义。

---

## 4. 维度四：循环依赖 / 双向依赖

**结论：无运行时循环 import，存在 hub-and-spoke 回引 + 隐式契约。**

- `service.py` 运行时 import `background_tasks`/`run_loop`/`resume`/`storage`（service.py:114/312/314/361）
- `background_tasks.py:55`、`run_loop.py:80`、`resume.py:30` 对 `service.VoidCodeRuntime` 仅 `TYPE_CHECKING` import——不存在 A import B 且 B import A 的运行时环
- 但三个 collaborator 都以**参数/构造注入持有 `VoidCodeRuntime` 实例**，并大量穿透私有成员（§1 数据）：

| 文件 | 对 runtime 的属性访问总数 | 其中私有属性/方法 | 最热私有访问 |
|---|---|---|---|
| background_tasks.py | 198 | **195** | `runtime._session_store` 71、`runtime._workspace` 74、`runtime._config` 9、`runtime._session_routing_for_request` 4、`runtime._append_parent_acp_delegated_lifecycle_event` 3、`runtime._publish_delegated_acp_event` 3 |
| run_loop.py | 118 | **118** | `runtime._failed_chunk` 33、`runtime._run_id_from_session_metadata` 9、`runtime._run_tool_hooks` 6、`runtime._effective_runtime_config_from_metadata` 6、`runtime._resolve_permission`/`_tool_policy_denial`/`_approval_resolution_outcome` 各 3 |
| resume.py | 136 | **135** | `runtime._resequence_event` 9、`runtime._session_store`/`_workspace` 各 7、`runtime._run_lifecycle_hooks`/`_lifecycle_hook_failure_chunk` 各 6、`self._runtime._background_task_supervisor` 4 |

合计 **448 次跨模块私有访问**。由于没有 Protocol/接口，这些访问构成**隐式契约**：`runtime._session_store` 具体类型是 `SqliteSessionStore` 而非 `SessionStore` Protocol（例如 background_tasks.py:1520 用 `isinstance(..., SessionEventAppender)` 做运行时检查来补偿）。

**service.py 侧的镜像问题**：19 个私有方法带 `# Referenced via extracted collaborators` 注释（1018、1138、1151、1160、1695、1725、2633、2846、5110、5158、5758、5763、6062、6095、6104、6145、6426、6916、6921）——这些是拆分后残留的 **proxy 转发方法**（如 `_prompt_from_events` → `prompt_from_events`、`_provider_attempt_from_metadata` → 模块函数）。collaborator 不直接 import 模块函数，而是绕道 runtime 实例调用其私有方法。`runtime-architecture-refactor-plan.md` 规则 7（"`VoidCodeRuntime` does not expose proxy properties for collaborator internals"）被此模式**违反**。

---

## 5. 维度五：对照 OMP 的改进方向（仅方向 + 边界，不含实现）

### 5.1 God class → 按已有 collaborator 边界收尾（对应耦合点 1、3）

方向：`runtime-service-decomposition-plan.md` 的 collaborator 名单正确，但"提取完成"不等于"契约完成"。下一步应把 collaborator 对 runtime 的**数据依赖显式化**：supervisor/run-loop/resume 构造时注入 `SessionStore`（用已有 Protocol）+ `RuntimeConfig` + hook runner，而不是注入整个 runtime 再穿透 `_session_store/_config/_workspace`。`_stream_chunks`（542 行）与 `execute_graph_loop`（1397 行）按"一次循环迭代的职责"切片（turn 准备 / 权限 / hook / 工具 / fallback 决策应用 / 终态判定各为一协作函数），不新增类型。

边界：治理（权限、审批、工具注册、hook、session/background truth）继续留在 runtime 侧；graph/CLI 不获得这些能力——与现有两份计划一致。

### 5.2 metadata dict → 类型化契约（对应耦合点 2）

方向：OMP 用 TS 类型 + JSON Schema 做契约；VoidCode 已有 `RuntimeRequestMetadata` TypedDict 与 `config_materializer` 的版本化 parse。把同样的模式推广到**持久化 session metadata 子集**：为 `runtime_state`、`plan_state`、`skill_snapshot`、`delegation` 各定义版本化 TypedDict + 严格 parse（未知 key 拒绝），并让 `session_metadata_helpers.py` 成为这些类型的唯一入口（当前它是"无类型 helper 集合"）。新增 metadata key 的准入门槛 = 先有类型。

边界：不引入"开放式 metadata 控制面"（`runtime-architecture-refactor-plan.md` 已明令禁止）；只把现有键收敛为类型，不扩展键空间。

### 5.3 平行推导收敛（对应耦合点 4、5、§3.4/§3.6）

方向：
- child preset：以 `agent_registry.executable_subagent_ids()`（manifest 驱动）为唯一权威；删除 `service.py:393` 死代码；`policy.py:641-643` 改为引用 registry 结果或仅作快照校验（与 runtime enforcement 同源），消除静默过滤漂移
- background-task 终态集合：`storage.py:3661`、`background_tasks.py:1637`、`service.py:4208` 改调 `task.py:is_background_task_terminal`
- terminal seal：`storage.py:138` 改引用 `session.py` 导出的窄终态常量，消除两个字面量
- 事件类型：`event_envelopes.py` 的 surface 子集改为从 events.py 的组合常量派生（或由 surface 的 manager 声明其可发事件类型，由 events.py 校验），并消灭 `storage.py:1269`/`run_loop.py:1641` 的裸字符串事件名（改常量引用）
- "什么算 child 终态"（`_child_terminal_status_from_response` 的 transcript 证据推导）与 run_loop final-step 判定：抽出单一 `child_terminal_outcome` 纯函数供 supervisor 与 run-loop 共用

方向性原则：每个语义（mode→read_only、task 终态、child preset 集合、session 终态、事件类型集合）**一个权威 + 零副本**；副本若必须存在（如 `_CALLABLE_SUBAGENT_PRESETS` 默认值），由测试断言其与权威一致（现有 `test_builtin.py` 模式），并显式标注。

### 5.4 hub-and-spoke 回引（对应维度四）

方向：OMP 的 agent registry / session tree / task 通过类型化接口协作。VoidCode 已有 `SessionStore` Protocol 与 `SessionEventAppender` Protocol——把同一模式推广为 `RuntimeSurface`（供 supervisor/run-loop 调用 hook/config 的最小接口）。supervisor 持 runtime 回引本身是**刻意设计**（它是 runtime 的调度扩展点，需要访问 store/config/hook），可接受；不可接受的是通过 `runtime._xxx` 访问——最小接口 + 显式依赖注入后，448 处私有访问中的绝大部分会消失，剩余（如 `_failed_chunk` 的 chunk 构造语义）应收敛为共享纯函数。

边界：不引入事件总线/中间件层（OMP 的 bus 不在 VoidCode 路线内）；保持"runtime 统一持有治理"的前提。

### 5.5 对照 OMP 的三个参照点的落点

- **session truth 单一权威**：SQLite 行 + 事件流是权威，`ACTIVE_SESSION_REGISTRY`（active_session.py）与 supervisor 线程状态是**可重建的进程内视图**（reconcile 从 SQLite 重建，background_tasks.py:2281）——方向上已符合 OMP"terminal seal + drain"；剩余风险是 §3.5 的 task↔session 双行终态桥接与 §3.6 的裸事件字符串
- **agent/session/task 边界**：VoidCode 的 keep-alive idle + background task + child session 三态对应 OMP 的 parked agent/session tree；边界已清晰（§2.2 职责簇），弱点是终态推导双实现（§3.5）与 child preset 枚举（§3.3）
- **类型化契约**：request metadata 已有 TypedDict、storage 已有 Protocol、config 已有版本化 parse；差距集中在**持久化 session metadata**（§3.7）与 collaborator↔runtime 接口（§4）

---

## 6. 与既有拆分计划的关系

### `docs/runtime-service-decomposition-plan.md` 已覆盖

- background task lifecycle 边界（第 1 节）：**已落地**——supervisor 独立文件 + facade 留在 service；但计划未量化"supervisor 对 runtime 的 195 次私有访问"，其"最接近完成的拆分"评价与 §4 数据形成反差：**文件拆出去了，契约没有**
- provider fallback policy 分离（第 2 节）：已落地（provider_fallback.py 纯决策）
- resume coordinator（第 3 节）：已落地，但 service 仍保留约 25 个 resume 私有方法（§2.1）
- tool registry scoping（第 4 节）、config materializer（第 5 节）：已落地

### `docs/runtime-architecture-refactor-plan.md` 已覆盖

- 规则 7（collaborator 所有权反映在调用点，无 proxy property）：**被 19 个 proxy 方法违反**（§4）——计划是"Change Gate"，没有把现有违反列为待清理项
- Metadata Ownership 表（request/persisted/turn-local/observability）：原则正确，但无强制机制（§3.7 的 76 个 key 仍在无类型 dict 里）

### 本次审计新增/计划遗漏的耦合点

1. **448 处跨模块私有访问的量化与最小接口方向**——两份计划都未量化此面（计划假设"移出即解耦"，审计显示"移出 + 回引穿透"）
2. **child preset 枚举平行推导**（§3.3，含 service.py:393 死代码与 policy.py 静默过滤）——`mode-composition-design.md:91` 已承认现状，但两份计划均未安排收敛
3. **storage.py 157 方法单体**——refactor 计划只限制"不扩大 SqliteSessionStore 职责"，无拆解方向（本次建议：按 §2.3 的领域簇拆私有实现模块，`SessionStore` Protocol 保持为公共契约）
4. **事件类型/终态集合的裸字面量与局部子集**（§3.6、§3.2 残留）——计划未涉及
5. **task↔session 双行终态推导重复**（§3.5）——keep-alive 设计文档未把它列为待收敛项

---

## 7. 「耦合但可接受」vs「耦合且有真实风险」判定

### 可接受（刻意设计，有文档/测试锚点）

| 耦合点 | 理由 |
|---|---|
| supervisor/run-loop/resume 持 runtime 回引 | runtime 是治理控制面，collaborator 需要 store/config/hook 访问；注入整个 runtime 是当前最简单形式。**可接受的前提**是访问收敛到最小接口（§5.4） |
| terminal seal 双层（storage 窄 / runtime 宽） | `session.py:26-30` 与 `storage.py:79-85` 明确文档化设计意图：存储层拒绝 `{completed, failed}` 迟到事件，运行时层额外封口 `interrupted`。低漂移风险（字面量同步成本低） |
| `_CALLABLE_SUBAGENT_PRESETS` 默认值 | resolver 的无参默认（task.py:89 `or` 语义），被 `contracts.py` 显式传入的 registry 结果覆盖；测试看守一致性 |
| `_parse_*` 枚举解析器（storage.py:1106-1159） | 反序列化必须枚举字符串值；风险仅在于类型别名漂移，可加测试锚定 |
| `ACTIVE_SESSION_REGISTRY` 进程内状态 | 可重建视图，SQLite 仍为 durable truth；reconcile 路径（background_tasks.py:2281、`_sealed_session_status` 7215）桥接正确 |

### 真实风险（会导致静默漂移或改动连锁）

| 耦合点 | 风险机制 |
|---|---|
| 448 处 `runtime._xxx` 私有访问 + 19 个 proxy 方法 | 契约是"属性名约定"：改名/重排 service.py 内部结构，collaborator 编译期无感知；`isinstance(SessionEventAppender)` 运行时补偿证明类型边界缺失。**违反 refactor 计划规则 7** |
| 76 个 metadata string key | 新增/修改 key 无编译期检查；`runtime_state`/`delegation` 跨 5 文件，写错一个字符只在运行期暴露 |
| `policy.py:641-643` 对 child preset 的过滤 | 与 `executable_subagent_ids()` 不同源：manifest 合法新增会被**静默剔除**（不报错），是最典型的平行推导漂移 |
| `_child_terminal_status_from_response` 终态推导 | 与 run_loop final-step 判定（run_loop.py:1749-1800）是两套"什么算完成"实现；改任一处可造成 task 终态与 session 行不一致 |
| `event_envelopes.py` 手工子集 + 裸事件字符串 | 新增事件类型忘加子集 → envelope 静默丢弃；`storage.py:1269`/`run_loop.py:1641` 裸字符串绕过 events.py 权威 |
| `service.py:393` 死代码常量 | 由测试（test_builtin.py:186）维持存在；新增 preset 时测试会失败（暴露问题），但删除它才是正确终点——文档（AGENTS.md:57）把它当成 load-bearing，实际不是 |

### 优先级建议（设计向）

1. **P0**：collaborator 依赖显式化（最小接口 / Protocol），消灭 448 处私有访问与 19 个 proxy——收益最大且不改变任何运行时语义
2. **P0**：child preset 枚举收敛到 manifest 权威，删死代码，修 policy.py 过滤——低成本、消除真实漂移
3. **P1**：持久化 session metadata 子集 TypedDict 化（runtime_state/delegation/plan_state/skill_snapshot）
4. **P1**：终态集合与事件类型字面量统一引用权威常量（含 `_child_terminal_status_from_response` 抽出共用纯函数）
5. **P2**：storage.py 按领域簇拆私有实现（保持 `SessionStore` Protocol 公共契约不变）；`execute_graph_loop` 切片

---

*本报告为 design-only 审计：未修改任何代码，所有结论基于只读调查；标注 `[推断]` 的仅出现在未直接验证的推测处（本报告未使用推断，所有结论均有代码证据）。*
