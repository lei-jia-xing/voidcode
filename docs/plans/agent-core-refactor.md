# Agent Core 重构实施计划

状态：P0 治理基线与 P1–P3 contracts / 显式 tool invocation / 单一 turn engine cutover 曾在 P3 工作区完成 scoped 行为验证；旧引用 P3 milestone `9dc84ab` 并非自包含源码交付（`src/voidcode/core/engine.py` 不在该 commit tree），core 源码随 P4 normal-hook milestone `c67b854` 一并提交。P4 scoped 实现与验证已由 `c67b854` 提交。P5 仍在实施，但已完成并有当前 focused evidence 的 slices 包括：严格 capability snapshot v4 / SQLite schema 2 / bundle schema 2 composition owner closure；typed reader/context projection；合法 `ToolTurn`/`FinalTurn` 与 `CallReported`/`CallPaused`/`CallStopped*` caller migration；`ReportedCall`/`ToolResultView` ownership and authoritative report codec；background task owner/reopened-runtime smoke。P5 当前明确阻塞为 external package tool/agent/context/typed-input runtime adapters 与 durable lifecycle phase contract；不得据此声称 P5/P6、all-ten 或 full gates 完成。
当前接受的窄范围证据仍仅覆盖各条目列出的 focused behavior tests 与 isolated runtime smokes；历史 milestone/审计段落保留为历史，不作为当前完成证明。

## 当前数据格式策略（后续用户要求，覆盖旧迁移条款）

最新用户要求“不要考虑向后兼容”覆盖下文旧的 database/bundle migration、旧版只读 decoder 或转换要求。当前格式为 `agent_capability_snapshot` v4、SQLite `user_version` 2 与 session bundle schema 2；snapshot/bundle 缺失或不支持的版本严格拒绝，SQLite 只允许无应用 schema 的空数据库从 version 0 初始化，并在 bootstrap/configuration 前拒绝旧版或含应用 schema 的 unversioned 数据库。不得添加旧格式 decoder、转换器、migration 或 compatibility path；拒绝时保留原有数据库、bundle 和旧数据不变，不自动删除/覆盖，需从新空数据库路径开始。

依据：[pi × VoidCode 可扩展性审计](../audits/pi-voidcode-extensibility.md)。审计是历史快照，不改写；本计划的当前状态须以源码和实际行为为准。目标覆盖审计 P0-1～P0-6、P1-7～P1-8、§5 的组合问题和 §8 全部十项验收，不把 roadmap 当成增加产品功能的授权。

## 目标与不可移动的边界

选择单 agent 的 **turn engine**，不把现有 `graph` 包装成不存在的 DAG/workflow 系统。

```text
provider adapters / tool implementations → core contracts + turn engine
                                            ↑
runtime host：policy / approval / redaction / persistence / recovery /
             lease / capability lifecycle / task supervision / composition
                                            ↑
                              CLI / TUI / HTTP / ACP
```

- core 必须实际完成 provider → tool batch → results → 下一轮 → 结束；没有 `VoidCodeRuntime`、SQLite、workspace、CLI、TUI、MCP 或 LSP 也能运行。无 workspace 不等于文件工具不需要 workspace：纯 core 和非文件工具不依赖它，文件工具显式要求它。
- runtime 仍是唯一的 authorization、approval、session truth、event persistence、recovery、execution ownership 和 capability lifecycle authority。core 只推进执行，不自行授权、写数据库、启动后台 manager 或恢复历史 side effect。
- provider 负责 normalized request/stream/result 和真实 wire 的解析；retry/fallback/recovery 由 runtime 决定。tool 通过显式 context 获得实际依赖，不从全局 `ContextVar` 读取 runtime truth。
- 原始 append-only history 与本次 model-facing transcript/projection 分开；provider view 不反写历史。保留 reasoning、tool-call/result pairing、分支和 continuity 语义，不复制 pi 的存储格式。
- clean cutover：每阶段迁移真实消费者并删除被替代的旧路径；最终无旧 graph aliases、兼容 re-export、私有 facade 或双控制面。运行中只能有一条 authority 路径，不能隐式双写。
- 上层信任已经建立的 typed lower contract；同进程不重复 serialize→parse、hash、复制或防御性校验。HTTP/JSON/SQLite、provider wire、工具输入、凭据、安全权限和数据完整性边界的校验保留。
- 只提取已有实现和确有消费者的接口。内存 host/store 是独立运行的实际实现；fake provider/tool 是确定性的验收 fixture，不是交付占位实现。

## 阶段基线与历史复用证据（非当前 API）

下表记录各阶段 cutover 前的源码基线与当时复用起点，不是 live API 清单；已删除的 graph / `SessionStore` 入口不能据此导入或调用。当前实现、删除状态与行为证据以各阶段已实施记录和现行 contracts 为准，历史 audits 不改写。

| 阶段基线 / 历史证据 | 影响与迁移落点 |
| --- | --- |
| P1：`core/transcript.py` 统一 `ToolResultView`、`ContextSegment`、`AssembledContext` 与 `ContextWindow`，provider/graph/runtime 全部真实消费者已切换 | provider/tool package init 已移除 eager facade；实际 graph import 不再经 command event re-export 加载 runtime |
| `graph/provider_graph.py::step` 持有 batch、session/run identity、approval-resume 判断并组装 provider turn；`pending_tool_call_count` 决定 safe boundary | P3 提取真实 turn/batch 算法，不是给旧 graph 加一个 engine facade |
| `runtime/run_loop.py::execute_graph_loop` 负责 context、steering、typed tool input、permission、intent、hook、execution、result、checkpoint 和 fallback | 抽出执行推进；治理、durable intent、safe checkpoint 和故障策略保留为 runtime host 行为 |
| P2：`runtime/run_loop.py::_execute_resolved_tool_call` 汇聚 native、approval-resume、`invoke_tool`；`runtime/tool_execution.py` 显式传入 `core/tool_context.py::ToolContext` | canonical execution boundary 与 progress/timeout/lease 保持 runtime ownership，只绑定本次工具实际需要的窄 resource/command |
| P2：旧 tool-identity ContextVar binder 与 runtime-shaped facade 已删除，`ToolDefinition.effects` 提供行为事实 | 共享 read-tier 分类与 per-call operation class 保留审批/plan 语义；public catalog 的派生 `read_only` 不是 grant，runtime execution-ownership ContextVar 仍保留 |
| `runtime/storage/sqlite.py` 的 `SessionStore` 同时覆盖 events、sessions、approvals、tasks 和维护；已有 SQLite owners/mixins | P4 按实际消费者拆窄 ports，复用存储算法和事务，不先造远程/JSONL backend |
| `runtime/tool_materializer.py` 已组合 base/MCP/local provenance，`RuntimeToolMaterialization` 可 scope registry | 复用 materialization seam；generation 有恢复消费者，不能因它是 hash 就删除 |
| `runtime/service.py::_agent_capability_snapshot` 组装 agent/prompt/tools/skills/hooks/MCP/delegation/runtime/execution；`config_materializer.py` 管 persisted config | P5 收敛 intent、authority、frozen execution plan，保持 env/user/repo/request/persisted/parent precedence |
| `runtime/agent_capability.py` 本轮修改前 snapshot version 为 3，但部分 shape 错误说明硬编码 v2；现已使用动态版本 | 只修复错误说明；持久化 shape/version 校验保留，其余契约版本漂移仍按 P4/P5 处理 |
| `provider/registry.py::with_defaults` 仍有 builtin adapter/table 组合，custom entry 主要是 endpoint-shaped，未知 id 会拒绝 | P5 支持真实 provider package entry；不能退化成未知 provider 静默 fallback，也不能重写已成立 wire adapters |
| `runtime/context/transforms.py` 已有 typed request/result、ordering、failure trace，但 scope 只有 `provider_context` | 复用已有 seam；P5 统一已有 typed phase，不提前增加所有可能的 transform 功能 |
| `runtime/context/provider.py` 的 debug projection 消费中立 `AssembledContext`，metadata 是开放 `dict[str, object]` payload；只读 helper 现接受 Mapping | custom/persisted metadata 仍不可信，parser 校验保留；中立 view 不接管 debug projection、redaction 或 policy |

