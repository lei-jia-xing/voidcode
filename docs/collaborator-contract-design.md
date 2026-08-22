# Collaborator 契约显式化设计（RuntimeSurface）

> 设计日期：2026-08-17 · 范围：`src/voidcode/runtime/` 三个 collaborator 与 `service.py` 之间的私有访问收敛
> 基准：`docs/runtime-coupling-audit.md`（§4 数据、§5.4 方向）、`docs/runtime-architecture-refactor-plan.md`（规则 7）、`docs/runtime-service-decomposition-plan.md`（原则 5、拆分顺序）
> **design-only**：本设计不修改任何代码；Protocol 签名为草案。所有结论附 `文件:行号` 代码证据；未直接验证处标注 `[推断]`。

---

## 1. 结论先行

### 1.1 最小接口形状

三个 collaborator 的构造注入改为 **显式依赖 + 一个窄 RuntimeSurface Protocol**，不再注入整个 `VoidCodeRuntime` 后穿透 `runtime._xxx`：

| collaborator | 构造注入（新增/变更） | 保留回引 |
|---|---|---|
| `RuntimeBackgroundTaskSupervisor`（background_tasks.py:150） | `session_store: SessionStore`、`workspace: Path`、`config: RuntimeConfig` | `surface: RuntimeSurface`（run 入口 / 配置组合） |
| `RuntimeRunLoopCoordinator`（run_loop.py:478） | 在 `tool_executor` 基础上新增 `session_store: SessionStore`、`workspace: Path`、`config: RuntimeConfig`、`permission_policy: PermissionPolicy`、`acp_adapter: AcpAdapter`、`mcp_manager: McpManager`、`lsp_manager: LspManager` | `surface: RuntimeSurface`（配置 truth / 权限治理 / context 装配） |
| `RuntimeResumeCoordinator`（resume.py:60） | `session_store: SessionStore`、`workspace: Path`、`config: RuntimeConfig`、`permission_policy: PermissionPolicy`、`acp_adapter: AcpAdapter`、`mcp_manager: McpManager`、`background_task_supervisor: RuntimeBackgroundTaskSupervisor`、`run_loop_coordinator: RuntimeRunLoopCoordinator` | `surface: RuntimeSurface`（配置 truth / 工具注册 / MCP 生命周期） |

注入的依赖按已存在类型走：

- **`SessionStore` Protocol（storage.py:174）**：已有 45 方法，直接复用；但需补 2 个缺口方法（见 §2.1），消除 `isinstance(SessionEventAppender)`（background_tasks.py:1522/1637/1726/1807/1973）与 `cast(SqliteSessionStore, ...)`（resume.py:1193）、`getattr(store, "load_session_status", None)`（background_tasks.py:914/2116）这三类"运行时类型补偿"。
- **`Path`**：`runtime._workspace` 的标注类型（service.py:622）。
- **`RuntimeConfig`**：`runtime._config` 的标注类型（service.py:627，config.py:124 导入）。
- **`AcpAdapter` / `McpManager` / `LspManager`**：runtime 已注入的同型对象（service.py:741-752），直接复用。
- **peer collaborator**：resume 需要 `background_task_supervisor`（6 处）与 `run_loop_coordinator`（1 处）——构造注入实例（runtime 构造顺序调整为 supervisor 先行，循环风险分析见 §4）。

### 1.2 收敛后预期剩余私有访问

448 处访问（audit §4：background_tasks 195 / run_loop 118 / resume 135，与本次逐文件计数一致）按四类收敛：

| 类别 | 处数 | 占比 | 收敛方式 |
|---|---|---|---|
| A. 纯数据依赖注入 | 202 | 45% | 构造注入 `SessionStore`/`Path`/`RuntimeConfig` |
| B. 纯函数 / 模块函数收敛 | 179 | 40% | 直连 import 现有模块函数，或迁入新纯模块（`chunk_builders.py` / `hook_runtime.py` / `acp.py` / `mcp.py` / `skills.py` / `session_metadata_helpers.py` / `session.py` / `provider_fallback.py` / `tool_scope.py` / `provider_context.py`） |
| C. 协作者间 peer 注入 | 14 | 3% | resume ← supervisor / run_loop / adapter / manager；run_loop ← adapter / manager / base `PermissionPolicy` |
| D. 治理回引（窄 RuntimeSurface） | **53** | 12% | `runtime._xxx` → `surface.xxx(...)`（public Protocol 方法，18 个） |

**预期终态：`grep -c 'runtime\._\|_runtime\._' background_tasks.py run_loop.py resume.py` ≈ 0；`runtime._xxx` 私有访问 448 → 0，53 处治理访问改为显式 `RuntimeSurface` 方法调用；19 个 proxy 方法从 service.py 全部移除（18 删 + 1 转为 Protocol 实现）。**

---

## 2. 分类收敛表

> 计数为本次代码逐行核验的出现次数（按 `runtime.X` / `self._runtime.X` 两种前缀合计）；三文件合计 448 与 audit §4 完全一致。行号为出现位置首行示例，完整清单见 §4 各 proxy 与阶段计划。

### 2.1 A 类：纯数据依赖 → 构造注入（202 处）

