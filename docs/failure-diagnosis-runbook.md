# 故障诊断与恢复操作手册

本文档为 VoidCode MVP 的 CLI + Web 路径提供一套可重复的故障诊断和恢复流程。它与 [`mvp-demo-guide.md`](./mvp-demo-guide.md) 中的规范演示流程配套使用，覆盖"出了问题怎么办"这一侧。

## 适用范围

本手册仅涉及当前 MVP 已实现的运行时表面：

- CLI：`voidcode run`、`voidcode sessions list`、`voidcode sessions resume`、`voidcode sessions answer`、`voidcode serve`、`voidcode tasks` 子命令、`voidcode config show`、`voidcode doctor`
- HTTP 传输：`voidcode serve` 暴露的 `/api/sessions`、`/api/tasks`、`/api/runtime/run/stream` 等端点
- SQLite 持久化：`.voidcode/sessions.sqlite3` 中面向操作员排障最重要的 `sessions`、`session_events`、`background_tasks`、`session_notifications` 表（另有用于事件投递去重的 `session_event_deliveries` 表）
- 会话状态字面量：`idle`、`running`、`waiting`、`completed`、`failed`
- 审批决策：`allow`、`deny`、`ask`
- 恢复检查点类型：`approval_wait`、`question_wait`、`terminal`

不在此范围内的内容（TUI、真实 LLM 编排、多代理拓扑、云端协作）不在本手册的恢复步骤中涉及。

---

## 一、常见故障场景矩阵

| 场景 | 典型表现 | 最可能的会话状态 | 首要观察点 |
|------|----------|------------------|-----------|
| 审批阻塞 | CLI 停在 `Approve ...? [y/N]:` 提示，或 HTTP 端点返回 `waiting` 状态 | `waiting` | `runtime.approval_requested` 事件、`pending_approval_json` 列 |
| 任务执行失败 | CLI 打印 `EVENT runtime.failed`，RESULT 为空或含错误信息 | `failed` | `runtime.failed` 事件的 `error` payload |
| 会话未找到 | `sessions resume` 或 HTTP `/api/sessions/{id}` 返回 404 | 无记录 | SQLite `sessions` 表中是否存在对应 `session_id` |
| 后台子任务卡住 | `tasks list` 显示 `queued` 或 `running` 但长时间无进展 | 父会话可能 `running` 或 `waiting` | `background_tasks` 表的 `status`、`error`、`cancellation_cause` 列 |
| 数据库损坏或 schema 不匹配 | 启动任何命令即报 `sqlite runtime schema mismatch` | 不可用 | `.voidcode/sessions.sqlite3` 文件是否存在、schema 是否与代码中 `_CANONICAL_SCHEMA` 一致 |
| 工作区路径错误 | 命令报 `workspace does not exist` | 不可用 | `--workspace` 参数指向的目录是否存在 |
| 配置解析失败 | `doctor` 返回非零退出码，或 `config show` 报错 | 不可用 | `.voidcode/config.json`（如存在）是否为合法 JSON |
| Provider / model 未就绪 | `doctor` 报 `provider.readiness`，`runtime.failed` 含 `provider_error_kind` | `failed` 或不可用 | `voidcode config show` 的 `provider_readiness` 与 `context_budget` |
| HTTP 服务未启动 | `curl` 连接被拒绝 | 不适用 | `voidcode serve` 进程是否在运行、端口是否被占用 |

---

## 二、按会话状态的恢复动作

### 2.1 `idle`

`idle` 是当前运行时契约中的合法状态字面量，但在 CLI + Web 主路径里通常不是贡献者会频繁看到的持久化终态。若它出现在排障上下文中，应理解为“当前没有活动执行”。

- **诊断**：`voidcode sessions list --workspace .` 查看状态列。
- **恢复**：通常无需恢复。如需重新执行，使用新的 `voidcode run` 命令创建新会话。

### 2.2 `running`

会话正在执行中。

- **诊断**：
  - CLI：观察 `EVENT` 流是否仍在推进。
  - HTTP：`GET /api/sessions/{id}` 返回 `"status": "running"`。
  - SQLite：`SELECT status, turn, last_event_sequence FROM sessions WHERE session_id = ?`。
- **恢复**：
  - 如果进程仍在运行，等待其自然完成。
  - 如果进程已异常退出但状态仍为 `running`，这是持久化快照与进程生命周期不同步的表现。当前 MVP 没有针对该状态的受支持“继续执行”入口；应先检查最近事件和相关后台任务，再基于现有上下文重新发起新会话。