已有行为测试映射见下方 P0 实际基线。重点文件包括 `tests/unit/runtime/test_typed_tool_hooks.py`、`tests/integration/test_process_crash_tool_resume.py`、`tests/unit/runtime/test_tool_execution_timeout.py`、`tests/unit/storage/test_session_fork.py`、`tests/unit/storage/test_session_checkout.py` 和 `tests/unit/runtime/test_checkout_provider_context.py`。

## 阶段与依赖

顺序为 P0 → P1 → P2 → P3 → P4 → P5 → P6。P0 是冻结和行为基线；P1 是第一份可执行代码 change set。每阶段是可独立 review/落地的内聚迁移，不按文件行数拆空壳 PR；每份代码 change set 同步更新受影响契约及消费者。

### P0：固定行为边界，停止继续扩张控制面

**进入：** 当前审计和源码已对照，尚未抽取 core。

**变更：** 暂缓新增 preset、hook surface、task topology、event family、中央 config section 和 provider-specific runtime 特例；允许 correctness/security/replay 修复。整理已有行为基线；删除只 pin 常量、文案、字段搬运、实现细节或 mock echo 的测试，不把它们重新 pin 到新架构。CLI/TUI 不保留永久自动测试，改为实际程序的人工 smoke。

**退出：** 下述治理场景有明确的可重复步骤和已有 backend 行为用例；当前失败、已验证结果和未验证项分开记录。不得以旧 graph event 精确列表或源代码文本作为“语义冻结”。

**验证场景：** 允许/拒绝工具；rewrite 后重新验证并对最终参数授权；approval 后只执行一次；cancel 后不出现晚到 completion；persisted replay 不执行 side effect；fork/checkout 不拆 tool pair。

**风险：** 删除过度测试时误删数据/权限行为基线。保留真实输出、副作用、权限结果、durable 状态和可见顺序的断言。

**P0 已完成（2026-10-01；仅冻结现有行为，不代表架构迁移已开始）。**

| 场景 | 可重复后端用例 / 观察 |
| --- | --- |
| allow / deny | `test_permission_plan_mode.py`、`test_approval_mode_tiers.py`；runtime 层的 workspace ask、shell deny、external-write deny 用例。 |
| rewrite 验证与最终参数授权 | `test_typed_tool_hooks.py::test_typed_rewrite_preserves_raw_graph_args_uses_final_execution_args_and_canonical_started_id` 与 `::test_invoke_tool_outer_is_not_rewritten_and_inner_runs_once` 验证最终参数执行且 permission 在 started 前；无单独永久用例覆盖“无效 rewrite + 最终外部路径 policy”同一场景，本轮用 throwaway runtime probe 确认无效 schema rewrite 在执行前 block，重写到 external path 则按最终 canonical path deny，side effect 不发生。 |
| approval resume 单次执行 | `test_approval_resume_uses_final_started_id_once_and_does_not_repeat_typed_handler`、`test_runtime_persists_pending_approval_until_single_resume_resolution`。 |
| cancel 后晚到事件 | `test_cancel_lands_while_tool_result_in_flight_drops_late_result`、`test_cancel_mid_provider_stream_drops_remaining_deltas`、`test_late_tool_completion_at_the_boundary_is_not_committed_as_the_tool_result`。 |
| persisted replay 不重放副作用 | `test_process_crash_during_a_tool_call_resumes_without_replaying_or_claiming_it`；恢复后已完成 read execution count 仍为 1，crashed in-flight call 不被 claim completed。 |
| fork / checkout pair 边界 | `test_fork_refuses_boundary_that_splits_a_tool_call`、`test_checkout_refuses_path_that_splits_a_tool_pair`、`test_rehydrated_tool_results_follow_the_checked_out_leaf`。 |

本轮可重复命令及观察结果：

```sh
uv run pytest -q \
  tests/unit/runtime/test_permission_plan_mode.py \
  tests/unit/runtime/test_approval_mode_tiers.py \
  tests/unit/runtime/test_typed_tool_hooks.py \
  tests/unit/runtime/test_session_lifecycle_seal.py::test_cancel_lands_while_tool_result_in_flight_drops_late_result \
  tests/unit/runtime/test_session_lifecycle_seal.py::test_cancel_mid_provider_stream_drops_remaining_deltas \
  tests/unit/runtime/test_tool_execution_timeout.py::test_runtime_timeout_signals_cancellation_and_stops_a_cooperative_tool \
  tests/unit/runtime/test_tool_execution_timeout.py::test_late_tool_completion_at_the_boundary_is_not_committed_as_the_tool_result \
  tests/integration/test_process_crash_tool_resume.py::test_process_crash_during_a_tool_call_resumes_without_replaying_or_claiming_it \
  tests/integration/test_read_only_slice.py::test_runtime_persists_pending_approval_until_single_resume_resolution \
  tests/integration/test_read_only_slice.py::test_runtime_rejects_stale_duplicate_approval_replay_after_resolution_even_if_pending_state_is_restored \
  tests/unit/storage/test_session_fork.py::test_fork_copies_prefix_own_watermark_and_leaves_source_unchanged \
  tests/unit/storage/test_session_fork.py::test_fork_refuses_boundary_that_splits_a_tool_call \
  tests/unit/storage/test_session_checkout.py::test_checkout_moves_leaf_and_keeps_the_abandoned_events \
  tests/unit/storage/test_session_checkout.py::test_checkout_refuses_path_that_splits_a_tool_pair \
  tests/unit/runtime/test_checkout_provider_context.py::test_rehydrated_tool_results_follow_the_checked_out_leaf
```

