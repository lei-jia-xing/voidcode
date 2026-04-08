# 当前实现状态

本文档提供了 VoidCode 仓库截至 2026 年 4 月的真实快照。VoidCode 目前处于 **pre-MVP 基础阶段，具有稳定的单智能体循环**。

关于将当前仓库状态连接到预期 MVP 的具体交付清单，请参阅 [`docs/mvp-todo-plan.md`](./mvp-todo-plan.md)。关于规范的客户端面向契约，请参阅 [`docs/contracts/README.md`](./contracts/README.md)。

## 概览
仓库包含两个主要的、独立的组件：
1.  **Python 后端**：一个类型化契约层以及稳定的单智能体循环实现。
2.  **Bun 前端外壳**：一个基于 React 的 Web 界面，用于未来的智能体运行时。

**当前集成状态**：🟡 **极简 Web 传输已接通**。后端现在暴露了本地 HTTP/SSE 传输，前端已经消费会话列表、会话重放和流式运行路径，但整体 Web 体验仍然是 pre-MVP 水平。

---

## 后端 (Python)

### 今日已实现
- [x] **项目结构**：支持 Hatch/UV 的布局，包含 `src/voidcode/runtime`、`src/voidcode/graph` 和 `src/voidcode/tools`。
- [x] **CLI 入口点**：`voidcode --help` 和 `voidcode run "read <path>" --workspace <dir>` 均可工作。
- [x] **依赖管理**：为本地开发完全配置了 `pyproject.toml` 和 `mise.toml`。
- [x] **开发工具**：集成了 Ruff (lint/format)、basedpyright (types) 和 pytest (tests) 并可正常运行。
- [x] **契约层**：代码中存在类型化的会话、事件、运行时、图和工具契约。
- [x] **稳定的单智能体循环**：CLI 可以通过运行时、图和工具边界执行受监管的本地确定性多步请求，并发出可观测事件。
- [x] **扩展基础设施基础**：运行时现在包括工具、技能、LSP 和 ACP 的类型化配置和发现基础设施，并为 hooks/config MVP 提供了可收敛的配置边界。
- [x] **内置工具提供商**：专门的 `BuiltinToolProvider` 负责通过运行时边界注册 `grep`、`read_file`、`shell_exec` 和 `write_file`。
- [x] **技能发现基础设施**：对 `.voidcode/skills/<name>/SKILL.md` 文件存在极简发现机制；运行时在每次运行时发出 `runtime.skills_loaded` 事件。
- [x] **LSP 和 ACP 配置载体**：为未来的语言服务器和传输集成存在类型化配置载体和禁用的管理器/适配器存根。
- [x] **极简 HTTP 传输**：精简的后端 HTTP 层现在暴露了 `GET /api/sessions`、`GET /api/sessions/{session_id}` 和 `POST /api/runtime/run/stream`，其中 SSE 数据块直接从运行时边界序列化，并且现在可以通过 `voidcode serve` 在本地提供服务。

### 计划中 / 进行中
- [x] **LangGraph 编排**：稳定的单智能体确定性循环实现，支持顺序轮次执行、工具解析和中断/恢复。
- [x] **运行时服务**：会话生命周期管理、SQLite 持久化支持以及审批-恢复连续性。
- [x] **权限引擎**：受监管的执行，支持 `allow`、`deny` 和 `ask` 模式，并在 CLI 中具有仅限 TTY 的内联审批。
- [x] **契约优先事件**：为轮次、工具和审批实现了规范事件模式，并具备跨会话恢复的一致性自动重新编号功能。
- [x] **HTTP 传输对等**：后端 HTTP 层现在完全暴露了与 CLI 对等的会话列表/恢复和运行/流式操作，包括审批解析端点。
- [x] **极简 hooks/config MVP 闭环**：运行时已实现最小 pre/post tool hooks、`approval_mode`/`model` 的窄优先级基础、恢复关键配置持久化，以及 CLI `config show` 检查路径。
- [x] **动态工具注册**：运行时现在包括工具的类型化配置和发现基础设施，支持 `BuiltinToolProvider`。
- [ ] **技能执行**：发现机制已实现（发出 `runtime.skills_loaded`），但运行时尚未执行技能逻辑或提供特定于技能的工具上下文。
- [ ] **真实的 LSP 和 ACP 集成**：配置载体已存在；实际的进程管理和传输支持待办。
- [x] **TUI 客户端**：Textual 聊天优先（chat-first）TUI 已实现，默认直接进入单一会话时间线与底部 prompt，支持 `--session-id` 直接打开已持久化会话并在会话内处理审批模态框。
- [~] **Web 客户端集成**：后端传输已就绪；前端已接入会话列表、会话重放和流式运行的最小路径，但更完整的运行时驱动体验仍在进行中。

---

## 前端 (React + Bun)

### 今日已实现
- [x] **UI 框架**：React 18、Tailwind CSS 和 Lucide React 外壳。
- [x] **组件库**：布局、导航和消息线程 UI 组件。
- [x] **最小运行时传输接入**：前端现在通过 Zustand store 和运行时客户端消费真实的会话列表、会话重放以及流式运行事件。
- [x] **前端工具**：基于 Vite 的开发服务器，支持 Bun、ESLint 和 Prettier。

### 计划中 / 进行中
- [~] **实时 API 集成**：针对极简传输，前端已经接入会话列表、会话重放和流式运行事件；更完整的运行时驱动任务体验仍待继续实现。
- [ ] **WebSocket 流式传输**：来自后端的实时智能体事件流。
- [~] **会话持久化**：后端数据库驱动的真实会话持久化和重放已经可用，前端对这一路径的消费仍限于当前 MVP 级会话视图。
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
