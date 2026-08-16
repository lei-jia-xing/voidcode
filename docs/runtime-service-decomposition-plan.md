# `runtime/service.py` 安全拆分计划

来源 issue：#428

## 目标

`src/voidcode/runtime/service.py` 仍是 runtime control plane 的主入口，但它同时承载执行入口、provider fallback、审批恢复、工具注册收窄、background task facade、配置 replay 与 capability 生命周期集成。拆分目标不是为了追求更小文件，而是把已经有测试保护的行为边界继续收敛到更明确的 runtime-owned collaborator。

本计划只定义安全拆分顺序、验收测试与禁止跨越的所有权边界。除非某个 slice 已经只剩 wrapper 清理，否则不把大规模实现重构列为本 issue 的交付要求。

## 当前热点

### 已经部分抽出的 collaborator

- `RuntimeRunLoopCoordinator`：`src/voidcode/runtime/run_loop.py` 已承载工具执行、provider transient retry、provider fallback loop、上下文压力事件与 graph step 推进；provider 错误重试、fallback 与终止 payload 判断已收束到 `src/voidcode/runtime/provider_fallback.py`。
- `RuntimeResumeCoordinator`：`src/voidcode/runtime/resume.py` 已承载 approval/question/provider-failure resume 的主要恢复逻辑；`service.py` 保留 `resume()` / `resume_stream()` public surface 与目标所有权校验。
- `RuntimeBackgroundTaskSupervisor`：`src/voidcode/runtime/background_tasks.py` 已承载 background task queue、worker lifecycle、result view、parent notification、cancel、reconciliation 与 lifecycle hook 触发；`service.py` 仅保留 public facade。
- `tool_provider.py`：已承载 builtin tool provider、agent allowlist/default scoping 与 local custom tool provider；`service.py` 仍组合 MCP/LSP/skill/task/question/background tools 并应用 workflow read-only policy。
- `execution_seams.py`：已承载 graph selection、cache key、fallback graph selection 与 session routing seams；`src/voidcode/runtime/config_materializer.py` 已承载 `EffectiveRuntimeConfig`、persisted runtime config parse/serialize、request override 和 persisted fallback model 显式错误判断，`service.py` 仍负责 registry、capability snapshot、workflow snapshot、agent validation 与配置优先级组合。
- `provider_catalog_cache.py`：已承载 provider model catalog cache 的 JSON hydrate/persist、坏条目容错与“不覆盖活跃 catalog”规则；它只依赖 provider registry 和 cache path。
- `provider_catalog_query.py`：已承载 provider model catalog 的只读 models/catalog projection、catalog override 与 inferred metadata 合并，以及 `ProviderModelsResult` 构造；refresh、auth presence 与 readiness 判断仍由 runtime service 组合。Runtime config reload 替换 provider registry 时必须重绑定 cache/query collaborator，避免持有旧 registry。
- `provider_inspection.py`：统一承载 provider summary projection、resolved readiness facts 的 status/ok/guidance 决策表、validation result projection、configured-provider 判断、API-key auth presence 与 Google/Copilot OAuth presence 特例；runtime service 仍拥有 effective config/catalog/reasoning facts、remote validation/refresh 与 inspect 调用顺序。Runtime config reload 替换 resolver/config 时必须同步重绑定 inspector。
- `tool_scope.py`：已承载 agent scoping 后的 runtime/workflow/memory tool policy materialization、delegated child manifest allowlist denial，并让 provider-visible registry 与 raw-call denial 查询共享同一 policy decision 来源；runtime service 仍拥有 builtin/local/MCP/LSP tool construction、effective config、session routing truth 与 execution ordering。Runtime config reload 改变 memory capability 时必须同步重绑定 resolver。
- `tool_materializer.py`：已承载已构造 base/MCP/local tool 的 registry 合并，并显式保留现有 collision 语义：MCP 同名项覆盖 base，local 同名项通过 `ToolRegistry.from_tools()` fail fast。内部 `RuntimeToolMaterialization` 同时保存来源类别、来源 identity、capability fingerprint 和稳定 generation；local fingerprint 额外包含 manifest command，能识别定义相同但执行入口变化的 drift。它不发现或构造工具、不拥有 MCP/LSP lifecycle、不做 agent/workflow scoping，也不执行 permission/approval；`service.py` 仍决定 refresh 时机和 session owner。
- `agent_capability.py`：已承载 agent/prompt/tool/delegation/MCP capability 的纯 payload projection；tool projection 接收本次 run 已经 scope 完成的 registry，避免 snapshot 与实际执行 registry 分别计算。父 session 查询、snapshot 写入和 refresh 时机仍由 `service.py` 持有。

### 仍集中在 `service.py` 的高风险区域