| 成员 | 计数 | 注入类型 | 证据 |
|---|---|---|---|
| `_session_store` | 93（bg 81 / rl 2 / rs 10） | `SessionStore` Protocol（storage.py:174） | bg:249-2605 密集；rl:490/552；rs:1055-1511 |
| `_workspace` | 99（bg 84 / rl 5 / rs 10） | `Path` | bg 各处 `workspace=runtime._workspace`；rl:491/553/805/2086/2804；rs:1056-1512 |
| `_config` | 10（bg 9 / rl 1） | `RuntimeConfig`（service.py:627） | bg:712/884/1876/1904/1919/2203/2214（`config.background_task`、`config.hooks`）；rl:96（`runtime._config.hooks`） |

**随 store 注入顺带补齐的 SessionStore Protocol 缺口（纯增量，无语义变化）**：

| 缺口 | 证据 | 现状补偿 |
|---|---|---|
| `truncate_session_events_after` 不在 Protocol | storage.py:2608 已实现 | resume.py:1193 `cast(SqliteSessionStore, runtime._session_store).truncate_session_events_after(...)` |
| `load_session_status` 不在 Protocol | storage.py:3061 已实现 | background_tasks.py:914-922、2115-2120 `getattr(store, "load_session_status", None)` |
| `append_session_event`（单事件）只在 `SessionEventAppender`（storage.py:385） | — | background_tasks.py 5 处 `isinstance(..., SessionEventAppender)` 运行时检查（1522/1637/1726/1807/1973）——注入 `SessionStore` 后，`SessionEventAppender` 检查保留（Protocol 语义不变），但对象由注入提供 |

### 2.2 B 类：方法调用 → 纯函数 / 模块函数（179 处）

