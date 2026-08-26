# 持久化 Session Metadata TypedDict 化设计（P1）

> 设计日期：2026-08-18 · 范围：`src/voidcode/runtime/`（design-only，未改代码）
> 上游：`docs/runtime-coupling-audit.md` §3.7（76 个 magic-string metadata key）+ §5.2（方向，直接采信）
> 约束：`docs/runtime-architecture-refactor-plan.md` 的 Hard Rules 1-7、Metadata Ownership 四类归属、Change Gate
> 方法论：本设计基于审计 §3.7 列出的 key 表 + 对 runtime 目录的代码级 key 提取（正则提取 `.get("x")`/`.pop("x")`/`["x"]`/dict-literal key，限定审计口径的 14 个核心 runtime 文件，详见 §2 附注）。凡未在审计表格中显式列出的 key，标注 `[推断]`。

---

## 1. 结论先行

### 1.1 四个 TypedDict

| TypedDict | 对应 persisted 结构 | 现状 | 本设计 |
|---|---|---|---|
| `RuntimeStateMetadata` | `session.metadata["runtime_state"]`（5 文件共享） | 当前严格 key-set payload | 版本化 TypedDict + key-set + strict parse |
| `PlanStateMetadata` | `session.metadata["plan_state"]`（2 文件） | 当前严格 key-set payload | 同上 |
| `PersistedDelegationMetadata` | `session.metadata["delegation"]`（8 文件共享语义） | 当前严格 key-set payload | 特化子类 + strict parse |
| `SkillSnapshotMetadata` | `session.metadata["skill_snapshot"]`（4 文件） | 已版本化 + 已 hash 校验 | TypedDict + 顶层未知 key 拒绝 |

### 1.2 严格 parse 与当前格式决策

当前持久化 metadata 只接受当前字段集合与类型。写入和读取共用严格 parser；未知字段、缺失必需字段、非对象 payload、类型错误均 fail fast，不静默迁移、不回退到 workspace defaults，也不保留旧字段。显式版本化结构（skill_snapshot、agent_capability_snapshot、runtime_policy、resolved_hook_presets）继续执行各自的 version/hash 校验。

runtime_state、plan_state、delegation 使用 key-set 作为当前契约边界；可选结构整体缺失仍表示该能力未启用，结构一旦存在则必须是完整的当前 payload。

### 1.3 唯一入口

`session_metadata_helpers.py` 成为这 4 个结构的**唯一读/写入口**：TypedDict 定义放在 `contracts.py`（与 `RuntimeRequestMetadata`/`RuntimeSubagentRoutingMetadata` 同居，保持 metadata 契约单文件）；helpers import + re-export 类型，并承载全部 parse/accessor/构造器。service.py / resume.py / run_loop.py / storage.py / context_window.py / todos.py / prompt_assembly.py / context_continuity.py / provider_execution_metadata.py 中的 20+ 处直接 `metadata.get("runtime_state")` + isinstance 跳舞改为调 helpers（§5 逐点列出）。

### 1.4 范围

P1 只做 persisted facts 的 4 个 TypedDict。turn-local（`provider_attempt` 等）与 observability（`error_kind`/`retry_guidance` 等）**只分类、不类型化**（§2、§7 边界）。`runtime_config`/`runtime_policy`/`agent_capability_snapshot`/`resolved_hook_presets`/`context_window` 已各有版本化/类型化 owner，**不在 P1**（§2.2）。

---

## 2. 76 个 key 的四类归属

> 附注：审计 §3.7 的 76 个 key 按其方法（AST 统计 14 个核心 runtime 文件的 metadata 语义共享）统计；表格仅列出代表性 key。本文下表基于审计表 + 本设计对同一 14 文件集合的字符串 key 提取（`agent_capability/background_tasks/config_materializer/contracts/event_envelopes/events/policy/resume/run_loop/service/session/session_metadata_helpers/storage/task`，共 202 个 ≥2 文件 key；其中属 session/request metadata 语义的见下表）。事件 payload key、storage 行列 key（如 `pending_approval_*`、`text_char_count`、`idle_episode_id`）不属 session metadata，归入 §7 非目标。

### 2.1 四类归属总表

**A. request configuration（请求配置）—— 已有 `RuntimeRequestMetadata` 收敛，P1 不新增类型**

