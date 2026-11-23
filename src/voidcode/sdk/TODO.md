# SDK TODO

## P1: 核心契约演进

- [ ] **Foreground suspension and the complete `eval` product.**
  - Ordinary `PluginServices.invoke_tool` remains receipt-only. No SDK contract retains or reconstructs an active Python frame across foreground suspension.
  - Expose a separately governed foreground composition lifecycle only if its owner, approval, lease, cancellation, persistence, and restart semantics can be represented without bypassing the ordinary tool pipeline.
- [ ] **Streaming/raw process-output capture.**
  - Shell artifacts should preserve byte-identical raw stdout/stderr without redundant string replacement copies.
- [ ] **资源能力发现/枚举与更多只读资源服务。**
  - 为 `read`、资源检索提供统一的能力发现与枚举契约，保持只读和 caller-scoped 隔离。
- [ ] **模型发起的持久化用户问答契约。**
  - 声明有类型的问题、上下文、单选/多选/自由输入、校验，以及 request/answer/cancel 的关联与生命周期，不滥用审批门伪装问答。
- [ ] **Snapshot current-run capture diagnostics.**
  - 提供最新的精确 terminal-run 投影（身份 + 捕获原因），与 latest eligible output 区分开。
