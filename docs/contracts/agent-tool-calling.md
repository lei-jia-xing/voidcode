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
- 不承诺 post-MVP multi-agent / delegated execution 已经落地；
- 不替代 `src/voidcode/tools/contracts.py` 中的 Python 类型定义。

## 与现有文档和代码的关系

| Surface | 负责什么 | 与本文档的关系 |
| --- | --- | --- |
| `src/voidcode/tools/contracts.py` | `ToolDefinition` / `ToolCall` / `ToolResult` 的实现级类型 | 权威代码契约；本文档解释 agent 如何消费这些 shape |
| `src/voidcode/tools/README.md` | 工具层边界：工具能力层负责实现，不负责运行时策略 | 本文档建立在该能力层之上，但不改变工具层职责 |
| `src/voidcode/runtime/service.py` | tool registry、permission、hook、event、session persistence 的 runtime truth | 本文档描述 agent-visible 调用路径；执行治理仍归 runtime |
| `docs/contracts/approval-flow.md` | 客户端可见 approval 请求与处理语义 | 本文档只说明哪些 tool call 会进入 approval 路径以及 agent 应如何预期 |
| `docs/contracts/runtime-events.md` | 客户端渲染的稳定事件词汇 | 本文档引用 tool lookup / permission / started / completed 等事件，不重新定义完整事件表 |
| `docs/agent-boundary.md` 与 `src/voidcode/agent/README.md` | agent preset / role intent 与 runtime 边界 | preset 可以描述希望携带的工具组合；实际执行、审批和恢复仍由 runtime 管理 |

## 核心调用 envelope

所有内置工具通过 runtime tool registry 暴露。agent 不直接实例化工具，也不绕过 runtime 调用工具。

### Tool definition

Runtime 暴露给 agent 的工具元数据遵循以下 shape：

```json
{
  "name": "read_file",
  "description": "Read a UTF-8 text file inside the current workspace.",
  "input_schema": {"path": {"type": "string"}},
  "read_only": true
}
```

字段含义：

- `name`：传给 `ToolCall.tool_name` 的稳定工具名；
- `description`：面向 agent 的简短用途说明；
- `input_schema`：参数对象的最小 schema；
- `read_only`：runtime permission 默认策略的主要输入。

### Tool call

Agent 发起工具调用时只提交工具名与参数对象：

```json
{
  "tool_name": "read_file",
  "arguments": {
    "path": "README.md"
  }
}
```

运行时负责：

1. 从 registry 解析 `tool_name`；
2. 根据 `ToolDefinition.read_only` 和当前 approval mode 做 permission 决策；
3. 在允许执行时运行 pre-tool hooks；
4. 在真正跨入工具执行边界时发出 `runtime.tool_started`；
5. 调用工具实现；
6. 发出 tool result 事件并运行 post-tool hooks；
7. 将 session、events、approval state 和 checkpoint 持久化。

### Tool result

工具实现返回统一 `ToolResult`：

```json
{
  "tool_name": "read_file",
  "status": "ok",
  "content": "file contents or human-readable summary",
  "data": {
    "path": "README.md",
    "line_count": 12
  },
  "error": null
}
```

字段含义：

- `status`：`ok` 或 `error`；
- `content`：适合 agent 直接阅读的文本内容或摘要，可为 `null`；
- `data`：结构化 metadata，按工具不同而不同；
- `error`：失败信息；`status="error"` 时必须存在，`status="ok"` 时必须为 `null`。

在 runtime event 流中，工具相关的稳定执行边界当前至少包括：

1. `runtime.tool_lookup_succeeded`：runtime 已经解析到真实工具；
2. `runtime.permission_resolved` / `runtime.approval_requested` / `runtime.approval_resolved`：runtime 已完成权限判断或进入/恢复审批路径；
3. `runtime.tool_started`：权限与 pre-tool hook 已通过，真实工具执行现在开始；
4. `runtime.tool_completed`：工具已经返回结果。

其中，成功执行后的结果以 `runtime.tool_completed` 发出，payload 至少包含：

