# Runtime Extension Points 契约

## 目的

定义 VoidCode 如何在不引入通用 plugin bus、不创建第二套执行控制面的前提下扩展能力。

核心模型是：

> typed extension point 负责行为；typed runtime event 解释行为。

## 当前方向

Runtime 继续拥有 session truth、工具执行、审批、provider fallback、context assembly、background task lifecycle 以及 replay/resume 行为。

extension point 只能通过类型化 input/output contract 转换或校验 runtime-owned 数据。runtime event 可以描述这些决策，但 event 不拥有 authority，客户端也不能通过 event 改写 runtime truth。

## Harness-like runtime 语义

在 VoidCode 里，harness-like runtime 指的是：

1. 运行时拥有显式 `read_only`、权限、工具可见性、shell 安全、hooks、session event 和 delegated-task 约束的控制面。
2. 结构性策略在工具执行前完成，不依赖 prompt wording 充当唯一防线；prompt guidance 只解释已解析策略，不能创建、放宽或隐藏 enforcement。
3. Shell execution retains runtime permission and timeout governance, but does not
   guess package managers, normalize commands, or inject non-interactive variables.
4. prompt stack and observability must be replayable, explainable, and redacted; observable payloads expose only bounded fragment metadata / redacted previews, not complete prompts, skill bodies, or secret-like values.
5. registry 只能做受控解析，不能绕开 runtime policy。
6. 新请求不接受已移除的 runtime mode 或 execution 字段；只读执行姿态必须通过显式 `read_only` 表达。

这不意味着复制 OpenHands、Continue、Aider 或 Open Interpreter，也不意味着引入任意多智能体拓扑、插件市场、云端执行、IDE 插件执行路径，或者把 AGPL 实现代码搬进来。

## Policy precedence

固定优先级，从高到低如下：

1. Hardcoded safety denylist and workspace/session invariants.
2. Explicit request `read_only` policy.
3. Parent session/delegated-task inherited constraints.
4. Workspace/project runtime config.
5. Agent manifest/tool allowlist.
6. CLI flags that map into runtime request fields.
7. Default tool registry/provider capabilities.

CLI flags never bypass runtime request policy, they only populate request fields.


## Runtime Harness Policy v1

Runtime Harness Policy v1 is the runtime-owned authorization and explanation layer for a single turn. It materializes effective intent, tool, delegation, and hook decisions into one persisted `RuntimePolicySnapshot`. Agent prompt material is the ordinary body selected by the resolved manifest, with optional local guidance supplied through `prompt_append`. Extension points may contribute validated declarations, diagnostics, or narrowing guidance, but runtime policy remains the only source of authorization.

`RuntimePolicySnapshot` schema v2 is a JSON-serializable, redacted object with these required fields:

| Field | Required shape | Meaning |
| --- | --- | --- |
| `schema_version` | integer, `2` | Snapshot schema version. Unsupported versions fail fast on load/import. |
| `policy_version` | string | Version of the materialization rules used to compute this snapshot. |
| `created_at` or `turn_id` | RFC3339 timestamp or stable turn identifier | Identifies when/for which turn the snapshot was created without exposing prompt body. |
| `agent_preset` | string | Selected runtime agent preset for the run or child session. |
| `agent_manifest_id` | string or null | Resolved builtin/custom manifest id used for capability binding. |
| `intent` | object | Bounded neutral intent metadata: label, confidence, matched rule ids, and optional guidance ids. It cannot grant or infer capability. Heuristic free-text classification is intentionally not part of v1. |
| `tool_policy` | object | Effective visible/callable tool policy, including allowed and denied tool ids plus stable denial reasons. |
| `delegation_policy` | object | Effective child routing policy, allowed child presets/categories, denied targets, and stable denial reasons. |
| `hook_policy` | object | Named event-scoped hook policy with allowed observe/report/cancel/guidance actions. Hooks are non-authoritative. |
| `precedence_trace` | array | Ordered, audit-safe decisions showing which source allowed, narrowed, or denied each policy facet. |
| `diagnostics` | object | Redacted bounded diagnostics. No raw prompts, skill bodies, env values, credentials, or unrestricted metadata escape hatches. |

