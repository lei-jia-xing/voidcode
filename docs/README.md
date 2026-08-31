# 文档地图

本文档是 `docs/` 的入口。建议按以下顺序阅读；当文档之间出现差异时，优先采用当前实现对应的契约和状态说明。

## 阅读顺序

### 1. 架构

- [`architecture.md`](./architecture.md) — runtime、graph、tools 与客户端边界的架构总览。
- [`agent-architecture.md`](./agent-architecture.md) — agent 声明层及其与运行时的关系。
- [`agent-boundary.md`](./agent-boundary.md) — agent/runtime/客户端边界与职责划分。

### 2. 当前状态

- [`current-state.md`](./current-state.md) — 当前已实现能力、边界和验证状态；判断“现在是什么”时以此为准。
- [`deliberate-omissions.md`](./deliberate-omissions.md) — 明确不纳入当前范围的能力与取舍。

### 3. 开发与贡献

- [`development.md`](./development.md) — 开发、验证与运行工作流。
- [`coding-standards.md`](./coding-standards.md) — 编码与提交规范。

### 4. Runtime contracts

- [`contracts/README.md`](./contracts/README.md) — 契约目录入口与权威性说明。
- [`contracts/runtime-config.md`](./contracts/runtime-config.md) — runtime 配置及优先级。
- [`contracts/agent-tool-calling.md`](./contracts/agent-tool-calling.md) — agent 工具调用与返回语义。
- [`contracts/agent-tool-enforcement.md`](./contracts/agent-tool-enforcement.md) — 工具 allowlist/default set 的 enforcement。
- [`contracts/client-api.md`](./contracts/client-api.md) — 客户端可见的 session/run/resume/stream API。
- [`contracts/runtime-events.md`](./contracts/runtime-events.md) 与 [`contracts/stream-transport.md`](./contracts/stream-transport.md) — 事件词汇和流传输行为。
- 其他 runtime lifecycle、typed tool hooks、background task 与 capability binding 契约，均从 [`contracts/README.md`](./contracts/README.md) 进入。

### 5. 操作指南

- [`mvp-demo-guide.md`](./mvp-demo-guide.md) — 本地演示与常用操作路径。
- [`failure-diagnosis-runbook.md`](./failure-diagnosis-runbook.md) — 故障诊断与排查步骤。
- [`mvp-todo-plan.md`](./mvp-todo-plan.md) — 历史交付清单/参考，不是当前实现状态的权威来源。

### 6. 比较记录与后续方向

- [`oh-my-pi-comparison-priorities.md`](./oh-my-pi-comparison-priorities.md) — 与 Oh My Pi 的比较及优先级记录；结合 `current-state.md` 判断已落地内容。
- [`roadmap.md`](./roadmap.md) — 阶段与史诗任务记录。

## 文档状态与归档规则

- `contracts/` 下的契约、[`current-state.md`](./current-state.md) 以及比较记录是判断当前实现的主要依据；架构、design、audit 文档可能保留历史背景，不能单独覆盖这些来源。
- 只有在确认**没有任何入链引用**、且内容明确属于已完成事项或历史资料时，才可移入 `docs/archive/`。
- 归档应使用保留 Git history 的移动操作；移动后必须更新所有引用，不能制造断链。
- 不删除文档，不归档 canonical docs；不把仍描述当前行为或仍被引用的文档归档。
- 新增或修订文档应在本地图登记，并清楚标注其当前性或历史性。
