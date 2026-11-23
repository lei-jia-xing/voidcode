# Core TODO

## P0: 输出策略不制造新的业务 MAX

- [ ] **分离原始结果、模型投影和 UI 折叠，删除层叠截断与重复载荷。**
  - 投影只携带必要的一份内容，状态、错误、完整结果 URI/游标保持可用。
  - 超出当前模型上下文预算的数据按需读，而非生产者、Core、客户端各截一遍。
  - UI 行数是折叠策略，不是结果大小或任务可接受性的限制；依据已知上下文/真实传输边界选择预算，不再因为“以防万一”增加常量。

## P1: 现代 Agent 的交互契约（客户端依赖，见 `tui/TODO.md`）

- [ ] **会话级审批策略。**
  - 审批可选“本会话内允许该工具/路径/命令前缀”，作为持久事件记录并由 Core 在后续工具调用前裁决；客户端不缓存策略。
  - 策略可列出、可撤销；`core.reload` 与重启后按记录的事件恢复，不跨会话泄漏。
- [ ] **带理由的拒绝回传模型。**
  - `ResumeSpec` 仅有 `decision`；拒绝可附用户理由，写入 `APPROVAL_DECIDED` 并进入模型投影，避免原样重试。
- [x] **排队追问可列出、可撤回。**
  - 已补充 `Core.list_followups` / `Core.withdraw_followup` 与 `FOLLOWUP_QUEUED` / `FOLLOWUP_WITHDRAWN` 事件及 SQLite 事务内 `pending` 状态检查 + `expected_revision` CAS 防竞争。
- [x] **运行失败/中断原因成为持久投影。**
  - `TranscriptRun` 现随 `core.run_failed` / `core.run_interrupted` 持久化并编码/解码 `error`、`reason` 与 `stop_cause`，客户端无需从临时 RPC 回执拼接。
- [ ] **会话列表携带可辨认摘要。**
  - `SessionRow` 仅有 id/status/turn；补充首条用户消息摘要、创建与最后活动时间，均由事件流投影。
- [ ] **按会话分页读取用户输入历史。**
  - 供客户端恢复跨重启的输入历史与反向搜索，复用有界历史读取，不新建客户端缓存。

## P1: 可靠性、并发与生命周期完整

- [ ] **流式模型调用补齐首字节前重试与错误属性透传。**
  - `propose_turn` 使用 `run_with_retry`，但默认启用的 `stream_turn`/`stream_provider_turn` 声明单次尝试完全不重试；且 `stream_provider_turn` 收到 `event.kind == "error"` 时硬编码 `retryable=False`，丢弃重试与 `retry_after` 信息，导致 429 限流或瞬时断连直接失败。
- [ ] **支持只读工具批次并发执行。**
  - `TurnExecutionManager.drive` 目前串行执行同一轮 `tool_calls`，多文件 `read`/`grep`/`web_fetch` 无法并发，成为代理检索延时瓶颈。
- [x] **生命周期钩子结果持久化并补齐中断路径。**
  - `ToolGovernance.lifecycle_events` 现为每个 owner/run 追加有界 `core.hook_invoked` 事件（note、failure 或 silent），按 run 身份去重；`TurnExecutionManager.stop` 的终态提交原子携带 `session_end(status="interrupted")`，completed/failed 路径保持一次。钩子失败只记录，不改变运行决定。
  - 只读/计划模式下被跳过的 mutating pre-tool gate 追加尽力而为的 `core.hook_skipped` 事件，拒绝语义不变；post-tool note 保持原 output 类型、parts、bounds/reference 与其余 `ToolSuccess` 字段。
- [ ] **在 `UsageFact` 中向观测器暴露当前模型上下文窗口。**
  - `compaction` 等观测器目前拿不到 `context_window`，导致自动压缩默认不触发（配套见 `sdk/TODO.md`、`plugins/TODO.md`）。

## P2: 模块收敛与强类型

- [ ] **拆分超大核心模块并收敛 `Any`/`type: ignore`。**
  - `core/engine/__init__.py`（~2537 行，含大量 Host 协议转发方法）、`core/delegation.py`（~2126 行）、`core/plugins.py`（~1890 行）、`core/services.py`（~1496 行）、`core/processes.py`（~1287 行）及 `core/storage/{continuations,sessions,records,state}.py`（均 1100–1400 行）职责过重；收敛核心内 `Any`（~81 处）与 3 处 `type: ignore`。
