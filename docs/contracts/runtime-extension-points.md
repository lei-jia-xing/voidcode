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

1. 运行时拥有 `mode` / `read_only`、权限、工具可见性、shell 安全、hooks、session event 和 delegated-task 约束的控制面。
2. 结构性策略在工具执行前完成，不依赖 prompt wording 充当唯一防线；prompt guidance 只解释已解析策略，不能创建、放宽或隐藏 enforcement。
3. 非交互式执行必须 fail fast，不接受会卡住的默认行为；shell command classification、timeout 选择与 package-manager non-interactive env injection 都属于 runtime/security policy，而不是 CLI 文案。
4. prompt stack 和 observability 必须可重放、可解释，并且保持 redacted；可观测 payload 只暴露有界 fragment metadata / redacted preview，不暴露完整 prompt、skill body、secret-like values 或注入 env values。
5. registry 只能做受控解析，不能绕开 runtime policy。
6. 默认行为保持 backward-compatible，现有请求仍然是 action-capable，除非显式选择 analyze、plan 或 read-only policy。

这不意味着复制 OpenHands、Continue、Aider 或 Open Interpreter，也不意味着引入任意多智能体拓扑、插件市场、云端执行、IDE 插件执行路径，或者把 AGPL 实现代码搬进来。

## Policy precedence

固定优先级，从高到低如下：

1. Hardcoded safety denylist and workspace/session invariants.
2. Runtime request mode and `read_only` policy.
3. Parent session/delegated-task inherited constraints.
4. Workspace/project runtime config.
5. Agent manifest/tool allowlist.
6. CLI flags that map into runtime request fields.
7. Default tool registry/provider capabilities.

CLI flags never bypass runtime request policy, they only populate request fields.

Memory tools are conservative by default. 它们只有在显式允许的 runtime-owned policy context 中才可见或可用（当前是 runtime 明确标记的 memory command/internal context）。更严格的 prompt guidance 只是附加说明，不是 enforcement 来源；CLI、frontend、graph prompt 或 agent manifest 不得仅靠 wording 暴露 memory tools。

`mode` 的当前稳定语义是：缺省 `normal` 保持 action-capable；`analyze` 与 `plan` 隐式 effective read-only；显式 `read_only=true` 会把 `normal` run 收窄为 read-only。CLI 只把 `--mode` / `--read-only` 映射进 runtime request metadata，不能在客户端侧复制 mutating-tool、memory-tool 或 shell policy enforcement。

## Runtime Harness Policy v1

Runtime Harness Policy v1 is the runtime-owned authorization and explanation layer for a single turn. It materializes the effective intent, tool, delegation, hook, and prompt-activation decisions into one persisted `RuntimePolicySnapshot`. It is not a second control plane: extension points may contribute validated declarations, diagnostics, or narrowing guidance, but runtime policy remains the only source of authorization.

`RuntimePolicySnapshot` v1 is a JSON-serializable, redacted object with these required fields:

| Field | Required shape | Meaning |
| --- | --- | --- |
| `schema_version` | integer, `1` | Snapshot schema version. Unsupported versions fail fast on load/import. |
| `policy_version` | string | Version of the materialization rules used to compute this snapshot. |
| `created_at` or `turn_id` | RFC3339 timestamp or stable turn identifier | Identifies when/for which turn the snapshot was created without exposing prompt body. |
| `agent_preset` | string | Selected runtime agent preset for the run or child session. |
| `agent_manifest_id` | string or null | Resolved builtin/custom manifest id used for capability binding. |
| `intent` | object | Bounded neutral intent metadata: label, confidence, matched rule ids, and optional guidance ids. It cannot grant or infer capability. Heuristic free-text classification is intentionally not part of v1. |
| `tool_policy` | object | Effective visible/callable tool policy, including allowed and denied tool ids plus stable denial reasons. |
| `delegation_policy` | object | Effective child routing policy, allowed child presets/categories, denied targets, and stable denial reasons. |
| `hook_policy` | object | Named event-scoped hook policy with allowed observe/report/cancel/guidance actions. Hooks are non-authoritative. |
| `prompt_activation` | object | One-time guidance activation ids, activation state, and redacted previews only. It cannot alter tool or delegation policy. |
| `precedence_trace` | array | Ordered, audit-safe decisions showing which source allowed, narrowed, or denied each policy facet. |
| `diagnostics` | object | Redacted bounded diagnostics. No raw prompts, skill bodies, env values, credentials, or unrestricted metadata escape hatches. |

### Runtime Harness Policy precedence

Policy materialization uses this fixed order from highest to lowest authority:

1. Runtime hard denials and security invariants, including product non-delegation.
2. Persisted session policy for resume/replay/import when a snapshot already exists.
3. Validated runtime config and schema-bounded policy fields.
4. Agent manifest declared capabilities and top-level/subagent mode.
5. Request/session options that can only select or narrow allowed behavior.
6. Hook preset metadata, which is guidance/guard/report only.
7. Bounded neutral intent metadata, which is non-authoritative and cannot grant or infer capability.
8. Runtime defaults and registry/provider capabilities.

Lower-precedence inputs may narrow or annotate higher-precedence decisions, but they cannot grant a tool, hook action, prompt activation, MCP binding, delegation target, approval, or product delegation denied above them. Intent metadata cannot grant capabilities or reintroduce heuristic prompt classification. Hooks cannot grant capabilities or mutate persisted policy truth. Prompt activation is guidance-only.

### Bounded observability

Runtime attaches a `runtime_policy` object to `runtime.request_received`, and additive clients may also tolerate a future standalone `runtime.policy_materialized` event. This payload is a bounded projection of the persisted `RuntimePolicySnapshot`, not a second policy source. It exposes schema/policy version, mode/read-only state, agent ids, neutral intent metadata, allowed/denied tool and delegation ids, hook policy actions/scopes, prompt-activation state, precedence trace, and diagnostics. Lists are capped, strings are preview-sized, and the payload never includes raw prompt bodies, skill bodies, env values, credentials, or unbounded metadata.

Tool allow/deny and approval decisions remain observable through `runtime.tool_lookup_succeeded`, `runtime.permission_resolved`, `runtime.approval_requested`, `runtime.approval_resolved`, `runtime.tool_started`, `runtime.tool_completed`, and `runtime.failed` denial payloads. Delegation allow/deny remains observable through background/delegated lifecycle events and explicit runtime failure payloads for policy denials. Hook decisions remain observable through `runtime.tool_hook_pre` / `runtime.tool_hook_post` with `hook_policy.authoritative=false`. Prompt activation persists inside `RuntimePolicySnapshot.prompt_activation`; replay/resume may show historical activation records but must project run-local `activated_this_turn` as false unless the current run actually activated it.

Legacy sessions without a stored snapshot synthesize a conservative v1 snapshot on replay/debug surfaces. Unsupported stored snapshot/schema versions fail fast rather than being silently migrated or widened.

### Product non-delegation invariant

`product` is a top-level selectable planning preset only. It must never be a callable child target through direct `subagent_type="product"`, configured alias, category mapping, local manifest reference, background helper, hook output, classifier output, imported state, replay, or bundle migration. The stable denial reason for this invariant is `delegation_denied_product_top_level_only`. `product` must not receive `task`, `background_output`, `background_retry`, `background_cancel`, or any child-spawn helper through its manifest, config, hook policy, prompt activation, or classifier output.

### Legacy snapshot synthesis

Legacy sessions and bundles without `RuntimePolicySnapshot` remain resumable/importable. The runtime must synthesize a conservative v1 snapshot using persisted session/config truth and hard denials, record the synthesis in `precedence_trace`, and keep product delegation denied. It must not recompute historical sessions from mutable live defaults when a stored snapshot exists.

### v1 non-goals

Runtime Harness Policy v1 does not define a generic policy DSL, LLM-based classifier, heuristic intent classifier, arbitrary multi-agent topology, product delegation, agent-to-agent bus, MCP redesign, marketplace/dynamic plugin system, or prompt-text enforcement layer. It keeps MCP behind existing config gates unless a stable policy identifier already exists.

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

### Tool output normalization

Tool output normalization 可以塑形 model-visible tool result，但必须保留 runtime explainability。

未来 normalizer 必须保留足够 metadata 来解释 raw output 与 model-visible output 的差异，包括数量、截断/脱敏原因，以及适用时的 artifact references。

normalizer 不得在 approval 之后修改已批准 tool arguments。

### Provider message validation

Provider message validator 可以在 provider execution 前检查 canonical runtime/provider message shape。

validator 可以通过 runtime-owned policy decision warn 或 block，但不能变成任意 message mutation hook。

### Capability materialization

agent、skill、MCP、LSP、provider 与 workflow declaration 会 materialize 成 runtime-owned capability snapshot。

declaration 可以选择或收窄 capability，但不能授予 permission、绕过 tool allowlist、启动 unmanaged process，或重定义 delegated/session truth。

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
- prompt wording 作为唯一的 memory tool enforcement 来源
- 默认把 memory tools 暴露给所有 runtime context
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