Observed: **60 passed**; additional policy execution cases `test_runtime_pattern_permission_rule_asks_for_workspace_write`、`test_runtime_pattern_permission_rule_denies_shell_command`、`test_runtime_pattern_permission_rule_cannot_bypass_external_write_policy` 为 **3 passed**。另一个 throwaway deterministic runtime + temporary SQLite smoke 观察到：pending approval 时目标文件不存在；allow 后 side-effect counter 恰为 1；新 runtime reopen 同一 SQLite 并 replay completed session 后 counter 仍为 1，tool_started/tool_completed 各恰一条。smoke 与 rewrite probe 均已删除，没有新增永久测试。

补充命令：`uv run pytest -q tests/unit/runtime/test_runtime_service_extensions.py::test_runtime_pattern_permission_rule_asks_for_workspace_write tests/unit/runtime/test_runtime_service_extensions.py::test_runtime_pattern_permission_rule_denies_shell_command tests/unit/runtime/test_runtime_service_extensions.py::test_runtime_pattern_permission_rule_cannot_bypass_external_write_policy`（**3 passed**）。

### P1：解开 lower types 的 runtime 依赖——第一个具体 change set

**进入：** P0 的 authority 和验证范围已确定。

**变更：**

1. 从当前 `ToolResultView`、context segment 和 provider request/result 中抽取真实使用的中立 message/tool-result/transcript contracts；在一个 lower owner 定义，区分 raw history 与本次 provider view。不要同时添加无人消费的 custom message/compaction/edit API。
2. 迁移 `provider/protocol.py`、`graph/contracts.py`、`graph/provider_graph.py`、`graph/deterministic_graph.py`、`tools/contracts.py` 及 runtime context/provider assembly 的全部现有消费者；类型引用和 package 初始化一起切换。
3. runtime-specific projection、session metadata/replay、redaction 和 policy 留在 runtime adapter；删除被迁走定义、obsolete imports/re-export。既有 provider 和 graph 立刻使用中立类型，不留“以后接入”的 facade。

**退出：** 真实 lower provider/tool contract 消费者已运行在新类型上；导入和构造现有 normalized provider request/result 不加载 runtime/SQLite/UI；runtime 的 context projection 和实际 provider turn 行为仍成立。此阶段**不是** standalone engine 完成。

**验证：** 干净 Python 进程 import + request/response smoke，实际调用现有 deterministic/provider adapter 的一轮（本地确定性 wire fixture），检查 tool-call/result identity 和 reasoning 不丢；运行受影响 backend 行为用例。导入隔离检查只是架构 smoke 的补充，不建立 source-text 永久测试。

**风险：** provider → runtime 的隐含依赖和 package init cycle；neutral 类型只做形状，不夹带 workspace policy 或存储算法。

**P1 已完成（2026-10-01）。** 现有 provider request/result、deterministic/provider graph、runtime context assembly/projection 和全部调用者已经直接消费 `core/transcript.py`。重复 segment 定义和 segment-like facade 已删除；`RuntimeContextWindow`、`ContextProjection` 与预算/continuity/replay 留在 runtime。`ToolResultView` 保留对原始结果及 provider data 的嵌套隔离。provider/tools 的 eager package facade 和隐藏 command event re-export 已切除；Python 导入须使用定义 owner，无旧别名。

实际行为证据：

- 干净进程构造 normalized request/result、修改 view 的嵌套 data：原始结果与 raw view result 未变，runtime/SQLite/CLI/TUI/MCP/LSP 模块加载数为 **0**。
- 真实 OpenAI SDK Chat Completions adapter + 本地 `MockTransport`：DeepSeek 返回的真实 reasoning 与 `native-call-1` 原始参数保留；下一轮 wire 的 assistant call/tool result ID 配对，真实 reasoning 重放，最终返回 `paired native answer`。同进程实际 deterministic graph 两步完成，仍无 runtime/SQLite/UI/capability manager 导入。
- 实际 runtime 读取临时文件，temporary SQLite 关闭/reopen/replay 保持一条 completed tool result；neutral provider projection 无 missing/orphan tool pair。另一个实际 runtime + native SDK wire 场景读取真实文件，第二轮消费配对结果，完成并持久化原始 reasoning。
- 独立验收 worker 的干净进程/native wire smoke 也验证了两条 call 的 ID/参数/结果顺序、raw history 隔离及最终输出；使用本地 transport，未声称在线 provider 验证。

受影响 backend 检查：`uv run pytest -q tests/unit/provider tests/unit/graph tests/unit/runtime/test_context_window_diagnostics.py tests/unit/runtime/test_context_compaction_wiring.py tests/unit/runtime/test_context_budget_from_catalog.py tests/unit/runtime/test_typed_tool_hooks.py tests/unit/runtime/test_checkout_provider_context.py tests/integration/test_process_crash_tool_resume.py tests/unit/tools/test_contract_matrix.py tests/unit/runtime/test_tool_execution_timeout.py` → **412 passed**。使用 throwaway scripts，未新增永久 import/source-text/CLI/TUI 测试；schema/catalog/wire event vocabulary 未更改。

facade 调用者检查：`uv run pytest -q tests/unit/tools tests/unit/runtime/test_permission_plan_mode.py tests/unit/runtime/test_session_bundle.py` → **238 passed**（与上批存在交集，不合并计数）。本阶段 touched source modules 的 scoped `uv run ty check <paths>` 通过；仅 touched Python files 执行 import sorting/formatting。全仓 gate 与 milestone commit hooks 由集成 owner 执行，不把 scoped 结果当全仓结果。

原有 percall abstraction 删除及 reminder 文档/行为用例变更与此阶段的中立 segment cutover 原子集成；不复活旧 alias。`uv run pytest -q tests/unit/runtime/test_todo_mid_run_nudge.py tests/unit/runtime/test_todo_reminder.py` → **27 passed**，保留 reminder 不进入 persisted transcript 的行为断言，不重新 pin 已删除的 cache-hash 实现断言。


### P2：显式 ToolContext，清除隐式执行依赖

**进入：** lower contract 已中立，P1 消费者已 cutover。

**变更：** 将已有 invocation context 变为 `Tool.invoke(..., context=...)` 的真实参数，迁移 builtin、MCP、LSP、local custom tools、runtime executor、test fixtures 和所有直接调用者。workspace、abort、progress 和实际需要的 resource/capability handles 显式传入；只给需要它的工具提供 dependency，不造无消费者 resource hierarchy。runtime command/resource adapter 不暴露 `_session_store` 或任意私有 runtime state。

以现有工具的实际行为建立 `read/write/execute/network/spawn/session` effect facts，policy 据此决策；迁移默认 read-only approval 规则和所有定义消费者，完成后删除旧 boolean 决策路径、ContextVar binder、obsolete facades。effect 是事实，不是许可；缺少能力必须诚实报错，不静默 fake fallback。

