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
# 显式进入产品规划模式，用于需求澄清、范围收敛和验收标准
uv run voidcode run --agent product "shape an issue for improving session replay" --workspace .
uv run voidcode run --json "read README.md" --workspace .
uv run voidcode sessions list --workspace .
uv run voidcode sessions list --json --workspace .
uv run voidcode sessions resume <session-id> --workspace .
uv run voidcode commands list --workspace .
uv run voidcode commands show review --workspace .
# Delegated/background task surfaces
uv run voidcode tasks list --workspace .
uv run voidcode tasks status <task-id> --workspace .
uv run voidcode tasks output <task-id> --workspace .
uv run voidcode tasks cancel <task-id> --workspace .
```

CLI 的默认输出面向人工阅读：`run` 会隐藏原始事件转储，只展示关键进度、最终结果和 session id。需要脚本消费时使用 `--json`；在 JSON 模式下，stdout 仅保留机器可解析的 JSON，进度/诊断信息输出到 stderr。

## mise 任务

仓库定义了以下 `mise` 任务：

### Python 任务

- `mise run lint` → `uv run ruff check .`
- `mise run format` → `uv run ruff format .`
- `mise run typecheck` → `uv run basedpyright --warnings src`
- `mise run test:fast` → 并行运行主要 unit tests，跳过 integration、fuzz-style tests 和大型 runtime extension 回归文件
- `mise run test` → `uv run pytest -n auto`
- `mise run test:coverage` → `uv run pytest -n auto --cov=voidcode --cov-report=term-missing`
- `mise run build` → `uv build`

Python 测试现在同时包含示例型测试和一小批基于 Hypothesis 的 property tests。当前这类覆盖刻意保持在 helper 层，主要用于验证像 `apply_patch`、`edit`、`todo_write`、`glob` 这类确定性字符串/patch/summary/路径规范化逻辑，而不是直接对 runtime 主循环做随机化测试。为了让 CI 行为稳定，这批测试使用有界 strategy，并通过 Hypothesis 设置保持可重复的 deterministic 运行。

默认本地 Python 测试不再自动启用 coverage：`mise run test:fast` 用于高频迭代，跳过 integration、fuzz-style 单测和大型 runtime extension 回归文件；`mise run test` 使用 `pytest-xdist` 并行运行完整 pytest；`mise run test:coverage` 与 CI 的 Python 测试路径保持 coverage-bearing 验证。

### 前端 任务

- `mise run frontend:install` → `bun install`
- `mise run frontend:dev` → `bun run dev`
- `mise run frontend:lint` → `bun run lint`
- `mise run frontend:typecheck` → `bun run typecheck`
- `mise run frontend:test` → `bun run test:run`
- `mise run frontend:e2e` → `bun run build && bun run test:e2e`
- `mise run frontend:coverage` → `bun run test:coverage`

### 全局 任务

- `mise run check` → 运行所有 Python 和前端静态/单元检查
- `mise run frontend:check:e2e` → 运行前端静态/单元检查后再运行 Playwright E2E
- `mise run ci` → 运行 Python lint/typecheck、coverage-bearing pytest、Python 构建、前端检查和前端生产构建，用于合并前的 CI parity 验证
- `mise run pre-commit` → `uv run pre-commit run --all-files`

当前 pre-commit 会直接执行仓库约定的 Python 质量门禁：`ruff check --fix` 会自动修复可安全修复的问题，`ruff format` 会直接格式化文件，随后再运行 `basedpyright` 做类型检查。

## MVP 演示与验证

关于规范的端到端演示流程和完整的验证阶梯（单元测试、集成测试、客户端冒烟测试），请参阅 [`docs/mvp-demo-guide.md`](./mvp-demo-guide.md)。使用该指南验证稳定的确定性运行时循环、内联审批和会话持久化。

`mise.toml` 不直接管理 Python 安装；它加载仓库现有的 `.venv` 并将 Python 依赖/环境管理委托给 `uv`。Release workflow 与本地支持政策保持一致，使用 Python 3.13 构建 Python 包；如果需要在本地复现合并前门禁，优先运行 `mise run ci`。

## 运行时可观测性与调试

当前仓库的主观测面不是单独的日志系统，而是 runtime 统一发出的事件流，以及会话持久化后可重放的 transcript。

排查 agent / runtime 行为时，优先看以下几层：

1. `docs/contracts/runtime-events.md`：稳定事件词汇表与顺序语义的权威来源。
2. CLI / HTTP / replay 输出：验证某次 run 实际暴露给客户端的事件序列。
3. `.voidcode/sessions.sqlite3` 中持久化的 session transcript：验证恢复 / 重放是否与首次运行保持一致。

对于工具调用，当前稳定契约建议按下面四个阶段理解：

- `graph.tool_request_created`：graph 计划调用某个工具。
- `runtime.tool_lookup_succeeded`：runtime 已经成功解析该工具。
- `runtime.tool_started`：权限与 pre-hook 已经通过，真实工具执行开始。
- `runtime.tool_completed`：工具结果已经返回并写入 transcript。

这能帮助你区分：

- 卡在审批前，还是卡在真正执行前；
- 时间消耗发生在 hook / permission，还是发生在工具本身；
- replay 缺口是事件持久化问题，还是执行路径本身没有发出事件。

当前不建议为这类分析优先新增第二套结构化日志。只要问题属于 session truth、审批、hook、tool execution 或 replay，一般都应先扩展或使用现有 `EventEnvelope` transcript，而不是并行维护另一套不可重放的日志面。

### Delegated / background task 调试

当前 delegated child execution 是 runtime-owned surface，不是 CLI、HTTP 或 ACP 各自维护的执行路径。排查这类问题时按以下边界验证：

- `docs/contracts/background-task-delegation.md` 是 parent/child linkage、notification、result、retry/cancel 语义的权威文档。
- `task` 工具负责 category / `subagent_type` routing 校验；runtime 负责 child session、tool allowlist guardrail、hooks、MCP lifecycle 与持久化。
- `background_output` 可以读取摘要，也可以用 bounded `full_session=true` 查看 child transcript；`message_limit` 被限制在 1 到 100，失败结果只建议在用户明确要求时用 `session_id` 继续或重试。
- `background_cancel` 对 unknown、running、completed、cancelled 等状态返回确定性 payload，不应被包装成未验证的文本约定。
- CI 与本地测试使用 fake provider 和 fake MCP 覆盖 delegated/MCP lifecycle；不需要 live provider，也不需要真实 `npx @playwright/mcp` 才能验证这些契约。

相关验证命令：

```bash
uv run pytest tests/unit/runtime/test_runtime_events.py tests/unit/interface/test_cli_delegated_parity.py
uv run pytest tests/unit/tools/test_background_task_tools.py tests/unit/runtime/test_mcp.py -k "background or cancel or output or mcp"
mise run check
```

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
5.  **运行 Launcher E2E**：`mise run frontend:e2e`。该路径会先构建 `frontend/dist`，再通过 `voidcode web --no-open` 启动本地 launcher，避免自动弹出额外浏览器窗口。
6.  **运行覆盖率测试**：`mise run frontend:coverage` 或使用 `mise run check` 进行常规全面验证。

仓库根目录不维护 `package.json`。根目录命令统一通过 `mise.toml` 暴露；Bun 脚本只在 `frontend/package.json` 中维护，避免出现两套前端命令入口。

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
