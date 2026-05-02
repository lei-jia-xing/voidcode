# 契约文档

本目录是面向客户端的运行时契约（Runtime Contracts）权威来源。

## 范围

这些文档定义了由运行时、CLI、当前 Web 客户端以及后续 TUI 实现共享的 MVP 契约层。

它们对以下内容具有规范性：

- 运行时事件词汇表
- 面向客户端的 session/run/stream API 形状
- 审批请求与处理语义
- 运行时配置界面及优先级
- 客户端预期的流传输行为

## 非目标

这些文件**不**定义：

- 每个运行时模块的具体实现细节
- post-MVP 的多智能体协议
- UI 布局或视觉设计决策
- GitHub 积压工作的归属或调度

## 当前契约集

- [`runtime-events.md`](./runtime-events.md) — 用于客户端渲染的稳定事件词汇表
- [`client-api.md`](./client-api.md) — 客户端可见的 session/run/load/resume/stream 契约
- [`approval-flow.md`](./approval-flow.md) — 受控执行与审批语义
- [`agent-tool-calling.md`](./agent-tool-calling.md) — 面向 agent 的工具调用、参数、返回、审批与选择指南
- [`agent-tool-enforcement.md`](./agent-tool-enforcement.md) — agent preset 中 tool allowlist/default tool set 如何进入 runtime enforcement
- [`agent-hook-presets.md`](./agent-hook-presets.md) — agent preset hook refs 如何通过 builtin hook preset catalog 校验与表达角色 intent
- [`agent-capability-bindings.md`](./agent-capability-bindings.md) — agent preset 的 tools/skills/hooks/MCP/provider binding 如何由 runtime materialize 成可重放 snapshot
- [`runtime-config.md`](./runtime-config.md) — MVP 配置界面及优先级
- [`runtime-lifecycle-hooks.md`](./runtime-lifecycle-hooks.md) — richer lifecycle hook phases 的 runtime-owned 执行契约
- [`background-task-delegation.md`](./background-task-delegation.md) — delegated/background task parent/child linkage、结果读取、retry/cancel 与 fake-provider/fake-MCP 验证立场
- [`stream-transport.md`](./stream-transport.md) — 运行时流的交付与重放预期

## 相关 Issue

- #13 运行时事件模式（Schema）
- #14 面向客户端的 API 契约
- #15 审批模式
- #16 运行时配置界面
- #17 流传输抽象
- #163 面向 agent 的工具调用契约与使用说明
- #174 从 runtime 发出 `runtime.background_task_waiting_approval`
- #175 在 runtime 中稳定 `RuntimeRequest.metadata` schema
- #169 runtime enforcement of agent tool allowlists/default tool sets
- #170 richer runtime lifecycle hook execution
- #289 delegated child execution E2E baseline
- #379 builtin hook preset catalog

## 相关代码

- `src/voidcode/runtime/contracts.py`
- `src/voidcode/runtime/events.py`
- `src/voidcode/runtime/session.py`
- `src/voidcode/runtime/service.py`
- `src/voidcode/hook/presets.py`
- `src/voidcode/graph/contracts.py`
- `src/voidcode/tools/contracts.py`
- `src/voidcode/cli/`

## 所有权规则

- 将模式（Schema）详情放在此处，而不是 `README.md`、`docs/roadmap.md` 或 `docs/current-state.md` 中。
- `docs/current-state.md` 应描述现状，然后链接到此处查看契约定义。
- `docs/roadmap.md` 应仅描述阶段/史诗任务，然后链接到此处查看契约前提条件。
- GitHub Issue 应指向这些文件，而不是在 Issue 正文中重复说明完整的模式。