### Runtime Harness Policy precedence

Policy materialization uses this fixed order from highest to lowest authority:

1. Runtime hard denials and security invariants, including plan non-delegation.
2. Persisted session policy for resume/replay/import when a snapshot already exists.
3. Validated runtime config and schema-bounded policy fields.
4. Agent manifest declared capabilities and top-level/subagent mode.
5. Request/session options that can only select or narrow allowed behavior.
6. Hook preset metadata, which is guidance/guard/report only.
7. Bounded neutral intent metadata, which is non-authoritative and cannot grant or infer capability.
8. Runtime defaults and registry/provider capabilities.

Lower-precedence inputs may narrow or annotate higher-precedence decisions, but they cannot grant a tool, hook action, MCP binding, delegation target, approval, or plan delegation denied above them. Intent metadata cannot grant capabilities or reintroduce heuristic prompt classification. Hooks cannot grant capabilities or mutate persisted policy truth.

### Bounded observability

Runtime attaches a `runtime_policy` object to `runtime.request_received`; that object carries a nested `materialization` marker (`materialization.kind == "runtime_policy_materialized"`, `source == "runtime_control_plane"`) recording that the runtime control plane has materialized policy for this turn. There is no standalone `runtime.policy_materialized` event in the event vocabulary. This payload is a bounded projection of the persisted `RuntimePolicySnapshot`, not a second policy source. It exposes schema/policy version, read-only state, agent ids, neutral intent metadata, allowed/denied tool and delegation ids, hook policy actions/scopes, precedence trace, and diagnostics. Lists are capped, strings are preview-sized, and the projection does not expose raw prompt bodies, skill bodies, secrets, env values, or unbounded policy inputs.

Tool allow/deny and approval decisions remain observable through `runtime.tool_lookup_succeeded`, `runtime.permission_resolved`, `runtime.approval_requested`, `runtime.approval_resolved`, `runtime.tool_started`, `runtime.tool_completed`, and `runtime.failed` denial payloads. Delegation allow/deny remains observable through background/delegated lifecycle events and explicit runtime failure payloads for policy denials. Hook decisions remain observable through `runtime.tool_hook_pre` / `runtime.tool_hook_post` with `hook_policy.authoritative=false`.

Sessions must contain a stored schema-v2 runtime policy snapshot on replay and debug surfaces. Removing the dead `prompt_activation` field changed the snapshot schema; persisted v1 snapshots are unsupported and fail fast without migration. This does not change Runtime Harness Policy config version `v1`.

### Plan delegation invariant

`default` 是唯一可作为 top-level active builtin agent 执行的 preset。`plan` 是 runtime-owned `task` delegation path 上的只读 plan child，必须通过 `subagent_type="plan"` 作为 delegated child 执行，并通过 `yield` 将 plan 交回 parent。它不能作为 top-level active agent，也不能作为任意 child-spawn orchestrator。Builtin `mode=primary/subagent` 是路由资格声明，不取代 runtime policy 对工具和 delegation 的治理。旧 builtin IDs `leader`、`worker`、`advisor`、`researcher`、`product` 及 prompt-profile keys 已移除，无 aliases 或迁移；旧存储 routes 可能自然失败。Custom namespace 不受这些旧名称限制，custom id `leader` 是独立 custom agent，不是 alias。

### v1 non-goals

Runtime Harness Policy v1 不定义通用 policy DSL、LLM/heuristic intent classifier、任意 multi-agent topology、plan top-level execution、agent-to-agent bus、MCP redesign、marketplace/dynamic plugin system 或 prompt-text enforcement layer。它保留现有 config gates 下的 MCP。

## Extension Families

### Context transforms

Context transform 在 runtime-owned context assembly 阶段贡献有界 provider-context injection 或 diagnostic。

当前例子：

- hook preset guidance
- runtime file rules

要求：

- 由 runtime-managed priority 决定执行顺序
- 可配置时必须通过 validated refs 选择
- trace metadata 持久化到 context-window metadata
- failure policy 由 runtime 处理
- observability event payload 不携带完整 prompt / rule / skill 注入正文

