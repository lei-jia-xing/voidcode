# `voidcode.agent` 边界说明

## 状态

**状态：目录已存在；声明层与 runtime-owned delegated execution 基线都已落地，但仍不是任意多 agent 平台**

本文档描述 `voidcode.agent` 边界的定位与约束。`src/voidcode/agent/` 目录现在已经存在；仓库已经具备 runtime-owned 的 delegated child-session/background-task 基线，但 `agent/` 本身仍主要承担声明层职责，而不是独立 runtime。

## 为什么需要这份文档

在当前 MVP 阶段，VoidCode 已经形成了比较清晰的 runtime-centric 架构：

- `runtime/` 是系统控制面
- `graph/` 负责执行循环与步骤推进
- `tools/`、`hook/`、`skills/`、`mcp/`、`provider/` 是能力层

随着后续 multi-agent 成为明确方向，仓库需要一个单独的边界来承载“预定义 agent 的声明式配置”，避免把这类配置散落到 runtime、graph 或客户端侧。

## 当前现实

当前仓库的真实情况是：

1. `VoidCodeRuntime` 仍是系统控制面，负责 session、approval、permission、persistence、event、transport、tool execution 与 capability lifecycle。
2. `graph/` 仍是 orchestration / execution layer，而不是产品级治理边界。
3. graph 层继续承载 deterministic/reference orchestration；provider-backed 与 delegated child execution 由 runtime 统一治理。
4. 顶层 active run 默认使用 `leader`，也可以显式选择 `product` 进入规划/需求模式；runtime-owned delegation path 上的 child run 还可执行 `advisor`、`explore`、`product`、`researcher`、`worker`。
5. 仓库已经具备 background task、parent/child session linkage、task notification/result retrieval 等 delegated substrate，但仍未扩展成任意拓扑的成熟 multi-agent 平台。

因此，`voidcode.agent` 的设计目标不是引入第二套 runtime，而是补上“agent 定义层”。

## `voidcode.agent` 边界

`voidcode.agent` 作为**预定义 agent 与 agent preset/configuration 的声明层**，负责描述一个 agent 是什么、默认携带哪些能力绑定，而不是负责执行它。

建议它承载的内容包括：

- agent manifest / preset
- prompt / profile 定义
- hook 绑定
- skill 绑定
- MCP server / profile 绑定
- tool allowlist / default tool set
- provider / model preference metadata

这些内容应当保持为**声明式配置或类型化定义**，供 runtime 解析与消费。

当前仓库里已经存在的 `src/voidcode/agent/README.md` 与 `src/voidcode/agent/<role>/README.md`，应被理解为这层声明边界的文档化外壳，而不是独立 agent runtime 的证据。`preset_hook_refs` 现在由 `src/voidcode/hook/presets.py` 中的 builtin hook preset catalog 校验，表达的是角色 guidance / guard / continuation intent，不是 runtime lifecycle hook command。`mcp_binding` 也同样只是 profile/server 绑定意图，不包含 MCP command/env，也不能绕过 runtime-owned MCP lifecycle。

## 哪些东西必须继续留在 `runtime/`

以下能力仍然必须由 `voidcode.runtime` 持有，而不能下沉到 `voidcode.agent`：

- session 持久化与恢复
- approval / permission 决策
- runtime event emission / routing
- transport / client-facing truth
- tool registry 与 tool invocation
- hook 执行时机
- MCP / LSP / ACP lifecycle truth
- provider fallback 与 execution governance
- resume / replay correctness

如果未来要支持“leader 异步调用其他 agent，并在完成后收到通知”的协作语义，那么以下能力也必须首先成为 runtime truth，而不能悬空挂在 `agent/` 或 hook 文档上：

- background task 状态机
- parent / child session 关系
- task completion / failure / cancellation / timeout lifecycle
- leader notification 路径
- background result retrieval / transcript recovery

换句话说，`agent/` 可以决定“一个 agent 想带什么配置”，但不能决定“系统最终怎样执行、治理和恢复它”。

## 为什么现在不需要把 LangGraph 当成前提

当前并不需要把 LangGraph 作为 `voidcode.agent` 的前置条件，原因有三点：

