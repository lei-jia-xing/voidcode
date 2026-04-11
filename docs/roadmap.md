# VoidCode 路线图摘要

关于将此路线图转变为具体交付阶段的执行清单，请参阅 [`docs/mvp-todo-plan.md`](./mvp-todo-plan.md)。关于面向客户端的运行时契约，请参阅 [`docs/contracts/README.md`](./contracts/README.md)。

## 当前状态

VoidCode 仍处于 pre-MVP 开发阶段。路线图从基础工作贯穿至 MVP 集成。仓库已经完成了初始环境/引导工作，现在还包括了一个稳定的确定性单智能体循环实现。同时，为工具、技能以及 LSP/ACP 的载体预留了初始扩展基础设施，而更广泛的 MVP 实现和 IDE 集成在当前阶段尚不在范围内。

## MVP 边界

### MVP 包含

- 用户可以提交开发任务
- 智能体可以阅读代码、搜索代码并调用工具
- 写入操作需要审批
- 会话可以恢复
- 确定性单智能体循环端到端运行
- 事件流可观测
- 为客户端渲染发出实时运行时事件

### MVP 不包含

- 多智能体团队系统
- 云端协作
- IDE 插件
- 插件市场支持
- 高级 MCP 生态系统工作
- 复杂的视觉工作台

## Epic 概览

### Epic 0: 基础

创建基准仓库和开发环境：Python 版本策略、`uv`、`mise`、仓库结构和 CI 基准。

**当前状态：** 基本完成。仓库现在拥有可用的开发者设置、CI、贡献者文档以及稳定的确定性单智能体循环。扩展基础设施已通过统一的配置模式、工具提供商接口和初始技能发现机制建立。

### Epic 1: LangGraph 核心循环

定义图状态、节点、图编译以及中断/恢复，以便执行单个智能体轮次。

**当前状态：** 完成。运行时现在实现了一个稳定的确定性单智能体循环，支持轮次执行、工具解析和会话恢复。

### Epic 2: 运行时骨架

构建自定义运行时外壳：会话管理器、运行时入口点、传输抽象以及运行时到图的集成。

**当前状态：** 完成。`VoidCodeRuntime` 边界已建立，支持 CLI 和 HTTP 传输，并具有统一的会话和事件处理。

### Epic 3: 工具注册与扩展

将工具和扩展作为运行时的一等公民，包含元数据、注册、内置功能和统一的执行管线。此 Epic 还包括作为运行时管理接口的技能、语言服务器（LSP）和智能体通信协议（ACP）的基础设施。

**当前状态：** 部分完成。内置工具和技能发现已实现。LSP 已具备 read-only runtime-managed 基线（manager、tool、事件与测试），但仍缺少独立的 server preset/config 模块；ACP 目前仍主要作为配置载体和 disabled stub 存在。

### Epic 4: 权限引擎

通过 `allow`、`deny` 和 `ask` 实现受控执行，在写入和高风险 shell 操作之前需要审批。

**当前状态：** 完成。运行时支持受监管的执行，并在所有传输方式中具有审批-恢复连续性。

### Epic 5: 钩子与事件引擎

通过钩子注册、工具前后钩子、轮次钩子和钩子执行日志添加事件驱动的扩展性。这包括用于客户端观测和轮次重新编号的规范运行时事件模式。

**当前状态：** 完成。运行时事件模式已稳定，并为所有主要循环阶段发出事件。已实现轮次重新编号，以确保会话恢复时的序列一致性。最小 pre/post tool hooks 已在运行时执行。 richer hook phases 和更宽的 hook framework 仍不在当前 MVP 范围内。

### Epic 6: 存储与恢复

在 SQLite 中持久化会话和执行状态，以便在重启后恢复中断的工作。

**当前状态：** 完成。完整的会话持久化（包括事件、输出和待审批项）已实现。

### Epic 7: 上下文与可观测性

管理长期运行的上下文，并提供对轮次、工具、审批、钩子和错误的追踪友好可见性。

**当前状态：** 部分完成。通过事件流实现的轮次级可观测性已完成；provider fallback、step budget 与恢复关键配置的运行时治理已经落地。`#70` 已经为 waiting / terminal session 落地了内部 resume checkpoint groundwork；当前最直接的后续工作是通过 `#82` 定义 retention / compaction / checkpoint invalidation 语义。`#83` 单独跟踪 corrupt / unreadable checkpoint 的 fallback correctness，而 `#84` 再承接 cold-session archive / replay 策略。

### Epic 8: TUI / CLI / Web 客户端

通过具有流式输出、审批交互和会话恢复的可用入口点暴露运行时。

**当前状态：** 进行中。CLI 仍是最完整的客户端入口；TUI 保持初始实现状态，优先级已下调，暂不作为当前 MVP 完成的硬条件；Web 客户端已经接入极简的真实运行时路径，并承担当前 MVP 主路径的主要客户端验证工作。

### Epic 9: MVP 集成

将整个路径连接成一个可演示的产品循环，包括端到端测试、故障处理、演示脚本和用户文档。

## Wave 概览

- **Wave 1:** 基础、初始图工作和运行时骨架（**已在仓库形式中部分完成**）
- **Wave 2:** 工具执行、权限和钩子
- **Wave 3:** 存储、恢复、上下文和可观测性
- **Wave 4:** 入口点、集成和 MVP 演示就绪

## MVP 完成信号

当 VoidCode 能够可靠地演示一个受监管的单智能体开发任务流，并具备持久化、审批、可观测性，以及至少一个经过真实验证的客户端入口点时，即认为达到了 MVP 边界。

## 当前最直接的后续工作

在最近几轮 runtime 配置、provider fallback、恢复语义和 checkpoint groundwork 收口之后，当前 backlog 中最直接的 runtime/platform follow-up 是两层：

### 1. 先完成当前存储/恢复主线的剩余 issue

- `#82`：定义 retention / compaction / checkpoint invalidation semantics
- `#83`：收口 corrupt / unreadable checkpoint fallback correctness
- `#84`：继续保持为后续的 cold-session archive / replay strategy，而不是现在就把 archive 实现塞进当前主线

### 2. 在不扩大协议面的前提下，启动下一批更大的 runtime capability issue

- runtime-managed provider config hardening
- runtime-managed skill execution semantics
- read-only managed LSP vertical slice
- tool contract hardening with formatter hook presets for common languages

这些 issue 都应继续遵守当前边界：

- 通过 runtime 统一进入执行路径
- 保持 CLI / HTTP / Web 共享同一套 persistence / replay truth
- 不把 ACP / MCP 提前拉进当前 MVP 主路径

因此，ACP 与 MCP 当前仍应作为边界预留或 design follow-up，而不是下一轮的实现主项。
