# MVP 演示指南

本文档定义了 VoidCode 的规范 MVP 演示场景和端到端验证清单。它基于目前已实现的稳定确定性单智能体循环、会话持久化、CLI 内联审批行为，以及已落地的双视图 Textual TUI。

## 前置条件

- 由 `uv` 管理的 Python 3.14 环境。
- 仓库已通过 `mise install && uv sync --extra dev` 初始化。
- 工作区中存在一个示例文件（例如 `README.md`）。
- 验证流程需要一个 TTY 环境（真实的终端）来演示内联审批。

## 规范演示流程

该流程证明了核心循环：执行、观察、审批、持久化和恢复。

1. **CLI 执行与内联审批**：运行一个受监管的、需要审批的写入任务。
   ```bash
   # 强制进入审批模式运行一个非只读任务
   uv run voidcode run "write hello.txt contents" --workspace . --approval-mode ask
   ```
   *预期结果*：
   - CLI 打印 `EVENT runtime.approval_requested`。
   - CLI 弹出类似于 `Approve write_file for write_file hello.txt? [y/N]: ` 的审批提示（具体的 target summary 可能包含工具名和路径）。
   - 输入 `y` 后，CLI 继续执行并显示写入状态的 `RESULT` 块。
   - 所有事件（请求接收、工具调用、审批通过等）均通过 `EVENT` 日志实时流式打印。

2. **会话持久化**：验证会话及其审批状态已记录。
   ```bash
   uv run voidcode sessions list --workspace .
   ```
   *预期结果*：表格显示该会话，状态为 `completed`（如果之前已完成）。

3. **会话恢复**：在不重新执行工具的情况下重放整个交互过程。
   ```bash
   uv run voidcode sessions resume <session-id> --workspace .
   ```
   *预期结果*：CLI 从持久化存储中检索并完整渲染所有历史事件及 `RESULT` 块。

4. **HTTP 传输观察**：通过 API 暴露会话状态。
   ```bash
   # 终端 A
   uv run voidcode serve --workspace . --port 8000

   # 终端 B
   curl http://127.0.0.1:8000/api/sessions
   ```
   *预期结果*：终端 B 收到包含该会话元数据（含 `prompt` 和 `status`）的 JSON 数组。

## 验证阶梯

只有通过以下所有步骤的任务才被认为是“MVP 可演示的”。

### 1. 单元层
- **契约**：确保遵守 `voidcode.runtime.contracts` 类型。
- **诊断**：`mise run typecheck` 必须返回零错误。
- **命令**：`uv run pytest tests/unit/`

### 2. 集成层
- **运行时循环**：验证完整的 `CLI -> 运行时 -> 图 -> 工具` 路径，包含审批中断。
- **持久化**：确保 SQLite 状态在进程重启后依然存在。
- **命令**：`uv run pytest tests/integration/test_read_only_slice.py`

### 3. 客户端冒烟测试
- **CLI TTY 审批**：验证在 TTY 模式下能否正确触发并响应内联审批提示。
- **HTTP/SSE**：通过 HTTP 验证流式序列化和会话重放。
- **命令**：`uv run pytest tests/integration/test_http_transport.py`

### 4. 手动 QA
- **视觉效果**：确认 CLI 中的 `EVENT` 和 `RESULT` 日志清晰可读。
- **服务**：确认 `voidcode serve` 处理并发请求。

### 5. TUI 冒烟层
- **启动页体验**：`uv run voidcode tui --workspace .` 默认进入 `StartupScreen`。验证只有 VoidCode 标题和极简 Composer，焦点位于 Composer。
- **提交与切换**：在启动页提交 prompt，验证界面立即切换到 `ConversationScreen` 并显示流式 Timeline。
- **直接打开会话**：`uv run voidcode tui --workspace . --session-id <session-id>` 验证直接进入 `ConversationScreen` 并重放历史。
- **审批模态框**：在 `waiting` 场景下，验证 `ApprovalModal` 自动弹出且聚焦于“Approve”。
- **命令**：`uv run pytest tests/unit/test_tui_app.py tests/unit/test_tui_session_view.py`

## 证据标准

要称之为 MVP 可演示，贡献者必须提供：
- `mise run check` 的输出（全绿）。
- 在新工作区成功执行**规范演示流程**（步骤 1-4）的日志，特别是包含内联审批交互的部分。
- 双视图 TUI 的最小验证证据：默认启动、`--session-id` 直开、流式 prompt、审批恢复与相关测试输出。
- 验证 `.voidcode/sessions.sqlite3` 中包含对应的会话和事件行。

## 边界与已知差距

### 目前可行的
- 确定性单智能体循环（支持多步、顺序执行）。
- 完整的 CLI TTY 内联审批（ask/allow/deny）。
- 本地 SQLite 会话持久化与恢复。
- 极简 HTTP/SSE 传输。
- Textual 重新设计的双视图 TUI：`StartupScreen` 启动页、`ConversationScreen` 会话页、多行 Composer、`--session-id` 直开、会话内审批模态框。

### 计划中（尚不可演示）
- **Web UI 集成**：React 外壳目前完全由模拟数据驱动，尚未接入真实的后端运行时。
- **真实 LLM 编排**：目前的图执行是基于确定性解析器的。
- **钩子主动执行**：事件（Events）已在运行时发出，但挂接在特定事件上的主动逻辑（Hooks）仍待实现。
