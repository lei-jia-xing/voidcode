# VoidCode 架构概览

## 状态与初衷

VoidCode 是一个受 OpenCode 和 Claude Code 启发而开发的本地优先（local-first）编程智能体运行时。当前的直接目标不是构建一个完整的平台，而是让 runtime 能够稳定承载一个受监管的开发任务执行闭环：

1. 用户提交开发任务
2. 运行时驱动执行引擎，调用工具，在需要时请求审批，并执行更改
3. 运行时记录状态和事件
4. 用户可以通过 CLI 等客户端观察进度并继续会话

关于规范的客户端面向契约层，请参阅 [`docs/contracts/README.md`](./contracts/README.md)。

## 系统上下文

系统上下文可以描述为从用户到工具的分层路径：

- 用户目前通过 CLI 客户端进行交互，并为 Web 前端或未来的 IDE 客户端预留了空间
- 客户端与 **VoidCode Runtime** 通信
- 运行时负责协调会话、权限、钩子（hooks）、工具注册、流式传输和存储
- 运行时选择并驱动具体的 execution engine / orchestration path
- 某些 graph path 使用 LangGraph，另一些则由 runtime 直接驱动的 graph implementation 承担
- delegated child execution 也从 runtime 进入，使用 parent / child session linkage、background task lifecycle 与 runtime-owned result retrieval，而不是客户端或 ACP 侧的旁路执行

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
- 基于 SQLite 的用户全局存储抽象（XDG state 路径、`workspace_id` scoped rows、`PRAGMA user_version` schema gate）
- 面向 CLI 或未来客户端的流式传输
- 上下文管理与压缩
- delegated child routing、background result retrieval、cancel/retry guidance 与 lifecycle hook guardrails

### `ProviderSingleAgentGraph` 负责（当前已实现的 provider-backed execution engine 路径）

- 直接调用 `SingleAgentProvider.propose_turn()`
- 不依赖 LangGraph，由 runtime 直接驱动
- 代表后续 provider-backed execution engine 的产品主路径方向

**核心架构决策：** 运行时统一持有执行治理；LangGraph 当前仅覆盖 deterministic/read-only 参考与 debug slice 的编排，provider-backed 执行路径由 runtime 直接驱动，并代表真实 agent 行为的主推荐路径。当前已交付的是 runtime-owned delegated child execution 基线，不是任意拓扑 multi-agent 平台。未来如果 multi-agent workflow 扩展 graph 编排范围，runtime 仍保持系统控制面地位。ACP 是单独的控制面 / 协议边界，与 execution engine 是不同维度，不应混为一谈。

## 关键组件

代码库当前仍以 `runtime/`、`graph/` 和 `tools/` 为最核心的三条主执行边界，但实际模块结构已经扩展为更完整的能力层与客户端分层：

### `runtime/`

运行时服务构成系统中心。该领域目前承载会话管理、权限检查、钩子、传输、持久化以及无头运行时入口点。

`src/voidcode/runtime/service.py` 仍是这一控制面的主要热点。未来拆分应遵循 [`runtime/service.py` 安全拆分计划](./runtime-service-decomposition-plan.md)，先围绕已有测试保护的 background task lifecycle、provider fallback、approval resume、tool registry scoping 与 persisted runtime config replay 边界推进，并保持治理语义继续由 runtime 持有。

### `graph/`

graph 是执行引擎和编排层，当前包含两条并行路径：

- `DeterministicReadOnlyGraph`：LangGraph-backed 确定性参考/debug 切片，通过正则匹配执行只读命令（read、grep、run、write），不调用外部模型，并继续用于无凭据 smoke test 与确定性回归测试。
- `ProviderSingleAgentGraph`：provider-backed 执行引擎路径，由 runtime 直接驱动，调用 `SingleAgentProvider.propose_turn()` 实现模型推理。

两条路径都由 runtime 统一选择和驱动，共享工具注册表、权限检查、钩子和检查点机制。后续 multi-agent workflow 扩展可以引入更复杂的编排拓扑，但不改变 runtime 作为控制面的前提。

### `tools/`

工具层已经通过运行时流水线暴露出内置能力，如 `read_file`、`grep`、`shell_exec` 和 `write_file`；后续仍可以在同一边界内继续扩展：