| 成员 | 计数 | 收敛方式 | 目标符号 / 模块 | 证据 |
|---|---|---|---|---|
| `_failed_chunk` | 33（rl） | **chunk 构造纯函数**（新模块 `chunk_builders.py`） | `failed_chunk(*, session, sequence, error, payload=None)`；依赖 `session_with_plan_state`（→ session_metadata_helpers.py，service.py:975 移出）、`with_runtime_failure_details`（service.py:2725 静态移出） | rl:113/606/704/720/748/895/929/983/1042/1080/1204/1247/1399/1449/1559/1570/1683/1699/1710/1761/1841/1874/1894/1911/2041/2176/2210/2290/2430/2468/2471/2877/2936/2974 |
| `_lifecycle_hook_failure_chunk` | 6（rs） | 同上（合并 run_loop.py 本地副本 `_hook_failure_chunk` 99-113 与 service.py:2611 三份重复） | `lifecycle_hook_failure_chunk(*, session, sequence, surface, error)` + `hook_failures_are_fatal(hooks)` | rs:401/436/896/929/1351/1387 |
| `_run_id_from_session_metadata` | 9（rl） | 直连 import | `provider_execution_metadata.run_id_from_session_metadata`（provider_execution_metadata.py:27；service.py:4702 已是薄转发） | rl:611/988/1047/1454/1766/2295/2435/2882/2941 |
| `_run_tool_hooks` | 6（rl） | **hook 包装共享化**（新模块 `hook_runtime.py`） | `run_tool_hooks_for_session(hooks, workspace, session, tool_name, phase, recursion_env_var, sequence, policy) -> RuntimeHookOutcome`（`RuntimeHookOutcome` 由 service.py:7360 `_RuntimeHookOutcome` 提升改名）；底层 `run_tool_hooks` 已在 hook/executor.py:95 | rl:724/1056/2017/2444/2729/2950 |
| `_run_lifecycle_hooks` | 8（rl 2 + rs 6） | 同上 | `run_lifecycle_hooks_for_session(...)`（底层 `run_lifecycle_hooks` hook/executor.py:203） | rl:1180/1223；rs:390/425/882/918/1337/1376 |
| `_hook_execution_policy_from_metadata` | 1（bg） | 纯函数 | `hook_execution_policy_from_metadata(metadata) -> HookExecutionPolicy`（service.py:2669 移出；`runtime_mode_from_metadata`+`runtime_read_only_from_metadata` 已在 mode.py:99） | bg:2232 |
| `_hook_recursion_env_var` | 1（bg） | 常量 | `HOOK_RECURSION_ENV_VAR = "VOIDCODE_RUNNING_TOOL_HOOK"`（service.py:660 类常量移入 hook_runtime.py） | bg:2218 |
| `_session_routing_for_request` | 4（bg） | 直连 import | `execution_seams.resolve_runtime_session_routing`（execution_seams.py:65；proxy 在 service.py:1159） | bg:1042/2343/2378/2404 |
| `_append_parent_acp_delegated_lifecycle_event` | 3（bg） | **ACP 事件纯函数**（acp.py） | `append_parent_acp_delegated_lifecycle_event(appender, task, ...)` + `delegated_execution_for_task(task, ...)`（service.py:1640 移出，纯函数：仅读 task 字段） | bg:1573/1768/1847；service.py:1725 |
| `_publish_delegated_acp_event` | 3（bg） | 同上 | `publish_delegated_acp_event(adapter, task, ...)`（service.py:1695 移出；`AcpAdapter.publish` + `current_state()` 已有） | bg:1579/1774/1853 |
| `_tool_policy_error` | 3（rl） | 纯函数 | `tool_scope.tool_policy_error(decision)`（service.py:1418 静态移出） | rl:702/1892/2647 |
| `_delegation_depth_from_metadata` / `_remaining_spawn_budget_from_metadata` | 3+3（rl） | 纯函数 | `session_metadata_helpers.delegation_depth_from_metadata` / `remaining_spawn_budget_from_metadata`（service.py:5730/5740 静态移出） | rl:818/819/2099/2100/2817/2818 |
| `_session_with_plan_state` | 4（rl 3 + rs 1） | 纯函数 | `session_metadata_helpers.session_with_plan_state`（service.py:975 移出；其依赖 `_plan_state_from_metadata` 已是纯 proxy → session_metadata_helpers.py:46、`_session_with_metadata` 纯静态 service.py:916） | rl:1145/1793/2310；rs:417 |
| `_provider_attempt_from_metadata` | 4（rl 2 + rs 2） | 直连 import | `provider_execution_metadata.provider_attempt_from_metadata`（provider_execution_metadata.py:9） | rl:1117/1535；rs:643/1271 |
| `_provider_retry_attempt_from_metadata` | 2（rl） | 直连 import | `provider_execution_metadata.provider_retry_attempt_from_metadata`（:18） | rl:1118/1777 |
| `_session_with_context_window_metadata` / `_session_with_context_window_payload_metadata` / `_session_with_todo_state` / `_session_with_provider_usage_metadata` | 1+4+1+1 | 直连 import | `session_metadata_helpers.py:82/128`、`provider_execution_metadata.py:43` | rl:1300/1337/2414/1773；rs:314/615/1238 |
| `_continuity_state_from_session_metadata` | 2（rs） | 纯函数 | `session_metadata_helpers.continuity_state_from_session_metadata`（service.py:6060 移出，内部调 `context_window.continuity_state_from_metadata_payload` :449） | rs:358/1309 |
| `_persist_tool_execution_intent` / `_clear_tool_execution_intent` | 2+2（rl） | 纯函数（注入 store+workspace） | `session_metadata_helpers.persist_tool_execution_intent(store, workspace, session, intent)` / `clear_tool_execution_intent(store, workspace, session)`（service.py:4757/4773 移出，体只读 metadata + `store.update_session_metadata`） | rl:768/1038/2777/2932 |
| `_fallback_graph_selection` | 1（rl） | 直连 import（调用方本地组合 config/chain） | `execution_seams.fallback_graph_for_provider_error`（:148）；rl 已有 `effective_runtime_config_from_metadata` 访问 → `config.resolved_provider.target_chain` 替代 proxy 内的 `_provider_chain_for_session_metadata` | rl:1538；service.py:1144 |
| `_provider_transient_retry_config` | 1（rl） | 纯函数 | `provider_fallback.provider_transient_retry_config(providers, provider_name)`（service.py:5772 移出；`DEFAULT_PROVIDER_TRANSIENT_RETRY_CONFIG` 已在 provider_fallback） | rl:1543 |
| `_renumber_events` | 1（rl） | 直连 import | `event_envelopes.renumber_events`（event_envelopes.py:165） | rl:1803 |
| `_graph_selection_for_effective_config` | 3（rl 1 + rs 2） | 直连 import | `execution_seams.select_graph_for_effective_config`（:128） | rl:2491；rs:646/1273 |
| `_envelopes_for_acp_events` / `_envelopes_for_mcp_events` / `_envelopes_for_lsp_events` | 1+1+1（rl） | 直连 import | `event_envelopes.py:69/124/37` | rl:3215/3229/3242 |
| `_reasoning_capture_state` + `_reasoning_output_diagnostic` | 1+1（rl） | **turn-local 观测移入 run_loop.py**（Metadata Ownership：turn-local/observability 归 run/resume coordinator） | `_ReasoningCaptureState`（service.py:6095 proxy）与 diagnostic（:6099）移入 run_loop.py；`_metadata_for_provider_model`（service.py:3750）由注入的 `RuntimeProviderCatalogQuery.metadata_for_model` 提供 | rl:1119/1818 |
| `_validate_session_workspace` | 8（bg 2 + rs 6） | 纯函数 | `session.validate_session_workspace(session, session_id, workspace)`（service.py:7146 移出，体仅比较 `metadata["workspace"]` 与 `str(workspace)`） | bg:1343/1362；rs:251/573/1202/1213/1481/1510 |
| `_prompt_from_events` | 1（bg） | 直连 import | `runtime_debug.prompt_from_events`（runtime_debug.py:192） | bg:1301 |
| `_approval_request_id_from_waiting_response` | 1（bg） | 直连 import | `permission_policy.approval_request_id_from_waiting_response`（permission_policy.py:181） | bg:1719 |
| `_waiting_reason_from_session` / `_resume_waiting_reason` | 3+1（rs） | 纯函数 | `session_metadata_helpers.waiting_reason_from_session(session)`（service.py:5292 移出）+ `resume_waiting_reason(response)`（service.py:5280 移出，依赖 `permission_policy.pending_approval_from_response` :275） | rs:887/1342、389 |
| `_reload_persisted_session` | 2（bg 1 + rs 1） | 纯函数 | `reload_persisted_session(store, workspace, session_id)`（= `store.load_session(...).session`，service.py:1014 移出） | bg:2547；rs:480 |
| `_validate_reasoning_effort_capability` | 3（rs） | 纯函数 | `validate_reasoning_effort_capability(config)`（service.py:1197 移出，体仅读 config + `provider_supports_reasoning_effort`） | rs:293/592/1217 |
| `_skill_prompt_context_for_assembly` | 3（rs） | 纯函数 | `skills.skill_prompt_context_for_assembly(skill_registry, applied_context, selected_skill_names)`（service.py:1390 移出，依赖 `_catalog_skill_context`/`loaded_skill_names`（skill_metadata.py:24）一并迁入） | rs:308/609/1232 |
| `_skill_binding_mismatch_payload` | 1（rs） | 纯函数 | `skills.skill_binding_mismatch_payload(expected, actual)`（service.py:6426 移出） | rs:552 |
| `_permission_policy_for_session` | 3（rs） | 直连 import（base policy 注入） | `permission_policy.permission_policy_for_session(base_policy=<注入>, metadata=...)`（permission_policy.py:149；proxy 在 service.py:6916） | rs:357/811/1308 |
| `_session_with_current_acp_metadata` | 4（rs）+1（rl） | 纯函数（acp_state 参数化） | `session_metadata_helpers.session_with_current_acp_metadata(session, acp_state)`（service.py:949 移出；`_runtime_state_metadata_with_acp_state` :949-971 亦纯） | rs:574/702/708/1214；rl:3220 |
| `_disconnect_acp_for_session_state` | 3（rs） | acp.py 模块函数 | `disconnect_acp_for_session_state(adapter, session)`（service.py:1010 移出） | rs:383/881/1336 |
| `_emit_acp_events` / `_emit_current_acp_drain` / `_finalize_run_acp` | 3+1+3（rs） | acp.py 模块函数 | `emit_acp_events(adapter, session, start_sequence, acp_events)` / `emit_current_acp_drain(adapter, session, start_sequence)` / `finalize_run_acp(adapter, session, sequence)`（service.py:1022/1050/1086 移出） | rs:766/820/868、697、413/908/1363 |
| `_release_mcp_session_events` | 3（rs） | mcp.py 模块函数 | `release_mcp_session_events(mcp_manager, session_id, start_sequence)`（service.py:1534 移出；`envelopes_for_mcp_events` 直连） | rs:447/940/1398 |
| `_validate_pending_approval_matches_recorded_request` / `_validate_pending_question_matches_recorded_request` | 1+1（rs） | **移入 resume.py**（resume 领域校验，proxy 在 service.py:5110/5158） | resume.py 模块函数；依赖 `permission_policy.request_event_and_resolution_state`（:278 直连） | rs:1491/1520 |
| `_provider_context_policy_decision_for_graph_request` | 1（rl） | 纯决策 `[推断]` | `provider_context.provider_context_policy_decision_for_graph_request(graph_request, effective_config)`（service.py:4581 移出；依赖 `_provider_context_snapshot_for_assembled_context` :4550 亦为纯投影 `[推断]`，需在实施时确认其无 runtime 状态读取） | rl:1374 |

