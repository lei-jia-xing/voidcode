# MVP 演示指南

本文档定义了 VoidCode 的规范 MVP 演示场景和端到端验证清单。它基于目前已实现的确定性只读运行时、会话持久化和 HTTP 传输行为。

## 前置条件

- 由 `uv` 管理的 Python 3.14 环境。
- 仓库已通过 `mise install && uv sync --extra dev` 初始化。
- 工作区中存在一个示例文件（例如 `README.md`）。
- 不要求已经存在 `.voidcode/sessions.sqlite3`，但持久化应该是可验证的。

## 规范演示流程

该流程证明了核心循环：执行、观察、持久化和恢复。

1. **CLI 执行**：运行一个受监管的只读任务。
   ```bash
   uv run voidcode run "read README.md" --workspace . --session-id demo-session
   ```
   *预期结果*：CLI 为每个阶段（请求已接收、工具查询等）打印 `EVENT` 日志，并以显示文件内容的 `RESULT` 块结束。

2. **会话持久化**：验证会话已记录。
   ```bash
   uv run voidcode sessions list --workspace .
   ```
   *预期结果*：表格显示 `demo-session`，状态为 `completed`。

3. **会话恢复**：在不重新执行工具的情况下重放会话。
   ```bash
   uv run voidcode sessions resume demo-session --workspace .
   ```
   *预期结果*：CLI 从持久化状态重新渲染完全相同的 `RESULT` 块。

4. **HTTP 传输观察**：通过 API 服务于会话列表。
   ```bash
   # 终端 A
   uv run voidcode serve --workspace . --port 8000

   # 终端 B
   curl http://127.0.0.1:8000/api/sessions
   ```
   *预期结果*：终端 B 收到一个包含 `demo-session` 元数据的 JSON 数组。

## 验证阶梯

只有通过以下所有步骤的任务才被认为是“MVP 可演示的”。

### 1. 单元层
- **契约**：确保遵守 `voidcode.runtime.contracts` 类型。
- **诊断**：`mise run typecheck` 必须返回零错误。
- **命令**：`uv run pytest tests/unit/`

### 2. 集成层
- **运行时循环**：验证完整的 `CLI -> 运行时 -> 图 -> 工具` 路径。
- **持久化**：确保 SQLite 状态在进程重启后依然存在。
- **命令**：`uv run pytest tests/integration/test_read_only_slice.py`

### 3. 客户端冒烟测试
- **CLI 卫生**：`voidcode --help` 和版本检查通过。
- **HTTP/SSE**：通过 HTTP 验证流式序列化和会话重放。
- **命令**：`uv run pytest tests/integration/test_http_transport.py`

### 4. 手动 QA
- **视觉效果**：确认 CLI 中的 `EVENT` 和 `RESULT` 日志是可读且不乱码的。
- **服务**：确认 `voidcode serve` 处理并发的 `GET /api/sessions` 请求。

## 证据标准

要称之为 MVP 可演示，贡献者必须提供：
- `mise run check` 的输出（全绿）。
- 在新工作区成功执行**规范演示流程**（步骤 1-4）的截图或日志。
- 验证步骤 1 之后 `.voidcode/sessions.sqlite3` 包含预期的行。

## 边界与已知差距

### 目前可行的
- 确定性只读执行（无需 LLM）。
- 本地 SQLite 会话持久化。
- 会话列表和恢复。
- 极简 HTTP/SSE 传输（会话、流式传输）。

### 计划中（尚不可演示）
- **TUI 客户端**：TUI 目前仅处于规范阶段 (`docs/tui-mvp-spec.md`)。
- **Web UI 集成**：React 外壳目前由 mock 数据驱动，尚未消费真实的 API。
- **写入审批**：针对 `ask/allow/deny` 的契约已存在，但默认的 CLI 循环中还没有真实的写入工具来触发它。
- **LLM 编排**：LangGraph 轮次循环目前是一个确定性的占位符。