| key | 文件数 | 现状 | P1 处置 |
|---|---|---|---|
| `agent` | 5 | TypedDict 字段（contracts.py:54）+ `validate_runtime_request_metadata` 类型校验（contracts.py:398-403） | 已收敛，不动 |
| `command` | 5 | `RuntimeCommandMetadata`（contracts.py:43-51）+ 严格校验（contracts.py:177-193） | 已收敛，不动 |
| `delegation`（作为请求路由） | 8 | `RuntimeSubagentRoutingMetadata`（contracts.py:78-88）+ 严格校验（contracts.py:249-263） | 请求侧已收敛；**persisted 副本进 P1**（§3.4） |
| `mode` / `read_only` | 9/5 | TypedDict + `parse_runtime_mode` + `runtime_read_only_from_metadata`（contracts.py:162-171，mode.py:99） | 已收敛（审计 §3.1），不动 |
| `max_steps` / `provider_stream` / `reasoning_effort` / `show_thinking` / `skills` / `force_load_skills` / `keep_alive` | 2-5 | TypedDict 字段 + 校验（contracts.py:427-472） | 已收敛，不动 |
| `abort_requested` | 2 | TypedDict 字段（contracts.py:53），turn-local abort 信号 | 已收敛，不动 |
| `background_run` / `background_rate_limit_retry` / `background_task_id` / `keep_alive_turn` | 3-4 | `InternalRuntimeRequestMetadata` + `_INTERNAL_RUNTIME_REQUEST_METADATA_KEYS`（contracts.py:105） | 已收敛，不动 |
| `context_transform_refs` | 2 | **已入 `_STABLE_RUNTIME_REQUEST_METADATA_KEYS`（contracts.py:96）且有校验（contracts.py:457-466），但漏写在 `RuntimeRequestMetadata` TypedDict 字段里（contracts.py:52-76）** | **P1 顺手补一个字段声明**（纯标注，零行为变化） |
| `original_prompt` / `raw_arguments` | 2 | `RuntimeCommandMetadata` 字段 | 已收敛，不动 |

结论：request 侧**基本收敛**，唯一缺口是 `context_transform_refs` 的 TypedDict 字段漏注（校验已存在，`[推断]` 为历史遗漏，P1 补注）。

**B. persisted facts（持久化事实）—— P1 核心**

| key | 文件数 | 现状 | P1 处置 |
|---|---|---|---|
| `runtime_state` | 5（resume/run_loop/service/session_metadata_helpers/storage） | 无类型 dict，8 已知字段 | **P1：`RuntimeStateMetadata`** |
| `plan_state` | 2（context_window/session_metadata_helpers） | 无类型 dict，4 字段 | **P1：`PlanStateMetadata`** |
| `skill_snapshot` | 4（resume/service/skill_metadata/storage） | 已有版本化+hash parser（skills.py:187），缺顶层未知 key 拒绝 | **P1：`SkillSnapshotMetadata`** |
| `delegation`（persisted 副本） | 8 | 与请求侧同形，但 **persisted 副本无独立 parse**；resume 只经 `runtime_subagent_route_from_metadata` 做 selected_preset/execution_engine 一致性抽查（contracts.py:354-378） | **P1：`PersistedDelegationMetadata`** |
| `runtime_config` | 3-4 | **已版本化**：`PERSISTED_RUNTIME_CONFIG_KEYS` + `parse_persisted_runtime_config`（config_materializer.py:19-38/131），读路径 service.py:6089 强制 | 不在 P1，保持 |
| `runtime_policy` | 4-6 | **已版本化**：`schema_version`/`policy_version` + `_snapshot_from_payload` 严格读（policy.py:421-478） | 不在 P1，保持 |
| `agent_capability_snapshot` | 2+ | **已版本化**：`snapshot_version: 3` + `validate_agent_capability_snapshot`（agent_capability.py:11-56） | 不在 P1，保持 |
| `resolved_hook_presets` | 2 | **已版本化**：`hook_preset_snapshot_from_payload`（hook_preset_metadata.py:22-31，hook/presets.py:368） | 不在 P1，保持 |
| `context_window` | 3 | 半版本化：`RuntimeContextWindow.metadata_payload()` 无顶层 version（context_window.py:227-260），内部 `projection` 带 version；`ContextWindowPolicy` 带 `version: 1` | **不在 P1**（§2.2 注），follow-up |
| `workspace` | 3 | 标量 str，session.py/storage 校验 | 标量，不类型化 |
| `selected_skill_names` / `applied_skills` / `applied_skill_payloads` | 2-3 | skill 选择族顶层 key（skill_metadata.py:97-104 `snapshot_to_session_metadata`）；`selected_skill_names` 已有 list[str] 校验（skill_metadata.py:52-68） | 随 `SkillSnapshotMetadata` 一并标注（§3.3），不入 4 个 TypedDict 的 payload |
| `run_id` | 3-6 | `runtime_state.run_id` 子 key（resume.py:97、run_loop.py:280、service.py:1749） | 并入 `RuntimeStateMetadata` |
| `prompt_activation` | 4-7 | `runtime_policy.prompt_activation` 子结构（policy.py 快照字段 + helpers:136-142 写） | 已被 runtime_policy 版本化覆盖，不在 P1 |
| `pending_tool_intent` / `context_projection` / `context_projection_summary` / `todos` / `acp` / `context_compacted` / `context_transform_applied` | 2-3 | `runtime_state` 子 key（§3.1 逐项） | 并入 `RuntimeStateMetadata` 嵌套 TypedDict |
| 已删除的 `continuity` / `continuity_summary` | 1（context_window.py:541） | 读取即硬报错 | 不属于当前 key-set |
| `prompt` / `raw_prompt_stored` | 3 | prompt 持久化与 session 行共存（storage.py 列 + events.py） | 非 session metadata 语义，不在 P1 |