### 2.3 C 类：协作者间 peer 注入（14 处）

| 成员 | 计数 | 注入类型 | 证据 |
|---|---|---|---|
| `_background_task_supervisor` | 6（rs） | 注入 `RuntimeBackgroundTaskSupervisor` 实例；只调其 **public** `finalize_background_task_from_session_response`（background_tasks.py:2027） | rs:102/176/1086/1115/1331/1417 |
| `_run_loop_coordinator` | 1（rs） | 注入 `RuntimeRunLoopCoordinator`；调 public `execute_approved_tool_call`（run_loop.py:662） | rs:752 |
| `_permission_policy` | 2（rl） | 注入 base `PermissionPolicy`（service.py:629 字段） | rl:1115/2706 |
| `_acp_adapter` / `_mcp_manager` / `_lsp_manager` | rl 3 + rs 2 | 注入 `AcpAdapter` / `McpManager` / `LspManager`（runtime 同款注入对象） | rl:3218/3232/3245；rs:576/695 |

### 2.4 D 类：合理保留 —— 窄 RuntimeSurface 治理回引（53 处）

| 成员 | 计数 | 保留理由 | 证据 |
|---|---|---|---|
| `_effective_runtime_config_from_metadata` | 12（rl 6 + rs 6） | 配置 truth 组合（181 行）：读 `_config`/`_agent_registry`/`_model_provider_registry`/`_context_window_config_override` 并对 agent 做 registry 校验；decomposition plan §5 明确"runtime config truth 保留在 service.py" | rl:758/801/1372/1857/2051/2762；rs:291/575/586/647/800/1215；service.py:6933 |
| `_resolve_permission` | 3（rl） | 权限主路径（133 行）：`_permission_engine.evaluate` + `resolve_permission` + outcome；原则 1"runtime 统一持有治理" | rl:1967/1976/2700；service.py:2820 |
| `_approval_resolution_outcome` | 3（rl） | 审批结果事件（`runtime.approval_resolved`）构造，approval 治理 | rl:676/1945/1960；service.py:2954 |
| `_tool_policy_denial` | 3（rl） | 工具策略治理：`_tool_scope_resolver.denial` + registry 组合 | rl:697/1887/2636；service.py:1423 |
| `_delegation_tool_policy_error` | 2（rl） | child preset 治理：`_agent_registry.executable_subagent_ids()` + resolver | rl:1868/2620；service.py:1433 |
| `_prepare_provider_context_window` | 5（rl 2 + rs 3） | context 装配编排（decomposition plan：装配编排留在 service） | rl:1262/1269；rs:322/623/1246；service.py:5831 |
| `_assemble_provider_context` | 4（rl 1 + rs 3） | 同上；还依赖 `_hook_preset_context_from_metadata`（读 resolved hook presets runtime 状态，service.py:6454） | rl:1318；rs:304/605/1228；service.py:5956 |
| `_tool_registry_for_effective_config` / `_skill_registry_for_effective_config` / `_build_skill_snapshot` / `_provider_tool_definitions` | 3+3+3+3（rs） | 工具/技能注册组合（runtime 治理：materializer/resolver/registry） | rs:296/595/1220、297/596/1221、298/598/1222、321/622/1245；service.py:1362/1236/6190/1372 |
| `_graph_for_session_metadata` | 3（rs） | graph 选择含 `_graph_override`/`_graph_cache` runtime 状态 | rs:345/644/1269；service.py:7119 |
| `_refresh_mcp_tools_for_session` | 1（rs） | MCP 生命周期门面（runtime 拥有 refresh 时机） | rs:581；service.py:1277 |
| `_should_skip_mcp_startup_for_request` | 1（rs） | 读 `_mcp_manager_is_injected`/`_graph_override` | rs:577；service.py:1321 |
| `_runtime_config_for_request` | 1（bg） | 配置组合（request override 决策） | bg:694；service.py:1163 |
| `_run_with_persistence` | 1（bg） | run 入口（supervisor 派发 worker 的合法回引） | bg:2448；service.py:1766 |
| `_persist_response` | 1（bg） | 持久化组合（terminal 时清 `pending_tool_intent` + `store.save_run`） | bg:2158；service.py:2759 |
| `_tool_registry = _base_tool_registry` 赋值 | 1（rs） | runtime 状态变更方法化（不能外移） | rs:590 → `surface.reset_tool_registry_to_base()`（见 §3） |