- Public runtime entry/replay：`run_stream()`, `resume()`, `resume_stream()`, `session_result()` 仍决定何时 replay、何时恢复 provider failure、何时 reconcile parent background notifications。
- Runtime config truth：`_runtime_config_for_request()`, `_runtime_config_metadata()`, `_effective_runtime_config_from_metadata()`, `_config_with_request_agent_override()` 仍共同决定 request overrides、persisted replay、agent defaults、fallback chain 与 capability snapshots。
- Tool registry scoping：`_tool_registry_for_effective_config()`, `_tool_registry_with_workflow_policy()`, `_delegation_tool_policy_error()`, `_workflow_tool_policy_error()` 仍是 provider-visible schema 与 raw tool-call guardrail 的最后 runtime enforcement。
- Background task public facade：`start_background_task()`, `load_background_task_result()`, `cancel_background_task()` 保持 public API；内部 worker state 只通过 supervisor 访问。
- Provider fallback metadata：`run_loop.py` 执行 fallback，但 fallback chain、persisted target、provider attempt 与 transient retry config 仍依赖 `service.py` 的 config/materialization helpers。

## 拆分原则

1. Runtime 继续拥有治理：权限、审批、工具注册、hook lifecycle、session truth、background task truth、provider fallback 与 capability lifecycle 都不能迁移到 graph、CLI、HTTP、Web/TUI 或 hook 脚本。
2. Graph 只推进执行步骤：新增 collaborator 可以服务 graph loop，但不应让 graph 直接知道 client/session persistence/tool registry ownership。
3. Clients 只消费契约：CLI/HTTP/Web/TUI 可以调用 runtime public surface 或 contract payload，不能自己拼接 background/result/approval/fallback truth。
4. 每个 slice 先建立 contract tests，再移动代码；移动后 public payload、事件顺序、session metadata 和 SQLite truth 必须满足当前版本契约。
5. collaborator 接管实现时同步迁移所有调用方并删除 private proxy。

## 建议拆分顺序

### 1. 固化 background task lifecycle 边界

当前 `RuntimeBackgroundTaskSupervisor` 已经是最接近完成的拆分。下一步应把它定义为正式边界，而不是继续让 `service.py` 承担语义。

**保留在 `service.py`**

- Public methods：`start_background_task()`, `load_background_task()`, `load_background_task_result()`, `list_background_tasks()`, `cancel_background_task()`。
- Runtime-owned request validation、workspace validation 与 session store ownership。
- Tests and callers access the owning collaborator or public runtime facade directly.

**留在 / 移入 `background_tasks.py`**

- Queue drain、worker thread lifecycle、concurrency slots、rate-limit retry backoff、cancel while queued/running/waiting、terminal reconciliation。
- Parent notification event append/dedupe、`BackgroundTaskResult` projection、`background_task_*` lifecycle hooks、delegated result hooks。
- `background_output` / `background_cancel` tool-facing behavior should continue to call runtime public methods, not session store directly.

**行为保护测试计划**

- Keep: `tests/unit/runtime/test_runtime_service_extensions.py` background task coverage around completion hook, queued cancel hook, delegated result hook, provider-failure resume reconciliation/finalization, and parent notification events.
- Keep: `tests/unit/tools/test_background_task_tools.py` for tool-level `background_output` / `background_cancel` payloads, full-session bounds, unknown task handling, terminal task handling and retrieval guidance.
- Keep: `tests/unit/interface/test_cli_delegated_parity.py` for CLI task status/output/list/cancel correlation fields.
- Add before deeper cleanup: a focused test that a restarted runtime calls `list_background_tasks()` / `session_result()` and backfills exactly one parent notification for each terminal or approval-blocked child.
- Add before worker cleanup: a fake provider test where a running background child is cancelled while waiting for approval and the child pending approval/question records are cleared before terminal task truth is persisted.

**Safe first slice**

No broad move is needed; the safe first slice is documentation plus tests for restart/backfill idempotence. Implementation cleanup can follow by moving remaining test-only private wrapper callers to supervisor methods while leaving public `VoidCodeRuntime` methods intact.

### 2. Separate provider fallback policy from run-loop mechanics

当前状态：`src/voidcode/runtime/provider_fallback.py` 已实现该边界的安全子集。它只根据 `ProviderExecutionError`、provider attempt、retry attempt、transient retry config 和可用 fallback target 返回 typed decision。`run_loop.py` 仍负责发出 `runtime.provider_fallback` / `runtime.provider_transient_retry` 事件、等待 retry delay、写入 `provider_attempt` / `provider_retry_attempt` metadata、切换 fallback graph，并把终止 decision 映射为 runtime failure chunk。provider registry、auth resolver、config materialization 与 fallback graph selection 没有迁出 runtime ownership。