**C. turn-local state（回合内状态）—— 归属 coordinator，P1 只列不类型化**

| key | 文件数 | 归属 | P1 处置 |
|---|---|---|---|
| `provider_attempt` | 3-4（resume/run_loop/service/provider_execution_metadata） | run_loop 递增（run_loop.py:1186）、resume 读取选图（resume.py:804-810）；顶层 session metadata key | **不类型化**。已有单入口 accessor `provider_attempt_from_metadata`（provider_execution_metadata.py:9-15），已是「该 key 的唯一入口」；顶层 key-set 不扩 |
| `provider_retry_attempt` | 2 | run_loop（run_loop.py:1187），transient retry 计数 | 同上（provider_execution_metadata.py:18-24 已单入口） |
| `abort_requested` | 2 | 请求/回合 abort 信号 | request 类已收敛（§2.1），不动 |
| `keep_alive_turn` | 3 | 内部请求字段，final-step 判定（run_loop.py:1870） | request 类已收敛，不动 |
| `resume_kind` | 2（resume/service） | resume 流程分类（resume.py:1432） | 不类型化，不入 key-set |
| `_prompt_activation_this_run` / `activated_this_turn` | 2-3 | prompt 装配回合内暂存，session.py:219 持久化时 `pop` | 明确**不入 persisted key-set**，永不持久化 |
| `approval_request_id` / `question_request_id` / `request_id` | 5-9 | 跨 plan_state + pending approval 行 + 事件三处共存（pending interaction 关联） | plan_state 内的副本随 `PlanStateMetadata` 类型化；行/事件侧不动 |

**D. observability（可观测性）—— 事件/debug 面，P1 只列不类型化**

| key | 文件数 | 现状 | P1 处置 |
|---|---|---|---|
| `error_kind` / `error_summary` / `error_details` / `retry_guidance` | 3-6 | `runtime.failed` 事件 payload + debug 快照；构造集中 chunk_builders.py:60-83 `with_runtime_failure_details` | 不类型化；事件 payload 类型化是独立议题（审计 §3.6 事件类型注册），不在 P1 |
| `provider_error_kind` / `provider_error_details` | 2-5 | provider-fallback 事件 payload（provider_fallback.py:128-165） | 不类型化 |
| `provider_usage` | 2 | 顶层 session metadata（latest/cumulative/turn_count），**已有单入口** `session_with_provider_usage_metadata`（provider_execution_metadata.py:56-96） | 不类型化（已有入口集中），保持 |
| `policy_observations` | 1（session.py:244） | session.py 持久化时注入 | 不类型化 |
| `matched_rule` / `matched_rule_ids` / `precedence_trace` / `policy_surface` / `operation_class` / `path_scope` / `tool_policy` / `denied` | 2-5 | 权限决策事件/debug 面 | 不类型化 |

### 2.2 归属判定要点

- **四类之间不可推断迁移**（refactor-plan Metadata Ownership 表）：request-only 字段（`skills`、`force_load_skills`、`provider_stream`）不得注入为 persisted 内部状态；turn-local（`provider_attempt`）不得混入 `RuntimeStateMetadata` key-set。`runtime_state` 的 `run_id` 属 persisted facts（run 标识快照，resume 需要跨回合读取），不属 turn-local。
- `context_window` 的处置：其 payload 结构大（16 字段，context_window.py:227-260）且 owner（context_window.py）已承担序列化职责，P1 不重复收敛；但**它在 `session_with_context_window_payload_metadata`（helpers:117-160）中的写入必须经 helpers 走 typed `runtime_state` 构造**（§5 Phase 2），这是 P1 的边界接触点。
- 76 key 中大量事件 payload / storage 行列 key（`text_char_count`、`idle_episode_id`、`pending_approval_*`、`cancel_requested_at` 等）语义上**不属于 session.metadata**，本次分类不含（§7 非目标），避免把事件契约/存储 schema 拖进 metadata 类型化。

---

## 3. 四个 TypedDict 的精确字段定义（草案）

### 3.1 `RuntimeStateMetadata`（persisted `runtime_state`）

**实际写入点证据**（当前无类型 dict 里存了什么）：