### Context transform provider descriptor

每个 `RuntimeContextTransformProvider` 都必须提供稳定的 `provider_id`、
`provider_version`、`scope`、`priority` 与 `failure_policy`。当前唯一允许的 scope 是
`provider_context`：provider 只能返回有界 injection 或 diagnostic，不能修改原始 request
prompt、工具参数、SQLite/session truth、approval/denial、owner boundary 或 tool allowlist。
registry 负责按 `(priority, provider_id)` 排序、校验唯一 id，并将 provider metadata 与
有界 trace 纳入现有 `context_transform` metadata；这不是新的 event bus 或 executor。
失败诊断的 severity 由 request-level context-transform `failure_policy` 决定；trace
分别保留 provider 声明的 `failure_policy` 与 request policy 的 `request_failure_policy`，
不能用 provider metadata 覆盖 runtime request policy。

### Prompt command registry

Prompt commands use the runtime-owned authoritative discovery path for both request resolution and `voidcode commands list/show`. Precedence is builtin, admitted/injected extension, user, `commands/`, then `.voidcode/commands/`; later sources replace earlier commands by normalized name. The default user directory is `$XDG_CONFIG_HOME/voidcode/commands`, or `~/.config/voidcode/commands` when `XDG_CONFIG_HOME` is unset. The CLI's explicit `--user-commands-dir` overrides only that user source.

Commands expand prompt text; they do not own policy, tools, session truth, or execution. Extensions provide typed `CommandDefinition` values and runtime assigns their source as `extension`.

### `before_agent_start`

The runtime dispatches admitted `before-agent-start` bindings once for each new request, after command expansion and before provider-context assembly or tool startup. They receive only the original prompt, current effective prompt, and resolved command metadata. Bindings run by ascending `(priority, name)`; transformations may replace only the effective prompt, while a block or callback error fails the request closed. This callback is distinct from `typed-input`, which remains a pre-tool input hook.

These are trusted installed Python callbacks, not sandboxed code; no runtime authority is handed through their ABI. Keep handlers pure/idempotent: the runtime does not promise exactly-once external side effects across an unpersisted crash window.

`runtime.before_agent_start` persists bounded handler name/version and status metadata, not a prompt body field. A block reason is callback-supplied and MUST NOT include prompt text or secrets. At most 32 handler records are exposed; names, versions, and reasons are capped at 240 characters, with an omitted count. The full effective prompt is stored only in existing session/checkpoint metadata for provider input. Approval, question, and interrupted-run continuations resume from that prepared prompt without re-dispatching the callback; a later fresh request in the same session dispatches it for that new request.

### Provider message validation（未实现）

Provider message validation **不属于**当前实现：代码库中不存在 provider message validator（无 `message_validator` / `ProviderMessageValidator` / `validate_provider_message` 符号）。这是显式非目标，而不是延后的 permissive contract；在出现经过验证的实现前，不定义该 family 的 input/output shape。

### Capability materialization

agent、skill、MCP、LSP、provider 与 workflow declaration 会 materialize 成 runtime-owned capability snapshot。

declaration 可以选择或收窄 capability，但不能授予 permission、绕过 tool allowlist、启动 unmanaged process，或重定义 delegated/session truth。

### Package Composition & Lifecycle Phases

已安装包组件支持 7 种 slots（`provider`、`tool`、`agent`、`context`、`typed-input`、`command`、`before-agent-start`）与 8 种生命周期 triggers。`command` 在 admission/preflight 中验证为 `CommandDefinition`，不会执行 factory；`before-agent-start` factory 只在 composition activation 后构造并绑定已声明的 handler。

1. **Factory 窄上下文 (Narrow Context Boundary)**:
   组件 factory ABI 统一为 `factory(value, context)`。`context` 必须是不可变窄上下文 `CompositionRef`（仅包含 `workspace`、`owner` [SessionCompositionOwner | TaskCompositionOwner]、`binding_id` 和 `plan_id`）；绝不通过 ABI 传递 `VoidCodeRuntime` 实例或内部能力句柄，也不将运行时治理、状态仓库或权限移交给插件。已安装 Python 本身是进程特权代码，此窄 ABI 不是 sandbox，不能限制该代码绕过 ABI 直接访问进程能力。