剩余工作应继续保持这一分工：policy helper 只判断 which errors are retryable, which fallback target is next, and which terminal error payload is emitted after exhaustion；metadata 持久化、事件顺序和 graph rebuilding 仍留在 runtime run loop / execution seam 内。

**保留在 `service.py`**

- Runtime config materialization and persisted session metadata truth.
- Provider registry ownership and auth resolver ownership.
- `_effective_runtime_config_from_metadata()` until a dedicated config materializer exists.

**提取候选**

- `RuntimeProviderFallbackCoordinator` or smaller pure helper module fed with `ResolvedProviderChain`, `EffectiveRuntimeConfig`, provider attempt, retry attempt and `ProviderExecutionError`.
- Keep graph rebuilding through existing `execution_seams.py` functions; the coordinator should return a decision, not call graph/client/storage directly.

**行为保护测试计划**

- Keep: provider fallback tests in `tests/unit/runtime/test_runtime_service_extensions.py` covering fallback event payloads, provider error details preservation, stream error mapping, retry attempt reset after successful provider call, persisted session provider config on retry, cancellation mapping without fallback, JSON context limit classification without fallback, and fallback exhaustion after multiple targets.
- Add before extraction: a table-style unit test for fallback decision inputs that covers retryable transient errors, fallbackable provider errors, non-fallbackable cancelled/context-limit errors, exhausted fallback chain, and provider error details passthrough.
- Add before extraction: a resume test proving `provider_attempt` and `provider_retry_attempt` survive persisted metadata and select the same target after restart.

**Acceptance gate**

- The emitted `runtime.provider_fallback`, `runtime.provider_transient_retry`, terminal `runtime.failed` payloads and final session metadata must be byte-for-byte equivalent for covered fake provider scenarios.

### 3. Keep approval/question/provider-failure resume as a runtime resume coordinator

`RuntimeResumeCoordinator` already owns most resume mechanics. The next work should reduce duplication between approval and question resume paths without moving approval ownership out of runtime.

**保留在 `service.py`**

- Public `resume()` and `resume_stream()` method shape.
- Validation that leader/parent sessions cannot answer or approve a child-owned pending request.
- Background task finalization after child resume.

**留在 / 移入 `resume.py`**

- Checkpoint envelope parsing and validation.
- Rebuild of prompt/tool_results/session metadata from checkpoint or stored events.
- Resume-specific lifecycle hooks, ACP startup/finalization resequencing, MCP release events, and persistence of resumed response.

**行为保护测试计划**

- Keep: resume checkpoint tests in `tests/unit/runtime/test_runtime_service_extensions.py` covering persisted checkpoint creation, restart resume, required-checkpoint enforcement, corrupt JSON rejection, payload/kind/version mismatch rejection, malformed tool result rejection, strict skill binding validation, and no duplicate session_start hooks.
- Keep: approval/question notification tests covering approval-blocked notifications, superseded approval blockers, session idle hook preservation, and end-hook failure not overriding terminal truth.
- Add before deduplication: a characterization test asserting approval resume and question resume both preserve MCP release ordering and do not re-emit `runtime.session_started`.
- Add before moving target-ownership code: a test where a parent session tries to approve a child-owned pending approval and receives the existing ownership error.

**Acceptance gate**

- Existing stored sessions must replay through `resume(session_id)` unchanged, and approval denial must remain tool-level feedback for provider sessions rather than converting into a terminal runtime failure.

### 4. Extract tool registry scoping and raw-call guardrails last

Tool scoping affects what schemas the provider sees and what raw tool calls runtime allows. It should be extracted after background/resume/fallback seams are stable because all those paths rebuild tool registries.

当前状态：`RuntimeToolScopeResolver` 已承载 agent scoping、workflow/runtime read-only policy、memory tool policy，以及 provider-visible registry 与 raw-call 共用的 denial decision。`RuntimeToolMaterializer` 已收束 base/MCP/local registry 合并，保留 MCP override 与 local collision fail-fast 的既有差异。`agent_capability.py` 的 tool projection 与执行前的最终 scoped registry 同源；fresh run 当前在同一 run 内复用该 registry，resume 继续使用持久化 capability truth。来源 discovery/construction、refresh sequencing、session owner 和 capability lifecycle 仍由 runtime service 持有。

Generation 没有使用进程内自增计数：该计数跨 restart 不稳定，也无法区分同名工具的来源或实现漂移。materialization 区分 base、MCP 和 local 来源，并从规范化来源 identity、capability definition 与 local manifest command 派生稳定 generation。fresh run 将最终 scoped materialization generation 写入 version 2 capability snapshot；persisted snapshot 缺失版本、版本非 2 或缺失 generation 时直接失败，不迁移、不合成，也不按当前 workspace 来源重新生成。

**保留在 `service.py`**

