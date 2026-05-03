# CLI/HTTP/Web 契约一致性规范

## 目的

定义 CLI、HTTP 传输层和 Web 客户端之间必须保持的字段和结构一致性规则。
这些规则确保三个消费方对相同的运行时状态和事件做出相同的解释，防止契约漂移。

## 范围

本规范覆盖：

- 事件信封（EventEnvelope）序列化
- 会话摘要（StoredSessionSummary）结构
- 会话状态（SessionState）结构
- Background task 状态/输出/结果结构
- 运行时通知（RuntimeNotification）结构
- 会话结果（RuntimeSessionResult）结构

## 所有权规则

- **运行时（Python）** 是契约的唯一权威来源
- CLI 和 HTTP 传输层是运行时的适配器，必须忠实地投影运行时边界类型
- Web 客户端的 TypeScript 类型必须与 Python 运行时类型保持字段级一致
- 任何字段的重命名或删除必须同时更新所有三个消费方

## 事件信封（EventEnvelope）一致性

### 必填字段

所有传输层必须包含以下字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `session_id` | `str` | 事件所属的会话 ID |
| `sequence` | `int` | 会话内单调递增的序列号 |
| `event_type` | `str` | 事件类型标识符 |
| `source` | `"runtime" \| "graph" \| "tool"` | 事件来源 |
| `payload` | `dict[str, object]` | 事件载荷 |

### 可选字段

| 字段 | 条件 | 说明 |
|------|------|------|
| `delegated_lifecycle` | 当事件为 delegated background task 事件时 | 类型化的 delegated lifecycle 载荷 |

### 实现位置

- Python 运行时：`src/voidcode/runtime/events.py:EventEnvelope`
- CLI 序列化：`src/voidcode/cli_support.py:serialize_event`
- HTTP 序列化：`src/voidcode/runtime/http.py:RuntimeTransportApp._serialize_event`
- TypeScript 类型：`frontend/src/lib/runtime/types.ts:EventEnvelope`

### 验证测试

参见 `tests/unit/test_contract_parity.py:test_cli_and_http_event_serialization_share_required_fields`

## 会话摘要（StoredSessionSummary）一致性

### 结构要求

所有传输层必须使用嵌套的 `SessionRef` 结构，而非扁平的 `id`/`parent_id` 字段：

```json
{
  "session": {
    "id": "session-123",
    "parent_id": "parent-456"
  },
  "status": "completed",
  "turn": 3,
  "prompt": "user prompt",
  "updated_at": 1234567890
}
```

### 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `session.id` | `str` | 会话 ID（必填） |
| `session.parent_id` | `str \| null` | 父会话 ID（仅子会话时出现） |
| `status` | `"idle" \| "running" \| "waiting" \| "completed" \| "failed"` | 会话状态 |
| `turn` | `int` | 轮次计数 |
| `prompt` | `str` | 用户提示 |
| `updated_at` | `int` | 最后更新时间戳 |

### 实现位置

- Python 运行时：`src/voidcode/runtime/session.py:StoredSessionSummary`
- CLI 序列化：`src/voidcode/cli_support.py:serialize_stored_session_summary`
- HTTP 序列化：`src/voidcode/runtime/http.py:RuntimeTransportApp._serialize_stored_session_summary`
- TypeScript 类型：`frontend/src/lib/runtime/types.ts:StoredSessionSummary`

### 验证测试

参见 `tests/unit/test_contract_parity.py:test_cli_and_http_session_summary_use_nested_session_ref`

## Background Task 状态一致性

### BackgroundTaskState 必填字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `task.id` | `str` | 任务 ID |
| `status` | `str` | 任务状态 |
| `request` | `BackgroundTaskRequestSnapshot` | 原始请求快照 |
| `created_at` | `int` | 创建时间戳 |
| `created_at_unix_ms` | `int \| null` | 创建时间戳（毫秒） |
| `started_at_unix_ms` | `int \| null` | 开始时间戳（毫秒） |
| `finished_at_unix_ms` | `int \| null` | 完成时间戳（毫秒） |
| `observability` | `dict \| null` | 可观测性数据 |

