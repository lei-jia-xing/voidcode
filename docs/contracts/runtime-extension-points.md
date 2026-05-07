# Runtime Extension Points 契约

## 目的

定义 VoidCode 如何在不引入通用 plugin bus、不创建第二套执行控制面的前提下扩展能力。

核心模型是：

> typed extension point 负责行为；typed runtime event 解释行为。

## 当前方向

Runtime 继续拥有 session truth、工具执行、审批、provider fallback、context assembly、background task lifecycle 以及 replay/resume 行为。

extension point 只能通过类型化 input/output contract 转换或校验 runtime-owned 数据。runtime event 可以描述这些决策，但 event 不拥有 authority，客户端也不能通过 event 改写 runtime truth。

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

## 非目标

这份契约不定义：

- 通用 user plugin loading
- 任意 `chat.message` mutation
- 任意 tool argument mutation
- 任意 post-tool output mutation
- client-owned execution policy
- hook script owned session、task 或 provider truth

## 新增 Extension Point 的验收规则

任何新的 extension point 都必须定义：

1. 类型化 request/result shape
2. runtime invocation location
3. ordering rules
4. failure policy
5. persisted/debug metadata
6. 如果对外可观测，则定义 event payload contract
7. 覆盖 behavior、failure、replay/resume 影响的测试