- Construction of runtime-scoped LSP/MCP/skill/task/question/background tool instances.
- Runtime-owned capability manager lifecycle and workspace/session ownership.

**提取候选**

- Capability snapshot contract 固定为 version 2，并持久化当前 run 的 scoped materialization generation；其他版本直接失败。不要加入 watcher、隐式热更新或公开 plugin contract。
- `tool_provider.py` 继续提供 builtin/local tools；materializer 不扩展为通用 plugin loader，resolver 也不复制工具构造。

**行为保护测试计划**

- Keep: `tests/unit/runtime/test_tool_provider.py` or equivalent scoped provider tests for manifest allowlist, builtin disable, explicit allowlist/default filtering and pattern matching.
- Keep: runtime tests in `tests/unit/runtime/test_runtime_service_extensions.py` covering child preset tool guardrails, worker no nested delegation by default, workflow read-only default, local custom tool scoping, skill force-load isolation and MCP tool visibility.
- Add before extraction: a focused test that a provider-visible allowlist and a malicious raw call produce the existing explicit delegation/workflow denial message rather than `unknown tool`.
- Add before extraction: a test that workflow read-only default filters schemas and also rejects raw write-tool calls at execution time.

**Acceptance gate**

- Provider-visible tool definitions, runtime lookup behavior and denial error messages must remain stable for leader, product and delegated child presets.

### 5. Move persisted runtime config replay behind a materializer only after the above

当前状态：`src/voidcode/runtime/config_materializer.py` 已实现该边界的安全子集。它承载 `EffectiveRuntimeConfig`、`PERSISTED_RUNTIME_CONFIG_KEYS`、`serialize_runtime_config_core()`、`parse_persisted_runtime_config()`、request override helper、persisted permission/tools/provider fallback parsing，以及 `fallback_models` 缺少 persisted `model` 时的显式错误。`service.py` 仍负责读取 base `RuntimeConfig`、解析 provider registry target、验证 agent execution、生成 hook/LSP/MCP/workflow snapshots、组合 categories/agents，并决定 request metadata 或 persisted session metadata 何时具有权威性。

剩余工作应避免把 materializer 扩大成新的 runtime owner。`service.py` 仍是配置优先级、registry/capability 快照和 workflow/agent validation 的组合点；materializer 只处理明确 payload 的 parse/serialize 与显式错误。

**保留在 `service.py`**

- Loading base `RuntimeConfig` and owning provider/model/agent registries.
- Deciding when request metadata or persisted session metadata is authoritative.

**提取候选**

- Pure parsing of persisted `runtime_config` fields, including unknown-key rejection, provider config parse errors, fallback model parse, context window parse, tools parse, agent parse, LSP/MCP snapshot projection and workflow snapshot projection.

**行为保护测试计划**

- Keep: effective config tests around persisted provider fallback, malformed resolved provider snapshots, provider retry persisted config, agent capability snapshots and runtime config metadata.
- Add before extraction: parameterized tests for every `_PERSISTED_RUNTIME_CONFIG_KEYS` field that prove accepted values and existing rejection text.
- Add before extraction: a restart/replay test using a persisted provider-backed child session with agent, skills, tools, LSP, MCP and workflow metadata present.

**Acceptance gate**

- No new fallback defaults, migration shims or best-effort parsing are introduced. Invalid persisted metadata continues to fail fast with the existing error wording.

## Explicit non-goals

- Do not move approval policy, provider fallback, background lifecycle, task routing, hook lifecycle, MCP/LSP lifecycle or tool allowlist enforcement into `graph/`.
- Do not let CLI, HTTP, Web, TUI or ACP create alternate execution/retry/cancel paths; they remain adapters over runtime contracts.
- Do not change `_EXECUTABLE_AGENT_PRESETS`, supported delegated presets, manifest tool allowlist semantics or workflow read-only policy as part of decomposition.
- Do not broaden delegated child execution into arbitrary multi-agent topology, peer-to-peer agent bus, scheduler semantics or workspace-scoped MCP lifecycle.
- Do not replace persisted session/background task truth with hook output, prompt text, in-memory notifications or client-local state.

## Verification matrix for future PRs

Any PR that moves code out of `service.py` should run the smallest relevant subset plus the full repo check before merge:

```bash
uv run pytest tests/unit/runtime/test_runtime_service_extensions.py -k "background or resume or approval or fallback or tool_policy or workflow"
uv run pytest tests/unit/tools/test_background_task_tools.py tests/unit/runtime/test_mcp.py -k "background or cancel or output or mcp"
uv run pytest tests/unit/interface/test_cli_delegated_parity.py
mise run check
```

If `mise run check` fails for unrelated pre-existing reasons, the PR must name the failing command and preserve green targeted tests for the touched boundary.
