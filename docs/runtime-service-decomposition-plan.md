# `runtime/service.py` 安全拆分计划

来源 issue：#428

## 目标

`src/voidcode/runtime/service.py` 仍是 runtime control plane 的主入口，但它同时承载执行入口、provider fallback、审批恢复、工具注册收窄、background task 兼容 wrapper、配置 replay 与 capability 生命周期集成。拆分目标不是为了追求更小文件，而是在不改变 runtime 语义的前提下，把已经有测试保护的行为边界继续收敛到更明确的 runtime-owned collaborator。

本计划只定义安全拆分顺序、验收测试与禁止跨越的所有权边界。除非某个 slice 已经只剩 wrapper 清理，否则不把大规模实现重构列为本 issue 的交付要求。

## 当前热点

### 已经部分抽出的 collaborator

- `RuntimeRunLoopCoordinator`：`src/voidcode/runtime/run_loop.py` 已承载工具执行、provider transient retry、provider fallback loop、上下文压力事件与 graph step 推进；`service.py` 的 `_execute_graph_loop()` 现在是兼容转发入口。
- `RuntimeResumeCoordinator`：`src/voidcode/runtime/resume.py` 已承载 approval/question/provider-failure resume 的主要恢复逻辑；`service.py` 仍保留 `resume()` / `resume_stream()` public surface、目标所有权校验与兼容转发入口。
- `RuntimeBackgroundTaskSupervisor`：`src/voidcode/runtime/background_tasks.py` 已承载 background task queue、worker lifecycle、result view、parent notification、cancel、reconciliation 与 lifecycle hook 触发；`service.py` 仍保留 public surface 与测试兼容 wrapper。
- `tool_provider.py`：已承载 builtin tool provider、agent allowlist/default scoping 与 local custom tool provider；`service.py` 仍组合 MCP/LSP/skill/task/question/background tools 并应用 workflow read-only policy。
- `execution_seams.py`：已承载 graph selection、cache key、fallback graph selection 与 session routing seams；`service.py` 仍负责把 persisted metadata 还原成 `EffectiveRuntimeConfig`。

### 仍集中在 `service.py` 的高风险区域

- Public runtime entry/replay：`run_stream()`, `resume()`, `resume_stream()`, `session_result()` 仍决定何时 replay、何时恢复 provider failure、何时 reconcile parent background notifications。
- Runtime config truth：`_runtime_config_for_request()`, `_runtime_config_metadata()`, `_effective_runtime_config_from_metadata()`, `_config_with_request_agent_override()` 仍共同决定 request overrides、persisted replay、agent defaults、fallback chain 与 capability snapshots。
- Tool registry scoping：`_tool_registry_for_effective_config()`, `_tool_registry_with_workflow_policy()`, `_delegation_tool_policy_error()`, `_workflow_tool_policy_error()` 仍是 provider-visible schema 与 raw tool-call guardrail 的最后 runtime enforcement。
- Background task compatibility seam：`start_background_task()`, `load_background_task_result()`, `cancel_background_task()`, `_run_background_task_worker()` 等已经是 wrapper，但其调用方依赖这些 public/private names，不能直接删除。
- Provider fallback metadata：`run_loop.py` 执行 fallback，但 fallback chain、persisted target、provider attempt 与 transient retry config 仍依赖 `service.py` 的 config/materialization helpers。

## 拆分原则

1. Runtime 继续拥有治理：权限、审批、工具注册、hook lifecycle、session truth、background task truth、provider fallback 与 capability lifecycle 都不能迁移到 graph、CLI、HTTP、Web/TUI 或 hook 脚本。
2. Graph 只推进执行步骤：新增 collaborator 可以服务 graph loop，但不应让 graph 直接知道 client/session persistence/tool registry ownership。
3. Clients 只消费契约：CLI/HTTP/Web/TUI 可以调用 runtime public surface 或 contract payload，不能自己拼接 background/result/approval/fallback truth。
4. 每个 slice 先建立 characterization tests，再移动代码；移动后 public payload、事件顺序、session metadata、SQLite truth 和 error text 必须保持可重放兼容。
5. 保留兼容 wrapper 至少一个 PR：先让 collaborator 承载实现，再在后续 PR 评估是否删除 private wrapper；不要在同一个 PR 内移动逻辑又大面积改调用方。

