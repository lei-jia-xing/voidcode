# 工作流预设契约

## 目的

本文定义 workflow preset 在 VoidCode MVP 中的语义。它只描述运行时拥有的声明式预设、快照和优先级收口，不把 workflow preset 说成 workflow engine，也不把它扩展成 DAG、调度器或可执行流程语言。

## 状态

当前实现已经包含一组内置 workflow preset，以及仓库本地声明式覆盖入口。它们由 runtime materialize 为 audit safe 的 workflow metadata，并进入 session 相关快照，用于 fresh run、resume、debug、replay 和 bundle 读回。内置 preset 现在只引用 source-backed builtin skill catalog metadata 和 descriptor/config-backed MCP intent，而不是只保存自由字符串或本地编写的假 skill body。

这层契约的核心是：workflow preset 记录的是运行时意图和约束，真正的执行治理仍归 runtime。

## 术语

| 概念 | 含义 | 在本契约中的角色 |
| --- | --- | --- |
| agent preset | 运行时消费的 agent 声明，携带 prompt profile、工具、技能、hook、MCP、模型和执行默认值 | workflow preset 可以选择一个默认 agent，但不会替代 agent preset 本身 |
| category | 任务分类标签，常用于 delegated child 路由和默认模型覆盖 | workflow preset 的 `category` 只表示分类语义，不表示调度拓扑 |
| skill | 可发现的技能元数据，默认是 catalog visible 的选择信号 | workflow preset 可以声明要选哪些 skill |
| force loaded skill | 会把完整 skill body 注入当前 run 或 child session 的技能 | 缺失时必须验证失败，不能静默降级成普通 skill selection |
| hook preset ref | 对 builtin hook preset catalog 的声明式引用 | 只 materialize 为 guidance 或 guardrail context，不扩大权限 |
| MCP binding intent | 对 MCP profile 或 server 可用性的声明式意图 | 只表达需要什么，不负责启动、发现或管理 MCP 生命周期 |
| workflow preset | 汇总 workflow 级意图的 runtime owned declaration | 组合 agent、category、skills、hooks、MCP intent、policy refs、read only intent 和 verification guidance |

## 预设选择与优先级

workflow preset 的选择和展开遵循以下顺序：

| 优先级 | 来源 | 说明 |
| --- | --- | --- |
| 1 | 显式 request metadata | `workflow_preset`、显式 child override、显式 prompt materialization、显式 `skills`、显式 `force_load_skills` 先于其他来源 |
| 2 | 选中的 workflow preset 默认值 | preset 的 `default_agent`、`prompt_append`、`skill_refs`、`force_load_skills`、`hook_preset_refs`、`mcp_binding_intents`、`tool_policy_ref`、`permission_policy_ref`、`read_only_default`、`verification_guidance` |
| 3 | agent manifest 默认值 | builtin 或本地 manifest 的 prompt materialization、工具 allowlist、skill refs、hook refs、MCP intent、model 和 execution defaults |
| 4 | runtime 默认值 | runtime config、builtin registry 和 permission / tool / MCP governance 的默认收口 |

说明：

1. `workflow_preset` 是严格的 request metadata key。repo local workflow 定义优先于 builtin 定义。
2. 选中的 workflow preset 只会补充默认值，不应静默覆盖显式 request metadata。
3. 对 prompt 来说，workflow preset 的 `prompt_append` 只负责追加，不负责替换 base materialization。
4. 当 preset 默认 agent 不是当前可执行的 top level agent 时，runtime 只记录 metadata，不把它当作顶层 active agent 切换。

## prompt materialization 与 prompt append

workflow preset 不定义新的 prompt 系统，它只参与现有 prompt materialization 的组合。

### 组合规则

1. 显式 request `prompt_materialization` 胜出。
2. 如果 request 没有提供 `prompt_materialization`，runtime 继续保留已经解析出的 materialization。
3. workflow preset 的 `prompt_append` 在 base materialization 之后追加。
4. prompt profile 变化时，不能静默丢弃自定义 materialization。

### 解释

这意味着 workflow preset 可以提供额外 guidance，但不能把已经解析好的自定义 prompt 改写成另一份默认 prompt。历史 session 仍要按持久化快照解释，而不是按当前文件内容重新生成。

## persisted snapshot truth

fresh run 会把 workflow 相关信息 materialize 成 audit safe snapshot。当前 runtime 的快照语义应保持以下不变量：

1. `workflow`、`runtime_config.workflow` 和 `agent_capability_snapshot.workflow` 记录的是可回放的工作流事实。
2. resume、debug、replay 和 bundle export 使用持久化快照，不重新解析 mutable live registry definitions。
3. 快照保存的是 selected preset、source、category、default agent、effective agent、read only intent、skill refs、force loaded refs、hook refs、MCP binding intent 和 verification guidance 等 metadata。
4. 快照不应该携带 raw args、stdin、secret 或把 workflow declaration 变成执行凭据。

## delegated child inheritance

