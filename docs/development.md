# 开发指南

本指南总结了为 VoidCode 贡献代码的本地工作流。

关于仓库的代码规范，请参阅 [`docs/coding-standards.md`](./coding-standards.md)。

## 工具基准

VoidCode 使用：

- `mise` 用于任务管理和加载现有的 `.venv`
- `uv` 用于依赖和包管理 (Python)
- `bun` 用于前端开发和依赖管理
- Python 3.14 作为官方支持的 uv 管理的本地版本

## 初始设置

安装工具链依赖和项目依赖：

```bash
mise install
uv sync --extra dev
mise run frontend:install
```

确认 CLI 入口点可用：

```bash
uv run voidcode --help
uv run voidcode run "read README.md" --workspace .
uv run voidcode sessions list --workspace .
uv run voidcode sessions resume local-cli-session --workspace .
```

## mise 任务

仓库定义了以下 `mise` 任务：

### Python 任务

- `mise run lint` → `uv run ruff check .`
- `mise run format` → `uv run ruff format .`
- `mise run typecheck` → `uv run basedpyright --warnings src`
- `mise run test` → `uv run pytest`

### 前端 任务

- `mise run frontend:install` → `bun install`
- `mise run frontend:dev` → `bun run dev`
- `mise run frontend:lint` → `bun run lint`
- `mise run frontend:typecheck` → `bun run typecheck`
- `mise run frontend:test` → `bun run test:run`
- `mise run frontend:coverage` → `bun run test:coverage`

### 全局 任务

- `mise run check` → 运行所有 Python 和前端检查
- `mise run pre-commit` → `uv run pre-commit run --all-files`

## MVP 演示与验证

关于规范的端到端演示流程和完整的验证阶梯（单元测试、集成测试和冒烟测试），请参阅 [`docs/mvp-demo-guide.md`](./mvp-demo-guide.md)。使用该指南验证核心的“只读”运行时循环和会话持久化在您的机器上是否正常工作。

`mise.toml` 不直接管理 Python 安装；它加载仓库现有的 `.venv` 并将 Python 依赖/环境管理委托给 `uv`。

## 前端开发

前端是一个基于 Bun 的 React 应用，位于 `frontend/` 目录中。

### 当前实现状态
- **UI 外壳**：功能性的导航和布局组件。
- **Mock 数据驱动**：所有智能体交互和会话数据目前在前端都是模拟的。
- **后端集成**：目前**没有**与 Python 后端运行时的实时连接。在此阶段，`src/voidcode` Python 包和 `frontend/` React 应用独立运行。

### 前端工作流

1.  **安装依赖**：`mise run frontend:install`
2.  **启动开发服务器**：`mise run frontend:dev` (运行在 [http://localhost:5173](http://localhost:5173))
3.  **Lint/类型检查**：`mise run frontend:lint` 和 `mise run frontend:typecheck`
4.  **运行组件测试**：`mise run frontend:test`
5.  **运行覆盖率测试**：`mise run frontend:coverage` 或使用 `mise run check` 进行全面验证。

## 项目布局

当前源码树为三个主要的实现领域预留了空间：

- `src/voidcode/runtime/`
- `src/voidcode/graph/`
- `src/voidcode/tools/`
- `frontend/` (React + Bun + Vite)

测试文件位于 `tests/` 目录下，原始规划文档保留在仓库根目录中（中文）。