2. **Typed-Input 强约束**:
   仅接受规范的 `ToolInputHandlerBinding`（校验 declaration name 与 handler 匹配），彻底切除 historical plain callable fallback。非规范对象在物化绑定阶段直接拒绝。

3. **8 个生命周期 Triggers 与 Scope IDs**:
   - `session` scope: `session_start`, `session_end`。Scope ID 为 `{session_id}`。
   - `run` scope: `before_run`, `after_run`。Scope ID 为 `{run_id}`。
   - `turn` scope: `before_turn`, `after_turn`。Scope ID 为 `{session_id}:{run_id}:{occurrence}`。
   - `tool` scope: `before_tool`, `after_tool`。Scope ID 为 `{session_id}:{run_id}:{occurrence}:{call_id}`。

4. **排序与失败/重放语义 (Execution Semantics)**:
   - `order`: 声明必须使用 `StrictInt`；执行计划中 phases 必须按 `order` 数字严格升序排序，数字排序优先于词法/列表声明顺序。
   - `state` & `write-before-effect`: 回调调用前必须先原子持久化 `state="started", failed=False, payload={}` 记录；回调结束后原子更新为 `state="completed"` 记录，携带 `snapshot(outcome)` 的负载。
   - `failure`:
     - `"continue"`: 回调异常时记录 `failed=True, state="completed"`，并继续执行后续 phases。
     - `"raise"`: 回调异常时记录 `failed=True, state="completed"`，并立即中断抛出异常。
   - `replay`:
     - `"once"`: 存在已完成记录时调用 `restore` 恢复结果，跳过回调执行；未完成的 `started` 记录拒绝重复执行。
     - `"repeat"`: 存在已完成记录时仍然重复调用回调。
     - `"refuse"`: 恢复时无记录或存在 `started` 状态记录则拒绝执行。
   - `restore_only`: 恢复已完成前缀时必须存在 `state="completed"` 的持久化记录，仅执行 restore，不产生真实 I/O。

5. **恢复身份隔离**:
   恢复的 pending turn/batch 必须保留原始 producer run ID（与 native pending call identities 严格一致），而当前的 resume execution registration 使用 fresh execution run ID，两者严格分离，不得以 session ID 伪造或混淆 run identity。

## Event Semantics

Runtime extension observability event 必须满足：

- additive 且 client-tolerant
- session-scoped 且 sequence-ordered
- 有界，并且不包含可能携带 secret 的原始 prompt/tool 内容
- 来源于 runtime-owned truth 或 persisted metadata
- replay-friendly：replay 展示历史 event，不重新执行 side effect

这个 family 的第一个 event 是 `runtime.context_transform_applied`。

Prompt-stack observability 也遵循同一模型：`prompt_stack` metadata 描述 fragment order、tier、source、preview length 与 redaction status，用于解释 provider context 是如何组装的；它不是新的 policy layer。secret-like text、provider credentials、raw skill/prompt bodies 与 runtime-injected env values 必须继续被排除或脱敏。

## 非目标

这份契约不定义：

- 通用 user plugin loading
- 任意 `chat.message` mutation
- 任意 tool argument mutation
- 任意 post-tool output mutation
- client-owned execution policy
- hook script owned session、task 或 provider truth
- 把 repo understanding 收敛成一个不可审计的 black-box overview dependency；未来 repo-understanding 能力应优先增强 agent 的自探索工具面、证据链与可回放 context，而不是替代 read/search/git/tool-driven exploration

## 新增 Extension Point 的验收规则

任何新的 extension point 都必须定义：

1. 类型化 request/result shape
2. runtime invocation location
3. ordering rules
4. failure policy
5. persisted/debug metadata
6. 如果对外可观测，则定义 event payload contract
7. 覆盖 behavior、failure、replay/resume 影响的测试