---

## 3. RuntimeSurface Protocol 定义（草案）

新模块 `src/voidcode/runtime/runtime_surface.py`（或放 service.py 顶部，`[推断]`：独立模块便于三个 collaborator 在 TYPE_CHECKING 下 import，避免任何运行时环）。`VoidCodeRuntime` 实现该 Protocol；collaborator 构造参数从 `runtime: VoidCodeRuntime` 改为 `surface: RuntimeSurface`。

方法集 18 个，每个方法 = §2.4 一行：

```python
class RuntimeSurface(Protocol):
    # --- config truth（读 _config / registries / graph override 状态） ---
    def effective_runtime_config_from_metadata(self, metadata: dict[str, object] | None) -> EffectiveRuntimeConfig: ...
    def runtime_config_for_request(self, request: RuntimeRequest) -> EffectiveRuntimeConfig: ...  # supervisor 专用

    # --- 权限 / 工具治理（runtime 统一持有） ---
    def resolve_permission(
        self,
        *,
        session: SessionState,
        tool: ToolDefinition,
        tool_instance: Tool,
        tool_call: ToolCall,
        permission_policy: PermissionPolicy | None,
        sequence: int,
    ) -> PermissionOutcome: ...  # PermissionOutcome 由 service._PermissionOutcome (7351) 提升
    def approval_resolution_outcome(
        self, *, session: SessionState, pending: PendingApproval, decision: PermissionResolution, sequence: int
    ) -> PermissionOutcome: ...
    def tool_policy_denial(self, *, session: SessionState, tool_name: str) -> ToolPolicyDecision | None: ...
    def delegation_tool_policy_error(self, *, session: SessionState, tool_name: str) -> str | None: ...

    # --- context 装配编排（service 保留组合点） ---
    def prepare_provider_context_window(
        self,
        *,
        prompt: str,
        tool_results: tuple[ToolResult, ...],
        session_metadata: dict[str, object],
        policy: ContextWindowPolicy | None = None,
        abort_signal: ProviderAbortSignal | None = None,
    ) -> RuntimeContextWindow: ...
    def assemble_provider_context(
        self,
        *,
        prompt: str,
        tool_results: tuple[ToolResult, ...],
        session_metadata: dict[str, object],
        skill_prompt_context: str = "",
        replayed_conversation_segments: tuple[RuntimeContextSegment, ...] = (),
    ) -> RuntimeAssembledContext: ...

    # --- 工具 / 技能注册组合（resume 专用） ---
    def tool_registry_for_effective_config(
        self, effective_config: EffectiveRuntimeConfig, metadata: dict[str, object] | None = None
    ) -> ToolRegistry: ...
    def skill_registry_for_effective_config(self, effective_config: EffectiveRuntimeConfig) -> SkillRegistry: ...
    def build_skill_snapshot(
        self, skill_registry: SkillRegistry, *, metadata: dict[str, object], agent: RuntimeAgentConfig | None
    ) -> SkillExecutionSnapshot: ...
    def provider_tool_definitions(self, tool_registry: ToolRegistry, effective_config: EffectiveRuntimeConfig) -> tuple[ToolDefinition, ...]: ...

    # --- graph 选择（读 _graph_override / _graph_cache） ---
    def graph_for_session_metadata(self, metadata: dict[str, object] | None) -> RuntimeGraph: ...

    # --- MCP 生命周期（resume 专用，runtime 拥有 refresh/启动判定） ---
    def should_skip_mcp_startup_for_request(self, *, request_metadata: Mapping[str, object], effective_config: EffectiveRuntimeConfig) -> bool: ...
    def refresh_mcp_tools_for_session(
        self, *, session: SessionState, sequence: int, failure_kind: str
    ) -> tuple[tuple[RuntimeStreamChunk, ...], SessionState, int, RuntimeStreamChunk | None]: ...
    def reset_tool_registry_to_base(self) -> None: ...  # 替代 resume.py:590 的字段赋值

    # --- run 入口 / 持久化（supervisor 专用） ---
    def run_with_persistence(self, request: RuntimeRequest, *, allow_internal_metadata: bool = False) -> Iterator[RuntimeStreamChunk]: ...
    def persist_response(self, *, request: RuntimeRequest, response: RuntimeResponse) -> None: ...
```

