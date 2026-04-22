# `voidcode.agent` 边界说明

## 状态

**状态：目录已存在，能力仍处于规划中**

关于 post-MVP 多智能体软件开发架构本身，请先阅读 [`docs/agent-architecture.md`](./agent-architecture.md)。本文只回答一个更窄的问题：

> 在 `leader -> manager -> worker` 这套架构下，`src/voidcode/agent/` 这层应该承载什么，不应该承载什么？

`src/voidcode/agent/` 目录现在已经存在，但当前仍只是一个文档化的声明层；仓库**尚未实现真实的 multi-agent 执行语义**。

## 为什么需要这份文档

在当前 MVP / pre-MVP 阶段，VoidCode 已经形成了比较清晰的 runtime-centric 架构：

- `runtime/` 是系统控制面
- `graph/` 负责执行循环与步骤推进
- `tools/`、`hook/`、`skills/`、`mcp/`、`provider/` 是能力层

随着后续 multi-agent 成为明确方向，仓库需要一个单独的边界来承载“预定义 agent / manager / worker type 的声明式配置”，避免把这类配置散落到 runtime、graph 或客户端侧。

## 当前现实

当前仓库的真实情况是：

1. `VoidCodeRuntime` 仍是系统控制面，负责 session、approval、permission、persistence、event、transport、tool execution 与 capability lifecycle。
2. `graph/` 仍是 orchestration / execution layer，而不是产品级治理边界。
3. provider-backed execution path 当前由 runtime 直接驱动，并不依赖 LangGraph。
4. `src/voidcode/agent/` 当前只是声明层，不是独立 runtime。
5. 真正的 `leader -> manager -> worker` 执行语义仍未实现。

因此，`voidcode.agent` 的设计目标不是引入第二套 runtime，而是补上“声明层”。

## `voidcode.agent` 应该承载什么

`voidcode.agent` 作为**预定义 agent / manager / worker type 与 preset/configuration 的声明层**，负责描述一个角色或执行单元是什么、默认携带哪些能力绑定，而不是负责执行它。

建议它承载的内容包括：

- `leader` preset
- 各 phase 的 `manager` preset
- `worker_type` catalog / template
- prompt / profile 定义
- hook 绑定
- skill 绑定
- MCP server / profile 绑定
- tool allowlist / default tool set
- provider / model preference metadata

这些内容应当保持为**声明式配置或类型化定义**，供 runtime 解析与消费。

当前仓库里已经存在的 `src/voidcode/agent/README.md` 与 `src/voidcode/agent/<role>/README.md`，应被理解为这层声明边界的文档化外壳，而不是独立 agent runtime 的证据。

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
- parent / child session relationship
- delegated task lifecycle truth

如果未来要支持“leader 调用 manager、manager 再调度 worker，并在完成后收到通知”的协作语义，那么以下能力也必须首先成为 runtime truth，而不能悬空挂在 `agent/` 或 hook 文档上：

- background task 状态机
- task completion / failure / cancellation / timeout lifecycle
- leader / manager notification 路径
- background result retrieval / transcript recovery

换句话说，`agent/` 可以决定“一个角色默认想带什么配置”，但不能决定“系统最终怎样执行、治理和恢复它”。

## 为什么现在不需要把 LangGraph 当成前提

当前并不需要把 LangGraph 作为 `voidcode.agent` 的前置条件，原因有三点：

1. provider-backed execution path 已经证明：runtime 可以在不依赖 LangGraph 的前提下驱动真实执行路径。
2. 当前最需要收口的仍然是 `leader -> manager -> worker` 所需的 capability substrate，而不是 graph-first 的多智能体工作流。
3. 如果在这些能力层尚未稳定时就把多智能体主骨架建立在 LangGraph 之上，容易把还不稳定的执行语义过早固化进 workflow 结构中。

这并不意味着 LangGraph 未来没有价值。它仍然适合：

- 复杂 branching / retry tree
- manager / worker 协作
- graph-shaped orchestration

但在当前阶段，`voidcode.agent` 的存在不应依赖这些能力已经落地。

## 分阶段推进建议

### Phase 0：先文档化边界

先明确 `voidcode.agent` 的职责与非职责，避免未来把 agent 配置、runtime 治理和 graph orchestration 混在一起。

### Phase 1：引入薄声明层

先让 `src/voidcode/agent/` 只承载：

- `LeaderDefinition` / preset
- `ManagerDefinition` / preset
- `WorkerTypeDefinition` / template
- prompt/profile
- tool allowlist
- hook/skill/MCP/provider 引用

这一阶段不做 multi-agent runtime，不做 agent-to-agent messaging，不做复杂 supervisor。

文档与声明可以优先落在：

- `src/voidcode/agent/README.md`
- `src/voidcode/agent/<role>/README.md`

这些文件用于描述角色 preset、本地权限倾向、建议 skills / hooks / MCP profile 以及与 runtime 的边界，但不代表这些角色当前已经拥有独立 runtime 实现。

### Phase 2：由 runtime 解析声明层

让 runtime 在现有 execution path 中能够解析和应用 agent / manager / worker type preset，同时继续保持 runtime 对 approval、permission、event、persistence 的控制。

当前实现已经完成 `leader` preset 的第一阶段 runtime-managed slice：`leader` 可以映射到 provider-backed single-agent 主路径，向 provider 注入 `prompt_profile`，应用 agent-scoped model / execution engine / provider fallback，收窄可见与可调用工具，并让 manifest `skill_refs` / agent-scoped skills 进入本次运行的 runtime-managed skill application。相关可执行配置会持久化到 session runtime config，以便 resume 保持同一 agent truth。其他内置角色仍是 declaration-only preset；runtime 可以解析它们的声明 shape，但不会把它们作为 active execution agent 运行。

当三层架构真正进入实现阶段时，也应延续这个原则：

- `agent/` 负责定义 `leader` / `manager` / `worker_type`
- `runtime/` 负责决定它们何时、如何、以什么治理语义被执行

### Phase 3：再评估 multi-agent orchestration

只有当 capability substrate 已经稳定、并且真实出现 multi-agent workflow 需求时，再决定是否引入更重的 orchestration 机制。

## 明确非目标

本文档明确**不**主张：

- 当前已经实现 multi-agent
- 当前必须使用 LangGraph 才能进入 agent 方向
- 把 runtime 的控制面职责迁移到 graph 或 agent
- 让客户端直接调用工具或绕过 runtime
- 在当前阶段就实现完整的 `leader -> manager -> worker` delegation runtime

## 结论

`voidcode.agent` 更适合作为一个**薄的、声明式的 agent 定义层**，而不是新的 runtime。当前最合理的方向是：

- 继续保持 runtime-centric 架构
- 先补 `leader -> manager -> worker` 所需的 capability substrate
- 再让 `agent/` 作为这套架构的声明与模板层进入系统
- 等真实 multi-agent 需求与能力层稳定后，再决定是否扩大 graph-based orchestration 的作用范围

这样可以在不破坏现有执行边界的前提下，为未来 multi-agent 留出清晰且可演进的位置。