**退出：** 同一真实文件工具可以由显式 in-memory host context 调用；runtime 下 native/resume/inner invoke 仍走同一治理边界，cancel/timeout/progress/lease 均由 runtime 掌管；工具不必依赖 hidden ContextVar 获得身份。

**验证：** 临时 workspace 读写真实文件；write 等待 approval 时文件不存在，allow 后出现且只执行一次；deny 不执行且返回可供模型调整的 tool feedback；timeout/cancel 之后晚到 progress/result 不被持久化。MCP/LSP 用可控本地进程验证生命周期，不需要在线服务。

**风险：** effect 翻译放宽默认权限、嵌套 invoke bypass、线程/progress 生命周期遗漏；不用 `session_id=""` 等伪默认值掩盖缺少真实依赖。

**已完成（P2）：**

- builtin、MCP、LSP、local custom、runtime executor 和实际直接消费者统一使用显式 `ToolContext`；workspace/session 不伪造。中立 question/todo/edit-schema values 移入 core，解析与持久化仍由 runtime 负责。
- task/process 实现移到 `runtime/execution/delegation`、`runtime/execution/process`；工具只接收 approved call 与真实 caller/workspace/cancel 绑定的 command。session artifact/transcript reader 绑定真实 caller，不能换 caller 读取无关 session。取消前置 guard 有真实 SQLite child-registration regression。
- standalone fresh-process smoke：真实 read/write、hash 校验、stale hash 不写入、空 identity 拒绝、缺失 session resource 报错；runtime modules loaded = 0。实际 native runtime smoke：approval 前无文件，restart/allow 后只写一次，completed reopen/replay 不重写，deny 反馈返回模型；local readonly 声明通过静态筛选但实际 plan argv 被 `execute` ceiling 拒绝。
- 实际 native shell smoke：完成前可见增量 progress；timeout 确认子进程停止并标为 settled；用户取消后无 late marker 或 terminal 后 durable tool rows。实际取消事件仍是 `runtime.failed` + `cancelled:true`，没有修改 wire vocabulary。
- 本地 stdio MCP/LSP 实际进程 smoke（独立验证）：真实响应、initialize/shutdown/stopped markers、runtime exit 后 PID 消失；无在线服务依赖。实际 CLI `VOIDCODE_EXECUTION_ENGINE=deterministic uv run voidcode run 'read probe.txt' --workspace <tmp> --json` 完成真实两行 read；`sessions list/entries` 观察到一次 tool completion。P2 未声称 TUI 验证。
- 最新独立 checks：`uv run pytest -q tests/unit/tools` → **169 passed**；`uv run pytest tests/unit/runtime/test_permission_plan_mode.py tests/unit/runtime/test_tool_execution_ownership.py tests/unit/runtime/test_tool_execution_timeout.py tests/unit/runtime/test_typed_tool_hooks.py -q` → **69 passed**（与工具目录 disjoint，不与历史 78 汇总）。backend contract 残留 package-export fixture 切到真实 defining module 后，`uv run pytest -q tests/unit/runtime/test_backend_contracts.py` → **7 passed**。P2 owned source scoped `ruff check` / `ty check` 均通过；全仓门禁由 integration owner 在 milestone review 后运行。
- 后续 consumer checks 暴露并修复遗漏的 ReadTool patched wrapper 与 empty-effects 默认误分类；未知 facts 在共享 classifier fail closed（scope/catalog/replay/permission 一致）。真实 resolver regression 不 pin tier 标签：`write` mode 下无 facts 要求 approval、显式 read 自动允许；MCP hint 经实际 `write/ask` admission 验证。`uv run pytest -q tests/unit/runtime/test_approval_mode_tiers.py` → **15 passed**。真实 scope/permission smoke 同时观察到 plan exclusion、replay `never`、`ask` pending 与 plan ceiling 对 `yolo` 的 deny。
- 历史 SQLite 实测：由 P1 commit `d33e51d` 的真实源码生成 waiting approval 与 snapshot v3，当前 P2 批准后真实 write completion 一次；fresh reopen/replay 仍只有一次且文件 content/mtime 不变。这不是全量旧数据迁移证明；effects 与 declared replay policy 已纳入当前 generation fingerprint。该历史段落记录当时要求 P4/P5 做显式迁移；该要求已被本计划开头的最新严格拒绝/保留旧文件策略取代，不要求转换 legacy data。


### P3：真实 turn engine 与 runtime host 单路径切换

**进入：** provider/tool/transcript contracts 已中立，真实工具依赖已显式。

**变更：** 从 `ProviderGraph` 和 run loop 提取 provider turn、stream/tool batch assembly、pairing、结果推进、continue/end、steering/follow-up 和 abort 的实际状态机。provider wire 解析归 provider adapter，normalized batch/lifecycle 归 core。deterministic 路径迁到同一 core contract 的实现或输入 adapter，不保留旧 graph public aliases。

runtime host 注入 context builder 和 governed tool executor，处理 approval pause/resume、permission、hooks、redaction、durable intent、checkpoint、provider retry/fallback/recovery 和 lease。已批准的最终参数不再经过会变更输入的 handler，也不能重复执行 side effect；runtime 仍执行 resume 必需的 approval identity、policy 和 execution ownership 校验，不借重构跳过治理。core 不知道 SQLite event 名称。`VoidCodeRuntime`/run loop 改消费 core 结果，删除旧 `GraphRunRequest`/`RuntimeGraph` 和被替代算法，而不是保留两个循环开关。

**退出：** 无 `VoidCodeRuntime`、SQLite、workspace、UI/capability manager 的实际内存 host 跑完两轮、有多工具 batch 的完整会话；同一 core 在现有 runtime host 执行 run/stream/resume。runtime 不再实现 provider turn/tool-call parsing。

**验证：** 确定性 fixture provider 第一轮给两个 tool calls，真实纯工具返回不同可断言结果，第二轮 provider 消费配对结果后结束；steering/follow-up 在定义的 safe boundary 生效，abort 停止下一调用。另用真实 wire adapter + 本地可控响应运行同一场景，不能声称仅 fake 证明 real-provider 兼容。恢复/批准/重试场景见总验收表。

**风险：** batch resume 重复执行、stream 已可见输出被静默 retry、失败被当成功结束。保留 externally meaningful 顺序，而非整个内部事件 golden dump：最终参数校验/授权 → durable intent → hook/execute → durable result → 下一 provider turn；progress 在 completion 之前，safe checkpoint 不拆 batch/pair。若现有 resume 特例顺序不同，先用副作用/恢复场景证明其含义，再统一，不能借 refactor 偷改行为。

**已完成（P3）：**

