# Typed Tool Input Hooks Contract

## 状态

这是 VoidCode 首期 typed tool-input hook 的实现契约。当前 runtime 使用
`builtin_tool_input_handler_registry()` 作为默认空 builtin registry；显式注入的
`ToolInputHandlerRegistry` 可以通过同一 stable composition seam 提供 handlers。当前
没有安全、通用且经过工具语义验证的 production canonicalizer，因此默认 registry
不伪造 path/arguments rewrite，也不通过 `.voidcode.json` 或 `ResolvedHookPlan`
声明 handler。现有 argv hooks 保持不变。

实现锚点：

- `src/voidcode/hook/typed.py`：`ToolInputEvent`、`ToolInputDecision`、
  `ToolInputHandlerBinding`、`ToolInputHandlerRegistry`、builtin composition helpers
- `src/voidcode/runtime/service.py`：默认 builtin registry 与显式 runtime 注入
- `src/voidcode/runtime/run_loop.py`：native tool 与 `invoke_tool` inner target 接入
- `tests/unit/hook/test_typed.py`
- `tests/unit/runtime/test_typed_tool_hooks.py`

## 目的与边界

Typed input handler 只能做额外的、纯的参数 canonicalization、校验、诊断或阻断。
它不是工具实现的替代品：每个 tool 自己的 Pydantic/schema 校验与 guards 仍然是
最终执行 authority。

Handler 不拥有以下能力：

- 不能授权工具、返回 `approved` 或绕过 permission/approval；
- 不能修改 session/task/approval truth 或直接写 SQLite；
- 不能改变 tool name、owner、delegation target 或 runtime policy；
- 不能把诊断文本变成用户批准；
- 不能替代现有 argv hook executor。

Registry 的 handler 输入是深拷贝 snapshot。handler 对 arguments 或 published
`input_schema` 的嵌套 mutation 不会改动 runtime 对象。

## 触发顺序

### 普通 provider/graph tool call

正常 native tool 的顺序为：

```text
graph.tool_request_created (原始请求，保持既有时序)
  -> delegation/tool policy
  -> tool lookup
  -> typed ToolInputHandlerRegistry
  -> rewritten 参数的 published schema gate
  -> 重新 tool lookup（同一 tool name）
  -> runtime permission / approval
  -> 既有 argv pre_tool hook
  -> runtime.tool_started
  -> tool.invoke（tool 私有 Pydantic/schema/guards 是最终校验）
  -> runtime.tool_completed
```

Typed handler 的 rewrite 发生在 permission/approval 之前。rewrite 影响路径、命令、
外部目录或执行等级时，后续 runtime permission/approval 会使用最终参数重新计算。

现有 `argv hooks.pre_tool` 的时序不变：它仍在 permission/approval 之后、工具执行
之前运行。两者不是同一个 executor，也不能把 argv hook 的 stdout 语义当成 typed
handler 的 return contract。

### `invoke_tool`

`invoke_tool` 是 dispatcher。Outer `invoke_tool` call **不执行 typed handler**；
runtime 解析 inner target 后，typed handler 只对 inner target 执行一次。这样同一
logical call 不会被 outer 与 inner 双重 canonicalize。

Inner target 的 `graph.tool_request_created` 仍记录 inner target 的原始参数；typed
结果通过专用的 `runtime.tool_input_processed` 事件记录，随后 permission/approval
与执行使用最终 inner 参数。

### Resume

Approved approval resume 直接使用已持久化 `PendingApproval.arguments`，不再次运行
typed handler。这样审批所看到、持久化和实际执行的参数保持一致。

普通 graph resume 是原 prompt + 已完成 tool results 的重新执行；新的 graph tool call
仍运行 typed registry，并把 `ToolInputEvent.is_resume` 设为 `true`。handler 必须是
纯的、可重复执行的 canonicalization/校验逻辑。runtime 不按整个 resumed run 跳过
handler，也不把新调用误当成旧调用。中断前已写入的 pending tool intent 按既有
runtime-owned execution intent contract 保存最终参数摘要；approved direct resume
仍以 PendingApproval 为唯一执行输入。

## Decision contract

每个 handler 必须返回 `ToolInputDecision`，且 action 只有四种：

### `unchanged`

```python
ToolInputDecision(action="unchanged")
```