delegated child session 默认继承父层 workflow snapshot 和相关限制。

### 规则

1. 如果父请求或父 session 已有 workflow snapshot，child 默认复用这份 snapshot。
2. 如果 child request 显式提供 `workflow_preset`，runtime 必须先解析该 preset，再验证它是否允许当前 selected child preset。
3. child override 需要写入审计可读的 metadata，不可以悄悄替换 parent snapshot。
4. child override 只能在 selected delegated child preset 的边界内生效，不能把任意 preset 强行套到不兼容的 child 上。

## 缺失 skill 与缺失 MCP 的行为

workflow preset 允许声明 intent，但验证必须保持确定性。

### skill

如果 `force_load_skills` 指向不存在的 skill，validation 必须失败。workflow preset 不能把缺失的 force loaded skill 静默降级成普通 skill selection。

### MCP

如果 `mcp_binding_intents` 标记为 required，且对应 profile 或 server 不可用，validation 必须失败。

如果 binding 是 optional，runtime 可以在 snapshot 里记录 degraded availability，但不把这件事当成 capability 补齐成功。

## conservative composition

workflow preset 只声明意图，不绕过 runtime 既有边界。

| 领域 | 契约要求 |
| --- | --- |
| read only | `read_only_default` 会保守过滤/拒绝 write 与 execute 类工具；只读工具以及 `skill`、`question`、`background_output`、`background_cancel` 这类非写入 runtime 支撑工具保留 |
| tool policy | workflow 不引入专用仓库操作策略；安全依赖 agent/tool allowlist、generic read-only filtering、runtime approval、hooks 和 guidance |
| permission policy | `permission_policy_ref=runtime_default` 解析为现有 runtime approval/permission 默认值，不提供绕过通道 |
| hook refs | hook preset ref 只能提供 guidance 或 guardrails，不等于执行生命周期钩子脚本 |
| MCP | MCP binding intent 不能启动 server，也不能声明 workspace global lifecycle |

这层契约的安全默认值是保守组合。只要 runtime primitive 还没有真正实现某个引用，就不要把它写成已实现的能力系统。

## 内置 MVP workflow presets

当前 builtin registry 固定为 5 个 id，分别是 `research`、`implementation`、`frontend`、`review`、`git`。

| preset | default_agent | category | read_only_default | 主要 intent |
| --- | --- | --- | --- | --- |
| research | `researcher` | `research` | true | 只读研究，强调公开资料和证据来源，配合 `background_output_quality_guidance`，并声明 `context7`、`websearch`、`grep_app` optional MCP/search intent |
| implementation | `leader` | `implementation` | false | 实现变更并验证；通用实现 guidance 保留在 preset prompt/verification guidance 中，配合 `todo_continuation_guidance` 和 `runtime_default` permission policy |
| frontend | `leader` | `frontend` | false | 前端变更和验证，声明 `frontend-design` 与 `playwright` skill ref，包含 `playwright` optional browser/MCP intent，配合 `todo_continuation_guidance` 和 `runtime_default` permission policy |
| review | `advisor` | `review` | true | 只读审查，声明 `review-work` skill ref 以及 `context7`、`websearch`、`grep_app` optional MCP/search intent，要求按严重性和文件位置报告问题 |
| git | `leader` | `git` | false | Git 操作受控且可审计，声明 `git-master` skill ref，使用 `runtime_default` permission policy 和普通 runtime approval；不使用专用仓库操作策略 |

### issue #405 命名能力映射

issue #405 中点名的 MVP examples 必须能在 builtin preset payload 和 persisted workflow snapshot 中直接看到。当前映射如下：

| issue example | VoidCode workflow declaration | 语义 |
| --- | --- | --- |
| `git-master` | `git.skill_refs = ["git-master"]`，并在 prompt guidance 中写明 `git-master-style safety guidance` | catalog-visible git 安全能力语义；不是自动 force-load，不绕过 tool/approval policy，也不添加专用仓库操作策略 |
| `frontend-design` | `frontend.skill_refs = ["frontend-design"]` | catalog-visible 前端设计和 UI 实现 guidance 语义；来源于 Anthropic public skill，本地 vendored 后离线加载 |
| Playwright / browser verification | `frontend.skill_refs = ["frontend-design", "playwright"]`，`frontend.mcp_binding_intents` optional server `playwright` | builtin `playwright` skill 带有 skill-scoped MCP descriptor `command: npx`, `args: ["@playwright/mcp@latest"]`；只有配置了 server 才可执行，不自动全局启动 |
| `context7` | `research` 和 `review` 的 optional MCP server intent | 文档查询/官方资料 lookup intent |
| `websearch` | `research` 和 `review` 的 optional MCP server intent | 公网搜索/研究 intent |
| `grep_app` | `research` 和 `review` 的 optional MCP server intent | 代码搜索 example intent；descriptor 不声明远程 endpoint，实际执行必须由用户配置同名 MCP server |
| `review-work` | `review.skill_refs = ["review-work"]`，并在 prompt guidance 中写明 result-quality/read-only analysis | catalog-visible review/result-quality guidance 语义 |