- Provider 与 deterministic producers 均经 `src/voidcode/core/engine.py::TurnEngine` 推进；`src/voidcode/runtime/run_loop.py::RuntimeHost` 仍拥有产品治理、context、工具执行、审批、持久化与恢复。旧 graph 包、`GraphRunRequest`、`RuntimeGraph` 与重复 turn loop 已删除，无兼容 alias。恢复 seed 的 completed prefix 按原始 call ID 校验，并只加入 engine history 一次，保留此前历史，不重放已完成工具。
- 实际纯工具控制 smoke（`python /tmp/p3_local_sdk_fixture.py`，exit 0）验证两工具 batch steering、follow-up history 与 batch 内 abort；工具副作用/原始 IDs/真实 reasoning 均按场景确认，runtime/SQLite/UI 模块未加载。实际 OpenAI SDK + 本地 SSE transport（`uv run python /tmp/p3_runtime_sdk_smoke.py`，exit 0）经 RuntimeHost 执行 Read/Glob 并 SQLite replay，2 个 SDK 请求保留原生 reasoning、call IDs 与真实结果；这不是在线 provider 运行。
- 实际 ReadTool 输出 smoke 和 12 个关联测试通过；CLI deterministic read 保留六行内容并只完成一次工具调用（未声称 TUI 验证）。恢复行为批次 165 passed，三项最终 root repair checks 3 passed；当前 core deterministic/read seeded-continuation test 一次读取保留完整已完成结果、返回真实输出且无第二次读取，1 passed。最终 provider/core+graph/read-only/runtime recovery 集成范围 494 passed。

### P4：拆 events/repositories，保留已有 SQLite 真相

**进入：** core 不依赖 runtime events，runtime host 已唯一执行入口。

**变更：** 建立 typed domain execution facts 与明确 codec/version、replay policy/owner；runtime 内完成必要 redaction 后持久化，再投影为现有 client delivery events。live-only stream delta 保持 live-only，不为抽象整齐增加持久化。不能把未经 redaction 的 core data 直接写 store/客户端。

按已有 consumers 从 `SessionStore` 拆 event append/read、session identity/branch、approval、task 和 bounded artifact ports。SQLite 实现继续复用已有 owners/mixins、事务、dedupe、terminal seal 和 write gateway；内存 EventStore 实际实现 append/read/branch 需要的语义，供无数据库 host 使用。事务/ownership 只在需要处提供，不为每个 repository 复制完整 mega-port。

格式变更采用严格 clean cutover：旧版本或 shape 不兼容时明确拒绝；不做数据库/bundle 数据转换、旧格式 decoder、双读写或兼容路径，且不得自动删除/覆盖原文件。需要新格式时使用新空数据库与当前 bundle 格式；永久保留旧文件中的原始数据。

**退出：** 同一 core 行为 contract 可以使用内存或 SQLite event adapter；domain facts 与 client delivery payload 有不同 owner；现有 approval/crash/fork/checkout/append-only 行为保持。

**验证：** 真 SQLite 临时库完成 run、关闭/reopen、replay、resume；side-effect counter 不增加；checkout/fork 原事件未改、pair 不拆，fork provenance 与 delegated parent 不混用；夺权后 late append/progress/result 被 write gateway 拒绝；旧版或 unversioned 数据拒绝时原始文件保持不变（不再要求 migration fixture）。

**风险：** ports 拆分破坏原子事务、未经 redaction 的 event 落库、旧 session 不可读、client wire/replay 语义混淆。

**P4 历史实现与 scoped 证据（normal-hook 源码 milestone `c67b854`）：**

- P4 当时没有引入 SQLite/bundle/checkpoint/fact-codec 新格式，物理与编码版本保持 v1；这是历史 P4 状态，不再表示 P5 格式切换尚未实施。当前 cap snapshot v4 / SQLite schema 2 / bundle schema 2 的严格拒绝规则见本计划开头。
- 原始 native batch 与实际已授权参数分开；completion 预发布校验真实 ID，再由已有 transaction 原子提交实际完成前缀/checkpoint，全部 dedupe-skipped 不改 leaf/watermark/checkpoint。bounded runtime-only 空页继续 cursor；fork/checkout 检查所选 ancestry 的真实 pair，保留原序号/parent edges/fork provenance，不合成 legacy ID。
- canonical metadata 使用既有 session snapshot projection；修复 whole-checkpoint payload redaction 对 hook `authority` 的 post-hash 变更，未绕过 validation/重算 hash/修改全局 redactor。实际批准 write 的 stream-close 和 terminal-save failure 后由新 owner resume：每场景写入一次，文件 bytes/mtime 不再变化，authentic prefix、当前 frozen hook hash 与 capability snapshot 保持。实际 question-answer stream-close/new-owner resume 保留回答 A、真实 write 一次、durable answer 一次。另有 enabled frozen hook 在新 runtime config 禁用 hook 后仍沿已记录语义执行的 consumer 证明；不扩大任意未来 signed snapshot 的安全声明。
- shared SessionRepository queue mutation owner 在 transaction 内 enqueue/drain；一般 snapshot 保留 current queue/delivery cursor，包括 consumed absence。实际 SDK follow-up 在保留先前 native pair/output 后处理新 user input，真实 Read/Write 各一次；并发真实 enqueue/drain 顺序与一次 delivery 已验证。corrupt non-object durable metadata 明确拒绝。
- 实际已运行 scope：memory/SQLite × scripted/real SDK matrix、SQLite partial-prefix reopen 与 secret/spoof-ID refusal；persistence/lifecycle 23 passed、lease late-writes 2 passed（各自已执行范围，非项目 gates）。修复后 `uv run pytest -q tests/unit/runtime/test_question_resume_lifecycle.py tests/unit/runtime/test_fact_store.py` 34 passed；stale approval / post-resolution steering 两项 2 passed。后续腐败测试清除 incidental whole-file byte equality，真实 queued session + committed event 的 metadata/checkpoint JSON、events、watermark/leaf 在拒绝后 reopen 不变：exact target `test_fact_store.py::test_corrupt_owned_metadata_rejects_snapshot_without_mutating_durable_truth` 1 passed（`local://p4-corrupt-metadata-logical-proof.txt`）。旧 `artifact://307` 是失败的 physical-byte fixture，不作通过证据。尚未声称本阶段项目级 gates、最终 CLI/TUI 或新增类型收敛通过。


### P5：注册与组合收敛，不建立任意权限 plugin bus

**进入：** core、explicit tools、typed facts 和 repository adapters 已切换；恢复输入采用明确版本策略，当前 capability/SQLite/bundle 格式严格拒绝旧版本且不转换旧数据。

**变更：**