设计约束：

- **方法即现有私有方法**：每个方法体与现 `_xxx` 一一对应（纯重命名 + 删下划线），不重排内部逻辑 → 行为字节级不变。
- **不暴露属性**：Protocol 无 `session_store`/`workspace`/`config` 等属性——数据依赖一律构造注入（§2.1），surface 只承载"需要 runtime 内部组合状态"的调用。
- **类型提升**：`_PermissionOutcome`（service.py:7351）→ `PermissionOutcome`（permission.py 或 contracts.py）；`_RuntimeHookOutcome`（service.py:7360）→ `RuntimeHookOutcome`（hook_runtime.py）。
- **越小越好原则**：凡能参数化为纯函数的（§2.2 的 179 处）一律不进 Protocol；本 Protocol 只保留 §2.4 的 53 处治理调用。

---

## 4. 19 个 proxy 方法删除清单

> 19 个 proxy 均为 service.py 中带 `# Referenced via extracted collaborators` 注释的方法（audit §4 行号）。其中 18 个是"薄转发/应外移逻辑"，1 个（`_resolve_permission`）是真实治理方法（注释在方法体内 2847），转为 §3 Protocol 实现。

| # | service.py 行 | proxy | 转发目标（模块:行） | 需迁移的 collaborator 调用方 | 循环依赖风险 |
|---|---|---|---|---|---|
| 1 | 1018 | `_resequence_event` | `event_envelopes.resequence_event`（:27） | resume.py ×9（421/669/674/716/721/792/846/915/1370）；service.py:2465 内部同步改直连 | 无（event_envelopes 不 import service） |
| 2 | 1138 | `_graph_selection_for_effective_config` | `execution_seams.select_graph_for_effective_config`（:128） | run_loop.py:2491；resume.py:646/1273 | 无 |
| 3 | 1151 | `_fallback_graph_selection` | `execution_seams.fallback_graph_for_provider_error`（:148）；调用方本地组合 `provider_chain=config.resolved_provider.target_chain` | run_loop.py:1538 | 无 |
| 4 | 1160 | `_session_routing_for_request` | `execution_seams.resolve_runtime_session_routing`（:65） | background_tasks.py ×4（1042/2343/2378/2404）；service.py:5755 `_resolve_session_id` 内部改直连 | 无 |
| 5 | 1695 | `_publish_delegated_acp_event` | 新 `acp.publish_delegated_acp_event(adapter, ...)` + `acp.delegated_execution_for_task`（service.py:1640 移出） | background_tasks.py ×3（1579/1774/1853） | 无（acp.py 不 import service；注入 adapter） |
| 6 | 1725 | `_append_parent_acp_delegated_lifecycle_event` | 新 `acp.append_parent_acp_delegated_lifecycle_event(appender, ...)` | background_tasks.py ×3（1573/1768/1847） | 无 |
| 7 | 2633 | `_run_tool_hooks` | 新 `hook_runtime.run_tool_hooks_for_session(...)`（底层 `hook/executor.run_tool_hooks` :95） | run_loop.py ×6（724/1056/2017/2444/2729/2950） | 无（hook_runtime 不 import service；config/workspace 参数化） |
| 8 | 2846 | `_resolve_permission` | **保留**：转 RuntimeSurface 方法（§3）；方法体 133 行是真实治理，非薄 proxy | run_loop.py ×3（1967/1976/2700） | 无（经 Protocol，非 import） |
| 9 | 5110 | `_validate_pending_approval_matches_recorded_request` | 移入 resume.py 模块函数（依赖 `permission_policy.request_event_and_resolution_state` :278） | resume.py:1491 | 无（resume.py import permission_policy，纯模块） |
| 10 | 5158 | `_validate_pending_question_matches_recorded_request` | 移入 resume.py 模块函数 | resume.py:1520 | 无 |
| 11 | 5758 | `_prompt_from_events` | `runtime_debug.prompt_from_events`（:192） | background_tasks.py:1301 | 无 |
| 12 | 5763 | `_provider_attempt_from_metadata` | `provider_execution_metadata.provider_attempt_from_metadata`（:9） | run_loop.py:1117/1535；resume.py:643/1271；service.py:5841/5979 内部改直连 | 无 |
| 13 | 6062 | `_continuity_state_from_session_metadata` | `session_metadata_helpers.continuity_state_from_session_metadata`（service.py:6060 移出，调 `context_window.continuity_state_from_metadata_payload` :449） | resume.py:358/1309；service.py:6041 内部改直连 | 无 |
| 14 | 6095 | `_reasoning_capture_state` | `_ReasoningCaptureState` 移入 run_loop.py（turn-local） | run_loop.py:1119 | 无 |
| 15 | 6104 | `_reasoning_output_diagnostic` | 移入 run_loop.py；`_metadata_for_provider_model`（service.py:3750）→ 注入 `RuntimeProviderCatalogQuery.metadata_for_model` | run_loop.py:1818 | 无 |
| 16 | 6145 | `_renumber_events` | `event_envelopes.renumber_events`（:165） | run_loop.py:1803 | 无 |
| 17 | 6426 | `_skill_binding_mismatch_payload` | `skills.skill_binding_mismatch_payload`（service.py:6426 移出） | resume.py:552 | 无 |
| 18 | 6916 | `_permission_policy_for_session` | `permission_policy.permission_policy_for_session`（:149），`base_policy` 构造注入 | resume.py ×3（357/811/1308） | 无 |
| 19 | 6921 | `_approval_request_id_from_waiting_response` | `permission_policy.approval_request_id_from_waiting_response`（:181） | background_tasks.py:1719 | 无 |