### 2.3 `waiting`

会话因审批或问题等待用户输入而暂停。

- **诊断**：
  - CLI 输出中最后一条事件应为 `EVENT runtime.approval_requested source=runtime ...` 或 `EVENT runtime.question_requested source=runtime ...`。
  - SQLite 中 `pending_approval_json` 或 `pending_question_json` 列非空。
  - `resume_checkpoint_json` 的 `kind` 字段为 `approval_wait` 或 `question_wait`。
- **恢复（CLI）**：
  - 如果 CLI 仍在前台等待输入，直接键入 `y`（允许）或 `N`（拒绝）。
  - 如果 CLI 已退出，使用 resume 命令提供审批决策：
    ```bash
    uv run voidcode sessions resume <session-id> \
      --workspace . \
      --approval-request-id <request-id> \
      --approval-decision allow
    ```
    其中 `<request-id>` 可从 `runtime.approval_requested` 事件的 `request_id` payload 中获取，或从 SQLite 的 `pending_approval_json` 中解析。
  - 如果等待原因是 `question_wait`，使用 `sessions answer` 提交回答并恢复会话：
    ```bash
    uv run voidcode sessions answer <session-id> \
      --workspace . \
      --question-request-id <request-id> \
      --response "answer text"
    ```
    单问题文本回答可重复传入 `--response` 形成同一问题的多个答案；多问题或精确 header 绑定场景使用 `--response-json '[{"header":"Confirm","answers":["yes"]}]'`。其中 `<request-id>` 可从 `runtime.question_requested` 事件的 `request_id` payload 中获取，或从 SQLite 的 `pending_question_json` 中解析。
- **恢复（HTTP）**：
  - 提交审批决策：
    ```bash
    curl -X POST http://127.0.0.1:8000/api/sessions/{session-id}/approval \
      -H 'Content-Type: application/json' \
      -d '{"request_id": "<request-id>", "decision": "allow"}'
    ```
  - 决策值为 `"allow"` 或 `"deny"`。响应为 `RuntimeResponse` JSON，包含恢复后的事件和当前输出；会话可能继续运行、再次进入 `waiting`，也可能到达 `completed` / `failed`。
  - 如果是问题等待（question_wait），也可以使用 `/api/sessions/{session-id}/question` 端点，payload 包含 `question_request_id` 和 `responses` 数组，例如：
    ```bash
    curl -X POST http://127.0.0.1:8000/api/sessions/{session-id}/question \
      -H 'Content-Type: application/json' \
      -d '{"question_request_id": "<request-id>", "responses": [{"header": "Confirm", "answers": ["answer text"]}]}'
    ```

### 2.4 `completed`

会话已成功完成。

- **诊断**：`sessions list` 显示 `completed`，`resume_checkpoint_json` 的 `kind` 为 `terminal`。
- **恢复**：无需恢复。使用 `sessions resume` 可重放完整历史用于审查。

### 2.5 `failed`

会话在执行过程中遇到不可恢复的错误。

- **诊断**：
  - CLI 输出中最后一条事件为 `EVENT runtime.failed source=runtime ...`。
  - SQLite 中 `status = 'failed'`。
  - `resume_checkpoint_json` 的 `kind` 为 `terminal`（失败也属于终态）。
  - 检查 `session_events` 表中最后几条事件的 `event_type` 和 `payload_json`，定位失败前的最后一个工具调用或图步骤。
- **恢复**：
  - 失败会话本身不可继续执行（终态检查点）。
  - 分析失败原因后，使用新的 `voidcode run` 命令发起新会话。
  - 如果失败由审批拒绝（`deny`）引起，这是预期行为，不是故障。

---

## 三、统一观察点

### 3.1 CLI EVENT / RESULT 输出

CLI 的 `_format_event` 函数将每个运行时事件格式化为：

```
EVENT {event_type} source={source} [key=value ...]
```

关键事件类型：

| 事件类型 | 含义 | 诊断价值 |
|----------|------|----------|
| `runtime.request_received` | 请求已接收 | 确认会话启动 |
| `runtime.tool_started` | 工具开始执行 | 跟踪执行进度 |
| `runtime.tool_completed` | 工具执行完成 | 检查 `status` 字段为 `ok` 或 `error` |
| `runtime.approval_requested` | 等待审批 | 提取 `request_id` 用于 resume |
| `runtime.approval_resolved` | 审批已解决 | 确认审批决策已记录 |
| `runtime.question_requested` | 等待用户回答问题 | 提取 `request_id` |
| `runtime.failed` | 会话失败 | 查看 `error` payload |
| `graph.response_ready` | 图执行完成，响应就绪 | 正常结束标志 |

