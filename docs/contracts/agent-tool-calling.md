# Agent-facing tool calling contract

来源 Issue：#163

## 目的

本文档定义 VoidCode runtime / CLI / Web 主路径中 **agent 应如何理解、选择和调用工具**。它面向会生成 `ToolCall` 的 agent / execution engine，而不是面向 UI 组件或工具实现作者。

它回答以下问题：

- 当前 runtime 可暴露哪些工具，以及这些工具如何分组；
- agent 调用工具时使用什么入口、参数 shape 与返回 shape；
- 哪些工具是只读，哪些会进入 approval / permission 路径；
- 相似工具之间如何选择；
- 本文档与 `tools` 契约、runtime contracts、agent preset 文档之间的边界关系。

## 范围与非目标

范围仅限当前 runtime、CLI 与 Web 主路径的 agent-facing tool usage 说明。

本文档不做以下事情：

- 不定义 TUI 专用交互；
- 不引入新的执行语义、权限语义或工具实现；
- 不扩展 secret / config 写入；
- 不把当前受控的 delegated execution 误述为 arbitrary multi-agent topology 或完整产品化协作平台；
- 不替代 `src/voidcode/tools/contracts.py` 中的 Python 类型定义。

## 与现有文档和代码的关系

| Surface | 负责什么 | 与本文档的关系 |
| --- | --- | --- |
| `src/voidcode/tools/contracts.py` | `ToolDefinition` / `ToolCall` / `ToolResult` 的实现级类型 | 权威代码契约；本文档解释 agent 如何消费这些 shape |
| `src/voidcode/tools/README.md` | 工具层边界：工具能力层负责实现，不负责运行时策略 | 本文档建立在该能力层之上，但不改变工具层职责 |
| `src/voidcode/runtime/service.py` | tool registry、permission、hook、event、session persistence 的 runtime truth | 本文档描述 agent-visible 调用路径；执行治理仍归 runtime |
| `docs/contracts/approval-flow.md` | 客户端可见 approval 请求与处理语义 | 本文档只说明哪些 tool call 会进入 approval 路径以及 agent 应如何预期 |
| `docs/contracts/runtime-events.md` | 客户端渲染的稳定事件词汇 | 本文档引用 tool lookup / permission / started / completed 等事件，不重新定义完整事件表 |
| `src/voidcode/agent/README.md` | agent preset / role intent 与 runtime 边界 | preset 可以描述希望携带的工具组合；实际执行、审批和恢复仍由 runtime 管理 |

## 核心调用 envelope

所有内置工具通过 runtime tool registry 暴露。agent 不直接实例化工具，也不绕过 runtime 调用工具。

### Tool definition

Runtime 暴露给 agent 的工具元数据遵循以下 shape：

```json
{
  "name": "read",
  "description": "Read a file inside the current workspace.",
  "input_schema": {
    "path": {"type": "string"},
    "offset": {"type": "integer"},
    "limit": {"type": "integer"}
  },
  "effects": ["read"]
}
```

字段含义：

- `name`：传给 `ToolCall.tool_name` 的稳定工具名；
- `description`：面向 agent 的简短用途说明；
- `input_schema`：参数对象的最小 schema；
- `effects`：`read/write/execute/network/spawn/session` 行为事实，不是权限授予；runtime 按实际调用、路径、规则和当前模式决定是否执行。

### Provider-visible metadata与按需 guidance

provider 消费的 typed `ToolDefinition` 包含短、准确的 `description`、
canonical `input_schema` 和 effect facts；具体 provider wire 由 adapter 编码。
provider-visible description 不包含 `src/voidcode/tools/*.txt` 中的长篇操作
guidance；不要把 sidecar guidance 复制进 description 或 schema。

工具的完整操作说明只通过现有的内部 URL 按需读取：

```text
read(path="voidcode://tool/<name>")
```

该 URI 从当前 live registry 解析工具，返回完整 sidecar `guidance`、当前
`input_schema`、`effects` 和派生的 `read_only` 观察元数据。因此它也适用于 essential/discoverable split
下未列在 provider 顶层的工具。读取文档不会授权或执行工具；需要调用时仍
使用顶层工具或 `invoke_tool`，并由 runtime 重新执行 allowlist、permission、
approval 和 replay policy 检查。动态 MCP/local tool 的事实以当前 declaration registry、canonical `ReportedCall.result.body` 和 runtime metadata/events 为准。`ToolResultView` 不携带 body；provider-visible output 不是结构化工具 body。

`invoke_tool` 只接受必填 `name` 与可选 `arguments`；未知 wrapper 字段会被拒绝。
先用 `read(path="voidcode://tool/<name>")` 获取目标 schema，再将目标参数放在
`arguments` 中。runtime 以真实目标工具名执行 policy、permission 与工具验证；
未知工具、目标参数错误和拒绝会作为目标工具的结构化错误反馈继续本轮。

### Rulebook resources

Discoverable workspace rules are exposed as bounded runtime resources. Read a rule
by its canonical URI before relying on its contents:

```text
read(path="voidcode://rule/<name>")
```

The runtime validates a single safe slug against the workspace rule catalog and
enforces line/byte bounds. Reading a rule never grants tools, approvals,
delegation, or other runtime capabilities. Rule metadata snapshots are versioned
and replay only uses entries whose current content hash matches the persisted
snapshot.

### Tool call

Agent 发起工具调用时只提交工具名与参数对象：

```json
{
  "tool_name": "read",
  "arguments": {
    "path": "README.md",
    "offset": 1,
    "limit": 2000
  }
}
```

运行时负责：

1. 从 registry 解析 `tool_name`；
2. 由 runtime 根据 effect facts、实际 operation class、路径、规则和当前模式做 permission 决策；
3. 在允许执行时运行 pre-tool hooks；
4. 在真正跨入工具执行边界时发出 `runtime.tool_started`；
5. 通过 `invoke(call, context=ToolContext(...))` 传入真实 workspace、身份、read ledger、abort/progress 与该工具实际需要的 resource handles；
6. 发出 tool result 事件并运行 post-tool hooks；
7. 将 session、events、approval state 和 checkpoint 持久化。

### Tool result

工具实现返回封闭的 `ToolSuccess` / `ToolFailure` alternative。Runtime 将实际报告包装为 `ReportedCall`：保留原始 tool-call ID、最终工具名、已授权 arguments 与 canonical result。严格 version-1 persisted report codec 位于 `runtime/execution/report_codec.py`；它没有通用 `data` facade。

`ToolSuccess` / `ToolFailure` 的 `output` 是 typed `TextOutput`、`EmptyOutput` 或 `AttachmentOutput`，带真实 `OutputBounds`。可选 `body` 是工具 owner 的 typed DTO，或仅限 external/package data 的 scoped `OpaqueToolBody`；它不提供 identity、permission、control 或 provider authority。控制语义由有限的 typed control alternatives 表达。

