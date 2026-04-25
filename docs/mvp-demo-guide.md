# MVP 演示指南

本文档定义了 VoidCode 的规范 MVP 演示场景和端到端验证清单。它基于目前已实现的稳定确定性运行时循环、会话持久化和 CLI 内联审批行为。

## 前置条件

- 由 `uv` 管理的 Python 3.13 环境。
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

4. **失败诊断观察面**：通过 CLI 或 HTTP 读取 runtime-owned debug snapshot。
   ```bash
   uv run voidcode sessions debug <session-id> --workspace .

   # 或者在 HTTP 服务启动后读取同一份诊断视图
   curl http://127.0.0.1:8000/api/sessions/<session-id>/debug
   ```
   *预期结果*：可以直接看到当前/持久化 session status、是否 active、是否可 resume / replay、最近相关事件、最近失败分类，以及建议的下一步操作，而不需要直接读 SQLite 或源码。

5. **HTTP 传输观察**：通过 API 暴露会话状态。
   ```bash
   # 终端 A
   uv run voidcode serve --workspace . --port 8000

   # 终端 B
   curl http://127.0.0.1:8000/api/sessions
   ```
   *预期结果*：终端 B 收到包含该会话元数据（含 `prompt` 和 `status`）的 JSON 数组，并可进一步访问 `/api/sessions/<session-id>/debug` 获取诊断快照。

## 验证阶梯

只有通过以下所有步骤的任务才被认为是“MVP 可演示的”。

### 1. 单元层
- **契约**：确保遵守 `voidcode.runtime.contracts` 类型。
- **诊断**：`mise run typecheck` 必须返回零错误。
- **命令**：`uv run pytest tests/unit/`

### 2. 集成层
- **运行时循环**：验证完整的运行时执行、delegated lifecycle 与审批中断路径。
- **持久化**：确保 SQLite 状态在进程重启后依然存在。
- **命令**：`uv run pytest tests/integration/test_http_transport.py tests/integration/test_http_delegated_parity.py`

### 3. 客户端冒烟测试
- **CLI TTY 审批**：验证在 TTY 模式下能否正确触发并响应内联审批提示。
- **HTTP/SSE**：通过 HTTP 验证流式序列化、会话重放和 session debug snapshot。
- **命令**：`uv run pytest tests/integration/test_http_transport.py`

### 4. 手动 QA
- **视觉效果**：确认 CLI 中的 `EVENT` 和 `RESULT` 日志清晰可读。
- **服务**：确认 `voidcode serve` 处理并发请求。

## 证据标准

要称之为 MVP 可演示，贡献者必须提供：
- `mise run check` 的输出（全绿）。
- 在新工作区成功执行**规范演示流程**（步骤 1-5）的日志，特别是包含内联审批交互和失败诊断快照的部分。
- 验证 `.voidcode/sessions.sqlite3` 中包含对应的会话和事件行。

## 边界与已知差距

### 目前可行的
- 确定性运行时循环（支持多步、顺序执行）。
- 完整的 CLI TTY 内联审批（ask/allow/deny）。
- 本地 SQLite 会话持久化与恢复。
- 极简 HTTP/SSE 传输。

### 已纳入当前 MVP 演示范围
- **Web UI 集成**：Web 前端已接入真实运行时路径，并对 run -> approval -> replay 主链路具备真实 store/client 闭环验证。

### 当前不作为 MVP 完成证明项
- **TUI 客户端**：TUI 仍处于初始实现阶段，当前不再作为 MVP 完成的硬性证明项。

### 暂不纳入当前 MVP 演示范围
- **真实 LLM 编排**：目前的图执行是基于确定性解析器的。
- **钩子主动执行**：事件（Events）已在运行时发出，但挂接在特定事件上的主动逻辑（Hooks）仍待实现。

## 提供商与演示边界（第一阶段）

本节定义 VoidCode 第一阶段演示的接受标准、提供商集成边界和凭证处理规则。

### 主验收门：桩支撑的确定性测试

第一阶段的主验收门**始终是桩支撑的确定性测试**，而非实时提供商调用。具体而言：

- 所有核心路径（CLI 入口、运行时循环、会话持久化、审批流、HTTP/SSE 传输）必须通过确定性测试验证，不依赖外部 LLM 提供商。
- 测试使用桩响应或确定性解析器，保证可重复、可离线、无外部依赖。
- `mise run check` 全绿是演示准备就绪的必要条件。
- 如果确定性测试失败，实时提供商冒烟测试不应执行。

### 实时提供商冒烟测试：次要且可选

实时提供商调用（即向真实 LLM API 发送请求并接收响应）属于**次要的、可选的**验证步骤，不作为 MVP 完成的硬性证明项。如果选择运行实时冒烟测试，必须遵守以下规则：

- **凭证必须通过环境变量提供**，例如 `OPENAI_API_KEY`、`ANTHROPIC_API_KEY` 等。
- **凭证不得提交到仓库**。`.env` 文件、本地配置文件或其他包含密钥的文件必须出现在 `.gitignore` 中。
- 冒烟测试应当使用最小化请求（例如单轮对话、简单 prompt），仅验证 provider 连接路径和响应解析是否正确。
- 冒烟测试失败不影响主验收门的判定。

### v1 是 leader-owned runtime，带已交付 delegated child execution

当前版本（v1）的执行模型仍然是 **leader-owned runtime**，而不是任意拓扑的多代理编排。明确说明：

- 顶层 active run 只接受 `leader`。
- runtime-owned delegation path 已经可以启动 `worker`、`advisor`、`explore`、`researcher`、`product` 这些 child preset，并通过 background task / child session / notification / result retrieval surfaces 暴露生命周期真相。
- 文档中不得把当前状态描述成“任意多代理平台”或“ACP 已经接管完整协作控制面”；但也不得再把 delegated child execution 描述成完全不存在。

### 凭证处理原则

- 仓库中不得出现任何硬编码的 API 密钥、token 或凭证。
- 所有敏感配置必须通过运行时环境变量或 `.gitignore` 覆盖的本地配置文件注入。
- 演示脚本、测试代码和文档示例中如涉及凭证，必须使用占位符（如 `<YOUR_API_KEY>`）或明确标注"从环境变量读取"。
