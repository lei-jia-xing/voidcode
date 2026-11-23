# SDK TODO

## P1: 核心契约演进

- [ ] **Foreground suspension and the complete `eval` product.**
  - Ordinary `PluginServices.invoke_tool` remains receipt-only. No SDK contract retains or reconstructs an active Python frame across foreground suspension.
  - Expose a separately governed foreground composition lifecycle only if its owner, approval, lease, cancellation, persistence, and restart semantics can be represented without bypassing the ordinary tool pipeline.
- [ ] **Streaming/raw process-output capture.**
  - Shell artifacts should preserve byte-identical raw stdout/stderr without redundant string replacement copies.
- [ ] **资源能力发现/枚举与更多只读资源服务。**
  - 为 `read`、资源检索提供统一的能力发现与枚举契约，保持只读和 caller-scoped 隔离。
- [ ] **模型发起的持久化用户问答契约（运行时已落，补齐客户端侧）。**
  - `sdk/ask.py` 与 `transport/rpc.py`（`ask.answer`/`ask.cancel`）已落地，但 `SessionClient` Protocol（`sdk/client.py`）缺少 `answer_ask`/`cancel_ask`，导致本地与 RPC 客户端均未暴露。
- [ ] **客户端交互契约类型（Core 侧见 `core/TODO.md` P1）。**
  - 排队追问列表与撤回（`QueuedFollowup`、`FollowupListReceipt`、`FollowupWithdrawReceipt` 与 `SessionClient.list_followups`/`withdraw_followup`）已以冻结 dataclass 落地；会话级审批策略、拒绝理由与 `SessionRow` 摘要待补。
  - 客户端能力声明（如嵌入模式才可切换模型/思考级别），替代客户端按实现类型判断。
- [ ] **在 `UsageFact` 与流式错误契约中补齐运行时所需元数据。**
  - `UsageFact` 携带当前模型的 `context_window`（或提供只读模型上限查询），供 `compaction` 等插件准确计算默认触发门限。
  - `ProviderStreamEvent` 错误变体保留 `retryable`/`retry_after`，避免流式建连失败丢失退避语义。
- [ ] **Snapshot current-run capture diagnostics.**
  - 提供最新的精确 terminal-run 投影（身份 + 捕获原因），与 latest eligible output 区分开。