Model-facing `ToolResultView` 是隔离投影，包含调用 ID、最终工具名、已授权 arguments、typed output/status，以及 failure 时的 error/diagnostics；它不引用 canonical body 或 control。Provider 只看到 output presentation，不读取内部结构化 body。Attachment 通过 typed `AttachmentOutput` 表达，不从任意 metadata 反推。


在 runtime event 流中，工具相关的稳定执行边界当前至少包括：

1. `runtime.tool_lookup_succeeded`：runtime 已经解析到真实工具；
2. `runtime.permission_resolved` / `runtime.approval_requested` / `runtime.approval_resolved`：runtime 已完成权限判断或进入/恢复审批路径；
3. `runtime.tool_started`：权限与 pre-tool hook 已通过，真实工具执行现在开始；
4. `runtime.tool_completed`：工具已经返回结果。

其中，成功执行后的结果以 `runtime.tool_completed` 发出，payload 至少包含：

```json
{
  "tool": "read",
  "status": "ok",
  "content": "...",
  "error": null
}
```

并会展开合并该工具的 `data` 字段。

### 取消与超时（execution lifecycle）

生命周期规范（取消如何传入、何时允许提交结果、何时失去所有权、何时释放资源）以
[`execution-lifecycle.md`](./execution-lifecycle.md) 为唯一权威表述；本节只保留工具作者需要知道的细节。

Runtime 拥有工具执行生命周期：工具不得自行判定"这次调用算不算结束"，也不得假设只有成功返回的结果才会被记录。

- **取消如何传入正在运行的工具**：runtime 交给工具的 `context.abort_signal` 就是该次调用的取消视图。用户中断取消 run 级信号；runtime 超时只取消该次调用（不得把 run 变成 `interrupted`）。两者都通过同一个槽位 `context.abort_signal.cancelled` 对工具可见；工具必须轮询它，并在观察到之后尽快停止，不得继续写入。
若工具在中断回收窗口内返回带 `ConfirmedStop` / `UnconfirmedStop` 的取消 `ToolFailure`，runtime 先记录 `runtime.tool_completed`，再以状态 `interrupted` 结束 run；终态 `runtime.failed` payload 包含 `kind="interrupted"`、`cancelled=true` 与取消 `reason`。在取消后才就绪的普通成功结果不会把其 output/body/control 提交给 provider；runtime 记录通用 `ToolFailure("run interrupted")` 与实际停止观察。若工具自身在观察取消后返回 `ToolFailure`，runtime 保留其错误/failure body，并补齐实际停止观察；无法确认停止时使用 `UnconfirmedStop` 且副作用状态为 `unknown`。显式取消不能降级为普通工具失败。
- **无法确认停止**：若回收窗口结束时工具执行仍未停止，运行时以 `UnconfirmedStop` 保留未知的执行 / 副作用状态；无论其迟到结果之后如何完成，均不得提交为该调用的结果。未知副作用不能伪装成 `settled`，也不表示已发生的副作用会回滚。
- **runtime 何时可以认为执行已经结束**：只有在有界回收窗口内确认执行线程已经退出（`execution_stopped=true`）时。超时路径先设置取消信号，再停止等待，然后回收；`runtime.tool_timeout`、该次调用的 `runtime.tool_completed`（`diagnostics.kind="tool_timeout"`）与终结 run 的 `runtime.failed` 都带同一组事实：`cancellation_signalled`、`execution_stopped`、`side_effect_state`（`settled` 当且仅当 `execution_stopped`）。
- **runtime 对副作用的承诺**：`settled` 只对这一条执行成立（runtime 已确认该执行线程退出，不再有来自它的新副作用；工具自行派生、已脱离该执行的写入者不在承诺范围内）；无法确认停止时必须报告 `side_effect_state="unknown"`，并在 `error` 文本中说明该执行可能仍在运行、副作用状态未知；不允许退化成普通的失败。`settled` 从不表示已发生的副作用被回滚或未发生，runtime 不承诺线程取消能回滚任何已完成的副作用。
- **结果所有权**：runtime 记录结果之后到达的完成结果不会被提交为工具结果，只会记入 runtime 日志用于诊断。
- 工具自行超时（`RuntimeTimeoutAwareTool.invoke_with_runtime_timeout`）时由工具保证停止，此时 `cancellation_signalled=false`，runtime 仍会在有界窗口内回收其执行线程并报告 `execution_stopped`。
- 不轮询取消信号的工具无法被中断，只能等它自己结束；runtime 会如实报告 `side_effect_state="unknown"`。

## Permission 与 approval 规则

`ToolDefinition.effects` 描述行为；审批 authority 仍只属于 runtime。共享
`is_read_tier(effects)` 保留静态 tool-scope 分类；它不是执行许可。例如
local manifest 的 `read_only=true` 翻译成 `read/execute/spawn`，可以通过静态
read-tier 筛选，但每次 argv 调用仍是 `execute`，在显式 `read_only` 策略下被拒绝。

| 实际 operation class | `ask` | `write` | `yolo`（默认） |
| --- | --- | --- | --- |
| `read` | 自动允许 | 自动允许 | 自动允许 |
| `write` | 等待审批 | 自动允许 | 自动允许 |
| `execute` | 等待审批 | 等待审批 | 自动允许 |

此表不覆盖更高优先级的 agent/tool allowlist、显式 `read_only` ceiling、
外部目录规则或显式 permission rules。`allow/deny/ask` 是单次 decision，
不是 approval-mode 词表；拒绝以 `ToolFailure(status="error")` 与
`permission_denied` 诊断返回给模型。详见 [审批契约](approval-flow.md)。

### Approval request shape

当非只读工具在 `ask` 策略下被拦截时，runtime 发出 `runtime.approval_requested`：

```json
{
  "event_type": "runtime.approval_requested",
  "source": "runtime",
  "payload": {
    "request_id": "approval-...",
    "tool": "write",
    "decision": "ask",
    "arguments": {"path": "README.md", "content": "..."},
    "target_summary": "write README.md",
    "reason": "non-read-only tool invocation",
    "policy": {"mode": "ask"}
  }
}
```

Agent 不处理 approval UI；CLI / Web 客户端把 `allow` 或 `deny` 决策交回 runtime。
`allow` 会恢复并执行原工具调用；`deny` 不执行原工具调用，而是通过
`runtime.tool_completed` 发出工具级错误反馈（例如 `permission denied for tool: write`），
使 provider-backed agent 可以观察该约束并重新规划。拒绝本身不应自动产生
`runtime.failed`。