`RESULT` 块在最终输出后打印，包含会话的最终输出文本。

### 3.2 sessions list / resume

```bash
# 列出所有会话
uv run voidcode sessions list --workspace .
# 输出格式：SESSION id=<id> status=<status> turn=<n> updated_at=<ts> prompt='<prompt>'

# 重放历史
uv run voidcode sessions resume <session-id> --workspace .
```

`sessions list` 的输出直接反映 SQLite 中 `sessions` 表的 `status`、`turn`、`prompt`、`updated_at` 列。

### 3.2.1 Provider / model / context readiness

Provider-first execution failures should be diagnosed from the shared runtime metadata instead of client-specific string matching:

```bash
uv run voidcode doctor --workspace . --verbose
uv run voidcode config show --workspace .
```

Key JSON fields:

- `provider_readiness.status`: `ready`, `missing_auth`, `unconfigured`, `missing_model`, or a provider feature/readiness status.
- `provider_readiness.guidance`: user-facing recovery step safe for CLI/Web/ACP display.
- `provider_readiness.fallback_chain`: effective primary + fallback provider/model targets.
- `context_budget.context_window` / `max_output_tokens`: model/catalog or configured budget metadata used to explain compaction/context-limit behavior.
- `runtime.failed.provider_error_kind`: structured provider failure kind such as `missing_auth`, `invalid_model`, `rate_limit`, `transient_failure`, `context_limit`, `unsupported_feature`, or `stream_tool_feedback_shape`.

Common recovery actions:

- `missing_auth`: configure the provider API key via environment variable or `.voidcode.json`.
- `invalid_model`: fix the provider/model name or model access permissions.
- `context_limit`: reduce prompt/tool context, rely on compaction, or switch to a larger-context model.
- `unsupported_feature`: disable streaming/tool features for that provider or switch provider/model.

### 3.3 SQLite 直接检查

数据库路径：`.voidcode/sessions.sqlite3`

```bash
# 查看会话列表
sqlite3 .voidcode/sessions.sqlite3 \
  "SELECT session_id, status, turn, prompt, last_event_sequence, updated_at FROM sessions ORDER BY updated_at DESC;"

# 查看特定会话的事件时间线
sqlite3 .voidcode/sessions.sqlite3 \
  "SELECT sequence, event_type, source, payload_json FROM session_events WHERE session_id = '<id>' ORDER BY sequence;"

# 查看待审批信息
sqlite3 .voidcode/sessions.sqlite3 \
  "SELECT session_id, pending_approval_json, pending_question_json, resume_checkpoint_json FROM sessions WHERE session_id = '<id>';"

# 查看后台任务状态
sqlite3 .voidcode/sessions.sqlite3 \
  "SELECT task_id, status, prompt, error, cancellation_cause, result_available FROM background_tasks ORDER BY updated_at DESC;"

# 查看未读通知
sqlite3 .voidcode/sessions.sqlite3 \
  "SELECT notification_id, session_id, kind, status, summary FROM session_notifications WHERE status = 'unread';"

# 如需确认事件是否已被投递/去重，可查看投递记录
sqlite3 .voidcode/sessions.sqlite3 \
  "SELECT workspace, session_id, dedupe_key, delivered_at FROM session_event_deliveries ORDER BY delivered_at DESC LIMIT 20;"
```

`resume_checkpoint_json` 列包含 JSON 对象，其 `kind` 字段为 `approval_wait`、`question_wait` 或 `terminal` 之一。

### 3.4 HTTP / API 表面

| 端点 | 方法 | 用途 |
|------|------|------|
| `/api/sessions` | GET | 列出所有会话 |
| `/api/sessions/{id}` | GET | 恢复（replay）指定会话 |
| `/api/sessions/{id}/result` | GET | 获取会话最终结果 |
| `/api/sessions/{id}/approval` | POST | 提交审批决策 |
| `/api/sessions/{id}/question` | POST | 回答问题请求 |
| `/api/sessions/{id}/tasks` | GET | 列出该会话的后台子任务 |
| `/api/tasks` | GET | 列出所有后台任务 |
| `/api/tasks` | POST | 启动后台任务 |
| `/api/tasks/{id}` | GET | 查看任务状态 |
| `/api/tasks/{id}/output` | GET | 获取任务输出 |
| `/api/tasks/{id}/cancel` | POST | 取消任务 |
| `/api/runtime/run/stream` | POST | 流式执行新请求（SSE） |
| `/api/settings` | GET/POST | 读取/更新 Web 设置 |
| `/api/notifications` | GET | 列出通知 |
| `/api/notifications/{id}/ack` | POST | 确认通知 |