**循环依赖结论**：所有 proxy 的转发目标都是**不 import service.py 的纯模块**（execution_seams / event_envelopes / permission_policy / provider_execution_metadata / runtime_debug / skills / session_metadata_helpers / hook_runtime / acp / mcp）或**注入实例**。service.py 保留 import collaborator + 这些模块（现有 hub-and-spoke 方向不变，audit §4 已确认无运行时环）。唯一需注意的构造顺序：resume 注入 `background_task_supervisor` / `run_loop_coordinator` 实例，runtime `__init__`（service.py:748-752）把 supervisor 构造移到 resume 之前（纯构造顺序调整，无环）；resume.py 对二者的类型仅 TYPE_CHECKING import（沿用现有模式 resume.py:30）。

**删除后 service.py 内部同步**：上述 proxy 若被 service 自身调用（如 `_resequence_event` 2465、`_provider_attempt_from_metadata` 5841/5979、`_continuity_state_from_session_metadata` 6041、`_validate_session_workspace` 3269-3337/7163、`_run_lifecycle_hooks` 2230/2492/2521、`_lifecycle_hook_failure_chunk` 2241/2436/2493/2522、`_failed_chunk` 1070/2624、`_session_with_plan_state` 2685/2897/2998/3010、`_approval_resolution_outcome` 2889 等），统一改为直连新模块函数或 Protocol 实现体，**一处实现、处处直调**。

---

## 5. 分阶段落地计划（按风险排序）

> 每阶段验收 = 行为不变 + 指定测试全绿。验证矩阵沿用 decomposition plan：`tests/unit/runtime/test_runtime_service_extensions.py`（background/resume/approval/fallback/tool_policy/workflow）、`tests/unit/tools/test_background_task_tools.py`、`tests/unit/interface/test_cli_delegated_parity.py`，合并前 `mise run check`。测试文件均已存在（tests/unit/runtime/、tests/unit/interface/、tests/unit/tools/）。

### Phase 1 —— 纯数据注入（session_store / workspace / config）

- **收敛**：§2.1 的 202 处（bg 174 + rl 8 + rs 20）。
- **新增**：三个 collaborator 构造参数（§1.1）；`SessionStore` Protocol 补 `truncate_session_events_after`（storage.py:2608 签名）与 `load_session_status`（storage.py:3061 签名）——纯协议补齐，供 resume.py:1193 / background_tasks.py:914,2116 使用。
- **删除**：全部 `runtime._session_store` / `runtime._workspace` / `runtime._config` 访问。
- **验收**：三个文件 `runtime._` 计数下降 202；上述 runtime 测试 + background_task 测试全绿；resume 的 `cast(SqliteSessionStore, ...)` 与 `getattr(store, ...)` 消失。
- **依赖**：无（唯一前置是 runtime `__init__` 构造顺序调整）。

### Phase 2 —— chunk / event / hook 纯函数收敛

- **收敛**：§2.2 的 179 处（rl 85 + rs 76 + bg 18）。
- **新增模块**：
  - `chunk_builders.py`：`failed_chunk` / `lifecycle_hook_failure_chunk` / `hook_failures_are_fatal` / `with_runtime_failure_details` / `user_interrupted_payload`（rl:610 模块函数并入）；**同时删除 run_loop.py:95-113 本地副本与 service.py:2611 副本（三份合一）**。
  - `hook_runtime.py`：`RuntimeHookOutcome` / `run_lifecycle_hooks_for_session` / `run_tool_hooks_for_session` / `hook_execution_policy_from_metadata` / `HOOK_RECURSION_ENV_VAR`（service.py:2569/2626/2669/660 移出；service 自身调用改直调共享函数）。
  - `acp.py` / `mcp.py` / `skills.py` / `session.py` / `session_metadata_helpers.py` / `provider_fallback.py` / `tool_scope.py` / `provider_context.py` / `resume.py` 按 §2.2 表接收移入函数。