当请求涉及 workspace 外路径时，approval payload 会额外包含：

- `path_scope`: `external`
- `operation_class`: `read` / `write` / `execute`
- `canonical_path`: canonical absolute path
- `matched_rule`: 命中的 external permission rule
- `policy_surface`: `external_directory_read` 或 `external_directory_write`

对应的 `runtime.approval_resolved` payload 在 external 场景下也会携带上述上下文字段，便于审计与客户端提示。

## 通用安全与路径规则

- 文件路径参数默认相对当前 workspace root 解析；当路径解析到 workspace 外时，会进入 runtime-owned external directory permission surface，而不是静默绕过治理。
- 文本文件工具默认使用 UTF-8；不支持二进制文件编辑。
- 搜索 / glob 会忽略常见生成目录，如 `.git`、`node_modules`、`__pycache__`、`dist`、`build`、`.venv` 等。
- `web_fetch` accepts HTTP(S) URLs and follows at most five redirects; it does not preflight DNS or block localhost, private, or metadata addresses, so those destinations may be fetched. The original requested URL is retained as provenance.
- `shell_exec` runs the supplied command string through a non-interactive shell; default timeout is 120 seconds and maximum is 600 seconds. Runtime permission, approval, output-capture, and cancellation bounds still apply.
- runtime hooks 可能在工具执行前后发出额外事件；agent 不应假设 tool call 与 tool result 之间只有一个事件，也不应把 `runtime.tool_lookup_succeeded` 误认为真实执行已经开始。

## 当前可用工具目录

默认内置 registry 当前包含下列工具。`lsp` 和 `mcp/<server>/<tool>` 属于 runtime-managed / dynamic 能力，只有在对应 subsystem 或配置启用时才会出现在 registry 中。文件格式化不通过独立工具暴露：`edit` 和 `write` 写后自动执行 format-on-write，由 formatter 配置（`formatter.enabled` / `hooks.enabled` / formatter presets）控制。

### Deterministic runtime tool catalog

Provider prompts may include a deterministic `runtime_tool_catalog` section. The
catalog is projected from the **current scoped live tool registry** used for the
request; it is not a second tool-discovery or authorization mechanism. Its rows
are stable facts about the tools in that current scope and contain:

- `name`: the runtime tool name;
- `visibility`: `essential` or `discoverable`, using the existing
  `ESSENTIAL_TOOL_NAMES` split;
- `read_only`: observational static read-tier metadata derived from the definition's effects; not an authorization grant;
- `replay_policy`: the effective replay policy (`safe` or `never`);
- `documentation_uri`: `voidcode://tool/<name>` for on-demand tool
  documentation.

The catalog is informational only: it does **not** grant permission, widen an
agent allowlist, or bypass runtime lookup, policy, approval, or replay
enforcement. The provider prompt places this independent catalog section after
`runtime_tool_policy_summary` and before `runtime_dynamic_boundary`.

When essential-only presentation is enabled, the catalog follows the current
scoped registry and shows essential tools plus tools selected by the existing
allowlist behavior. When essential-only presentation is disabled, it shows the
complete current scoped registry. In both cases, `visibility: discoverable`
describes a non-essential tool; it is not an authorization decision.

### Workspace 读取与搜索工具

#### `read`

- 分组：workspace read
- 只读：是
- 用途：读取 workspace 内文件；UTF-8 文本以内联内容返回，图片和显式 `view="raw"` 的 PDF 以 typed attachment 表达（可能通过 artifact reference 而非内联 data URI 传递）。SQLite、Notebook 与默认 PDF 读取提供有界、只读派生视图。
- 参数：

```json
{"path": "relative/path.txt", "offset": 1, "limit": 2000}
```

`path` 为必填；`offset` 与 `limit` 可省略，默认分别为 1 和 2000。其他路径字段名无效。

格式派生视图与参数单位：

- SQLite 文件（`.db`、`.sqlite`、`.sqlite3` 或识别出的 SQLite 文件）默认展示表结构与样例行；`view="sqlite"` 可指定 `table` 或一条只读 `SELECT`/`WITH` `query`。`offset`/`limit` 是 1-based 行分页，最多 200 行、50 KiB、有 0.5 秒查询时间限额，并响应 runtime cancellation；写入、PRAGMA、附加数据库和扩展加载均被拒绝。
- `.ipynb` 默认返回派生的 cell/output 视图；`offset`/`limit` 是 1-based cell 分页（最多 200 cells），`cell` 是 0-based 单 cell 选择器，`output_offset`/`output_limit` 是 1-based 输出分页且要求 `cell`（最多 20 个 outputs）。单个视图的文本预算为 50 KiB，图片总量为 5 MiB；PNG/JPEG/WebP 数据解码为真实附件工件。来源文本被裁剪或存在不支持的输出 MIME 时，原始 Notebook JSON 通过 model-visible artifact URI 提供。
- `.pdf` 默认使用 Linux Poppler `pdftotext` 提取文本；`page` 是 1-based 页码，`offset`/`limit` 是 1-based 行分页。原生提取限时 5 秒、文本源最大 10 MiB，并响应 runtime cancellation；单页预览最多 32 KiB。无文本层的 PDF 明确报告不执行 OCR。原始 PDF 仍可用 `view="raw"` 读取，提取文本超出常规输出预算时可经 artifact URI 读取完整文本。

这些派生视图返回 `OpaqueToolBody`，不授予对原始文件的 whole-file 写权限；只有 `view="raw"` 返回普通文件语义。`source_artifact` 与 `artifacts` 是 canonical body 的顶层元数据，完整来源/输出可通过同样出现在 model-visible output 中的 `voidcode://artifact/<id>` 引用读取。PDF artifact URI 也接受 `view="pdf"` 与相同的页/行选择器。

- canonical successful file `ReportedCall.result.body`:

```json
{
  "type": "file",
  "line_count": 10,
  "offset": 1,
  "limit": 2000,
  "next_offset": null,
  "truncated": false,
  "partial": false,
  "byte_count": 123,
  "content_hash": "sha256 hex digest",
  "lines": [{"line": 1, "text": "first line", "truncated": false}],
  "raw_content": "first line\n..."
}
```

`ReadResultBody` 是内部 frozen file-page DTO；它不保存路径。Canonical path 位于 `ReportedCall.authorized_arguments["path"]`，read tracking 与规则发现只能使用这份最终已授权身份。`ReadLine.truncated` 是 producer 记录的真实裁剪事实，不能从后缀或文本长度推导。Provider-facing `TextOutput` 提供有界正文、SHA-256 与适用的 `Next offset`，`OutputBounds` 保留实际 clipped/partial/reference 信息。

