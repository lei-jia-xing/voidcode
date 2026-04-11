# 当前实现状态

本文档提供了 VoidCode 仓库截至 2026 年 4 月的真实快照。VoidCode 当前已经具备清晰的 **MVP 主路径基线**：稳定的单智能体循环、受监管的工具执行、会话恢复，以及由 CLI 与 Web 共享的真实运行时路径都已经落地；TUI 仍停留在初始实现阶段。

关于将当前仓库状态连接到预期 MVP 的具体交付清单，请参阅 [`docs/mvp-todo-plan.md`](./mvp-todo-plan.md)。关于规范的客户端面向契约，请参阅 [`docs/contracts/README.md`](./contracts/README.md)。

## 概览
仓库包含两个主要的、独立的组件：
1.  **Python 后端**：一个带有类型化契约层的无头运行时，以及稳定的单智能体循环实现。
2.  **客户端层**：包含 CLI、初始 TUI，以及一个基于 React + Bun 的 Web 前端。

**当前集成状态**：🟡 **CLI 与 Web 主路径已经打通**。CLI 和 Web 都已经连接到共享运行时边界，并且核心链路 `运行 -> 审批 -> 持久化 -> 重放/恢复` 已有自动化验证与手工证据；TUI 目前仍是较早期的客户端壳层。

---

## 后端 (Python)

### 今日已实现
- [x] **项目结构**：支持 Hatch/UV 的布局，包含 `src/voidcode/runtime`、`src/voidcode/graph` 和 `src/voidcode/tools`。
- [x] **CLI 入口点**：`voidcode --help` 和 `voidcode run "read <path>" --workspace <dir>` 均可工作。
- [x] **依赖管理**：为本地开发完全配置了 `pyproject.toml` 和 `mise.toml`。
- [x] **开发工具**：集成了 Ruff (lint/format)、basedpyright (types) 和 pytest (tests) 并可正常运行。
- [x] **契约层**：代码中存在类型化的会话、事件、运行时、图和工具契约。
- [x] **稳定的单智能体循环**：CLI 可以通过运行时、图和工具边界执行受监管的本地确定性多步请求，并发出可观测事件。
- [x] **扩展基础设施基础**：运行时现在包括工具、技能、LSP 和 ACP 的类型化配置和发现基础设施，并为 hooks/config MVP 提供了清晰的配置边界。
- [x] **内置工具提供商**：专门的 `BuiltinToolProvider` 负责通过运行时边界注册 `grep`、`read_file`、`shell_exec` 和 `write_file`。
- [x] **技能发现基础设施**：对 `.voidcode/skills/<name>/SKILL.md` 文件存在极简发现机制；运行时在每次运行时发出 `runtime.skills_loaded` 事件。
- [x] **LSP 和 ACP 扩展基础设施**：LSP 已具备运行时管理的基础能力（配置、manager、事件与只读 tool 基线），ACP 仍主要停留在类型化配置与适配器存根阶段。
- [x] **极简 HTTP 传输**：精简的后端 HTTP 层现在暴露了 `GET /api/sessions`、`GET /api/sessions/{session_id}` 和 `POST /api/runtime/run/stream`，其中 SSE 数据块直接从运行时边界序列化，并且现在可以通过 `voidcode serve` 在本地提供服务。
- [x] **运行时配置分层**：运行时现在显式支持 `execution_engine`、`provider_fallback` 与 `max_steps`，并将恢复关键配置持久化到 `SessionState.metadata["runtime_config"]`，以保证 `config show`、resume 和 provider fallback 语义一致。

