# Agent Capability Binding Contract

## 状态

当前仓库已经把 agent preset 从单纯的 prompt/profile 扩展成 runtime-consumed capability bundle。这个 bundle 仍然是 **runtime materialization snapshot**，不是 agent-owned execution governance。

## 输入来源与优先级

runtime materialize agent capability 时按以下顺序收口：

1. resolved `AgentManifest` defaults：builtin preset defaults，或从 project/user 本地 markdown manifest 发现并解析出的 custom defaults（prompt body、tool allowlist、skill refs、hook preset refs、MCP binding intent、model/execution defaults）；
2. repo/runtime config overrides：`agent`、`agents`、`categories`、provider/model、tool/skill/MCP config；
3. request metadata overrides：request `agent`、`skills`、`force_load_skills`、delegation route metadata；
4. delegated child-only bindings：`load_skills` / `force_load_skills` 只进入目标 child session。

这个优先级会写入 `session.metadata.agent_capability_snapshot.precedence`，用于 debug/replay 时解释一个 session 的能力来源。

## Snapshot 形状

每次 run / delegated child session 会持久化 `agent_capability_snapshot`，包含：

- `snapshot_version`：当前为 `1`；
- `agent`：selected preset、manifest id、mode、来源、source scope/path；
- `prompt`：profile/ref/source 与 prompt materialization metadata；builtin 使用 profile/version，custom markdown 使用 `source=custom_markdown`、`format=markdown`、持久化 markdown body 与可选 `prompt_append`；
- `tools`：manifest allowlist、request allowlist/default、builtin-tool 开关、effective tool names；
- `skills`：manifest refs、selected names、force-loaded names、target-session scope；
- `hooks`：manifest refs、resolved refs、resolved guidance snapshot、`guidance_only` materialization；
- `mcp`：declarative binding intent、runtime configured state、configured server names、runtime/session-scoped governance label；
- `execution`：execution engine、model、resolved provider、provider fallback、reasoning controls。

同一个 snapshot 也作为 skill snapshot 的 `binding_snapshot` 使用，保证 skill replay/debug 不需要从变动后的 manifest/catalog 重新推导历史绑定。

## MCP 边界

`AgentManifest.mcp_binding` / `RuntimeAgentConfig.mcp_binding` 只表达声明式 intent：例如 profile 名称或已配置 server 名称。它不能包含 server command/env，也不能启动 MCP。

MCP lifecycle 仍由 runtime/session-scoped `runtime.mcp` 管理：

- `mcp.enabled` 与 `mcp.servers` 仍是 config-gated；
- runtime 决定何时启动、刷新、关闭 MCP server；
- session-scoped MCP 仍按 session owner 隔离；
- MCP tools 仍必须经过 runtime tool registry、agent tool allowlist、approval 与 normal tool execution path。

因此，agent MCP binding 不能绕过 runtime MCP lifecycle、approval、scope 或 tool allowlist。

## Builtin preset、configured alias 与 local manifest

当前有三类容易混淆的 agent 输入：

- **builtin preset**：仓库内置 `leader`、`product`、`worker`、`advisor`、`explore`、`researcher` manifest 与 prompt profile。
- **configured alias/default**：`.voidcode.json` 的 `agent` / `agents.<key>` 只配置已存在 preset 的 model、fallback、tools、skills、MCP intent 等 runtime defaults。它不定义新 prompt，也不创建新 agent manifest。
- **true local manifest**：project `.voidcode/agents/*.md` 或 user config agents 目录中的 markdown 文件。frontmatter 定义 manifest metadata，markdown body 定义 prompt material。project scope 对同 id 的 user scope custom manifest 覆盖；custom manifest 不允许替换 builtin id。

Custom markdown manifest 与 config-defined prompt override 的 prompt materialization 会随 session 持久化到 runtime config metadata / `agent_capability_snapshot`，而不是在 replay 时重新读取文件或重新解析 `.voidcode.json`。`body` 表示 base prompt，`prompt_append` 表示追加 guidance，provider context 渲染时只追加一次。这保证文件变更或删除后，历史 session 仍按当时的 prompt/capability snapshot 解释。

Custom manifest 仍只是 declaration layer：它可以收窄或建议 capability defaults，但不能授予 runtime 未配置、未允许、未审批或未通过现有 delegation/session contract 的执行权。

## Hooks 与 skills 边界

- Hook preset snapshot 是 guidance/guard/continuation context only；它不会执行 lifecycle hook command，也不会扩大 permissions、tool allowlist 或 delegation budget。
- Manifest `skill_refs` 是 catalog-visible default selection；request/delegated `force_load_skills` 会注入 full skill body，但只作用于当前 run / child session。
- Parent session 的 force-loaded skill bodies 不会自动泄漏到 child session；child 必须通过 delegated `load_skills` / `force_load_skills` 明确加载。

## Replay 要求

Replay/debug 应优先读取 persisted `agent_capability_snapshot`。旧 session 的能力解释不能因为 builtin manifest、hook catalog、skill registry 或 MCP config 后续变化而被静默改写。

## 非目标

- 不定义任意多 agent topology；
- 不定义 agent-to-agent messaging bus；
- 不定义 MCP marketplace 或 workspace-scoped MCP lifecycle；
- 不把 provider/model/tool/MCP execution governance 移交给 agent declaration layer；
- 不允许客户端直接调用 tools/MCP 绕过 runtime。

## 相关代码

- `src/voidcode/agent/models.py`
- `src/voidcode/agent/builtin.py`
- `src/voidcode/runtime/config.py`
- `src/voidcode/runtime/service.py`
- `src/voidcode/runtime/skills.py`
- `src/voidcode/runtime/mcp.py`
- `tests/unit/agent/test_builtin.py`
- `tests/unit/runtime/test_runtime_config.py`
- `tests/unit/runtime/test_runtime_service_extensions.py`