- 栅格图片与显式 raw PDF 使用 typed `AttachmentOutput` 表达 MIME 和 artifact reference；只有存在实际 data URI 时才有内联数据 URI。通用二进制文件返回 MIME、大小、hash 与 artifact reference 的文本元数据；派生视图返回有界 JSON `TextOutput` 与 `OpaqueToolBody`，绝不虚假构造 `ReadResultBody.whole_file` 或赋予写授权。
工具结果经共享边界限制模型可见文本与 attachment presentation 至多 50 KiB / 1000 行（含截断提示）；已有 artifact reference 不会绕过该限制，原始授权 artifact 仍可通过 runtime 读取。
对于有效的非内联媒体，runtime 保留既有有界 presentation 与完整 JSON artifact reference；无法 hydration 时，fallback 仅包含 MIME、`byte_count`、SHA-256 和 canonical raw artifact URI，不重构或合成 URI。

读取溢出工件 `read(path="voidcode://artifact/<id>")` 支持有界文本切片、图片/PDF 附件与通用二进制元数据导出。图片/PDF 的 typed attachment 可能只带 session-scoped artifact reference，不保证存在内联 data URI。工件存储于 session-owned durable 目录（`$XDG_STATE_HOME/voidcode/artifacts/<session_id>/`，模式 0o700/0o600 fail-closed）；session bundle 是 schema-versioned exact 全量快照，包含原始 provider/reasoning/tool 内容与可读工件字节，可能含提示词、凭据或其他敏感数据。CLI/API 文件导出默认精确快照，输出文件模式为 0o600；仅保存在私有本地位置，不要上传或外发。取消了无法导入的 support/redact/省略字段模式。目录、archive、Rule、Artifact、Transcript 与 tool-document bodies 保持各自 scoped DTO/Opaque contracts，不折叠为 file body。目录探索应使用 `glob`。

- 选择原则：已知文件路径或工件 URI 且需要内容时使用；不要用它做目录发现或全仓搜索。

#### `glob`

- 分组：workspace discovery
- 只读：是
- 用途：按 glob pattern 查找文件。
- 参数：

```json
{"pattern": "**/*.py", "path": "src"}
```

`path` 可省略，默认 workspace root。

- 成功返回：content 列出实际返回的路径，每行一个；`data.matches` 是相同的完整结构化路径列表。

```json
{
  "content": "src/example.py",
  "data": {"pattern": "**/*.py", "path": "src", "count": 1, "truncated": false, "matches": ["src/example.py"]}
}
```


- 选择原则：用来定位文件集合；不要用它搜索文件内容。

#### `grep`

- 分组：workspace search
- 只读：是
- 用途：在 workspace 内的文件或目录目标上做 literal 或 regex 文本搜索。
- 参数：

```json
{
  "path": "src",
  "pattern": "ToolCall",
  "regex": false,
  "context": 0,
  "include": ["**/*.py"],
  "exclude": ["**/generated/**"]
}
```

`regex`、`context`、`include` 与 `exclude` 都可省略。`path` 可指向单个文件，也可指向目录。

- 成功返回：content 包含所有被选中结果的 1-based 文件行、匹配列与请求的上下文行；`match_count` 统计匹配出现次数，`limit` 限制返回的匹配行数。恰好达到 limit 而无后续匹配时 `truncated=false`。
```json
{
  "content": "src/example.py:10:1: ToolCall",
  "data": {
    "path": "src", "pattern": "ToolCall", "regex": false, "context": 0,
    "match_count": 1, "truncated": false, "partial": false,
    "matches": [{"file": "src/example.py", "line": 10, "text": "ToolCall", "columns": [1], "before": [], "after": []}]
  }
}
```


- 选择原则：已知目标路径并需要文本搜索时使用；literal 搜索默认开启，regex 搜索显式设置 `regex=true`。结构化代码搜索仍优先 `ast_grep`。

### Workspace 写入与编辑工具

#### `write`

- 分组：workspace write
- 只读：否，默认触发 approval
- 用途：写入完整 UTF-8 文本文件；可创建父目录。
- 参数：

```json
{"path": "docs/new.md", "content": "# New\n"}
```

- 成功返回：

```json
{
  "content": "# New\n",
  "data": {
    "path": "docs/new.md",
    "byte_count": 6,
    "diff": "--- a/docs/new.md\n+++ b/docs/new.md\n@@ -0,0 +1 @@\n+# New\n"
  }
}
```

- `diff` 是面向客户端工具活动视图的 unified diff 预览；当 formatter 修改写入内容时，它反映 formatter 后落盘内容与写入前内容的差异。

- 选择原则：创建新文件或完整重写小文件时使用；已有文件替换、同文件有序编辑或多文件 patch 使用统一的 `edit` 工具。

#### `edit`

- 分组：workspace edit
- 只读：否，默认触发 approval
- 用途：通过单点文本替换、有序 `edits` 批次或 patch 文本修改文件。支持 unified diff 与 marker patch；需要同一文件每步按当前文本匹配时用 `edits`，跨文件 diff、rename 与 delete 用 patch。
- 参数：

```json
{
  "path": "src/example.py",
  "oldString": "old text",
  "newString": "new text",
  "replaceAll": false
}
```

`replaceAll` 可省略，默认 `false`。批量模式传 `{ "path", "expectedHash", "edits": [...] }`；各 edit 按数组顺序基于前一步文本匹配。所有 edit 会先在内存中预检，通过后才写一次文件；预检失败不会写入任何改动，诊断会提供失败索引与可重试的完整 batch 提示。Patch 模式传 `{ "patch": "...", "expectedHashes": { "existing/path": "<sha256>" } }`；已有文件的修改与删除必须提供读取所得哈希。

- 成功返回：

```json
{
  "content": "Edit applied successfully.",
  "data": {
    "path": "src/example.py",
    "match_count": 1,
    "additions": 1,
    "deletions": 1,
    "diff": "..."
  }
}
```

如果 formatter hooks 配置存在，返回 `data` 可能包含 `formatter` 或 `diagnostics`。

- 选择原则：新建或完整重写文件用 `write`；已有文件片段、同文件有序替换或 patch diff 均用 `edit`。


#### `apply_workspace_edit`

- 分组：workspace edit（已批准的 LSP edit 批量应用入口）
- 只读：否，默认触发 approval
- 用途：把一组带哈希与可选 LSP 版本校验的 text edits 先完整预检，再写入 workspace。
- `edits` 中每项必须带 `path`、1-based `startLine`/`startCharacter`/`endLine`/`endCharacter`、`newText` 与从 `read` 取得的 `expectedHash`。可选 `expectedVersion`（或 `version`）必须匹配 runtime 实际打开的 LSP 文档版本；指定版本但没有打开文档状态时 fail closed。runtime 有打开文档时始终核对其原始 UTF-8 SHA-256。
- `positionEncoding` 可省略；优先使用该打开文档协商出的 `utf-8`、`utf-16` 或 `utf-32`，无文档状态时默认为 LSP 默认 `utf-16`。UTF-8/16/32 列位置按 Unicode 编码单位转换，CRLF 原样保留；请求编码不得与已打开文档的协商编码不一致。
- 先预检所有范围和重叠，再写入；成功 `data.files` 返回 formatter 后的 `byte_count`、`content_hash`、unified `diff` 和可用的 formatter 结果。