| 字段 | 类型 | 写入点 | 读取点 |
|---|---|---|---|
| `run_id` | `str \| None` | service.py:1749 `_runtime_state_metadata(run_id=...)`（5919-5943）；resume.py:96-98 `_metadata_with_resume_run_id` | run_loop.py:280-301/3198-3201/3250；provider_execution_metadata.py:27-33；active_session.py |
| `acp` | `dict`（见 `AcpStateMetadata`） | service.py:5922-5941；helpers:305-315 `_runtime_state_metadata_with_acp_state` | helpers `session_with_current_acp_metadata`；service `_acp_*` |
| `context_projection` | `dict`（ContextProjection payload，version:1） | helpers:153（`session_with_context_window_payload_metadata`） | context_window.py:545-556；helpers:283-292；storage.py todo 投影 |
| `context_projection_summary` | `dict`（`{anchor, source}`） | helpers:154 | context_window.py:548-553 |
| `todos` | `dict`（`todo_state_payload`，version:1） | helpers:169-174；storage.py:1269/3257 | todos.py:157-161；helpers:194-205；run_loop |
| `pending_tool_intent` | `dict`（`ToolExecutionIntent.metadata_payload()`） | helpers:340-341 `persist_tool_execution_intent` | service.py:4204-4213；helpers:368-377 |
| `context_compacted` | `dict`（`{last_summary_anchor, last_original_tool_result_count, last_retained_tool_result_count, last_emitted_run_id}`） | run_loop.py:3275-3280 | run_loop.py:3252-3271 |
| `context_transform_applied` | `dict`（`{last_emitted_fingerprints, last_emitted_run_id}`） | run_loop.py:302-318 | run_loop.py:277-287（`_unseen_context_transform_payloads`） |
| 已删除的 `continuity` / `continuity_summary` | —— | 已不写，读取即硬报错 | 不属于当前 key-set |

**TypedDict 草案**（放 contracts.py）：

```python
class AcpStateMetadata(TypedDict, total=False):
    mode: str
    configured_enabled: bool
    status: str
    available: bool
    last_error: str | None
    last_request_type: str | None
    last_request_id: str | None
    last_event_type: str | None
    last_delegation: dict[str, object] | None  # AcpDelegationPayload（acp.py:68-69 as_payload）


class PendingToolIntentMetadata(TypedDict, total=False):
    tool_call_id: str
    tool_name: str
    arguments: dict[str, object]
    replay_policy: str  # Literal["safe", "never"]，tool_replay.py
    status: str  # Literal["pending", "completed"]


class TodosStateMetadata(TypedDict, total=False):
    version: int  # 恒 1
    revision: int
    todos: list[dict[str, object]]
    summary: dict[str, object]


class ContextProjectionMetadata(TypedDict, total=False):
    version: int
    projection_id: str | None
    source_event_sequence: int
    # ...（结构由 context_window.py ContextProjection.metadata_payload 定义，见 context_window.py:121-）


class ContextCompactedStateMetadata(TypedDict, total=False):
    last_summary_anchor: str | None
    last_original_tool_result_count: int
    last_retained_tool_result_count: int
    last_emitted_run_id: str | None


class ContextTransformAppliedStateMetadata(TypedDict, total=False):
    last_emitted_fingerprints: list[str]
    last_emitted_run_id: str | None


RUNTIME_STATE_METADATA_KEYS = frozenset(
    {
        "run_id",
        "acp",
        "context_projection",
        "context_projection_summary",
        "todos",
        "pending_tool_intent",
        "context_compacted",
        "context_transform_applied",
    }
)


class RuntimeStateMetadata(TypedDict, total=False):
    run_id: str
    acp: AcpStateMetadata
    context_projection: ContextProjectionMetadata
    context_projection_summary: dict[str, str]
    todos: TodosStateMetadata
    pending_tool_intent: PendingToolIntentMetadata
    context_compacted: ContextCompactedStateMetadata
    context_transform_applied: ContextTransformAppliedStateMetadata
```

要点：全部 `total=False` 表示字段可选；但一旦叶子结构存在，parser 严格校验其类型、未知 key 与当前必需字段。`run_id` 可选（新 run 的 runtime_state 可不写该 key）。嵌套 payload 的深度校验委托各自 owner。

### 3.2 `PlanStateMetadata`（persisted `plan_state`）

**实际写入点证据**：helpers `plan_state_from_metadata`（78-111）与 `session_with_plan_state`（215-247）；status 取值来自 run_loop（`completed`/`interrupted`/`waiting`，1214-1216/1871/2394）、resume（`completed`，572）、service（`waiting`/`in_progress`，2345/2446-2459）、chunk_builders（`failed`，27-33）。读取点：context_window.py:1186-1197（`_pending_state_segment`）、helpers:383-393（`waiting_reason_from_session`）。