```json
{
  "tool": "read_file",
  "status": "ok",
  "content": "...",
  "error": null
}
```

并会展开合并该工具的 `data` 字段。

## Permission 与 approval 规则

`read_only` 是 agent-facing 合约中判断审批预期的核心字段。

| `read_only` | 默认 runtime 行为 | Agent 预期 |
| --- | --- | --- |
| `true` | 自动 `allow`，发出 `runtime.permission_resolved` | 可作为低风险上下文收集工具使用 |
| `false` | 默认 `ask`，发出 `runtime.approval_requested` 并让 session 进入 `waiting` | 需要预期暂停、恢复或被拒绝 |

CLI / server 可以通过 `--approval-mode allow|deny|ask` 或等价 runtime config 改变非只读工具的策略：

- `allow`：非只读工具仍记录 permission / approval resolution，但可以继续执行；
- `deny`：非只读工具会被拒绝，agent 应选择只读替代方案或停止该分支；
- `ask`：非只读工具暂停等待客户端 / 操作员决策。

只读工具不应触发 approval。非只读工具包括写文件、编辑、patch、shell、格式化、todo 写入、AST rewrite，以及动态 MCP 工具。

### Approval request shape

当非只读工具在 `ask` 策略下被拦截时，runtime 发出 `runtime.approval_requested`：

```json
{
  "event_type": "runtime.approval_requested",
  "source": "runtime",
  "payload": {
    "request_id": "approval-...",
    "tool": "write_file",
    "decision": "ask",
    "arguments": {"path": "README.md", "content": "..."},
    "target_summary": "write_file README.md",
    "reason": "non-read-only tool invocation",
    "policy": {"mode": "ask"}
  }
}
```

Agent 不处理 approval UI；CLI / Web 客户端把 `allow` 或 `deny` 决策交回 runtime，runtime 再恢复或终止 session。

## 通用安全与路径规则

- 文件路径参数默认相对当前 workspace root 解析；路径逃逸 workspace 会失败。
- 文本文件工具默认使用 UTF-8；不支持二进制文件编辑。
- 搜索 / list / glob 会忽略常见生成目录，如 `.git`、`node_modules`、`__pycache__`、`dist`、`build`、`.venv` 等。
- `web_fetch` 只允许 `http://` 与 `https://`，并阻止 localhost、private、loopback、link-local、reserved、multicast、metadata host 等目标。
- `shell_exec` 不通过 shell 执行命令；它会用 shell-like split 解析 command，默认 30 秒超时，最大 120 秒。
- runtime hooks 可能在工具执行前后发出额外事件；agent 不应假设 tool call 与 tool result 之间只有一个事件，也不应把 `runtime.tool_lookup_succeeded` 误认为真实执行已经开始。

## 当前可用工具目录

默认内置 registry 当前包含下列工具。`lsp`、`format_file` 和 `mcp/<server>/<tool>` 属于 runtime-managed / dynamic 能力，只有在对应 subsystem 或配置启用时才会出现在 registry 中。

### Workspace 读取与搜索工具

#### `read_file`

- 分组：workspace read
- 只读：是
- 用途：读取 workspace 内 UTF-8 文本文件。
- 参数：

```json
{"path": "relative/path.txt"}
```

- 成功返回：

```json
{
  "content": "完整文件内容",
  "data": {"path": "relative/path.txt", "line_count": 10}
}
```

- 选择原则：已知文件路径且需要完整内容时使用；不要用它做目录发现或全仓搜索。

#### `list`

- 分组：workspace discovery
- 只读：是
- 用途：列出目录树，适合初始探索。
- 参数：

```json
{"path": "src", "ignore": ["generated/**"]}
```

`path` 可省略，默认 workspace root；`ignore` 可省略。

- 成功返回：

```json
{
  "content": "tree-like listing",
  "data": {"path": "/absolute/workspace/src", "count": 42, "truncated": false}
}
```

- 选择原则：需要了解目录结构时先用；已知文件名模式时优先用 `glob`。

#### `glob`