- 选择原则：用于承载 LSP 侧（rename / codeAction 等）生成的编辑结果；手写文本替换或 patch 使用 `edit`。
- 成功返回的具体字段见 `src/voidcode/tools/apply_workspace_edit.py`。


### 命令、格式化与任务状态工具

#### `shell_exec`

- 分组：command execution
- 只读：否，默认触发 approval
- 用途：在 workspace 内执行本地命令。
- 参数：

```json
{"command": "pytest tests/unit", "timeout": 600}
```

`timeout` 可省略，默认 120 秒，最大 600 秒。

- 成功返回：

```json
{
  "content": "stdout + stderr",
  "data": {
    "command": "pytest tests/unit",
    "exit_code": 0,
    "stdout": "...",
    "stderr": "...",
    "timeout": 600,
    "truncated": false
  }
}
```

stdout/stderr 返回独立的有界预览与 raw-byte artifacts；共享 output cap 触发时，可用的 raw artifact URI 与 stderr preview 位于 stdout preview 前，避免公共 prefix cap 将它们裁掉。每个流最多捕获 128 MiB；超限返回显式失败和部分 artifacts。成功退出时 stderr 仍显示。超时/取消返回部分输出，并通过 `stop_confirmed` 报告进程组停止状态。

- 选择原则：用于测试、构建、诊断或无法通过内置读写工具完成的本地操作；能用更窄工具时不要首选 shell。


#### `todo`

- 分组：agent work state
- effects：`session`；返回当前 session 的 todo work-state proposal，runtime 负责持久化，工具不执行 workspace 写入。
- 用途：对当前 session 的 runtime-owned todo phases 执行一个操作；工具不写 `.voidcode/todos.json` 或其他 workspace 文件。
- 参数是单操作对象（没有 `todos` 数组包装）：

```json
{
  "op": "append",
  "phase": "Implementation",
  "items": ["Add tests", "Run verification"]
}
```

支持的操作为 `init`、`start`、`done`、`drop`、`block`、`unblock`、`append`、`rm`、`view`。`init` 接受 `list: [{"phase": string, "items": string[]}]`，也接受扁平 `items` 与可选 `phase`；`append` 接受 `phase` 与 `items`。`start`/`done`/`drop`/`block`/`unblock`/`rm` 按精确 task 内容或 phase 名称定位；`block` 可带 `reason`。`view` 只读，不触发状态归一化。

任务状态为 `pending`、`in_progress`、`completed`、`abandoned`、`blocked`；runtime 会自动归一化为最多一个 `in_progress` 任务，并跳过 blocked 任务的自动激活。blocked 任务须先 `unblock` 才能 `start`；completed/abandoned 不可重启，同一终态操作幂等，二者之间不可切换。错误会丢弃整个操作，不产生部分状态。

- 工具成功返回（runtime 随后负责持久化）：

```json
{
  "content": "Todo append applied.",
  "data": {
    "op": "append",
    "mutated": true,
    "phases": [{"name": "Implementation", "tasks": [{"content": "Add tests", "status": "in_progress"}]}],
    "summary": {"total": 1, "pending": 0, "in_progress": 1, "completed": 0, "abandoned": 0, "blocked": 0, "active": 1}
  }
}
```

`data.phases` 始终携带完整状态；`init`/`view` 的文本输出显示完整表格，其它操作只显示有变化的任务、下一活跃任务和 summary。`data.mutated` 仅在快照实际变化时为 true。

runtime 在成功的 mutation 后更新 session metadata 中的 runtime todo state（`SessionState.metadata["runtime_state"]["todos"]`），并发出 `runtime.todo_updated` 事件；SQLite session store 只由 runtime 持久化该 metadata 与事件。`todo` 工具不直接写存储。

- 选择原则：用于 runtime-visible work state；不要把它当成项目文档或长期 memory。

### 委托、提问与工作流状态工具

以下工具属于默认内置 registry 中的 runtime-owned 协作 / 委托 / 状态工具，参数与返回 shape 见各自契约文档与 `src/voidcode/tools/` 实现：

#### `skill`

- 分组：agent work state
- 只读：是
- 用途：按名称加载可用 skill 到当前上下文；正文与缩进、空行原样保留。
- 参数：`name`（必填）、`user_message`（可选）。返回 metadata 含 `sourcePath`（SKILL.md 路径）和 `baseDirectory`；可用 `read(path=sourcePath)` 检查该目录中的现有文件。

#### `task`

- 分组：delegated execution
- 只读：是
- 用途：委托工作给子 runtime session；需提供 `prompt`、`run_in_background`、`load_skills`，并给出 `subagent_type`。
- 详见 `docs/contracts/background-task-delegation.md`。

#### `question`

- 用途：向用户提出澄清问题并等待回答。
- 参数：`questions`（`{question, header, options, multiple, allow_freeform}` 数组）；`allow_freeform` 默认为 `false`，仅在明确设为 `true` 时允许空 `options`。
- runtime 在进入等待前校验 prompt/header/option；同一请求的 header 和 option label 必须唯一。回答必须覆盖每个 header 恰好一次；单选恰好一个答案，多选至少一个且不重复。除 `allow_freeform=true` 外，答案必须精确匹配声明的 option label。
- `voidcode sessions answer <session_id> --question-request-id <id> --cancel` 取消整个待回答问题，且不得与 `--response` / `--response-json` 同用。HTTP `POST /api/sessions/{session_id}/question` 使用 `{"request_id":"<id>","cancel":true}`，并与 `responses` 互斥。
- 取消通过既有 `QuestionCancellation` 控制联合类型提交；正常回答仍是 `{header, answers}`，不含 `cancelled` 字段。请求 ID 必须匹配该 session 当前 pending question，已解决的 request 不可重放。

#### `yield`