### 计划中 / 进行中
- [x] **LangGraph 编排**：稳定的单智能体确定性循环实现，支持顺序轮次执行、工具解析和中断/恢复。
- [x] **运行时服务**：会话生命周期管理、SQLite 持久化支持以及审批-恢复连续性。
- [x] **权限引擎**：受监管的执行，支持 `allow`、`deny` 和 `ask` 模式，并在 CLI 中具有仅限 TTY 的内联审批。
- [x] **契约优先事件**：为轮次、工具和审批实现了规范事件模式，并具备跨会话恢复的一致性自动重新编号功能。
- [x] **HTTP 传输对等**：后端 HTTP 层现在完全暴露了与 CLI 对等的会话列表/恢复和运行/流式操作，包括审批解析端点。
- [x] **极简 hooks/config MVP 闭环**：运行时已实现最小 pre/post tool hooks、`approval_mode` / `model` / `max_steps` 的恢复关键优先级基础、provider fallback 与 step budget 的持久化恢复语义，以及 CLI `config show` 检查路径。
- [x] **动态工具注册**：运行时现在包括工具的类型化配置和发现基础设施，支持 `BuiltinToolProvider`。
- [x] **Provider-backed 单智能体路径**：运行时已经具备 provider fallback、context window 管理、approval resume 连续性与可配置 step budget 的运行时治理基础。
- [ ] **技能执行**：发现机制已实现（发出 `runtime.skills_loaded`），但运行时尚未执行技能逻辑或提供特定于技能的工具上下文。
- [ ] **LSP preset/config 模块与 ACP 真实集成**：LSP 的只读 runtime-managed 基线已经存在，但仍缺少独立的 server preset/config 模块（extension/language 映射、root markers、默认 command、preset override merge）；ACP 仍待真实传输与生命周期集成。
- [ ] **长会话保留策略**：`#70` 已完成 waiting / terminal session 的内部 resume checkpoint groundwork；当前 runtime 主线的直接后续工作是 `#82`，用于定义 retention / compaction / checkpoint invalidation 语义。`#83` 另行跟踪 corrupt / unreadable checkpoint 的 fallback correctness，`#84` 再继续承接 cold-session archive / replay 策略。
- [~] **TUI 客户端**：已具备提示词输入和审批处理的初始实现，但会话管理、恢复/重放与规范冒烟验证仍未收口，当前优先级也已下调。
- [x] **Web 客户端集成**：已接入真实的会话列表、会话重放、流式运行和审批处理路径，并具备真实 store/client 闭环验证。

---

## 前端 (React + Bun)

### 今日已实现
- [x] **UI 框架**：React 18、Tailwind CSS 和 Lucide React 外壳。
- [x] **组件库**：布局、导航和消息线程 UI 组件。
- [x] **最小运行时传输接入**：前端现在通过 Zustand store 和运行时客户端消费真实的会话列表、会话重放、流式运行事件以及审批处理路径。
- [x] **前端工具**：基于 Vite 的开发服务器，支持 Bun、ESLint 和 Prettier。

### 计划中 / 进行中
- [x] **实时 API 集成**：前端已经接入会话列表、会话重放、流式运行事件与审批处理，并具备面向真实 store/client 的闭环验证。
- [ ] **WebSocket 流式传输**：来自后端的实时智能体事件流。
- [x] **会话持久化**：后端数据库驱动的真实会话持久化和重放已经可用，CLI/TUI/Web 都能消费这一共享路径。
- [ ] **文件系统浏览器**：与本地工作区集成以进行代码阅读。

### 规划状态
- [x] **基础 / Epic 0**：开发工具、仓库结构、CI 基准和面向贡献者的文档已基本到位。
- [ ] **面向客户端的可执行契约层**：契约文档现在存在于 `docs/contracts/` 下，但针对它们的实现工作仍在进行中。

---

## 仓库元数据与链接
- **规范仓库**：[https://github.com/lei-jia-xing/voidcode](https://github.com/lei-jia-xing/voidcode)
- **默认分支**：`master`
- **Issue 追踪**：已在 GitHub 上启用。
- **项目范围**：受 OpenCode 和 Claude Code 启发而开发的本地优先编程智能体运行时。