| 字段 | 类型 | 说明 |
|---|---|---|
| `status` | `str` | 取值 `waiting`/`waiting_approval`/`waiting_question`/`in_progress`/`completed`/`interrupted`/`failed`（枚举见上） |
| `approval_request_id` | `str`（可选） | helpers:96-99 |
| `blocked_tool` | `str`（可选） | helpers:101-104；context_window.py:1193 读 |
| `last_error` | `str`（可选） | helpers:106-109 |

```python
PLAN_STATE_METADATA_KEYS = frozenset({"status", "approval_request_id", "blocked_tool", "last_error"})


class PlanStateMetadata(TypedDict, total=False):
    status: str
    approval_request_id: str
    blocked_tool: str
    last_error: str
```

`status` 在 helpers 构造语义下必填（`session_with_plan_state` 总是带 status 写），但旧数据缺失时读侧默认 `"waiting"`（`waiting_reason_from_session`，helpers:385-393）——故 total=False + 读侧默认值，行为不变。

### 3.3 `SkillSnapshotMetadata`（persisted `skill_snapshot`）

**实际写入点证据**：skills.py `snapshot_payload`（181-185）/`build_skill_execution_snapshot`（131-148）；service.py `build_skill_snapshot`（5305-5349）→ `snapshot_to_session_metadata`（skill_metadata.py:97-104）写入顶层 `skill_snapshot` + 兄弟 key `selected_skill_names`/`applied_skills`。读取点：skill_metadata.py:123-131、resume.py:705-709/1155-1159、storage.py:1392-1412、service.py:1532-1535。

| 字段 | 类型 | 说明 |
|---|---|---|
| `snapshot_version` | `int`（恒 1） | skills.py:188-193 严格校验 |
| `source` | `Literal["run","resume","replay"]` | skills.py:216-219 严格校验 |
| `selected_skill_names` | `list[str]` | skills.py:195-201 |
| `applied_skill_payloads` | `list[dict[str, str]]` | `{name, description, content, prompt_context, execution_notes, source_path}`，skills.py:202-215 |
| `skill_prompt_context` | `str` | skills.py:220-222 |
| `binding_snapshot` | `dict` | 由 `agent_capability_snapshot` 投影（skill_metadata.py:133-167）；**opaque** |
| `snapshot_hash` | `str` | skills.py:183/233-236，重算比对 |

```python
SKILL_SNAPSHOT_METADATA_KEYS = frozenset(
    {"snapshot_version", "source", "selected_skill_names", "applied_skill_payloads", "skill_prompt_context", "binding_snapshot", "snapshot_hash"}
)


class SkillSnapshotMetadata(TypedDict, total=False):
    snapshot_version: int
    source: str
    selected_skill_names: list[str]
    applied_skill_payloads: list[dict[str, str]]
    skill_prompt_context: str
    binding_snapshot: dict[str, object]
    snapshot_hash: str
```

P1 增量：`parse_skill_snapshot_metadata` 在调 `snapshot_from_payload`（skills.py:187）**之前**先做顶层未知 key 拒绝（现状缺口：`snapshot_from_payload` 的 hash 只覆盖 6 个已知字段，payload 里多塞一个未知 key 不会破坏 hash，会静默通过）。**不改** skills.py 的 parser（hash 语义与字节格式是另一契约），只在 helpers 包装层加拒绝。兄弟 key（顶层 `selected_skill_names`/`applied_skills`）随 `snapshot_to_session_metadata` 一并标注，不独立建 TypedDict。

### 3.4 `PersistedDelegationMetadata`（persisted `delegation`）

**回答任务问题：delegation 就是 `RuntimeSubagentRoutingMetadata` 吗？—— 是。** `depth` 和 `remaining_spawn_budget` 已经是 `RuntimeSubagentRoutingMetadata` 的字段（contracts.py:80-81），不存在"另有字段"。持久化副本的完整字段：

| 字段 | 类型 | 写入点 | 说明 |
|---|---|---|---|
| `mode` | `Literal["sync","background"]` | service.py:5855（`_metadata_with_resolved_subagent_route`） | 请求侧校验已严格（contracts.py:249-263） |
| `subagent_type` | `str` | 同上 | task.py:141-147 读 |
| `description` / `command` | `str`（可选） | 同上 | task.py:148-153 读 |
| `depth` | `int` | service.py governance writer | strict parser 校验非负整数 |
| `remaining_spawn_budget` | `int` | service.py governance writer | strict parser 校验非负整数 |
| `selected_preset` | `str` | service.py:5849 | resume 一致性抽查（contracts.py:367-378） |
| `selected_execution_engine` | `str`（恒 `"provider"`） | service.py:5850 | contracts.py:372-378 校验 |
| `parallel_group_id` / `parallel_group_size` | `str` / `int`（可选） | 请求侧透传 | contracts.py:295-305 |