---

## 四、标准恢复工作流（CLI + Web）

当演示或日常使用中遇到问题时，按以下顺序排查：

### 步骤 1：确认工作区和数据库

```bash
# 确认工作区目录存在
ls -la .voidcode/sessions.sqlite3 2>/dev/null || echo "数据库不存在"

# 运行能力检查
uv run voidcode doctor --workspace .
```

如果数据库不存在，说明尚未执行过任何会话。直接运行 `voidcode run` 即可创建。

### 步骤 2：查看会话状态

```bash
uv run voidcode sessions list --workspace .
```

找到目标会话，记录其 `status`。

### 步骤 3：根据状态采取行动

- **`waiting`**：按 [2.3 节](#23-waiting) 的恢复步骤提供审批或回答问题。
- **`failed`**：查看事件时间线定位失败原因，然后发起新会话。
- **`running`**：检查进程是否仍在运行。如果进程已退出但状态仍为 `running`，参考 [2.2 节](#22-running)。
- **`completed`**：无需恢复，必要时只做 replay 审查。
- **`idle`**：把它视为“当前无活动执行”的词汇级状态；若确实看到该状态，通常不需要恢复，直接重新发起任务即可。

### 步骤 4：检查后台任务（如涉及委派执行）

```bash
uv run voidcode tasks list --workspace .
uv run voidcode tasks status <task-id> --workspace .
uv run voidcode tasks output <task-id> --workspace .
```

如果任务卡在 `queued` 或 `running` 且长时间无响应：

```bash
uv run voidcode tasks cancel <task-id> --workspace .
```

### 步骤 5：HTTP 路径验证（Web 客户端场景）

```bash
# 确认服务在运行
curl -s http://127.0.0.1:8000/api/sessions | python3 -m json.tool

# 查看特定会话
curl -s http://127.0.0.1:8000/api/sessions/{id} | python3 -m json.tool

# 如果会话 waiting，提交审批
curl -s -X POST http://127.0.0.1:8000/api/sessions/{id}/approval \
  -H 'Content-Type: application/json' \
  -d '{"request_id": "...", "decision": "allow"}' | python3 -m json.tool
```

### 步骤 6：数据库级深度诊断

如果上述步骤无法定位问题，直接查询 SQLite：

```bash
# 查看最近会话的完整事件时间线
sqlite3 .voidcode/sessions.sqlite3 \
  "SELECT e.session_id, e.sequence, e.event_type, e.source, e.payload_json
   FROM session_events e
   JOIN sessions s ON e.session_id = s.session_id
   WHERE s.updated_at = (SELECT MAX(updated_at) FROM sessions)
   ORDER BY e.sequence;"
```

### 步骤 7：重置本地存储（最后手段）

如果数据库损坏或 schema 不匹配：

```bash
rm .voidcode/sessions.sqlite3
# 重新运行任意 voidcode 命令将自动重建数据库
```

注意：这将丢失所有已持久化的会话历史。

---

## 五、与 MVP 证据标准的对齐

[MVP 演示指南](./mvp-demo-guide.md) 中的证据标准要求：

1. `mise run check` 全绿
2. 规范演示流程（步骤 1.4）成功执行
3. `.voidcode/sessions.sqlite3` 中包含对应的会话和事件行

本手册提供的诊断和恢复流程用于支撑 MVP 证据标准：

- 当演示流程中的任一步骤失败时，贡献者可以按状态分类快速定位问题，而不是猜测。
- 恢复动作仅使用当前已实现的 CLI 命令、HTTP 端点和 SQLite 查询，不引入未实现的行为。
- 所有观察点（EVENT 日志、sessions list、SQLite 表、HTTP 响应）与运行时实际发出的数据一致。
- 失败是可观测的：`runtime.failed` 事件携带错误信息，`session_events` 表保留完整时间线，`background_tasks` 表记录 `error` 和 `cancellation_cause`。

它把 MVP 待办计划中“失败是足够可观测的，无需猜测即可调试”的要求，落成了面向贡献者的可执行排障步骤。