- provider package entry 组合 wire adapter、catalog/model metadata、auth 和 endpoint declaration。迁移 builtin 注册到同一机制；复用当前 normalized boundary。未知 provider 仍拒绝，runtime 保留 retry/fallback policy，metadata 来自 discovery/generator 而非手改生成 JSON。
- 复用 ToolMaterializer、agent/skill registry、MCP declaration 和 context transform seam，建立 `discover → validate → register → materialize`。已有内置能力成为实际注册消费者；可信进程内 package 能定义声明/生命周期，但不能自行 grant、写 session truth 或绕过 runtime lifecycle。
- 将 manifest、request overrides、parent binding 等 intent 收敛为 `CapabilityBinding`；policy 单独 materialize authority；最终冻结 `ExecutionPlan`。保留 recovery-critical 值和 provenance，删除重复 feature snapshot/中央 preset 判断；materialized plan 必须在可能发生第一个 side effect 前持久化。
- 核心 config 只保留 runtime execution/policy/storage 必需规则，extension-specific declaration 由所属 package 验证；中央 loader 只组合已注册 section。保持 env/user/repo/request/persisted/parent precedence，不改变现有 config 输入的权限意义。schema 由 source models 生成；格式改变即按严格 clean cutover 拒绝旧格式，不永久保存旧入口或迁移旧数据。
- typed context transforms、tool-input handlers 和已有 typed lifecycle consumers 使用收敛 phase contract，明确 lifecycle/ordering/failure/snapshot/replay；argv hooks 保留外部 command adapter，guidance-only hook preset 不变成可授权插件。`observe/transform/gate/schedule` 只实现有现有消费者的 phase：gate 只缩权，schedule 只提交 runtime command。不顺带新增任意 prompt mutation、compaction strategy 或 provider transform feature。

**退出：** 在单一 extension package 内添加 provider/tool/agent/context extension 的 declaration、validator、materializer 和 phase 语义，只需注册该 package，不修改 `service.py`、`run_loop.py`、`config_models.py`、`events.py`。同一 registry 不意味不同能力拥有同一 authority；MCP/LSP 的资源创建/refresh/shutdown 仍由 runtime owner 处理。

新增类型收敛的步骤 1–3 是 P5 前置实施项，步骤 4 在 P5 明确所有权；当前已完成的 focused slices 与仍阻塞的 external-slot/phase contract 见下方进展记录。P5 退出仍需真实 producer/consumer cutover 与行为证据，不能以本节 progress 取代阶段验收。

**验证：** package fixture 注入新 provider、实际执行的纯工具、agent declaration 和 context transform，完整 runtime run 得到可断言输出；在 config/request precedence、parent policy 限制、ordering/failure、frozen snapshot recovery 中验证行为；恢复时磁盘 declaration 改动不能偷偷替换已冻结执行语义，无法恢复则明确拒绝。argv hook timeout/failure 仍按原配置处理，gate 不可扩权。

**风险：** generic dict section 成为弱校验后门；agent selection 与 policy 混同；复用 snapshot 时丢失 skill scope/force load 或 MCP intent；phase 合并改变 rewrite/resume ordering。

**当前已核实的 P5 进展（focused evidence，不是阶段完成声明）：**

- 严格 composition/storage/bundle ABI 已落地：capability snapshot v4、SQLite schema 2、bundle schema 2，composition owner closure 只保留 canonical reference，旧格式不迁移；reopened-runtime owner activation 与 background task owner/retry 路径已有 isolated SQLite/XDG smoke。
- reader/context slice 已使用 typed `ReadResultBody`、`ReportedCall` 与 output-only `ToolResultView`；authoritative report codec 已覆盖 completed-call checkpoint/replay 与 output/control alternatives。`ReportedCall` 保留 authorized arguments 与 immutable tool body，provider/model view 不携带 canonical body。
- legal turn states 已迁移真实测试 callers 至 `ToolTurn`/`FinalTurn` 与 `CallReported`/`CallPaused`/`CallStopped*`；源码无旧 callable `TurnPlan` 构造或 completion flag fallback。ToolResultView ownership 与 projection/context consumers 已有 focused mutation/report/runtime evidence。
- 当前 blockers：外部 package 的 tool/agent/context/typed-input slots 仍只有 declaration/admission，service 没有既有 runtime adapters/registry registration API；composition `PhaseDeclaration` 仍没有 runtime trigger 与 durable `PhaseRecord` persistence/replay contract。两者需要上游 API/owner，不能在本阶段伪造。

上述证据只表示列出的 focused tests/smokes 已执行；不声称 P5/P6、all-ten 或 full repository gates。

### P6：任务 substrate / delegation adapter，完成最终切除

**进入：** P5 注册/组合和 frozen plan 已可被现有执行消费者使用。

**变更：** 从 `runtime/background/supervisor.py` 抽出 runtime-owned task lifecycle、ownership、persistence、cancellation、result channel（最小实际 `TaskSpec/Handle/Result`）；把 preset、parent/child session、`task/task_batch`、`yield` 和 leader notification 留为 delegation adapter。迁移现有 queued/running/idle/terminal、keep-alive、steer/retry/shutdown 路径，不重新发明 task engine。

用一个已有需求级别的内存/local workflow fixture组合 core + binding + task/context接口，证明无 service 中央分支；不新增用户产品 workflow。最终删除旧 aliases、private facades、mega-port、obsolete snapshots/events/config paths 和失效测试；契约只描述当前 authority，索引指向当前文档，历史审计保持不变。

**退出：** 现有 delegated feature 通过 substrate 运行，task/session lineage 和 fork provenance 仍分离；新上层组合不新增 service 分支；十项硬验收全部有行为证据，未完成项不能标记重构完成。

新增类型收敛的步骤 1–4 在 P6 完成最终切除：全部实际工具、core/transcript/provider/runtime/codec/recovery/task 消费者迁移，旧 dict helpers、aliases、重复 schema 路径和双重事实源删除；不能以类型注解或 inventory 检查代替行为验收。

**验证：** 排队→执行→完成/失败/取消；keep-alive idle 后 steer；owner 被夺走后老 worker 不写状态/结果；yield/result/parent notification 只有一次；shutdown 不留活动 owner。CLI/TUI 人工实际程序 smoke 检查 run、approval、resume、cancel、fork/checkout 及流式 projection；不新增永久 CLI/TUI 自动测试。

**风险：** substrate 抽取意外扩大 preset/topology 权限、通知重复、child session ownership 与 fork lineage 混合；最终 cleanup 不得删除仍有消费者的 contract。

**当前 focused 进展（不代表 P6 完成）：** queued→dispatch→child session→result/retry/steer/reopen、keep-alive ownership seizure/fresh resume、explicit ToolContext parity、typed facts, provider/core parsing, recovery and governance cells have focused behavioral evidence. A concrete direct-task child-session collision was fixed by forcing allocation when a background request lacks an explicit session id. Remaining acceptance blockers are external package all-slot runtime adapters and durable package phase owner; unselected historical tests are not completion evidence.

## 审计覆盖映射