继续使用当前参数，不产生参数变换。它不能携带 `arguments`、`reason` 或
`diagnostic`。

### `rewrite`

```python
ToolInputDecision(
    action="rewrite",
    arguments={"path": "canonical/path.txt"},
)
```

提供新的完整 arguments mapping。runtime 随后会：

1. 对 published tool input schema 做 generic gate；
2. 重新从 runtime tool registry lookup 同名 tool；
3. 用最终 `ToolCall` 重新执行 permission/approval；
4. 让 tool 自己在 `invoke` 内再次执行其私有 Pydantic/schema/guards。

Generic gate 不是 tool self-validation 的替代品；tool 私有校验失败仍按现有
runtime tool error 路径处理。

### `block`

```python
ToolInputDecision(action="block", reason="unsafe target")
```

阻止当前工具执行。runtime 产生标准 tool-level error feedback；handler 不能把
session truth 伪造为 completed，也不能直接决定 approval resolution。

Handler exception 或 invalid decision 也 fail closed 为 block，并带 bounded reason。

### `diagnostic`

```python
ToolInputDecision(action="diagnostic", diagnostic="canonical form checked")
```

只记录 bounded diagnostic，继续使用当前参数。它不修改参数、不阻止执行，也不改变
permission/approval。诊断不会自动注入 provider context。

## 事件与参数真相

`graph.tool_request_created` 是 graph/provider 提出的请求事实，保存原始 arguments。
Typed rewrite 不覆盖这个事件，以便 replay/debug 能区分“模型原始请求”和“runtime
后续 canonicalization”。

当 typed handler 产生 rewrite 或 diagnostic，runtime 追加专用的
`runtime.tool_input_processed` event，payload 至少包含：

```json
{
  "surface": "typed_input",
  "session_id": "session-1",
  "tool_name": "read",
  "hook_status": "ok",
  "policy": {"mode": "normal", "read_only": false},
  "action": "rewrite",
  "handler_names": ["canonicalize"],
  "diagnostics": [],
  "rewrite": {
    "original_sha256": "...",
    "final_sha256": "...",
    "original_argument_keys": ["path"],
    "final_argument_keys": ["path"]
  }
}
Rewrite metadata只保存 hash、bounded key list、bounded handler names/diagnostics 与
action，不保存完整原始/最终 arguments；因此它是 observability trace，不是新的
authority。`runtime.tool_input_processed` 是 typed 专用事件，不复用 argv
`runtime.tool_hook_pre`。现有 `runtime.tool_lookup_succeeded` 只表达 initial lookup
成功；typed trace 位于它之后、permission/approval 之前。

限制：

- handler names 最多保存 32 项；
- diagnostics 最多保存 32 项；
- argument key 列表有上限，超出时使用 omission marker；
- hash 使用 canonical JSON 表示，不能用于恢复参数；
- raw arguments 是否在 graph/tool result/approval payload 中出现，继续由原有
  runtime redaction/persistence contract 决定。

所有事件仍通过 runtime 的 SQLite session event append 路径持久化；handler 不直接
追加事件、不分配 sequence，也不能修改已成立的 session/task/approval truth。

## 稳定组合

`ToolInputHandlerBinding` 提供：

- 唯一的 handler name；
- 显式 integer priority；
- handler callable。

Registry 先按 priority 升序排序；相同 priority 保持注册顺序。每个 handler 看到
前一个 handler 成功 rewrite 后的当前参数 snapshot，因此组合是确定的。第一个
block 短路；diagnostic 累积；多个 rewrite 按稳定顺序依次应用。

## 与现有 runtime 治理的关系

Typed input hook 是 runtime tool governance 中的附加候选变换层：

```text
typed candidate
  -> published schema gate
  -> runtime tool lookup
  -> permission / approval
  -> argv pre_tool hook
  -> tool private validation and guards
  -> execution and persistence
```

Typed handler 不能放宽 agent tool allowlist、read-only policy、shell policy、external
path policy、approval mode 或 delegation budget。Background lifecycle hooks 的
post-truth observer 语义也不因 typed input hook 改变。

本契约只覆盖 ToolInputHandler；ToolResultHandler、context transform、provider
request transform、extension registration 和 plan v3 不属于首期实现。
