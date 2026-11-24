# SDK TODO

## P1: 核心契约演进

- [ ] **Foreground suspension and the complete `eval` product.**
  - Ordinary `PluginServices.invoke_tool` remains receipt-only. No SDK contract retains or reconstructs an active Python frame across foreground suspension.
  - Expose a separately governed foreground composition lifecycle only if its owner, approval, lease, cancellation, persistence, and restart semantics can be represented without bypassing the ordinary tool pipeline.
- [ ] **Streaming/raw process-output capture.**
  - Shell artifacts should preserve byte-identical raw stdout/stderr without redundant string replacement copies.
- [ ] **资源能力发现/枚举与更多只读资源服务。**
  - 为 `read`、资源检索提供统一的能力发现与枚举契约，保持只读和 caller-scoped 隔离。
- [x] **模型发起的持久化用户问答契约（运行时已落，补齐客户端侧）。**
  - `SessionClient` Protocol 与 Local/RPC Client 现完整提供 `answer_ask` 与 `cancel_ask` 方法，并已接入 CLI 与 TUI。
- [x] **客户端交互契约类型（Core 侧见 `core/TODO.md` P1）。**
  - 排队追问列表与撤回、会话级审批策略（`ApprovalPolicyRule`、`ApprovalPolicyListReceipt`、`ApprovalPolicyRevokeReceipt`）、拒绝理由（`ResumeSpec.reason`）与 `SessionRow` 摘要/时间戳已以冻结 dataclass 完整落地。
- [x] **在 `UsageFact` 与流式错误契约中补齐运行时所需元数据。**
  - `UsageFact` 现携带当前模型的 `context_window`，供 `compaction` 等插件准确计算默认触发门限。
  - `ProviderStreamEvent` 错误变体保留 `retryable`/`retry_after`，流式建连失败退避重试完整保留。
- [ ] **Snapshot current-run capture diagnostics.**
  - 提供最新的精确 terminal-run 投影（身份 + 捕获原因），与 latest eligible output 区分开。