```python
# contracts.py —— 紧邻现有 RuntimeSubagentRoutingMetadata（78-88）
class PersistedDelegationMetadata(RuntimeSubagentRoutingMetadata, total=False):
    """Persisted 形态的 delegation：与请求侧同形；
    depth/remaining_spawn_budget 由 _metadata_with_delegation_governance
    在请求入 session 前解析（service.py:5004-5009），selected_preset/
    selected_execution_engine 由路由解析填充（service.py:5849-5850）。"""
```

独立 parse 的理由仅是区分请求侧 `RuntimeRequestError` 与持久化侧 `ValueError`；两者都严格拒绝未知字段、类型错误和缺失的当前必需字段，不为旧 session 提供宽容路径。

---

## 4. 严格 parse + 版本化方案

### 4.1 parse 函数签名（统一严格）

```python
def parse_runtime_state_metadata(raw: object) -> RuntimeStateMetadata: ...
def parse_plan_state_metadata(raw: object) -> PlanStateMetadata: ...
def parse_delegation_metadata(raw: object) -> PersistedDelegationMetadata: ...
def parse_skill_snapshot_metadata(raw: object) -> SkillSnapshotMetadata: ...
```

四个 parser 对存在的结构执行严格对象、key-set、类型与版本/hash校验。结构整体缺失由调用点按“未启用”处理；非对象值不会被转换成空字典。

---

## 5. `session_metadata_helpers.py` 唯一入口设计

### 5.1 模块职责重组

现状：helpers 是"无类型 helper 集合"（审计 §3.7），已含 plan_state/context_window/todo/delegation/acp/tool-intent 的 15 个函数。目标形态：

```
contracts.py                      # TypedDict 定义（与现有 metadata TypedDict 同居）
  RuntimeStateMetadata / PlanStateMetadata / PersistedDelegationMetadata / SkillSnapshotMetadata
  + 嵌套（AcpStateMetadata 等）+ *_METADATA_KEYS 常量

session_metadata_helpers.py       # 唯一读/写入口
  ├─ re-export 4 个 TypedDict + key-set 常量（保持单 import 面）
  ├─ parse_*_metadata（严格当前格式校验；可选结构缺失由调用点处理）
  ├─ 类型化 accessor（§5.2 只读点）
  └─ 类型化构造器 session_with_*（§5.3 写点；已有 6 个，新增 4 个）
```

**唯一入口的强制规则**：任何模块不得直接 `metadata.get("runtime_state")`/`metadata["plan_state"]`/`metadata["delegation"]`/`metadata["skill_snapshot"]` 之外再手工解析这 4 个结构；读取必须经 helpers accessor/parse，写入必须经 helpers 构造器（写路径 strict 校验内置）。`runtime_config`/`runtime_policy`/`agent_capability_snapshot` 等已有自己的 owner，不并入。

### 5.2 读路径迁移清单（现状 → helpers，行为逐点等价）

| 调用点 | 现状 | 迁移后 |
|---|---|---|
| run_loop.py:278-287（`_unseen_context_transform_payloads`）、298-318（transform applied） | `metadata.get("runtime_state")` + cast | `parse_runtime_state_metadata(session.metadata)` + accessor `runtime_state_context_transform_applied(...)` |
| run_loop.py:3197-3201 / 3248-3251 / 3252-3271 | run_id / context_compacted 手工读 | `runtime_state_run_id(metadata)` / `runtime_state_context_compacted(metadata)` |
| resume.py:94-98（`_metadata_with_resume_run_id`） | 手工 `runtime_state["run_id"] = run_id` | 构造器 `session_with_run_id(session, run_id=...)`（写路径） |
| provider_execution_metadata.py:27-33（`run_id_from_session_metadata`） | 手工 | 委托 helpers accessor（函数保留为薄转发，消去第二份解析） |
| context_window.py:538-556（`_previous_continuity_state`） | 手工取 `runtime_state` + continuity 拒绝 | 读 `parse_runtime_state_metadata`；保留 continuity 硬失败分支 |
| todos.py:157-161（`todo_state_from_session_metadata`） | 手工 | `runtime_state_todos(metadata)` accessor |
| helpers:194-205（`todo_state_matches_payload`） | 手工 | 内部改用 parse + accessor（本身在 helpers，统一实现） |
| service.py:4208-4213（`pending_tool_intent`） | 手工 | `runtime_state_pending_tool_intent(metadata)` accessor |
| service.py:2209-2212（persist_response 清理） | 手工 pop | 构造器 `session_without_tool_intent(session)`（写路径） |
| prompt_assembly.py:277-279 | 手工 | `runtime_state_value(metadata, key)` accessor（`_prompt_activation_this_run` 同层读取，不进 key-set） |
| context_continuity.py:50-58 | 手工剥离 recoverable keys | `parse_runtime_state_metadata` + 明确 key 过滤清单（`_RECOVERABLE_RUNTIME_CONTEXT_KEYS` 与 key-set 关系在 docstring 中声明） |
| storage.py:1174-1178 / 1266-1270 / 3247-3259（todo 投影） | 手工读/写 `runtime_state.todos` | 读走 accessor；**写**改走 helpers `session_with_todo_state`（写路径，消除 storage 第二份 todo 写实现；无 import 环：helpers 对 storage 仅 TYPE_CHECKING，storage→helpers 运行时安全） |
| run_loop.py:465-485（`_provider_attempt_reset`）、resume.py:804-810 | 顶层 `provider_attempt` 手工 | **不迁移**（turn-local，已有单入口 provider_execution_metadata.py，§2.1C） |