| 审计项 | 落地阶段 |
| --- | --- |
| P0-1 独立 kernel | P1/P2 前置，P3 完整运行和 cutover |
| P0-2 transcript/message | P1 定义并迁消费者，P3 pairing/turn，P4 durable projection |
| P0-3 explicit tools/effects | P2 全调用路径，P5 binding/policy 组合 |
| P0-4 domain/store/client events | P3 脱离 wire 输入，P4 typed codec/projection |
| P0-5 narrow stores/repositories | P4，P6 task consumers 最终迁移 |
| P0-6 extension/resource registry | P5 注册实际消费者，P6 上层组合证明 |
| P1-7 task/supervision substrate | P6，runtime ownership 不下放 core |
| P1-8 provider/plugin boundary | P1 neutral contract，P3 wire/turn owner，P5 package registry |
| §5.1 graph 名称/抽象 | P3 单 turn engine；不造 DAG |
| §5.2 declaration/capability/policy 重叠 | P5 binding→policy→frozen plan；P4 版本基础 |
| §5.3 hook/phase | P5 已有 typed seam 收敛 + argv adapter |
| §5.4 task/delegation | P6 substrate 与 delegation owner 分离 |
| §5.5 config composition | P5 package validated declarations + precedence 保留 |
| §5.6 event owner | P4 事实→durable→delivery |
| §5.7 context pipeline | P1 transcript 前置，P5 已有 phases 收敛；新 transform 功能非本轮要求 |

## §8 十项硬验收：当前 focused evidence matrix

以下矩阵只记录当前源码与已执行的 focused behavior/smoke evidence；它不是 P5/P6、all-ten 或 full-gate 完成声明。`BLOCKED` 表示缺少既有 owner/API，不能用 source-text 或 fake fallback 代替。

| # | 当前状态 | 当前证据 / 精确 blocker |
| --- | --- | --- |
| 1 | **BLOCKED** | installed provider 有真实 runtime path；tool/agent/context/typed-input package slots 只有 CompositionOwner declaration/admission，service 没有 package registry/materializer adapters。缺少四中央文件不改的可执行 owner/API。 |
| 2 | **PASS (focused)** | fresh subprocess MemoryHost + TurnEngine 完成两轮 multi-tool，输出 `done:4`；abort run 提交 1 个结果并停止 pending call。 |
| 3 | **PASS (focused)** | privilege/security refusal 17 passed；approval/read-write side-effect 5 passed；lease seizure/late-write/fresh resume 3 passed；durable writes route through runtime/storage owners in these scenarios。 |
| 4 | **PASS (focused)** | same CaptureTool in fresh MemoryHost/RuntimeHost subprocess observed explicit session/run/call IDs and abort; RuntimeHost supplied workspace/progress callback and emitted `runtime.tool_progress`; no ContextVar path. |
| 5 | **PASS (focused)** | typed-facts/live-only matrix 6 passed; MemoryEventStore and isolated SQLite reject durable StreamFact, while RuntimeHost publishes live deltas and persists typed durable facts. |
| 6 | **PASS (focused)** | parameterized Read→Write matrix: MemoryEventStore 2 cases, SQLiteFactStore 2 cases, deterministic RuntimeHost 1, local OpenAI-compatible wire/runtime 1; total 6 passed. |
| 7 | **PASS (focused)** | crash/approval/fallback/timeout/fork/checkout selected cells passed; hard-kill suite 5 passed; timeout module 35 passed. The matrix remains focused and is not a claim that every historical test module is migrated. |
| 8 | **PASS (focused)** | crash/reopen matrix preserves completed native IDs and authorized args; read side-effect count remains 1, crashed in-flight call is never completed, resumed call gets a fresh ID; blocking side-effect count is exactly 2 (crash + fresh resume). |
| 9 | **BLOCKED** | ActiveComposition has pure `run_phase` only; no runtime trigger consumer or durable PhaseRecord repository/checkpoint owner. A package cannot recover phase semantics end-to-end without an upstream API/owner. |
| 10 | **PASS (focused)** | provider/core parsing matrix 9 passed: local OpenAI wire, finish/retry/fallback, streamed/native tool IDs and legal ToolTurn/FinalTurn; RuntimeHost governs selection/abort/recovery rather than reparsing. |

## 追加：工具结果与内部协议的类型收敛（P5 实施中）

用户新增要求：减少真实同进程协议中的匿名结构与非法状态组合，不把所有 `None` / `get` / `isinstance` 或工具 JSON body 都视为应删除的校验。本节记录仍在进行的实施；已有 core/MemoryHost 与 body-ownership scoped 证明仅覆盖下文明确列出的范围，不代表 RuntimeHost、SQLite 或全部调用方已完成。

**历史类型基线（非当前状态）；当前分步状态见下表：**

- `src/voidcode/tools/contracts.py`, `core/turns.py`, `core/transcript.py` own typed success/failure, output/body, `ReportedCall`, legal turn/result alternatives and the explicit model view. Core/MemoryHost and RuntimeHost focused behavior now cover the listed result/reader/replay paths; remaining stale callers are explicitly reported blockers, not silently treated as migrated.
- `runtime/execution/tool_result_projection.py`, `runtime/execution/turn_recovery.py`, `fact_codec.py`, `fact_store.py` and `run_loop.py` now have focused report/checkpoint/runtime evidence: canonical reports preserve identity/control, typed facts reject live-only deltas, and RuntimeHost persists governed facts before client projection. This is scoped evidence, not a full repository or P6 claim.
- `core/tool_context.py` typed Artifact/Transcript/Rule readers and `ReadResultBody`/canonical `ReportedCall` consumers are exercised through focused replay/tracker/write-guard/rule/resource smoke; package all-slot adapters and lifecycle phase persistence remain blocked as recorded in the ten-row matrix.
- `core/engine.py`, provider producer, MemoryHost and RuntimeHost use legal phase/outcome and explicit result-view seams; provider/runtime abort/recovery/governance evidence is listed only where the focused matrix actually ran.

**分步 ownership / 状态：**