- **删除**：对应 service.py 私有方法（`_failed_chunk`、`_lifecycle_hook_failure_chunk`、`_run_lifecycle_hooks`、`_run_tool_hooks`、`_hook_execution_policy_from_metadata`、`_hook_recursion_env_var`、ACP/MCP 家族、metadata 家族等）。
- **验收**：`runtime.failed` / `runtime.session_started` / `runtime.approval_resolved` 等 payload 与序列逐字节不变（decomposition plan 的 fallback/resume acceptance gate 复用）；`test_runtime_service_extensions.py -k "hook or background or resume or approval"` 全绿。
- **依赖**：Phase 1（多数纯函数签名需要 store/workspace/config 参数）。

### Phase 3 —— 19 个 proxy 删除（直连 import）

- **收敛**：§4 清单；collaborator 直连 import 模块函数；service 内部调用点同步替换（§4 末段）。
- **新增**：无（全部指向 Phase 2 已就位的模块函数）。
- **删除**：18 个 proxy 方法；`_resolve_permission` 的注释行与重命名（→ Protocol 方法，Phase 4 收口）。
- **验收**：`grep -n "Referenced via extracted" service.py` 为空；audit 规则 7 恢复成立；三个 collaborator 的 TYPE_CHECKING import 保留。
- **依赖**：Phase 2（proxy 的转发目标必须已存在）。

### Phase 4 —— 窄 RuntimeSurface Protocol（治理回引收口）

- **收敛**：§2.4 的 53 处（rl 20 + rs 30 + bg 3）+ rs:590 字段赋值。
- **新增**：`runtime_surface.py`（§3 Protocol，18 方法）；`PermissionOutcome` / `RuntimeHookOutcome` 类型提升；三个 collaborator 构造签名改 `surface: RuntimeSurface`；service.py 实现 Protocol（public 方法名转发到现有实现体，或直接改名）。
- **删除**：最后 53 处 `runtime._xxx` 私有访问；`_PermissionOutcome` / `_RuntimeHookOutcome` 私有类名。
- **验收**：`grep -c 'runtime\._\|_runtime\._' background_tasks.py run_loop.py resume.py` ≈ 0（仅 TYPE_CHECKING 注释残留为 0）；`tests/unit/runtime/`、`tests/unit/tools/test_background_task_tools.py`、`tests/unit/interface/test_cli_delegated_parity.py` 全绿；`mise run check` 通过。
- **依赖**：Phase 3。

### 阶段依赖图

```
Phase 1（数据注入，无前置）
   └─▶ Phase 2（纯函数收敛，需 Phase 1 签名）
          └─▶ Phase 3（proxy 删除，需 Phase 2 目标）
                 └─▶ Phase 4（RuntimeSurface，需 Phase 3 清理）
```

---

## 6. 边界与非目标

- **不引入事件总线 / 中间件层**：与 audit §5.4 一致（OMP 的 bus 不在 VoidCode 路线）；所有收敛目标都是**函数调用 / 注入参数**，无消息传递语义。
- **runtime 统一持有治理不迁移**：权限（`_resolve_permission`）、审批（`_approval_resolution_outcome`）、工具注册（`_tool_registry_for_effective_config` 等）、hook policy（`hook_execution_policy_from_metadata` 只是把纯推导函数化，hook 编排与 config 仍在 runtime/hook 模块）、session/background truth（`SessionStore`）保持原位；本设计只改变**访问通道**（私有属性 → Protocol 方法 / 注入），不改变所有权。
- **行为字节级不变**：每个收敛函数体与现方法一一对应（含 frozen dataclass 输出、异常文案、事件 payload）；无默认值回退、无迁移 shim、无新语义；验收以现测试套件全绿 + 事件 payload 逐字节比对为准。
- **448 处访问的归属**：A 202 + B 179 + C 14 = 395 处收敛（注入/纯函数/peer 注入）；D 53 处**刻意保留**为窄 RuntimeSurface 方法（§2.4 表，理由=治理/配置组合/runtime 状态）。
- **非目标（本设计不覆盖）**：
  - 不拆分 `execute_graph_loop`（1397 行）或 `_stream_chunks`（542 行）——那是 audit 的 P2 切片项，与本契约设计正交。
  - 不做 session metadata magic-string TypedDict 化（audit §3.7 / §5.2 是另一份设计）。
  - 不扩展 `SqliteSessionStore` 职责、不拆 storage 单体（audit P2）。
  - 不引入 DI 框架 / 不改变 collaborator 的 public API（`finalize_background_task_from_session_response` 等保持 public 方法名）。
  - 不动 child preset 枚举 / 终态集合 / 事件类型字面量收敛（audit §3.2/3.3/3.6 各自独立）。

---

### 附：与既有文档的一致性

| 规则 | 本设计如何满足 |
|---|---|
| refactor 规则 7（无 proxy property） | Phase 3/4 删除 19 个 proxy，剩余访问经 Protocol 方法（非属性） |
| decomposition 原则 5（协作方接管时删 private proxy） | Phase 3 全量迁移调用方（collaborator 直连 import + service 内部同步），无遗留 shim |
| decomposition 原则 1（runtime 统一持有治理） | §2.4 治理访问保留，但经窄 Protocol；§6 边界不迁移任何治理所有权 |
| audit §5.4 方向（RuntimeSurface 最小接口） | §3 Protocol 18 方法；数据依赖全部注入，surface 只留"需 runtime 组合状态"的调用 |