- 分组：delegated execution；只读：是。
- 仅 delegated child 可调用。参数允许 `summary`、`data`、`type`、`result`、`error`，并拒绝未知字段；`data` 必须是有限、可 JSON 编码的对象。
- 终态成功：`summary` 必须是非空字符串；`type` 省略或为 `"result"`；`data` 是可选结构化对象。终态失败：`error` 必须是非空字符串；可用 `type: "error"`，不得同时提供 `summary` 或 `result`。
- 非终态进度：`type` 为除 `result`/`error` 外的非空字符串或非空字符串数组；必须提供 `result` 或非空 `data`，不得提供 `summary`。每次调用提交一个 bounded progress section，不完成 child。
- runtime 在 strict `outputSchema` 下于成功 handoff/child completion 前校验完整 data；不符合 schema 的 yield 会使 child/task 失败。permissive 模式保留结果并记录验证 verdict。
- 完整结构化 JSON 保留在 typed yield control；大数据使用 canonical `voidcode://artifact/<id>` artifact，输出只显示有界 preview 和可见 URI，生成的 metadata 放在已知 `body.artifacts` / output reference 槽中。读取必须经过 runtime 的 session/task owner 授权；任意 URI 字符串不授予访问权。
- runtime 为 progress section 分配单调 `ordinal`，每段最多 4096 字符、累计最多 100 段/65536 字符，`type` 最多 100 项且每项最多 64 字符；超限按确定性错误/截断规则处理。Progress 的完整 control 和生成 artifact 保留，parent 只收到有界 projection 与可读 URI。
- progress 先作为 child 的 `runtime.tool_completed` truth，再以 `runtime.background_task_progress` 的有界、去重 projection 投递 parent；它不是 peer bus，也不替代 terminal result 或 transcript。
- parent 通过 runtime events/outbox、`task(operation="output")`（单任务可读 bounded progress；聚合仍不返回 transcript）消费；完整 child history 仍走显式 transcript/session recovery。
- 详见 `docs/contracts/background-task-delegation.md`。

#### `task`

- 分组：delegated execution
- 权限分类按 `operation` 区分：`output` / `ps` 为 `read`（只读）；`cancel` 为 `execute`；`steer` 为 `write`。后两者会修改 runtime-owned task state，必须经过对应的 execute/write governance；显式 `read_only` 策略拒绝 `cancel` 与 `steer`，不能把整个 facade 视为只读。
- 用途：通过统一 facade 管理 runtime-owned 后台任务；`operation` 必须是 `output`、`ps`、`cancel` 或 `steer`。
- `operation="output"` 要求 selector 严格三选一：`task_id`（单任务）、`task_ids`（显式聚合，1–100 个且不得重复）或 `parallel_group_id`（runtime-owned 聚合组）。聚合读取使用当前 runtime tool context 的 parent session owner。
- `operation="output"` 的 `block=false` 立即返回当前快照；`block=true` 通过 runtime lifecycle wait 等待一次。`timeout` 仅用于阻塞等待，单位为毫秒且至少 1000ms；超时返回当前状态并标记 `block_timed_out`，不得把任务误标为失败。
- `full_session=true` 仅对单任务 selector 生效，返回 bounded child-session metadata/transcript preview；聚合 selector 始终是 no-transcript projection。`message_limit` 被限制在 1–100。
- `operation="ps"` 无额外参数，读取当前 active parent session 的 bounded background-task roster，不返回 prompt、transcript 或 child result content。
- `operation="cancel"` 接受一个 `task_id`；unknown、running、completed/cancelled 等状态返回确定性的单任务 status payload。
- `operation="steer"` 接受 `task_id`、`prompt`，向 parent-owned keep-alive delegated worker 派发下一轮指令。
- 详见 `docs/contracts/background-task-delegation.md`。

#### `background_process`

- 分组：background process control
- 只读：否（`op` 取决于具体操作）
- 用途：通过统一 facade 控制 runtime-owned long-lived processes；`op` 支持 `start`、`ps`、`logs`、`send`、`stop`。
- 详见 `src/voidcode/tools/process/background_process.py` 与 `docs/contracts/background-task-delegation.md`。

### 结构化代码搜索与代码智能工具

#### `ast_grep`

- 分组：structural code search & rewrite
- effects：`write`（保守的静态 scope，plan 会隐藏/拒绝该工具）；实际调用中 search/preview 按 `read`、replace 按 `write` 治理，不能把 replace 当作只读自动放行。
- 用途：用 ast-grep 做结构化代码匹配与改写；`search` / `preview` / `replace` 三种模式统一由一个工具表达。
- 参数：

```json
{
  "mode": "search",
  "pattern": "class $NAME",
  "path": "src",
  "lang": "python"
}
```

`mode`、`pattern`、`path` 必填；`mode` 取值 `search` / `preview` / `replace`；`lang`、`rewrite`、`apply` 可省略。

- `search`：只做结构化匹配，不修改文件。
- `preview`：预览 rewrite 影响范围，需要 `rewrite`，不修改文件。
- `replace`：实际应用 rewrite；必须同时提供 `rewrite` 且 `apply=true`，否则工具会拒绝执行。

- `search` 的 content 列出结构化匹配；`preview` 列出源码匹配及 rewrite 预览；`replace` 列出已应用的 rewrite。
- 三种模式的 `data.matches` 最多包含 50 条文件/位置摘要；`data.match_count` 是完整总数，`data.truncated` 表示摘要是否省略条目。
- 完整 native JSONL（含原始 range、源码和 replacement）保存在 session-owned `data.body_artifact` / `data.artifacts`。模型可见的 `read(path="...")` URI 指向该真实 artifact；使用 `read` 读取完整记录。
- 目录 replace 在任何写入前，逐一解析并预检 native preview 命中的文件与行；任何文件未读或行未见都会整体拒绝且不改动文件。native `-U` 只接收这组已预检文件，不会再次递归原目录。
- `expectedHash` 只适用于单文件目标。单文件成功结果保留 `data.final_hash` 与 unified `data.diff`；目录成功结果在 `data.files` 中逐文件返回 source/final SHA-256、byte count、diff 和 formatter 状态。

- 选择原则：代码结构匹配优先于 text grep；执行结构化 rewrite 前先用 `mode=preview` 验证影响范围；普通文字或单文件 literal 搜索用 `grep`。

#### `lsp`

- 分组：code intelligence
- 只读：是
- 可用性：仅当 runtime-managed LSP subsystem 启用并注入该工具时暴露。
- 用途：执行基础 LSP 查询，并额外支持文档级 diagnostics。
- 使用模式：分为两类。位置型操作要求精确的 `path` + `line` + `character`；符号/搜索型操作只需要 `path`，其中 `workspaceSymbol` 额外接受 `query`。
- 参数：

```json
{
  "operation": "goToDefinition",
  "path": "src/example.py",
  "line": 10,
  "character": 5,
  "server": "pyright",
  "offset": 1,
  "limit": 50
}
```