## 建议拆分顺序

### 1. 固化 background task lifecycle 边界

当前 `RuntimeBackgroundTaskSupervisor` 已经是最接近完成的拆分。下一步应把它定义为正式边界，而不是继续让 `service.py` 承担语义。

**保留在 `service.py`**

- Public methods：`start_background_task()`, `load_background_task()`, `load_background_task_result()`, `list_background_tasks()`, `cancel_background_task()`。
- Runtime-owned request validation、workspace validation 与 session store ownership。
- Compatibility wrappers for tests/callers until all internal references stop depending on private `service.py` names.

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

`run_loop.py` already performs provider retry/fallback mechanics. The next boundary should isolate policy inputs: which errors are retryable, which fallback target is next, which metadata must be persisted, and which terminal error payload is emitted after exhaustion.

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

- Keep: resume checkpoint tests in `tests/unit/runtime/test_runtime_service_extensions.py` covering persisted checkpoint creation, restart resume, checkpoint missing fallback, corrupt JSON rejection, payload/kind/version mismatch rejection, malformed tool result rejection, null successful tool content preservation, skill binding mismatch event, and no duplicate session_start hooks.
- Keep: approval/question notification tests covering approval-blocked notifications, superseded approval blockers, session idle hook preservation, and end-hook failure not overriding terminal truth.
- Add before deduplication: a characterization test asserting approval resume and question resume both preserve MCP release ordering and do not re-emit `runtime.session_started`.
- Add before moving target-ownership code: a test where a parent session tries to approve a child-owned pending approval and receives the existing ownership error.

**Acceptance gate**

- Existing stored sessions must replay through `resume(session_id)` unchanged, and approval denial must remain tool-level feedback for provider sessions rather than converting into a terminal runtime failure.

### 4. Extract tool registry scoping and raw-call guardrails last

Tool scoping affects what schemas the provider sees and what raw tool calls runtime allows. It should be extracted after background/resume/fallback seams are stable because all those paths rebuild tool registries.

**保留在 `service.py`**

- Construction of runtime-scoped LSP/MCP/skill/task/question/background tool instances.
- Runtime-owned capability manager lifecycle and workspace/session ownership.

**提取候选**

- `RuntimeToolScopeResolver` that takes base registry, effective config, workflow snapshot and session metadata, then returns visible registry plus explicit guardrail errors for delegated/workflow raw calls.
- `tool_provider.py` should remain the provider of builtin/local tools; the resolver should not duplicate tool construction.

**行为保护测试计划**

- Keep: `tests/unit/runtime/test_tool_provider.py` or equivalent scoped provider tests for manifest allowlist, builtin disable, explicit allowlist/default filtering and pattern matching.
- Keep: runtime tests in `tests/unit/runtime/test_runtime_service_extensions.py` covering child preset tool guardrails, worker no nested delegation by default, workflow read-only default, local custom tool scoping, skill force-load isolation and MCP tool visibility.
- Add before extraction: a focused test that a provider-visible allowlist and a malicious raw call produce the existing explicit delegation/workflow denial message rather than `unknown tool`.
- Add before extraction: a test that workflow read-only default filters schemas and also rejects raw write-tool calls at execution time.

**Acceptance gate**

- Provider-visible tool definitions, runtime lookup behavior and denial error messages must remain stable for leader, product and delegated child presets.

### 5. Move persisted runtime config replay behind a materializer only after the above

`_effective_runtime_config_from_metadata()` is still the densest runtime truth function. It should not be the first extraction because every other slice depends on it. Once the other collaborators are stable, move parsing/validation into a `RuntimeConfigMaterializer` that is still owned by `runtime/`.

**保留在 `service.py`**

- Loading base `RuntimeConfig` and owning provider/model/agent registries.
- Deciding when request metadata or persisted session metadata is authoritative.

**提取候选**

- Pure parsing of persisted `runtime_config` fields, including unknown-key rejection, provider config parse errors, fallback model parse, context window parse, tools parse, agent/category parse, LSP/MCP snapshot projection and workflow snapshot projection.

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