- 分组：workspace discovery
- 只读：是
- 用途：按 glob pattern 查找文件。
- 参数：

```json
{"pattern": "**/*.py", "path": "src"}
```

`path` 可省略，默认 workspace root。

- 成功返回：

```json
{
  "content": "src/example.py\n...",
  "data": {
    "pattern": "**/*.py",
    "path": "src",
    "count": 12,
    "truncated": false,
    "matches": ["src/example.py"]
  }
}
```

- 选择原则：用来定位文件集合；不要用它搜索文件内容。

#### `grep`

- 分组：workspace search
- 只读：是
- 用途：在单个 UTF-8 文本文件内做 literal string 搜索。
- 参数：

```json
{"path": "src/example.py", "pattern": "ToolCall"}
```

- 成功返回：

```json
{
  "content": "Found 2 match(es) for 'ToolCall' in src/example.py\n10: ...",
  "data": {
    "path": "src/example.py",
    "pattern": "ToolCall",
    "match_count": 2,
    "matches": [{"line": 10, "text": "...", "columns": [5]}]
  }
}
```

- 选择原则：已知目标文件并需要 literal search 时使用；跨文件先 `glob`，结构化代码搜索优先 `ast_grep_search`。

### Workspace 写入与编辑工具

#### `write_file`

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
  "data": {"path": "docs/new.md", "byte_count": 6}
}
```

- 选择原则：创建新文件或完整重写小文件时使用；修改已有文件片段优先 `edit` / `multi_edit` / `apply_patch`。

#### `edit`

- 分组：workspace edit
- 只读：否，默认触发 approval
- 用途：在单个文件中用 `oldString` -> `newString` 替换文本。
- 参数：

```json
{
  "path": "src/example.py",
  "oldString": "old text",
  "newString": "new text",
  "replaceAll": false
}
```

`replaceAll` 可省略，默认 `false`。

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

- 选择原则：小范围、明确文本替换时使用；同一文件多个有序替换优先 `multi_edit`；跨文件或大 diff 优先 `apply_patch`。

#### `multi_edit`

- 分组：workspace edit
- 只读：否，默认触发 approval
- 用途：对同一文件按顺序应用多个 edit。
- 参数：

```json
{
  "path": "src/example.py",
  "edits": [
    {"oldString": "a", "newString": "b", "replaceAll": false},
    {"oldString": "c", "newString": "d", "replaceAll": true}
  ]
}
```

- 成功返回：

```json
{
  "content": "Applied 2 edits to src/example.py",
  "data": {
    "path": "src/example.py",
    "applied": 2,
    "edits": [{"index": 1, "result": {}}],
    "additions": 2,
    "deletions": 2,
    "diff": "..."
  }
}
```

- 选择原则：同一文件内多个依赖顺序的替换使用；不要用它编辑多个文件。

#### `apply_patch`

- 分组：workspace edit
- 只读：否，默认触发 approval
- 用途：应用 unified diff patch。
- 参数：

```json
{"patch": "diff --git a/file.txt b/file.txt\n..."}
```

- 成功返回：

```json
{
  "content": "M file.txt",
  "data": {"changes": [{"path": "file.txt", "status": "M"}], "count": 1}
}
```

- 选择原则：需要表达跨文件 diff、rename、delete 或较大改动时使用；简单单点替换优先 `edit`。

### 命令、格式化与任务状态工具

#### `shell_exec`

- 分组：command execution
- 只读：否，默认触发 approval
- 用途：在 workspace 内执行本地命令。
- 参数：

```json
{"command": "pytest tests/unit", "timeout": 120}
```

`timeout` 可省略，默认 30 秒，最大 120 秒。

- 成功返回：

```json
{
  "content": "stdout + stderr",
  "data": {
    "command": "pytest tests/unit",
    "exit_code": 0,
    "stdout": "...",
    "stderr": "...",
    "timeout": 120,
    "truncated": false
  }
}
```

- 选择原则：用于测试、构建、诊断或无法通过内置读写工具完成的本地操作；能用更窄工具时不要首选 shell。

#### `format_file`

- 分组：formatting
- 只读：否，默认触发 approval
- 可用性：仅当 runtime 配置了 formatter capability 时暴露。
- 用途：对单个文件执行 formatter preset / fallback command。
- 参数：

```json
{"path": "src/example.py"}
```

- 成功返回：

```json
{
  "content": "Successfully formatted example.py (python)",
  "data": {
    "path": "/absolute/workspace/src/example.py",
    "language": "python",
    "cwd": "/absolute/workspace",
    "command": ["ruff", "format", "src/example.py"]
  }
}
```

- 选择原则：只做格式化时使用；内容修改仍应通过 `edit` / `multi_edit` / `apply_patch`。

#### `todo_write`

- 分组：agent work state
- 只读：否，默认触发 approval
- 用途：把 agent 当前 todo list 写入 `.voidcode/todos.json`。
- 参数：

```json
{
  "todos": [
    {"content": "Implement docs", "status": "in_progress", "priority": "high"}
  ]
}
```

`status` 允许 `pending`、`in_progress`、`completed`、`cancelled`；`priority` 允许 `high`、`medium`、`low`。

- 成功返回：

```json
{
  "content": "Updated 1 todos",
  "data": {
    "path": ".voidcode/todos.json",
    "summary": {"total": 1, "pending": 0, "in_progress": 1, "completed": 0, "cancelled": 0}
  }
}
```

- 选择原则：用于 runtime-visible work state；不要把它当成项目文档或长期 memory。

### 结构化代码搜索与代码智能工具

#### `ast_grep_search`

- 分组：structural code search
- 只读：是
- 用途：用 ast-grep pattern 做结构化代码匹配。
- 参数：

```json
{"path": "src", "pattern": "class $NAME", "lang": "python"}
```

`lang` 可省略。

- 成功返回：

```json
{
  "content": "Found 3 AST match(es) in src",
  "data": {
    "path": "src",
    "pattern": "class $NAME",
    "lang": "python",
    "match_count": 3,
    "matches": []
  }
}
```

- 选择原则：代码结构匹配优先于 text grep；普通文字或单文件 literal 搜索用 `grep`。

#### `ast_grep_preview`

- 分组：structural code rewrite preview
- 只读：是
- 用途：预览 ast-grep rewrite，不修改文件。
- 参数：

```json
{
  "path": "src/example.py",
  "pattern": "foo($A)",
  "rewrite": "bar($A)",
  "lang": "python"
}
```

- 成功返回：

```json
{
  "content": "Previewed 2 AST replacement(s) in src/example.py",
  "data": {
    "path": "src/example.py",
    "replacement_count": 2,
    "matches": [],
    "applied": false
  }
}
```

- 选择原则：执行结构化 rewrite 前先用它验证影响范围。

#### `ast_grep_replace`

- 分组：structural code rewrite
- 只读：否，默认触发 approval
- 用途：应用 ast-grep rewrite。
- 参数：

```json
{
  "path": "src/example.py",
  "pattern": "foo($A)",
  "rewrite": "bar($A)",
  "lang": "python",
  "apply": true
}
```

`apply` 必须为 `true`；否则工具会拒绝执行。

- 成功返回：

```json
{
  "content": "Applied 2 AST replacement(s) in src/example.py",
  "data": {
    "path": "src/example.py",
    "replacement_count": 2,
    "matches": [],
    "applied": true
  }
}
```

- 选择原则：只在 `ast_grep_preview` 结果清楚且 rewrite 适合结构化批量修改时使用。

#### `lsp`

- 分组：code intelligence
- 只读：是
- 可用性：仅当 runtime-managed LSP subsystem 启用并注入该工具时暴露。
- 用途：执行基础 LSP 查询。
- 参数：

```json
{
  "operation": "textDocument/definition",
  "filePath": "src/example.py",
  "line": 10,
  "character": 5,
  "server": "pyright"
}
```

`server` 可省略。`line` 与 `character` 是 1-based。

支持的 `operation` 包括：

- `textDocument/definition`
- `textDocument/references`
- `textDocument/hover`
- `textDocument/documentSymbol`
- `workspace/symbol`
- `textDocument/implementation`
- `textDocument/prepareCallHierarchy`
- `callHierarchy/incomingCalls`
- `callHierarchy/outgoingCalls`

- 成功返回：

```json
{
  "content": null,
  "data": {"lsp_response": {"jsonrpc": "2.0", "result": []}}
}
```

- 选择原则：需要定义、引用、hover、symbol、call hierarchy 等 language-server 语义时使用；纯文本搜索不要用 LSP。

### 外部资料与网络工具

#### `web_search`

- 分组：external research
- 只读：是
- 用途：搜索网页，返回标题、URL 与 snippet。优先 Exa API（如果 `EXA_API_KEY` 存在），否则 DuckDuckGo HTML fallback。
- 参数：

```json
{"query": "VoidCode runtime tool contract", "numResults": 8}
```

`numResults` 可省略，默认 8，最大 20。

- 成功返回：

```json
{
  "content": "1. Result title\n   https://example.com\n   snippet...",
  "data": {"query": "...", "num_results": 8, "source": "exa"}
}
```

- 选择原则：不知道具体 URL、需要发现外部资料时使用；已有 URL 时用 `web_fetch`。

#### `web_fetch`

- 分组：external research
- 只读：是
- 用途：抓取 URL 内容，支持 `text`、`markdown`、`html` 输出。
- 参数：

```json
{"url": "https://example.com/docs", "format": "markdown", "timeout": 30}
```

`format` 可省略，默认 `markdown`；`timeout` 可省略，最大 120 秒。

- 成功返回：

```json
{
  "content": "fetched content",
  "data": {
    "url": "https://example.com/docs",
    "content_type": "text/html",
    "format": "markdown",
    "byte_count": 12345
  }
}
```

图片响应可能以 `data.attachment` 形式返回 base64 data URI。

- 选择原则：有具体 URL 且需要正文时使用；不要用它探测 localhost 或内部网络。

#### `code_search`

- 分组：external code research
- 只读：是
- 用途：搜索编程示例，优先 Exa MCP `web_search_exa`，失败时 DuckDuckGo fallback。
- 参数：

```json
{
  "query": "Python dataclass ToolResult examples",
  "numResults": 5,
  "livecrawl": "fallback",
  "type": "auto",
  "contextMaxCharacters": 10000
}
```

默认 `numResults=5`，最大 20；`livecrawl` 允许 `fallback` / `preferred`；`type` 允许 `auto` / `fast` / `deep`；`contextMaxCharacters` 范围为 1000 到 50000。

- 成功返回：

```json
{
  "content": "Snippet 1:\n...\n\nSources:\n- https://example.com",
  "data": {
    "query": "...",
    "num_results": 5,
    "source": "exa_mcp_web_search_exa",
    "snippet_count": 3,
    "sources": ["https://example.com"]
  }
}
```

- 选择原则：需要外部代码示例或实现参考时使用；普通网页资料用 `web_search` / `web_fetch`。

### 动态 MCP 工具

#### `mcp/<server>/<tool>`

- 分组：dynamic external capability
- 只读：否，默认触发 approval
- 可用性：只有当 MCP server 启动并暴露工具时才出现在 registry。
- 用途：代理调用 MCP server tool。
- 参数：

参数 shape 来自 MCP tool 自身的 `input_schema`。agent 必须以 registry 暴露的 `ToolDefinition.input_schema` 为准：

```json
{
  "tool_name": "mcp/github/search_issues",
  "arguments": {"query": "repo:owner/name is:issue"}
}
```

- 成功返回：

```json
{
  "content": "joined text content from MCP response",
  "data": {
    "server": "github",
    "tool": "search_issues",
    "content": []
  }
}
```

- 选择原则：只在内置工具无法表达所需外部能力、且对应 MCP server 明确配置时使用。由于当前 wrapper 将 MCP 工具标记为非只读，agent 应预期 approval。

## 相似工具选择指南

| 目标 | 推荐工具 | 避免 |
| --- | --- | --- |
| 看目录结构 | `list` | 不要用 `shell_exec ls` 作为首选 |
| 找文件名 / 扩展名 | `glob` | 不要读取整个仓库 |
| 读取已知文件 | `read_file` | 不要用 `shell_exec cat` |
| 单文件 literal 搜索 | `grep` | 不要用 LSP 或 AST 搜索 |
| 结构化代码搜索 | `ast_grep_search` | 不要用 brittle text grep 匹配语法结构 |
| 代码定义 / 引用 / hover | `lsp` | 不要猜测调用链 |
| 小范围文本替换 | `edit` | 不要完整重写文件 |
| 同一文件多个替换 | `multi_edit` | 不要多次独立调用造成中间状态漂移 |
| 跨文件或大 diff 修改 | `apply_patch` | 不要把 patch 拆成大量不相关 edit |
| 创建或完整重写文件 | `write_file` | 不要用 patch 表达全量新内容，除非需要 diff 审阅 |
| 执行测试 / 构建 | `shell_exec` | 不要用 shell 做已有窄工具可完成的读取操作 |
| 只格式化文件 | `format_file` | 不要用 edit 修改格式细节 |
| 搜索网页 | `web_search` | 不要在已有 URL 时重复搜索 |
| 抓取已知 URL | `web_fetch` | 不要访问内部 / localhost 目标 |
| 搜索外部代码示例 | `code_search` | 不要把外部示例当作本仓库事实 |
| 调用配置的外部能力 | `mcp/<server>/<tool>` | 不要假设 MCP 工具只读或永远可用 |

## Agent 调用准则

1. **优先只读上下文收集。** 在没有足够上下文前，使用 `list` / `glob` / `read_file` / `grep` / `ast_grep_search` / `lsp` 收敛事实。
2. **选择最窄工具。** 能用 `read_file` 就不要用 `shell_exec cat`；能用 `edit` 就不要完整 `write_file`。
3. **预期 approval pause。** 所有 `read_only=false` 的调用都可能让 session 进入 `waiting`，agent 不应假设调用立即执行。
4. **把外部资料与本地事实分开。** `web_search` / `web_fetch` / `code_search` 给的是外部证据；本仓库状态仍以 workspace 工具和 runtime events 为准。
5. **不要绕过 runtime。** UI、agent preset、graph / provider engine 都不应直接执行工具或自行处理审批。
6. **读取 `ToolResult.data`，不要只读 `content`。** `content` 用于人类/agent 摘要，`data` 才是稳定结构化 metadata。
7. **错误是可恢复信号。** 参数错误、路径越界、approval denial、tool error 都应让 agent 收缩下一步，而不是重复同一调用。

## 最小示例：读取文件后小范围编辑

1. Agent 先读取目标文件：

```json
{"tool_name": "read_file", "arguments": {"path": "docs/example.md"}}
```

2. Runtime 自动 allow，因为 `read_file.read_only=true`。

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

4. Runtime 发现 `edit.read_only=false`：
   - `approval-mode=ask`：发出 `runtime.approval_requested` 并暂停；
   - `approval-mode=allow`：记录 allow 后执行；
   - `approval-mode=deny`：拒绝执行。

5. 当 approval / permission 与 pre-tool hook 都已通过后，runtime 先发出 `runtime.tool_started`，表示真实工具执行开始。

6. 执行成功后，runtime 发出 `runtime.tool_completed`，payload 中包含 diff、additions、deletions 等结构化结果。

## 维护规则

- 新增或修改工具时，同时更新：
  - `ToolDefinition` / 参数校验代码；
  - 相关单元或集成测试；
  - 本文档的工具目录、参数 shape、返回 shape 与选择指南。
- 如果工具的 `read_only` 发生变化，必须同步检查 `docs/contracts/approval-flow.md` 中的审批预期。
- 如果 agent preset 只改变“希望携带哪些工具”，更新 `src/voidcode/agent/` 文档；如果改变实际执行/审批/恢复语义，必须更新 runtime contracts。
