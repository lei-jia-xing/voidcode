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