图工具请求 → 运行时元数据查询 → 权限检查 → 前置钩子 → 工具执行 → 后置钩子 → 持久化 → 结果返回至图

设计假设读取操作可以并发运行，而写入操作则保持受控且由审批驱动。

### `hook/`

`hook/` 负责 hook 配置与执行器逻辑，为 runtime 提供 pre/post execution 扩展点。

### 能力层目录

`lsp/`、`skills/`、`provider/`、`acp/` 与 `mcp/` 当前主要承担能力边界与后续抽离方向的定义。其中部分实现仍位于 `runtime/` 下，但目录边界已经存在，不应再被文档忽略。

### `agent/`

`voidcode.agent` 已存在，并作为预定义 agent 定义与 agent preset/configuration 的声明边界，用于描述具体 agent 的配置元数据：

- prompt / profile 定义
- hook 绑定
- skill 绑定
- MCP server/profile 绑定
- tool allowlist / default tool set
- provider / model preference metadata

`voidcode.agent` 不拥有 session state、审批/权限、持久化、事件路由、transport 或 provider invocation loop。这些仍由 `voidcode.runtime` 持有；`voidcode.graph` 继续负责步骤推进与编排；`hook/`、`skills/`、`mcp/`、`tools/`、`provider/` 仍是可复用能力层。当前 runtime 会消费 `leader` / `product` 顶层 preset，并允许受支持的 child preset 通过 delegated path 执行；后续 multi-agent workflow 扩展在编排层面的作用范围可以扩大，但不影响 runtime-owned 治理和 agent/ 配置边界的分离。

### Delegated child execution 与明确非目标

当前已经实现的 delegated/subagent 行为保持收敛：

- 顶层 active run 默认是 `leader`，也可显式选择 `product`。
- `task` 工具会先验证 category / `subagent_type` routing，再创建 runtime-owned background task 与 child session lineage。
- 支持的 child preset 是 `advisor`、`explore`、`product`、`researcher`、`worker`；它们不等价于可任意直接启动的顶层 agent。
- runtime 根据 agent manifest 和 request tool config 收窄 provider 可见工具，并在实际 tool lookup 时再次执行 allowlist guardrail。
- `skill_refs` 是 manifest/catalog 默认选择；`force_load_skills` 与 delegated `load_skills` 只在目标 run 或 child session 注入完整 skill body，不从 parent 泄漏到 child。
- MCP server lifecycle 由 runtime 以 runtime scope 或 session scope 管理，并通过 fake MCP 覆盖测试；当前不宣称 workspace-scoped MCP、MCP 生态市场式语义或动态 agent marketplace。
- 背景结果通过 `background_output` / `load_background_task_result` 读取，可选择有界 full-session transcript；失败输出只给出显式 user-request retry guidance，不做无限自动重试。

以下能力仍不属于当前实现：workspace-scoped MCP、provider/agent marketplace、动态 agent 发现、peer-to-peer agent bus、任意拓扑 multi-agent orchestration，以及 #285 context assembly / compaction 的完整产品化语义。

### `tui/`

`tui/` 是当前较早期的终端客户端层，用于消费 runtime 暴露的 session / event / approval 语义。

## 设计原则

当前的架构由几个明确的原则指导：

- **清晰的分层：** 保持 UI、运行时、编排和基础设施的分离。
- **治理优先于执行：** 每次工具调用都要经过注册表、权限和钩子。
- **可恢复的状态：** 会话、消息、审批、工具执行和检查点应当是可恢复的。
- **可观测的执行：** 轮次（turns）、工具、钩子、审批、重试和错误应当发出事件。
- **MVP 优先：** 在扩展更复杂 workflow、ACP 协调面或编排范围之前，先交付一个稳定的 execution engine 循环，并保持 runtime/control-plane 边界稳定。

## MVP 边界

MVP 旨在包括：

- 稳定的 execution engine 核心循环
- 基础内置工具集
- 会话持久化与恢复
- 审批与权限流
- 基础钩子
- 至少一个可用的入口点，例如 CLI
- 一个可工作的无头运行时基础

MVP 明确推迟了更深层次的 IDE 集成、云端协作、插件市场，以及更复杂的 workflow 编排范围扩展与 ACP 扩展；这些方向在 post-MVP 阶段继续推进时，也应通过 runtime-owned 治理与现有 `agent/` 声明边界进入系统，而不是绕过运行时。