这些映射是声明式 capability bundle semantics：runtime 会把它们写入 workflow snapshot / capability snapshot，并对 generic read-only workflow policy 执行保守 runtime enforcement，但不会因此实现平台式 MCP 语义、全局 MCP lifecycle、浏览器自动启动、专用仓库操作策略、或新的 agent 拓扑。

### builtin skill / MCP registry

VoidCode backend ships these builtin skills even when the workspace has no `.voidcode/skills/**/SKILL.md` files:

| skill | 用途 |
| --- | --- |
| `git-master` | git 安全、hook 保留、generic runtime approval guidance |
| `frontend-design` | 前端设计、UI 实现与验证 guidance |
| `playwright` | VoidCode-local browser verification guidance；Playwright MCP 仍由 descriptor/config intent 表达 |
| `review-work` | 只读审查、发现报告、严重性与证据 guidance |

这些 builtin skill metadata 和可注入内容必须离线来自本地 package resource。`frontend-design` 基于 Anthropic public `skills/frontend-design/SKILL.md` 做本地适配，因为其内容不依赖 OhMyOpenAgent 专属 agent 语义；`playwright` 是 VoidCode-local concise browser verification guide，借鉴通用 Playwright 验证流程但不复制 Claude/plugin-specific 操作假设；`review-work` 是 VoidCode-local adaptation，只描述当前支持的只读审查、验证、MCP descriptor intent 与 delegated child preset；`git-master` 保留 OpenAgent commit `e17850cbab3e3a609444c1a0cec26afb244c3fc5` 的来源记录，但在 VoidCode 中仍受 runtime approval、tool policy 与 config-gated MCP 约束。

本地 workspace skill 与 builtin skill 同名时保持 fail closed：registry 会报 duplicate skill name，而不是悄悄覆盖 builtin catalog metadata。

VoidCode backend also ships these builtin MCP descriptors:

| descriptor | transport | lifecycle | 用途 |
| --- | --- | --- | --- |
| `context7` | remote HTTP descriptor | descriptor-only, config-gated | documentation lookup intent |
| `websearch` | remote HTTP descriptor | descriptor-only, config-gated | public web search intent |
| `grep_app` | configured server intent descriptor | descriptor-only, config-gated | optional code search intent; requires a user-configured `grep_app` server for execution |
| `playwright` | stdio command `npx @playwright/mcp@latest` | skill-scoped descriptor-only, config-gated | browser verification intent |

Remote/configured-server-intent descriptors are availability metadata in workflow snapshots; current runtime execution still supports configured stdio MCP lifecycle only. `grep_app` is descriptor/intent-only and intentionally carries no builtin URL because no endpoint is verified here; actual code-search MCP execution requires a real user-configured server named `grep_app`. Required workflow MCP intents fail fresh run when their named configured server is absent. Optional intents record degraded/missing availability and do not fail normal runs.

### builtin 语义补充

1. `research` 和 `review` 都是保守只读默认值，适合分析和审查类任务。
2. `implementation` 和 `frontend` 默认面向修改和验证，但仍受 runtime 权限和工具边界约束。
3. `git` 不是自由的 repo 改写入口，它只声明 narrow、auditable、user requested 的 git intent，并继续经过 runtime approval。
4. `git` 的安全指导必须继续提醒用户检查状态、保留 hooks、避免扩大权限；runtime 不维护专用仓库操作策略，因为这类策略无法枚举完整风险面。
5. `context7`、`websearch`、`grep_app` 和 `playwright` 都是 optional MCP binding intent；普通 workspace 没有配置这些 server 时 builtin preset validation 仍应通过，snapshot 记录 descriptor availability 与 degraded/missing server availability。

## verification guidance

`verification_guidance` 是 workflow preset 的明确输出，不是自动执行器。

它应当用于提醒调用方和 runtime 哪一类验证最有意义，例如：

1. `research` 强调证据来源和置信边界。
2. `implementation` 强调覆盖变更的定向测试或检查。
3. `frontend` 强调前端 type、lint、test 或 build 检查。
4. `review` 强调严重性和文件定位。
5. `git` 强调前后 `git status`、保留 hooks 和显式意图。

## 非目标

本契约明确不描述以下内容：

1. native hook workflow migration。
2. workspace global 或 marketplace-style 的 MCP 生命周期。
3. 任意动态 agent 拓扑。
4. direct peer-to-peer agent messaging bus。
5. workflow DAG。
6. executable workflow DSL。
7. frontend UI parity。

## 相关代码

- `src/voidcode/runtime/workflow.py`
- `src/voidcode/runtime/service.py`
- `src/voidcode/runtime/config.py`
- `src/voidcode/runtime/contracts.py`
- `src/voidcode/agent/builtin.py`
- `src/voidcode/hook/presets.py`
- `docs/contracts/agent-capability-bindings.md`
- `docs/contracts/runtime-config.md`