1. provider-backed execution path 已经证明：runtime 可以在不依赖 LangGraph 的前提下驱动真实执行路径。
2. 当前最需要收口的仍然是 capability substrate：skill execution、hook model、MCP config/profile、provider resolution/fallback，而不是 graph-first 的 multi-agent orchestration。
3. 如果在这些能力层尚未稳定时就把 multi-agent 主骨架建立在 LangGraph 之上，容易把还不稳定的执行语义过早固化进 workflow 结构中。

这并不意味着 LangGraph 未来没有价值。它仍然适合：

- 复杂 branching / retry tree
- supervisor / worker 协作
- subagent handoff
- graph-shaped orchestration

但在当前阶段，`voidcode.agent` 的存在不应依赖这些能力已经落地。

## 分阶段推进建议

### Phase 0：先文档化边界

先明确 `voidcode.agent` 的职责与非职责，避免未来把 agent 配置、runtime 治理和 graph orchestration 混在一起。

### Phase 1：引入薄声明层

先让 `src/voidcode/agent/` 只承载：

- AgentDefinition / preset
- prompt/profile
- tool allowlist
- hook/skill/MCP/provider 引用

这一阶段不做 multi-agent runtime，不做 agent-to-agent messaging，不做复杂 supervisor。

文档与声明可以优先落在：

- `src/voidcode/agent/README.md`
- `src/voidcode/agent/<role>/README.md`

这些文件用于描述角色 preset、本地权限倾向、建议 skills / hooks / MCP profile 以及与 runtime 的边界，但不代表这些角色当前已经拥有独立 runtime 实现。

同样，这里的“建议 hooks”也应理解为 preset intent。当前 builtin agent 的 `preset_hook_refs` 已经必须引用 hook preset catalog 中存在的 ref；但这些 ref 仍不等价于 session-start/session-end、background completion notification 或 message transform 等 runtime lifecycle hook command。

### Phase 2：由 runtime 解析 agent preset

让 runtime 在现有 execution path 中能够解析和应用 agent preset，同时继续保持 runtime 对 approval、permission、event、persistence 的控制。

当前实现已经完成 runtime 对 agent preset 的第一阶段落地：`leader` 作为默认顶层 active preset 进入 provider-backed 主路径，`product` 可以作为显式顶层 planning preset 被选择。runtime 会向 provider 注入所选 preset 的 `prompt_profile` / `prompt_materialization`，应用 agent-scoped model / execution engine / provider fallback，收窄可见与可调用工具，校验 agent hook preset refs，并让 manifest `skill_refs` / agent-scoped skills 进入本次运行的 runtime-managed skill application。runtime 还会把 tools、skills、hook preset guidance、MCP binding intent 与 provider/model metadata materialize 成 `agent_capability_snapshot` 并持久化到 session metadata；skill snapshot 的 binding 也引用这份 snapshot，从而让 replay/debug 使用历史 truth，而不是重新从变动后的 catalog 推导。`advisor`、`explore`、`product`、`researcher`、`worker` 现在也可以作为 delegated child preset 进入 runtime-owned child execution，但除 `leader` 与 `product` 外，它们仍不能被当作任意顶层 active agent 直接启动。

### Phase 3：再评估 multi-agent orchestration

只有当 capability substrate 已经稳定、并且真实出现 multi-agent workflow 需求时，再决定是否引入更重的 orchestration 机制（包括但不限于 LangGraph）。

## 明确非目标

本文档明确**不**主张：

- 当前已经实现 multi-agent
- 当前必须使用 LangGraph 才能进入 agent 方向
- 把 runtime 的控制面职责迁移到 graph 或 agent
- 让客户端直接调用工具或绕过 runtime
- 在当前阶段就实现完整的 supervisor / worker / delegation runtime

## 结论

`voidcode.agent` 更适合作为一个**薄的、声明式的 agent 定义层**，而不是新的 runtime。当前最合理的方向是：

- 继续保持 runtime-centric 架构
- 先补 capability substrate
- 再让 `agent/` 作为组合层进入系统
- 等真实 multi-agent 需求与能力层稳定后，再决定是否扩大 graph-based orchestration 的作用范围

这样可以在不破坏现有执行边界的前提下，为未来 multi-agent 留出清晰且可演进的位置。
