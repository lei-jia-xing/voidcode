# 开发指南

本指南总结了为 VoidCode 贡献代码的本地工作流。

关于仓库的代码规范，请参阅 [`docs/coding-standards.md`](./coding-standards.md)。

## 工具基准

VoidCode 使用：

- `mise` 用于任务管理和加载现有的 `.venv`
- `uv` 用于依赖和包管理 (Python)
- `bun` 用于前端开发和依赖管理
- Python 3.13 作为官方支持的 uv 管理的本地版本

## 初始设置

安装工具链依赖和项目依赖：

```bash
mise install
uv sync --extra dev
mise run frontend:install
```

确认 CLI 入口点可用，且能处理多步执行与审批：

```bash
uv run voidcode --help
# 执行带内联审批的任务 (非只读工具如 write)
uv run voidcode run "write hello.txt hello" --workspace . --approval-mode ask
uv run voidcode sessions list --workspace .
uv run voidcode sessions resume <session-id> --workspace .
```

## mise 任务

仓库定义了以下 `mise` 任务：

### Python 任务

- `mise run lint` → `uv run ruff check .`
- `mise run format` → `uv run ruff format .`
- `mise run typecheck` → `uv run basedpyright --warnings src`
- `mise run test` → `uv run pytest`
- `mise run build` → `uv build`

Python 测试现在同时包含示例型测试和一小批基于 Hypothesis 的 property tests。当前这类覆盖刻意保持在 helper 层，主要用于验证像 `apply_patch`、`edit`、`todo_write`、`glob` 这类确定性字符串/patch/summary/路径规范化逻辑，而不是直接对 runtime 主循环做随机化测试。为了让 CI 行为稳定，这批测试使用有界 strategy，并通过 Hypothesis 设置保持可重复的 deterministic 运行。

### 前端 任务

- `mise run frontend:install` → `bun install`
- `mise run frontend:dev` → `bun run dev`
- `mise run frontend:lint` → `bun run lint`
- `mise run frontend:typecheck` → `bun run typecheck`
- `mise run frontend:test` → `bun run test:run`
- `mise run frontend:coverage` → `bun run test:coverage`

### 全局 任务

- `mise run check` → 运行所有 Python 和前端检查
- `mise run ci` → 运行 `mise run check`，随后构建 Python 包和前端生产包，用于合并前的 CI parity 验证
- `mise run pre-commit` → `uv run pre-commit run --all-files`

当前 pre-commit 会直接执行仓库约定的 Python 质量门禁：`ruff check --fix` 会自动修复可安全修复的问题，`ruff format` 会直接格式化文件，随后再运行 `basedpyright` 做类型检查。

## MVP 演示与验证

关于规范的端到端演示流程和完整的验证阶梯（单元测试、集成测试、客户端冒烟测试），请参阅 [`docs/mvp-demo-guide.md`](./mvp-demo-guide.md)。使用该指南验证稳定的确定性运行时循环、内联审批和会话持久化。

`mise.toml` 不直接管理 Python 安装；它加载仓库现有的 `.venv` 并将 Python 依赖/环境管理委托给 `uv`。Release workflow 与本地支持政策保持一致，使用 Python 3.13 构建 Python 包；如果需要在本地复现合并前门禁，优先运行 `mise run ci`。

## 前端开发

前端是一个基于 Bun 的 React 应用，位于 `frontend/` 目录中。

### 当前实现状态
- **UI 外壳**：已经具备可用的导航、布局和主要信息面板。
- **最小运行时接入**：前端已经接入真实的会话列表、会话重放、流式运行和审批处理路径。
- **仍在进行中的部分**：更完整的运行时驱动任务体验、客户端状态打磨以及更强的端到端验证仍待继续完善。

### 前端工作流

1.  **安装依赖**：`mise run frontend:install`
2.  **启动开发服务器**：`mise run frontend:dev` (运行在 [http://localhost:5173](http://localhost:5173))
3.  **Lint/类型检查**：`mise run frontend:lint` 和 `mise run frontend:typecheck`
4.  **运行组件测试**：`mise run frontend:test`
5.  **运行覆盖率测试**：`mise run frontend:coverage` 或使用 `mise run check` 进行全面验证。

## 项目布局

当前源码树已经不止三个主要实现领域，而是以三条核心执行边界为中心，并补充了能力层与客户端子模块：

- `src/voidcode/runtime/`
- `src/voidcode/graph/`
- `src/voidcode/tools/`
- `src/voidcode/hook/`
- `src/voidcode/lsp/`
- `src/voidcode/skills/`
- `src/voidcode/provider/`
- `src/voidcode/acp/`
- `src/voidcode/mcp/`
- `src/voidcode/tui/`
- `frontend/` (React + Bun + Vite)

测试文件位于 `tests/` 目录下，原始规划文档保留在仓库根目录中（中文）。
