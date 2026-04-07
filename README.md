<p align="center">
  <a href="./LICENSE">
    <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License" />
  </a>
</p>

# VoidCode

VoidCode 是一个受 OpenCode 和 Claude Code 启发而开发的本地优先（local-first）编程智能体运行时。

> **状态：** VoidCode 目前处于早期开发的 pre-MVP 阶段。当前仓库重点在于构建运行时基础、架构边界以及首个端到端智能体循环所需的开发人员工作流。

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

目前的研发方向是有意收敛的：在扩展到更大的平台之前，先交付一个稳定的单智能体 MVP 循环。

## 快速上手

推荐的本地设置使用 uv 管理的 Python 环境和 Bun。支持的 Python 版本为 3.14。

> **注意：** 目前的实现包含了一个真实的确定性 CLI → 运行时 → 只读工具切片，以及一个极简的本地 HTTP/SSE 传输层。前端壳程序目前尚未对接该后端。

```bash
# 安装工具和 Python 环境
mise install
uv sync --extra dev

# 安装前端环境
mise run frontend:install

# 启动 CLI
uv run voidcode --help

# 验证确定性的只读切片
uv run voidcode run "read README.md" --workspace .

# 列出已持久化的会话
uv run voidcode sessions list --workspace .

# 恢复已持久化的会话
uv run voidcode sessions resume local-cli-session --workspace .

# 在 localhost:8000 启动本地后端传输服务
uv run voidcode serve --workspace . --host 127.0.0.1 --port 8000

# 启动 Web 前端（Mock 数据驱动）
mise run frontend:dev
```

## 架构概览

VoidCode 采用分层架构，其中 **LangGraph 负责智能体编排**，而**自定义运行时处理产品级逻辑**。

- 运行时（Runtime）是会话、权限、钩子、存储、流式传输和工具治理的系统边界。
- LangGraph 是计划中的编排引擎，用于处理图状态、路由、检查点和中断/恢复流程；目前仓库仅包含一个确定性的只读切片。
- CLI、未来的 Web 前端或未来的 IDE 集成等客户端与运行时通信，而不是直接调用工具。
- 代码库围绕三个核心领域组织：
  - `src/voidcode/runtime/`：运行时服务和执行边界
  - `src/voidcode/graph/`：LangGraph 编排和状态转换
  - `src/voidcode/tools/`：内置工具和工具元数据

从架构方案中延续的关键设计原则：

- 保持运行时、图编排和 UI 职责的清晰分离
- 在执行前通过注册表、权限和钩子对工具进行治理
- 使会话和执行状态可恢复
- 在控制写入操作的同时允许并发读取
- 优先考虑轮次、工具、审批、钩子和错误的观测性
- 围绕一个稳定的单智能体任务循环精简 MVP 范围

关于英文版的架构和路线图摘要，请参阅：

- [`docs/architecture.md`](./docs/architecture.md)
- [`docs/roadmap.md`](./docs/roadmap.md)
- [`docs/mvp-todo-plan.md`](./docs/mvp-todo-plan.md)
- [`docs/mvp-demo-guide.md`](./docs/mvp-demo-guide.md)
- [`docs/contracts/README.md`](./docs/contracts/README.md)
- [`docs/development.md`](./docs/development.md)

原始设计文档仍保留中文版：

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