`server` 可省略。`line` 与 `character` 是 1-based；`diagnostics` 与 `documentSymbol` 不需要位置参数，`workspaceSymbol` 额外接受 `query`。位置字符单位使用 server 在 initialize 时协商的 position encoding。列表结果支持 1-based `offset`（默认 1）和 `limit`（默认 50，最大 50）；`item_count` 是完整总数，`truncated` / `next_offset` 标记后续页。

支持的 `operation` 包括：

- `diagnostics`
- `goToDefinition`
- `findReferences`
- `rename`
- `codeAction`
- `willRenameFiles`
- `hover`
- `documentSymbol`
- `workspaceSymbol`
- `goToImplementation`
- `incomingCalls`
- `outgoingCalls`

- runtime 在 initialize 中声明支持 `utf-16`、`utf-8`、`utf-32`；成功结果的 `data.position_encoding` 是 server 选定值。支持文档同步的 server 会收到完整文本 `didOpen` / `didChange`；`data.document_state` 暴露打开文档的版本和原始内容 SHA-256。
- `rename` 返回 preview only，不写文件；每页最多显示 50 个 workspace edit 的路径、1-based range、替换文本和 `documentChanges` 版本。所有完整 JSON-RPC 响应都写入 session-owned `application/json` artifact，`body_artifact` / `artifacts` 保存 canonical 元数据，model-visible `content` 包含 `voidcode://artifact/<id>` 读取 URI。用 `read(path=uri, offset=..., limit=...)` 读取完整响应，并取其 `result` WorkspaceEdit 传给 `apply_workspace_edit`；原始完整 JSON 不再复制到 `data.lsp_response`。
- 选择原则：
  - 已知符号名但不知道位置时，先用 `workspaceSymbol`。
  - 需要文件内结构或先找函数/类时，用 `documentSymbol`。
  - 已知精确位置时，再用 definition / references / hover / implementation / call hierarchy。
  - `incomingCalls` / `outgoingCalls` 内部会先做 `prepareCallHierarchy`，agent 不需要自己拆成两步。
  - 纯文本搜索、关键字匹配、批量模式查找不要用 LSP。

- 成功返回：

完整原始 JSON-RPC 响应不以内联 body 字段返回；canonical `ReportedCall.result.body`
包含可解析的 `body_artifact` 与 `artifacts` 元数据。模型只看到 typed output，
其中包含可直接传给 `read` 的 artifact URI：

```text
Full LSP JSON response: read(path="voidcode://artifact/<id>").
```

`read` 的 `offset` / `limit` 对 artifact 内容按行分页；持续读取 `next_offset`
直至为 `null`，即可获得完整 JSON。对于超长单行值（例如 hover 内容），改用
`byte_offset` / `byte_limit` 并继续读取 `next_byte_offset`。`document_state` 为
`null` 时表示 server 未提供可跟踪的文档同步状态。

### 外部资料与网络工具

#### `web_search`

- 分组：external research
- 只读：是
- 用途：搜索网页并返回标题、URL、snippet 与结构化结果；优先使用 Exa `/search`（存在 `EXA_API_KEY` 时，按官方 `query` / `type=auto` / `contents.highlights` 合约请求），否则使用 DuckDuckGo HTML fallback。
- 参数：

```json
{"query": "VoidCode runtime tool contract", "numResults": 8}
```

`numResults` 可省略，默认 8，最大 20。

- 成功返回：

```json
{
  "content": "Search source: exa\n\n1. Result title\n   https://example.com\n   snippet",
  "data": {
    "query": "...",
    "num_results": 8,
    "timeout_seconds": 30,
    "source": "exa",
    "fallback_reason": null,
    "results": [{"rank": 1, "title": "Result title", "url": "https://example.com", "snippet": "snippet"}]
  }
}
```

- Exa 响应中的完整 `highlights` 片段会合并为 snippet；不在单条结果层截断，超长输出由通用输出限制及引用处理。

- DuckDuckGo 没有可解析结果时仍是成功结果，`source` 为 `duckduckgo-empty` 且 `results` 为空；搜索请求失败时返回错误，`source` 为 `duckduckgo-error`。若存在，`fallback_reason` 说明 Exa 请求或 DuckDuckGo fallback 的失败/空结果原因。

- 选择原则：不知道具体 URL、需要发现外部资料时使用；已有 URL 时用 `web_fetch`。

#### `web_fetch`

- 分组：external research
- 只读：是
- 用途：抓取 URL 内容并返回 `text`、`markdown` 或原始 HTML 文本；保留原始响应字节以供后续读取。
- 参数：

```json
{"url": "https://example.com/docs", "format": "markdown", "timeout": 30}
```

`format` 可省略，默认 `markdown`；`timeout` 可省略，最大 120 秒。

- 成功返回：

```json
{
  "content": "Source URL: https://example.com/docs | Final URL: https://example.com/docs | MIME: text/html | charset: utf-8 | raw: voidcode://artifact/...\n\n# fetched content",
  "data": {
    "original_url": "https://example.com/docs",
    "final_url": "https://example.com/docs",
    "content_type": "text/html; charset=utf-8",
    "mime": "text/html",
    "format": "markdown",
    "byte_count": 12345,
    "timeout_seconds": 30,
    "used_encoding": "utf-8",
    "provenance": "web_fetch",
    "source_artifact": {"uri": "voidcode://artifact/...", "path": "...", "source": "https://example.com/docs"}
  }
}
```

- 响应流在超过 5 MiB 时中止；最多跟随 5 次 redirect，不做 DNS、private/localhost 或 metadata 地址预检。loopback、private-network、metadata 等地址可能被请求；原始 requested URL 保留为 provenance。响应原始字节存入 `source_artifact`，与渲染后的 `content` 分开。

- 文本结果正文会包含来源 URL、最终 URL、MIME、实际字符集与原始响应引用，供模型直接读取。

- 已支持的栅格图片响应使用 typed `AttachmentOutput` 携带 MIME 与 data URI；SVG 为文本，PDF 与其它 opaque 响应保留为原始字节引用，不伪装为文本。

- 选择原则：有具体 URL 且需要正文时使用。工具不禁止 localhost、内部网络、private 或 metadata 地址；这些地址可能被请求。

`format=markdown` 对 `text/html` 响应使用 `markdownify` 转换：保留标题层级（ATX）、链接目标、代码围栏语言（`language-*` / `lang-*` 类）、GFM 表格、嵌套/有序列表和图片 alt；只移除 `script` / `style` / `noscript` / `iframe` / `object` / `embed`，不做正文提取（不剥离导航或样板内容）。`format=text` 仍是纯文本抽取。

### 动态 MCP 工具

#### `mcp/<server>/<tool>`

