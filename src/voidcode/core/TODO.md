# Core TODO

## P0: 输出策略不制造新的业务 MAX

- [x] **分离原始结果、模型投影和 UI 折叠，删除层叠截断与重复载荷。**
  - `ResultProjector._compact_composite_parts` 在计算预算时计入 `presentation`，超限时优先淘汰重复顶层 `presentation` 并按精确 UTF-8 JSON 预算一次性二分截断溢出 `TextPart`（保留完整 raw artifact `reference`）。
  - `context_projection._apply_projection_allocation` 二次收缩阶段传入 `include_images=False` 复用已加载图片列表，消除重复读取与 base64 编码，保留唯一真实边界与 URI 游标。

## P1: 现代 Agent 的交互契约（客户端依赖，见 `tui/TODO.md`）

- [x] **会话级审批策略。**
  - 审批可选“本会话内允许该工具/路径/命令前缀”，作为持久事件 `core.approval_policy_granted`/`core.approval_policy_revoked` 记录，由 Core 在后续工具调用前裁决；客户端不缓存策略。
  - 策略支持 `list_approval_policies` / `revoke_approval_policy`，重放事件恢复，跨会话隔离。
- [x] **带理由的拒绝回传模型。**
  - `ResumeSpec` 支持 `reason` 与 `grant_session_policy`；拒绝理由记录入 `APPROVAL_DECIDED` 并投影回传模型，避免原样重试循环。
- [x] **排队追问可列出、可撤回。**
  - 已补充 `Core.list_followups` / `Core.withdraw_followup` 与 `FOLLOWUP_QUEUED` / `FOLLOWUP_WITHDRAWN` 事件及 SQLite 事务内 `pending` 状态检查 + `expected_revision` CAS 防竞争。
- [x] **运行失败/中断原因成为持久投影。**
  - `TranscriptRun` 现随 `core.run_failed` / `core.run_interrupted` 持久化并编码/解码 `error`、`reason` 与 `stop_cause`，客户端无需从临时 RPC 回执拼接。
- [x] **会话列表携带可辨认摘要。**
  - `SessionRow` 补充首条用户消息摘要（`summary`）、`created_at` 与 `updated_at` 时间戳，由事件流持久投影。
- [x] **按会话分页读取用户输入历史。**
  - TUI 启动与会话切换时从持久事件流恢复用户输入历史至 composer，实现跨重启持久可用（Ctrl+R 搜索交互保留后续讨论）。

## P1: 可靠性、并发与生命周期完整

- [x] **流式模型调用补齐首字节前重试与错误属性透传。**
  - `stream_provider_turn` 支持首字节/首 token 前退避重试，透传 `retryable` 与 `retry_after` 属性；一旦有 token 流出则禁止重试，杜绝重试导致内容重复。
- [x] **支持只读工具批次并发执行。**
  - `TurnExecutionManager` / `ToolExecutionManager` 识别整批只读且免审批工具调用，利用线程池并发执行，并在主线程按确定性原始序号严格顺序提交 `TOOL_INTENT` 和 `TOOL_COMPLETED` 事件。
- [x] **生命周期钩子结果持久化并补齐中断路径。**
  - `ToolGovernance.lifecycle_events` 现为每个 owner/run 追加有界 `core.hook_invoked` 事件（note、failure 或 silent），按 run 身份去重；`TurnExecutionManager.stop` 的终态提交原子携带 `session_end(status="interrupted")`，completed/failed 路径保持一次。钩子失败只记录，不改变运行决定。
  - 只读/计划模式下被跳过的 mutating pre-tool gate 追加尽力而为的 `core.hook_skipped` 事件，拒绝语义不变；post-tool note 保持原 output 类型、parts、bounds/reference 与其余 `ToolSuccess` 字段。
- [x] **在 `UsageFact` 中向观测器暴露当前模型上下文窗口。**
  - `ProjectionAllocation`、`UsageFact` 和 `compaction_observer` 现完整传递 `context_window`，保证自动压缩根据真实上下文门限准确触发。

- [x] **修复存储事件写入旁路、唤醒恢复状态误判、会话分页截断与只读批次挂起缺陷。**
  - `storage/adoption.py`、`wakeups.py`、`continuations.py`、`sessions.py` 统一复用 `insert_session_event_locked`，消除绕过索引维护的原样 `INSERT`；`adoption.recover_claimed_wakeup_runs` 复用到共享的 `RUN_INTERRUPTED` `stop_cause` 状态映射（`cancelled`/`failed`）与 checkpoint 更新，并为坏 checkpoint JSON 提供容错。
  - `fact_delivery.py`、`activation_coordinator.py`、`engine/_host_adapters.py`、`engine/_lifecycle.py` 与 `bootstrap.py` 在全量枚举会话时显式传入 `limit=None`，消除超过 100 条会话后的静默漏扫；`fact_delivery.py` 拒绝将 `bool`/`float` 误认作 `dispatch_sequence`。
  - `engine/_sessions.py` 的 `list_approval_policies`/`revoke_approval_policy` 改用 `self.session(session_id)` 强制校验 workspace 归属；`tool_execution.py` 并发只读批次支持 `ToolSuspension`/`ActivationFenced`、统一 `TOOL_INTENT` 提交与拒答上报路径并透传 `staged_wakeups`；`delegation/_spawn.py` 崩溃窗口恢复复用统一的 `CHILD_SPAWNED` 载荷构造（补齐 `root_action_budget`/`result_schema`）。

## P2: 共享运行时机制、模块收敛与强类型

- [x] **为插件补齐“只读受监管服务进程”核心机制（`spawn_query_service_process`）。**
  - `sdk/services/protocols.py`、`core/processes/` 与 `core/services/_processes.py` 现提供 `spawn_query_service_process`：允许只读工具租约启动 generation 托管的服务子进程（共享 `_readonly_sandbox()` 强制只读沙箱、禁止 hook 租约、`write_process`/`cancel_process` 对只读租约仅限其自身 `query_service=True` 句柄且不记录 mutating 观测），已供 `lsp` 直接复用。
- [ ] **为插件补齐统一子进程托管与原始流捕获原语（供 `mcp` 等复用）。**
  - `mcp` 仍在 `ProcessHost` 之外自行起 stdio 进程且丢弃 stderr 到 `/dev/null`，`sdk/services/processes.py` 的 `RawStreamCapture` 尚未接通运行时原始流捕获。
- [ ] **按需接通 SDK 已声明但 Core 尚无接线的通用契约。**
  - `sdk/resources.py` 的 `ResourceSchemeCaps`/枚举搜索能力发现与 `sdk/foreground.py`/`RawStreamCapture` 当前尚无 Core 侧实现或分发路径。
- [ ] **继续收敛大模块及核心内 `Any` / `cast`。**
  - `core/engine`、`core/delegation/`、`core/plugins/`、`core/services/`、`core/processes/` 均已完成包化拆分；剩余 `core/storage/{continuations,sessions,state}.py` 仍偏大；`src/voidcode/core/` 现已消除全部 `# type: ignore`（`_invocation.py` 最后 1 处已清除），尚存 64 处 AST `Any` 引用与 7 处 `cast(...)`（主要在 `core/delegation/`）待继续收敛。