| 步骤 | 阶段与 owner | 新增变更与完成边界 | 当前状态 |
| --- | --- | --- | --- |
| 1：共同工具结果与已完成调用 | P5 前置：shared tool contracts、实际 producer / host fact / control owner；P6：全部消费者最终切除 | 建立真实 typed success/error/result/output common contract；native correlation 与最终已授权 arguments 由实际 completed-call/host facts 拥有，不藏入任意 body。producer 一次生成 typed presentation/content；question/yield/progress/artifact 与实际消费的 builtin payload 由各自 owner 建模。package/MCP 真正开放的 JSON body 仅是有 scope 的边界数据，不能携带 authority/control；package-owned generic/typed DTO 不要求中央所有 tool-name union。 | 纯 core/MemoryHost 真实 SDK multi-tool、abort/partial-prefix continuation、reasoning 与 history/body isolation（#0DBF）；OpaqueToolBody 与 MemoryEventStore fact-publication 边界（#E35F）；report/checkpoint codec-v2 identity/control isolation及所列受限 parser/replay smoke（#832F）。独立 composition source（#718B）、pure composition（#B43C）与 fatal-once（#D219）是不同范围，不构成本行上述核心/存储证明。真实 RuntimeHost Read→Write、SQLite report replay 通过。provider #D714 限于 neutral static slice（75 定向测试、2 次真实 SDK 请求）；tool #C8E5 限于 21 个纯静态…
| 2：显式 context reader 协议 | P5：Artifact/Transcript/Rule capability producer、storage/file adapter 与 Read consumer；P6：旧 mapping helpers 切除 | 已知返回值直接 typed，missing/available/error/cursor 按实际合法 alternatives 表达；external/files/SQLite 解析一次，可信同进程消费者访问字段，不重复猜测 shape。 | `ReadResultBody` 是具体 file-page DTO，无 `path` 字段；每行保存 producer-owned `truncated`，canonical path 来自已授权调用 arguments，strict body decoder 拒绝旧 path-bearing/flagless 形状。真实 Read→ReportedCall→JSON→strict replay→tracker→Write guard 与规则读取 smoke 覆盖分页/clip/empty/bytecap，以及 Artifact/Transcript/Rule/archive/PDF 等实际 resource surfaces；相关 targeted tests 32 passed（`local://p5-read-changed-proof.json#EB34`）。仍不代表所有 P5 composition/store/task/fork/bundle 验收已完成。
| 3：core phase/result 合法状态 | P5：core TurnPlan/CallOutcome 与 provider/runtime host consumers；P6：旧组合式构造/解析切除 | 用明确合法 alternatives 表达阶段/结果；保留 abort before result 等真实 absence，不清除有效 `None` 或仅改名校验。复用 canonical checkpoint → CallSeed parser，persisted raw shape/version/integrity 校验仍保留在 trust boundary。 | core producer/MemoryHost 的真实 SDK multi-tool、partial-prefix continuation、原 reasoning 与合法 typed result 有 scoped 证明；RuntimeHost `ToolTurn`/`FinalTurn` Read→Write、SQLite report replay 及 deterministic actual-program output 有窄范围验证。真实 OpenAI SDK + SQLite reopened-runtime-owner #5623 同批恢复保留原始 reasoning/native call IDs，Read/Write 各执行一次，且 owner/ref/checkpoint 在 activation 前匹配（同进程新 owner，不是进程重启）。其余 provider callers、checkpoint/recovery 矩阵、lifecycle phases、queued task/fork、cold-runtime MCP、bundle/import admission、P6 与十项验收仍未完成。
| 4：authoritative result 与 isolated model view | P5：result/transcript ownership；P6：已证明冗余的复制/转发最终切除 | 明确不可被 projection 修改的 authoritative history 与隔离 typed model view；证明 raw history 不会突变后才删除冗余 deepcopy / forwarding，不 blanket 删除 defensive copies。 | core history/view、opaque body nested mutation isolation、strict report replay 与 focused SQLite/runtime projections 已通过；未迁移的 stale test fixtures 与 blocked package/phase consumers 仍不计入完成。

复用现有 typed `ToolInvocation` / `ToolDiagnostics` / `ToolResultView` / `CallSeed`，不新增结果框架，也不以 `TypedDict` / `cast` facade 冒充真实 cutover。继续迁移全部实际 producers/core/transcript/provider/runtime/codec/recovery/task consumers；不双写，不保留旧 dict helpers、aliases、重复 schema 或双重 data sources。

**边界与验收：** task output 已从 typed `BackgroundTaskResult` / group result 投影为 model/wire dict，这个实际序列化不是内部 typing debt；event/checkpoint/provider/MCP JSON parsing 属于真实边界，保留验证。以上步骤须执行十项验收中新增的真实结果/reader/control/合法状态场景；新 package 的 typed result 不改四个中央文件或中央 tool-name cases。inventory/removal review 仅补充证据，不新增 source-text、annotation 或 mock-echo tests，不声称新增计划已通过。

## 验证策略与交付规则

- 永久测试只留 backend 可观察行为：权限/错误、状态转换、边界、precedence、side effect 与数据完整性。不要为常量、错误文案、copy/forward、内部字段名或新 helper 再建 suite；更不能把旧 incidental assertions 改名保留。
- 每个阶段至少一条实际 smoke：执行真实变更路径并观察结果。provider 网络可用本地 transport fixture，文件/process/SQLite 用临时资源；throwaway 脚本验证后清除。涉及 UI 的验证只启动实际 CLI/TUI，观察输出、交互及 durable state，不增加永久自动测试。
- scoped backend checks 在 change set 集成后运行；最终 `mise run check`、完整 pre-commit 和 schema check（schema 受影响时）一次执行。实现阶段若发现当前 task 配置引用被删除 CLI/TUI tests，同步删除过时入口，不用空目录/空 suite 保绿。没有运行的检查不得报告通过。
- 每份阶段交付包括：真实消费者 cutover、旧路径删除、相应契约/索引更新、已执行行为证据与剩余 blocker；不交付 stub、fake fallback 或“后续接线”的 skeleton。

## 本轮已落地的有限清理

- `runtime/context/provider.py`：`blocking` 已包含所有 error diagnostics，删除再合并其子集 transform failures 的重复计算；仍然保留 transform failure 对 `mode=off` 的硬 block。debug transform projection 直接读取 Mapping，不再为只读 helper 复制 dict。开放 custom/persisted metadata 的 parser 校验保留，不把它误判为过度防御。
- `runtime/agent_capability.py`：shape 错误说明使用 snapshot owner 的真实版本；不测试错误文案，不删除持久化输入验证。
- 独立前置修复：首次 smoke import 发现 `runtime/reminders.py` 的重复原始 doc 段落和多余三引号导致 `SyntaxError`；只移除重复段落/引号，保留原 docstring 和所有 runtime 逻辑。
- 前置清理验证（历史结果，本次未重跑）：九组 context-transform failure × policy mode 的 before/after projection/policy smoke 输出一致，重复 blocking code 保持去重、无效 persisted snapshot 保持拒绝；`uv run pytest tests/unit/runtime/test_context_window_diagnostics.py -q` 为 **2 passed**。最终 `mise run check` 观察到 **1709 Python / 144 frontend**，完整 pre-commit 通过；CLI/TUI 实际程序 deterministic read smoke 通过。以上是前置清理的历史基线，不是 P0 本次新运行的检查。
- CLI/TUI 专用测试和其他非行为断言已按 [`testing.md`](../testing.md) 清理；这一策略调整不等于架构迁移，也不单独证明 UI 行为。本次没有运行 CLI/TUI smoke。

## 明确延期：审计 §9

任意 agent-to-agent bus、marketplace/dynamic remote plugin、cloud execution、scheduled runs、完整任意拓扑 multi-agent planner、OS sandbox 均不做。也不为它们提前造协议、config、事件或 stores。它们不影响本计划十项硬验收；进程内可信 extension 不是安全隔离，approval consent 也不是 OS containment。