- 分组：dynamic external capability
- 只读：否，默认触发 approval
- 可用性：只有当 MCP server 启动并暴露工具时才出现在当前 registry；provider 顶层是否列出还受 essential/discoverable split 与 allowlist 影响。
- 用途：代理调用 MCP server tool。
- 参数：

参数 shape 来自 MCP tool 自身的 live `input_schema`。如果该工具出现在
provider-visible `ToolDefinition` 中，provider 会收到这份 canonical schema
与短 description；如果它是 discoverable，则先通过
`read(path="voidcode://tool/mcp/<server>/<tool>")` 读取当前 schema 和完整
guidance，再用 `invoke_tool` 调用。不要把 MCP 动态事实写死在 provider
prompt 或 sidecar 文档中；server 返回的事实属于 canonical `ReportedCall.result.body` 与 runtime
metadata/events，不能作为授权依据。
```json
{
  "tool_name": "mcp/github/search_issues",
  "arguments": {"query": "repo:owner/name is:issue"}
}
```

- MCP 响应正文在 `ReportedCall.result.body` 中保留原始 JSON 形状：`content`、`structured_content` 和 `is_error`；`result_artifact` 指向完整、规范化的 JSON artifact，输出文本也会显示此 URI。渲染后的可读内容位于 typed `ToolOutput`，不替代原始响应。
- 单个有效栅格图片会产生带 artifact 引用的 `AttachmentOutput`；多个图片保留为独立 artifact 引用。SVG 保留为 UTF-8 文本 artifact，PDF、音频和二进制 `EmbeddedResource` 保留原始 MIME 与字节；文本 `EmbeddedResource` 直接显示。
- 非 `AttachmentOutput` 的 `TextOutput.bounds.reference` 指向完整响应 JSON artifact；只有用于图片 hydration 的 `AttachmentOutput` 才引用栅格图片 artifact，文本输出被公共 cap 限制时仍能从 canonical JSON 来源恢复。
- `ResourceLink` 仅显示 URI、描述等引用信息，不会自动读取或抓取远程资源。
- `is_error=true` 返回 `ToolFailure`，但仍保留完整响应 JSON、已解析内容和 artifact 引用。未知内容块、无效 base64/MIME、内容 MIME 与字节签名不匹配或单个二进制内容超过 50 MiB 时显式失败，并保留可用的原始响应 artifact。

```json
{
  "content": [{"type": "text", "text": "MCP server response"}],
  "structured_content": {"count": 1},
  "is_error": false,
  "result_artifact": {"uri": "artifact://..."}
}
```

- 选择原则：只在内置工具无法表达所需外部能力、且对应 MCP server 明确配置时使用。由于当前 wrapper 将 MCP 工具标记为非只读，agent 应预期 approval。

## 相似工具选择指南

| 目标 | 推荐工具 | 避免 |
| --- | --- | --- |
| 找文件名 / 扩展名 | `glob` | 不要读取整个仓库 |
| 读取已知文件 | `read` | 不要用 `shell_exec cat` |
| 文本搜索（文件或目录范围） | `grep` | 不要用 LSP 或 AST 搜索 |
| 结构化代码搜索 | `ast_grep`（`mode=search`） | 不要用 brittle text grep 匹配语法结构 |
| 代码定义 / 引用 / hover / diagnostics | `lsp` | 不要猜测调用链 |
| 小范围文本替换 | `edit` | 不要完整重写文件 |
| 同一文件多个有序替换 | `edit` 的 `edits` 批次 | 不要分散为独立写入 |
| 跨文件、rename 或大 diff | `edit` 的 `patch` | 不要拆成多个工具调用 |
| 创建或完整重写文件 | `write` | 不要用 patch 表达全量新内容，除非需要 diff 审阅 |
| 执行测试 / 构建 | `shell_exec` | 不要用 shell 做已有窄工具可完成的读取操作 |
| 搜索网页 | `web_search` | 不要在已有 URL 时重复搜索 |
| 抓取已知 URL | `web_fetch` | 不要把它当 DNS / private-network 预检器 |
| 调用配置的外部能力 | `mcp/<server>/<tool>` | 不要假设 MCP 工具只读或永远可用 |

## Agent 调用准则

1. **优先只读上下文收集。** 在没有足够上下文前，使用 `glob` / `read` / `grep` / `ast_grep` / `lsp` 收敛事实。
2. **选择最窄工具。** 能用 `read` 就不要用 `shell_exec cat`；能用 `edit` 就不要完整 `write`。
3. **预期 approval pause。** 按实际 operation class、模式与规则决定审批，不从目录中的派生 `read_only` 推断执行许可。
4. **把外部资料与本地事实分开。** `web_search` / `web_fetch` 给的是外部证据；本仓库状态仍以 workspace 工具和 runtime events 为准。
5. **不要绕过 runtime。** UI、agent preset、core turn engine 都不应直接执行工具或自行处理审批。
6. **Provider 只读取 `ToolResultView.output`。** 可信 host consumer 按需读取 `ReportedCall.result.body`；body 不是 provider authority，也不改变 runtime policy。
7. **错误是可恢复信号。** 参数错误、路径越界、approval denial、tool error 都应让 agent 收缩下一步，而不是重复同一调用。

## 最小示例：读取文件后小范围编辑

1. Agent 先读取目标文件：

```json
{"tool_name": "read", "arguments": {"path": "docs/example.md"}}
```

2. Runtime 在 workspace read 路径按 `read` operation class 和当前规则决定是否允许。

3. Agent 生成最小 edit：

```json
{
  "tool_name": "edit",
  "arguments": {
    "path": "docs/example.md",
    "oldString": "old wording",
    "newString": "new wording"
  }
}
```

4. Runtime 将 `edit` 的实际操作归为 `write`：
   - `approval-mode=ask`：发出 `runtime.approval_requested` 并暂停；
   - `approval-mode=write|yolo`：规则与其他 hard boundary 允许时执行；
- 显式 `read_only` 或显式 deny rule：拒绝执行。

5. 当 approval / permission 与 pre-tool hook 都已通过后，runtime 先发出 `runtime.tool_started`，表示真实工具执行开始。

6. 执行成功后，runtime 发出 `runtime.tool_completed`，payload 中包含 diff、additions、deletions 等结构化结果。

## 维护规则

- 新增或修改工具时，同时更新：
  - `ToolDefinition` / 参数校验代码；
  - 相关单元或集成测试；
  - 本文档的工具目录、参数 shape、返回 shape 与选择指南。
- 如果工具的 effects、实际 operation class 或 replay policy 变化，必须同步检查 `docs/contracts/approval-flow.md` 中的审批预期与冻结恢复语义。
- 如果 agent preset 只改变“希望携带哪些工具”，更新 `src/voidcode/agent/` 文档；如果改变实际执行/审批/恢复语义，必须更新 runtime contracts。