### 5.3 写路径迁移清单（新构造必过 strict 闸）

| 构造点 | 现状 | 迁移后 |
|---|---|---|
| service.py:5919-5943（`_runtime_state_metadata`） | 裸 dict 构造 | helpers `runtime_state_metadata_payload(run_id=..., acp_state=...)`（strict 写） |
| service.py:1736-1751（fresh-run session metadata 装配） | `{**session_request_metadata, "runtime_state": ...}` | 只把 `runtime_state` 段换成 helpers 构造器；其余顶层 key 不动（P1 不收敛顶层全量） |
| helpers:119-160（`session_with_context_window_payload_metadata`） | 裸 `{**runtime_state, "context_projection": ...}` | 构造器内置 strict 校验（本项目内自洽） |
| helpers:165-191（`session_with_todo_state`） | 同上 | 同上 |
| helpers:294-316（`_runtime_state_metadata_with_acp_state`） | 同上 | 同上 |
| helpers:332-378（`persist_tool_execution_intent` / `clear_tool_execution_intent`） | 裸写/删 | 走构造器 + strict 写校验 |
| run_loop.py:3275-3280（context_compacted）/ 302-318（transform applied） | 裸写 | 迁入 helpers：`session_with_context_compacted_state(session, ...)` / `session_with_context_transform_applied_state(session, fingerprints=...)`（run_loop 现有私有函数原样搬移，改调 helpers） |
| service.py:4973-5010（`_metadata_with_delegation_governance`） | 请求侧裸 dict 写 `delegation["depth"]` 等 | 构造经 `parse_delegation_metadata(..., strict=True)` 校验后写回（含 depth/remaining_spawn_budget 非负 int 校验） |
| helpers:78-111 / 215-247（`plan_state_from_metadata` / `session_with_plan_state`） | 裸 dict | 构造器内置 strict 校验（status 枚举 + 类型） |
| skill_metadata.py:97-104（`snapshot_to_session_metadata`） | 直接拼 payload | 经 `parse_skill_snapshot_metadata` 校验后输出（顶层未知 key 拒绝） |

**明确不迁的边界**：`session.py:209-248 session_metadata_for_persistence` 是**全量 metadata 的持久化净化层**（脱敏/截断/policy_observations 注入），作用于整个顶层 dict——它继续作为顶层收口，不并入 4 个 TypedDict 的 strict 校验（否则每回合持久化都会对整包未知顶层 key fail）。其与 helpers 的分工：session.py 管"顶层全量安全"，helpers 管"4 个结构的类型契约"。

### 5.4 新增 helper 命名与既有 __all__ 扩充

accessor 命名统一 `runtime_state_<field>(metadata) -> <typed> | None`（`runtime_state_run_id`、`runtime_state_acp`、`runtime_state_todos`、`runtime_state_pending_tool_intent`、`runtime_state_context_compacted`、`runtime_state_context_transform_applied`、`runtime_state_context_projection`）；`plan_state_<field>`、`delegation_<field>`（`delegation_depth`/`delegation_remaining_spawn_budget` 替换现有 `delegation_depth_from_metadata`/`remaining_spawn_budget_from_metadata` 实现，函数名保留避免动 3 处调用点，内部改走 parse）。全部加入 `__all__`（helpers:408-424 现有清单扩充）。

---

## 6. 分阶段落地计划 + 验收

参照 refactor-plan Change Gate 验收模板（contract 显式 / removed fields rejected / fresh+resume+replay+debug+bundle 一致 / 测试覆盖 / 无第二 parser）。P1 范围小，3 个阶段，Phase 1 与 2 可合并执行但不可跳过验收。

### Phase 1 —— 类型定义 + 只读接入（零行为变化，风险最低）

- **内容**：
  1. contracts.py：4 个 TypedDict + 嵌套 TypedDict + 3 个 key-set 常量（§3）；补 `RuntimeRequestMetadata.context_transform_refs: list[str]` 字段标注（§2.1 缺口）。
  2. helpers：`parse_*_metadata`（严格当前格式）+ 全部只读 accessor（§5.2 左栏迁移）。
  3. 迁移 §5.2 的只读调用点；当前恢复安全路径（checkpoint/context projection/pending_messages）保持不变。