### BackgroundTaskResult 必填字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `task_id` | `str` | 任务 ID |
| `status` | `str` | 任务状态 |
| `duration_seconds` | `float \| null` | 执行时长（秒） |
| `tool_call_count` | `int` | 工具调用次数 |
| `observability` | `dict \| null` | 可观测性数据 |

### 实现位置

- Python 运行时：`src/voidcode/runtime/task.py:BackgroundTaskState`
- Python 运行时：`src/voidcode/runtime/contracts.py:BackgroundTaskResult`
- HTTP 序列化：`src/voidcode/runtime/http.py:RuntimeTransportApp._serialize_background_task_state`
- TypeScript 类型：`frontend/src/lib/runtime/types.ts:BackgroundTaskState`

### 验证测试

参见 `tests/unit/test_contract_parity.py:test_background_task_state_includes_unix_ms_timestamps`

## RuntimeSessionResult 一致性

### 必填字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `session` | `SessionState` | 会话状态 |
| `prompt` | `str` | 用户提示 |
| `status` | `str` | 会话状态 |
| `summary` | `str` | 会话摘要（必填，非可选） |
| `output` | `str \| null` | 输出内容 |
| `error` | `str \| null` | 错误信息 |
| `transcript` | `EventEnvelope[]` | 事件转录 |
| `last_event_sequence` | `int` | 最后事件序列号 |
| `revert_marker` | `RuntimeSessionRevertMarker \| null` | 回退标记 |

### 实现位置

- Python 运行时：`src/voidcode/runtime/contracts.py:RuntimeSessionResult`
- HTTP 序列化：`src/voidcode/runtime/http.py:RuntimeTransportApp._serialize_session_result`
- TypeScript 类型：`frontend/src/lib/runtime/types.ts:RuntimeSessionResult`

### 验证测试

参见 `tests/unit/test_contract_parity.py:test_runtime_session_result_includes_revert_marker`

## RuntimeStatusSnapshot 一致性

### 必填字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `git` | `GitStatusSnapshot` | Git 状态 |
| `lsp` | `CapabilityStatusSnapshot` | LSP 状态 |
| `mcp` | `CapabilityStatusSnapshot` | MCP 状态 |
| `acp` | `CapabilityStatusSnapshot` | ACP 状态 |
| `background_tasks` | `RuntimeBackgroundTaskStatusSnapshot` | Background task 状态概览 |

### 实现位置

- Python 运行时：`src/voidcode/runtime/contracts.py:RuntimeStatusSnapshot`
- TypeScript 类型：`frontend/src/lib/runtime/types.ts:RuntimeStatusSnapshot`

### 验证测试

参见 `tests/unit/test_contract_parity.py:test_runtime_status_snapshot_includes_background_tasks`

## 字段变更流程

当需要添加、重命名或删除任何运行时边界字段时：

1. **更新 Python 运行时类型**：修改 `contracts.py`、`session.py`、`task.py` 或 `events.py`
2. **更新 CLI 序列化**：修改 `cli_support.py` 中的序列化函数
3. **更新 HTTP 序列化**：修改 `http.py` 中的 `_serialize_*` 方法
4. **更新 TypeScript 类型**：修改 `frontend/src/lib/runtime/types.ts`
5. **更新契约文档**：修改本文档和相关的 `docs/contracts/*.md`
6. **更新测试**：添加或修改 `tests/unit/test_contract_parity.py` 中的验证测试
7. **运行验证**：确保所有契约一致性测试通过

## 相关测试

- `tests/unit/test_contract_parity.py` — CLI/HTTP 序列化字段一致性测试
- `tests/unit/runtime/test_backend_contracts.py` — 后端契约类型测试
- `tests/unit/interface/test_cli_delegated_parity.py` — CLI delegated lifecycle 一致性测试
- `tests/integration/test_http_delegated_parity.py` — HTTP delegated lifecycle 一致性测试

## 相关代码

- `src/voidcode/runtime/contracts.py` — 运行时边界类型
- `src/voidcode/runtime/events.py` — 事件信封和类型
- `src/voidcode/runtime/session.py` — 会话类型
- `src/voidcode/runtime/task.py` — Background task 类型
- `src/voidcode/cli_support.py` — CLI 序列化
- `src/voidcode/runtime/http.py` — HTTP 传输和序列化
- `frontend/src/lib/runtime/types.ts` — TypeScript 类型定义
