# VoidCode 架构概览

## 状态与初衷

VoidCode 是一个受 OpenCode 和 Claude Code 启发而开发的 pre-MVP 本地优先（local-first）编程智能体运行时。当前的直接目标不是构建一个完整的平台，而是使开发者任务循环的一个环节实现可靠的端到端运行：

1. 用户提交开发任务
2. 智能体进行推理，调用工具，在需要时请求审批，并执行更改
3. 运行时记录状态和事件
4. 用户可以通过 CLI 等客户端观察进度并继续会话

关于规范的客户端面向契约层，请参阅 [`docs/contracts/README.md`](./contracts/README.md)。

## 系统上下文

系统上下文可以描述为从用户到工具的分层路径：

- 用户目前通过 CLI 客户端进行交互，并为 Web 前端或未来的 IDE 客户端预留了空间
- 客户端与 **VoidCode Runtime** 通信
- 运行时负责协调会话、权限、钩子（hooks）、工具注册、流式传输和存储
- 运行时调用 **LangGraph orchestrator** 进行图执行、状态转换、检查点（checkpoints）以及中断/恢复行为
- LangGraph 最终通过运行时边界驱动 LLM 提供商、工作区访问和工具执行

有两个边界尤为重要：

- LangGraph **不**直接与 UI 客户端通信
- UI 客户端 **不**直接调用工具

所有流程都经过运行时，以确保治理、持久性和可观测性保持一致。

## LangGraph 与自定义运行时边界

VoidCode 使用 LangGraph 作为编排引擎，而不是整个产品运行时。

### LangGraph 负责（仅 deterministic/read-only slice）

- `DeterministicReadOnlyGraph` 中的步骤编排
- 该 slice 的图状态与检查点
- 该 slice 的中断与恢复

### 自定义运行时负责（全部执行路径）

- 运行时入口（run/stream/resume）
- 工具注册表与元数据
- 权限决策（`allow`、`deny`、`ask`）
- 钩子执行
- 会话创建、加载与恢复
- 基于 SQLite 的存储抽象
- 面向 CLI 或未来客户端的流式传输
- 上下文管理与压缩

### `ProviderSingleAgentGraph` 负责（provider-backed 主路径）

- 直接调用 `SingleAgentProvider.propose_turn()`
- 不依赖 LangGraph，由 runtime 直接驱动
- 当前主要的单智能体执行引擎

**核心架构决策：** 运行时统一持有执行治理；LangGraph 仅用于 deterministic/read-only 测试路径的编排，而非 provider-backed 主循环。

## 关键组件

代码库当前仍以 `runtime/`、`graph/` 和 `tools/` 为最核心的三条主执行边界，但实际模块结构已经扩展为更完整的能力层与客户端分层：

### `runtime/`

运行时服务构成系统中心。该领域目前承载会话管理、权限检查、钩子、传输、持久化以及无头运行时入口点。

### `graph/`

图代码为主要的智能体循环建模。计划中的流程大致为：

`prepare_context` → `call_model` → `decide_next_step` → `permission_gate` → `execute_tool` → `handle_tool_result` → `finalize_response`

MVP 有意保持图的规模较小，以便在设计扩展之前使主循环保持稳定。

### `tools/`

工具层已经通过运行时流水线暴露出内置能力，如 `read_file`、`grep`、`shell_exec` 和 `write_file`；后续仍可以在同一边界内继续扩展：

图工具请求 → 运行时元数据查询 → 权限检查 → 前置钩子 → 工具执行 → 后置钩子 → 持久化 → 结果返回至图

设计假设读取操作可以并发运行，而写入操作则保持受控且由审批驱动。

### `hook/`

`hook/` 负责 hook 配置与执行器逻辑，为 runtime 提供 pre/post execution 扩展点。

### 能力层目录

`lsp/`、`skills/`、`provider/`、`acp/` 与 `mcp/` 当前主要承担能力边界与后续抽离方向的定义。其中部分实现仍位于 `runtime/` 下，但目录边界已经存在，不应再被文档忽略。

### `tui/`

`tui/` 是当前较早期的终端客户端层，用于消费 runtime 暴露的 session / event / approval 语义。

## 设计原则

当前的架构由几个明确的原则指导：

- **清晰的分层：** 保持 UI、运行时、编排和基础设施的分离。
- **治理优先于执行：** 每次工具调用都要经过注册表、权限和钩子。
- **可恢复的状态：** 会话、消息、审批、工具执行和检查点应当是可恢复的。
- **可观测的执行：** 轮次（turns）、工具、钩子、审批、重试和错误应当发出事件。
- **MVP 优先：** 在探索多智能体或插件密集的设计之前，先交付一个稳定的单智能体循环。

## MVP 边界

MVP 旨在包括：

- 单智能体核心循环
- 基础内置工具集
- 会话持久化与恢复
- 审批与权限流
- 基础钩子
- 至少一个可用的入口点，例如 CLI
- 一个可工作的无头运行时基础

MVP 明确推迟了更深层次的 IDE 集成、云端协作、插件市场以及高级多智能体协调。