- **验收**：
  - targeted metadata/resume/replay/approval 测试通过。
  - parser 对未知 key、类型不符、非对象、旧字段与旧 coercion 输入抛 ValueError。
  - fresh current metadata round-trip 字节稳定。
  - `git diff` 无持久化字节变化（Phase 1 纯读，golden：同一输入 session 的 `update_session_metadata` 载荷前后一致——由测试套件中既有 fixture 隐式覆盖）。
  - Change Gate 第 5 条：全仓搜索确认 `runtime_state`/`plan_state`/`delegation` 的**读**不再有 helpers 之外的第二解析（`grep` 验收）。

### Phase 2 —— 写路径收敛（行为中性，触碰持久化）

- **内容**：
  1. helpers 新增构造器：`runtime_state_metadata_payload`、`session_with_run_id`、`session_with_context_compacted_state`、`session_with_context_transform_applied_state`、`session_without_tool_intent`（run_loop/resume/service 现有实现原样搬移，行为不变）。
  2. 迁移 §5.3 写点（service `_runtime_state_metadata`/fresh-run 装配/`_metadata_with_delegation_governance`、run_loop 两个 state writer、resume run_id、helpers 内部 4 处、storage todo 写）。
  3. 所有写路径构造器内置 `parse(..., strict=True)` 闸。
- **验收**：
  - 同一输入下 persisted 字节与 Phase 1 前**完全一致**（golden：fresh run 快照、todo 更新、approval 等待、context 压缩、tool intent 持久化的 session 行 metadata JSON 前后 diff 为空）。
  - 新增单测：严格拒绝旧 fixture；写路径严格拒绝未知 key 和非法 delegation depth。
  - Change Gate 第 4、5 条：写点全部经 helpers；全仓 grep 无裸 `"runtime_state": {` 构造（除 session.py 净化层外）。

### Phase 3 —— 严格读取开关 + 收尾（可选合并入 2）

- **内容**：
  2. 删除所有遗留宽容分支；恢复安全的可恢复 context 与队列合并逻辑保留。
  3. 文档：key-set 常量 docstring 记录每个 key 的 owner（§2 表作为 docstring 蓝本）。
- **验收**：
  - 新 session 全流程 strict 读绿（fresh→resume→replay→debug→bundle 一致，Change Gate 第 3 条）。
  - 旧 fixture session 在标记关闭下仍可 resume（双模式集成测试）。
  - 全量测试套件绿。

### 风险排序依据

当前实现统一使用严格 parser；不提供未标记存量的 lenient 读取路径。

---

## 7. 边界与非目标

1. **不引入开放式 metadata 控制面**（refactor-plan 已明令禁止）：不新增任何运行时 metadata 配置/注入 API；4 个 TypedDict 只收敛现有键，**不扩展键空间**。
2. **turn-local 不类型化**：`provider_attempt`/`provider_retry_attempt`/`resume_kind` 等保持现状（已有单入口 accessor），不进任何 key-set；`_prompt_activation_this_run`/`activated_this_turn` 明确不持久化。
3. **observability 不类型化**：`error_kind`/`retry_guidance`/`provider_error_kind` 等是事件 payload / debug 快照键，事件契约类型化是独立议题（审计 §3.6 关联），不在 P1。
4. **已版本化结构不重复收**：`runtime_config`/`runtime_policy`/`agent_capability_snapshot`/`resolved_hook_presets` 各有 owner 与严格 parser，P1 不动；`context_window` payload 结构大且有 owner，只做 P1 边界接触（helpers 写时经 typed runtime_state 构造），其自身 TypedDict 化列 follow-up。
5. **事件 payload / storage 行列 key 不在分类内**（`pending_approval_*`、`text_char_count`、`idle_episode_id` 等）：属事件契约/存储 schema，非 session metadata。
6. **不改字节格式**：P1 不新增任何持久化字段（含 version 字段）、不重排、不删除；`skill_snapshot` 的 hash 语义、`snapshot_from_payload` parser 均不动。
7. **`session.py:209-248` 净化层不并入 strict 校验**（§5.3 边界）：顶层全量脱敏/观察注入保持独立，避免每回合持久化对整包未知 key fail fast。
8. **不重写 request 校验面**：`validate_runtime_request_metadata` 等保持 RuntimeRequestError 语义；`PersistedDelegationMetadata` 的 parse 是独立读侧契约，不混用请求侧 validator。

---

*本设计为 design-only：未修改任何代码。所有字段/调用点证据来自 `src/voidcode/runtime/` 只读调查（行号为 2026-08-18 版本）；标 `[推断]` 的仅出现于审计表格未显式列出的 key 分类处。*
