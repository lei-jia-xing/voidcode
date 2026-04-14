<p align="center">
  <a href="./LICENSE">
    <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License" />
  </a>
</p>

# VoidCode

VoidCode 是一个受 OpenCode 和 Claude Code 启发的本地优先（local-first）编程智能体运行时。

> **状态：** VoidCode 目前仍处于 pre-MVP 阶段。当前仓库的重点是打磨运行时基础、收紧架构边界，并完成首个可重复验证的端到端单智能体循环。

> **当前文档状态：** `docs/current-state.md` 描述了现状，`docs/roadmap.md` 描述了高层路线图，`docs/mvp-todo-plan.md` 包含执行清单，`docs/mvp-demo-guide.md` 提供了端到端验证步骤，`docs/contracts/` 定义了面向客户端的运行时契约。

## VoidCode 的目标

VoidCode 旨在提供以以下能力为中心的本地开发智能体体验：

- 对话式任务执行
- 代码阅读与搜索
- 受控的工具调用与文件编辑
- 针对危险操作的权限检查点
- 钩子（hooks）与事件流
- 会话持久化与恢复
- 与 CLI 或未来 UI 客户端分离的无头运行时

当前的研发重点保持聚焦：在扩展到更大的平台之前，先交付一个稳定、可演示的单智能体 MVP 循环。multi-agent 支持已被确定为 post-MVP 方向，但不会改变 runtime 作为系统控制面的定位。

## 快速上手

推荐的本地设置使用 uv 管理的 Python 环境和 Bun。支持的 Python 版本为 3.13。

> **注意：** 当前仓库已经具备真实的确定性 CLI → 运行时 → 单智能体循环，支持多步执行、会话持久化与恢复，以及 TTY 环境下的内联写入审批。后端还提供了极简的本地 HTTP/SSE 传输层。TUI 已有初始实现，Web 前端也已经接通了最小可用的运行时路径（会话列表、会话重放、流式运行与审批处理），但它们都还没有达到与 CLI 完全对齐的成熟度。

```bash
# 安装工具和 Python 环境
mise install
uv sync --extra dev

# 安装前端环境
mise run frontend:install

# 启动 CLI
uv run voidcode --help

# 运行一个多步执行的确定性任务
uv run voidcode run "read README.md" --workspace .

# 运行一个需要审批的写入任务（触发内联审批）
uv run voidcode run "write hello.txt hello world" --workspace . --approval-mode ask

# 列出已持久化的会话
uv run voidcode sessions list --workspace .
```

## 架构概览

VoidCode 采用 runtime-centric 分层架构：**runtime 是系统控制面**，**graph 是执行/编排层**，而 LangGraph 当前只覆盖其中一条确定性切片。

- 运行时（Runtime）是会话、权限、工具、存储、流式传输和治理的系统边界。
- graph 负责执行循环与状态推进；当前 `DeterministicReadOnlyGraph` 使用 LangGraph，而 provider-backed 单智能体路径由 runtime 直接驱动，不依赖 LangGraph。
- CLI、未来的 Web 前端或未来的 IDE 集成等客户端与运行时通信。CLI 支持在 TTY 环境下进行实时的内联写入审批（inline approval）。
- 未来的 multi-agent 方向将继续保持 runtime-owned 治理，并计划引入 `src/voidcode/agent/` 作为预定义 agent 定义边界。
- 代码库当前围绕运行时控制面、编排层、工具层以及若干能力/客户端子模块组织：
  - `src/voidcode/runtime/`：运行时服务和执行边界
  - `src/voidcode/graph/`：执行/编排层（当前既包含 LangGraph-backed slice，也包含非 LangGraph path）
  - `src/voidcode/tools/`：内置工具和工具元数据
  - `src/voidcode/hook/`：hook 配置与执行器
  - `src/voidcode/lsp/`、`skills/`、`provider/`、`acp/`、`mcp/`：能力层边界目录

未来计划引入 `src/voidcode/agent/`，作为预定义 agent 定义边界，用于声明 prompt / hook / skill / MCP / tool / provider 配置。
  - `src/voidcode/tui/`：终端客户端层

从架构方案中延续的关键设计原则：

- 保持运行时、图编排和 UI 职责的清晰分离
- 在执行前通过注册表、权限和钩子对工具进行治理
- 使会话和执行状态可恢复
- 在控制写入操作的同时允许并发读取
- 优先考虑轮次、工具、审批、钩子和错误的观测性
- 围绕一个稳定的单智能体任务循环精简 MVP 范围

关于架构、路线图、MVP 计划和运行时契约，请参阅：

- [`docs/architecture.md`](./docs/architecture.md)
- [`docs/roadmap.md`](./docs/roadmap.md)
- [`docs/mvp-todo-plan.md`](./docs/mvp-todo-plan.md)
- [`docs/mvp-demo-guide.md`](./docs/mvp-demo-guide.md)
- [`docs/contracts/README.md`](./docs/contracts/README.md)
- [`docs/development.md`](./docs/development.md)

仓库根目录中仍保留了更早阶段的原始中文设计文档：

- `voidcode-architecture-v1.md`
- `voidcode-backlog-v1.md`

## 开发工作流

一次性安装依赖：

```bash
mise install
uv sync --extra dev
```

常用任务定义在 `mise.toml` 中：

```bash
# Python 任务
mise run lint
mise run format
mise run typecheck
mise run test

# 前端任务 (Bun)
mise run frontend:install
mise run frontend:dev
mise run frontend:lint
mise run frontend:typecheck

# 联合检查 (Python + 前端)
mise run check

# Pre-commit 检查
mise run pre-commit
```

在本地设置 pre-commit 钩子：

```bash
uv run pre-commit install
```

当前的 pre-commit 配置会运行仓库卫生检查以及 Ruff 和 basedpyright。`mise` 会加载现有的 `.venv` 进行任务执行；uv 仍是 Python 环境和依赖的真实来源。

## 贡献与社区

- 贡献指南：[`CONTRIBUTING.md`](./CONTRIBUTING.md)
- 行为准则：[`CODE_OF_CONDUCT.md`](./CODE_OF_CONDUCT.md)
- 更新日志：[`CHANGELOG.md`](./CHANGELOG.md)

## 开源协议

VoidCode 采用 [MIT 协议](./LICENSE)发布。
