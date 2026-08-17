# Workflow 组合设计

## 状态

- 状态：proposed
- 范围：design-only
- 目标仓库：`voidcode`

## 背景与动机

VoidCode 当前的 "workflow" 概念散落在多个互不知晓的子系统里：`WorkflowMode` 枚举、`HookPreset` 声明、hook preset 合并胶水、命令 frontmatter → workflow_mode 的解析路径、以及 `build_prompt_assembly_plan` 里的固定 prompt 槽位。这些子系统各自维护一份"这个请求处于什么 workflow、应该注入什么指导文本"的知识，彼此之间没有共享契约。

对 Oh My Pi / OMP 的调研（见 `docs/oh-my-pi-comparison-priorities.md`）得出两个结论：

1. **耦合过高**：同类 guidance 注入逻辑被复制成 N 条平行硬编码通道，新增一种 policy 需要同时改多个文件。
2. **可组合的缝已经存在但被绕过**：`RuntimeContextTransformRegistry` 已经是一个带 priority / ordering / injection metadata 的组合点，但 `workflow_mode`、`skill`、`memory`、`agent` 各自绕开它，走独立槽位。

本文档记录这次调研的决策与架构结论，作为后续删改的依据。文档只记录决策与目标形态，不包含实现代码。

## 核心结论

1. **删除** `/start-work` 命令及其 `workflow_plan` hydration 逻辑，**删除** continuation loop 全套（命令、类型、存储表、runtime 暴露方法、prompt effect handler），且**不需要任何先后兼容**。
2. 删除之后剩下的 workflow 形式统一收口到已有的 `RuntimeContextTransformRegistry`：mode、skill、memory-usage 等都变成 registry 里的命名 provider，由 registry 的 priority / ordering / injection 机制决定如何进入 prompt。
3. 一句话概括目标形态：**命名组合，不是新类型**——与 OMP 的 capability / registry / 事件模型一致，但不复制 OMP 的功能数量。

## 已定决策

以下决策已定，原样记录。执行删除时以本节为准。

### 决策 1：删除 `/start-work` 命令与 `workflow_plan` hydration

- 删除 `src/voidcode/command/loader.py` 中的 `CommandDefinition(name="start-work", ..., workflow_mode="sustain")` 内置命令。
- 删除 `src/voidcode/runtime/command_effects.py` 中的 `hydrate_start_work_prompt`（从 plan session metadata 读取 `workflow_plan`、渲染 `<workflow_plan_artifact>` 注入 prompt、并把 `workflow_plan` snapshot 写回 metadata 的逻辑），以及 `apply_runtime_command_effects` 里 `command_name == "start-work"` 的分支。
- 同属这条硬编码通道、随之一并清理的附属逻辑：
  - `src/voidcode/runtime/command_effects.py` 的 `session_with_command_artifacts` 中由 `/plan` 写入 `workflow_plan` snapshot 的部分（`source="plan-command"`）——其唯一消费者是 `/start-work` 的 hydration。
  - `src/voidcode/runtime/contracts.py` 中 `workflow_plan` 作为 internal runtime metadata 字段的校验（`validate_runtime_request_metadata`）。
  - `src/voidcode/runtime/resume.py` 的 `_response_with_refreshed_workflow_plan` 刷新路径。
  - `src/voidcode/command/loader.py` 中 `/plan` 模板里的 "Start-work handoff section" 引导语。

### 决策 2：删除 continuation loop 全套

- `src/voidcode/runtime/task.py`：删除 `ContinuationLoopStatus`、`ContinuationLoopVerificationStatus`、`ContinuationLoopStrategy`、`ContinuationLoopRef`、`ContinuationLoopState`、`StoredContinuationLoopSummary` 等类型，以及 `validate_continuation_loop_id`、`parse_continuation_loop_strategy`、`parse_continuation_loop_verification_status`、`is_continuation_loop_terminal`、`is_continuation_loop_transition_allowed` 等辅助函数。
- `src/voidcode/runtime/storage.py`：删除 `continuation_loops` 表（schema 声明、`CREATE TABLE IF NOT EXISTS continuation_loops`、`continuation_loops_workspace_idx` 索引、`storage_sequences` 里的 `continuation_loops` scope），`SessionStore` protocol 与 `SqliteSessionStore` 上的 `create_continuation_loop` / `load_continuation_loop` / `list_continuation_loops` / `record_continuation_loop_iteration` / `mark_continuation_loop_verification_pending` / `mark_continuation_loop_verified` / `mark_continuation_loop_verification_failed` / `mark_continuation_loop_terminal` / `cancel_continuation_loop` 等全部方法。
- `src/voidcode/runtime/service.py`：删除 `start_continuation_loop`、`load_continuation_loop`、`list_continuation_loops`、`record_continuation_loop_iteration`、`mark_continuation_loop_verification_pending`、`mark_continuation_loop_verified`、`mark_continuation_loop_verification_failed`、`mark_continuation_loop_terminal`、`cancel_continuation_loop` 等全部暴露方法。
- `src/voidcode/command/loader.py`：删除 `/continuation-loop`、`/intensive-loop`、`/cancel-continuation` 三个内置命令（含它们各自的 `workflow_mode` 绑定：sustain / deep_work / 无）。
- `src/voidcode/runtime/command_effects.py`：删除 continuation loop 的 prompt effect handler——`apply_runtime_command_effects` 中对三个命令的分支、`continuation_loop_metadata`、`render_intensive_loop_prefix`、`_cancel_requested_continuation_loop`、`INTENSIVE_LOOP_MAX_ITERATIONS`，以及 `RuntimeCommandEffectHost` protocol 中的 `start_continuation_loop` / `cancel_continuation_loop` / `list_continuation_loops` 方法声明。
- `src/voidcode/runtime/contracts.py`：删除 `RuntimeContinuationLoopMetadata` 与 `validate_runtime_continuation_loop_metadata`。
- `src/voidcode/runtime/__init__.py`：清理相关导出。
- `src/voidcode/command/README.md`：删除三个命令的表格行。
- 相关测试同步删除或改写：`tests/unit/command/test_command_registry.py`、`tests/unit/runtime/test_command_resolution.py`、`tests/unit/runtime/test_continuation_loop_storage.py`、`tests/unit/runtime/test_runtime_service_extensions.py` 中的对应用例。

### 决策 3：不需要任何先后兼容（硬约束）

本次删除**不需要任何先后兼容**，这是硬约束，执行时不得弱化：

- 不做 schema migration 兼容：`continuation_loops` 表直接删，不写迁移、不保留废弃表。
- 不保留 shim / 别名：不保留 `workflow_plan` 字段的 deprecated 透传、不保留续 loop 类型或方法的转发层、不保留任何 "removed in future" 标记。
- 不向后兼容旧持久化数据：旧 workspace 中已存在的 continuation loop 数据与 `workflow_plan` metadata 直接视为失效；旧库按新 schema 初始化或重建即可，不需要读旧数据。

## 当前耦合诊断

**诊断结论：耦合高，且是错误的耦合类型——不是"可组合组件共享契约"，而是"N 条平行的硬编码通道"。**

### 证据 1：`build_prompt_assembly_plan` 的硬编码槽位

`src/voidcode/runtime/prompt_assembly.py` 的 `build_prompt_assembly_plan` 是 guidance 注入的唯一出口，但它的拼装规则是写死的：

- 函数签名暴露 **6 个字符串型命名槽位**：`agent_prompt_context`、`workflow_mode_prompt_context`、`skill_prompt_context`、`todo_prompt_context`、`workspace_memory_context`、`continuity_summary`，每个槽位对应函数体内一条固定 source 的 `append_system`（如 `workflow_mode_prompt` + `layer="mode_policy"`、`skill_prompt` + `layer="skills"`、`runtime_todo_state` + `layer="task_state"`）。
- 函数体内另有 **约 20 处携带固定 source 标签的 `append_system` / `append_block` 调用点**（含分支与循环）：`runtime_base_safety`、`agent_identity_header`、`agent_capability_block`、`runtime_instruction_precedence`、`runtime_memory_usage_guidance`、`runtime_tool_policy_summary`、`runtime_dynamic_boundary`、`runtime_environment_stable` / `runtime_environment_dynamic`、prompt activation、preserved system segments、pending state、artifact references、context-transform injections 等。
- 其中只有 `context_transform_result.injections` 这一处是遍历 registry 结果；其余都是"这个槽位填什么、什么顺序、什么 source"在函数里写死的。

结果是：新增一种 guidance（比如某个新的 mode 注入）就要在调用方和本函数里各加一个参数，而不是注册一个 provider。

### 证据 2：workflow 概念散在 5 个子系统

- **`WorkflowMode`**（`src/voidcode/runtime/workflow.py`）：`WorkflowMode` dataclass 携带硬编码的 `hook_preset_refs: tuple[str, ...]`；内置 mode（default / deep_work / review / product / sustain）各自写死 refs 元组（如 deep_work → `role_reminder, delegated_task_timing_guidance, background_output_quality_guidance, ...`）。
- **`HookPreset`**（`src/voidcode/hook/presets.py`）：preset 声明携带 `kind`（guidance / guard / continuation）、`event_scopes`、`allowed_actions`，内置 preset 如 `delegation_guard` 声明 `allowed_actions=("observe", "report", "cancel", "guidance")`。
- **合并胶水**（`src/voidcode/runtime/hook_preset_metadata.py`）：`hook_preset_refs_for_mode_and_agent` 把 mode refs 与 agent refs 合并去重，`service.py::_build_hook_preset_snapshot` 再解析成 snapshot。
- **命令 frontmatter → workflow_mode 的独立解析路径**（`src/voidcode/command/loader.py` + `src/voidcode/runtime/service.py`）：`CommandDefinition` 上有 `workflow_mode` 字段（如 `/plan` → `product`、`/start-work` → `sustain`），`service.py::_workflow_mode_resolution_for_request_metadata` 从 command metadata / CommandDefinition / metadata `workflow_mode` / workflow snapshot 继承四条来源里解析 mode，再经 `_workflow_mode_prompt_context` 渲染成一段文本填入 `workflow_mode_prompt_context` 槽位。
- **prompt 槽位**（`src/voidcode/runtime/prompt_assembly.py`）：上述 6 个命名槽位之一。`src/voidcode/command/README.md` 甚至明确写了 "Workflow mode is assembled through the dedicated `workflow_mode_prompt_context` slot"，把这个硬编码路径当成既有约定。

这 5 个点各自持有"workflow 是什么"的部分事实：mode 持有 refs 元组，preset 持有 kind / scopes / actions，胶水持有合并顺序，命令解析持有来源优先级，prompt 组装持有注入位置。任何一个环节加内容，其他环节都要跟着动。

### 证据 3：关键矛盾——registry 已经存在，却被绕过

`src/voidcode/runtime/context_transforms.py` 已经具备完整的组合机制：

- `RuntimeContextTransformRegistry`：按 `(priority, provider_id)` 排序的 provider 集合，支持 `filtered()`（按 refs 裁剪）与统一的 `build_result()`。
- provider 带显式 metadata：`HookPresetGuidanceTransformProvider`（`priority=100`）、`RuntimeFileRulesTransformProvider`（`priority=200`）、`DirectoryReadmeContextTransformProvider`（`priority=250`）。
- `RuntimeContextTransformInjection` 携带 `role` / `content` / `metadata`，`service.py` 通过 `build_provider_context_transform_result` 把结果注入 `build_prompt_assembly_plan`。

矛盾在于：**hook presets 已经走这条可组合通道**（`HookPresetGuidanceTransformProvider`），而 `workflow_mode`、`skill`、`memory`、`agent` 各自绕开 registry，走独立硬编码槽位。也就是说，仓库里同时存在"正确的组合缝"和"N 条平行通道"，且后者占多数。

## OMP 参考做法（供对照，不主张复制功能数量）

OMP 的答案不是更多类型，而是一个统一 capability / registry 模型 + 事件总线 + context-transform 链：

- **一切皆 capability，一个发现模型**：provider 发现 + priority 排序 + first-wins 去重；`disabledProviders` 一个开关就能切断整个 provider，而不是逐槽位删文本。
- **一个 Extension API（`pi`）组合一切**：`pi.on` + `registerTool` + `registerCommand` + renderer，工具、命令、指导、渲染都从同一个扩展点接入。
- **事件总线 + mutation 语义**：handler 返回 `{block}` / `{input}` / `{content}` / `{messages}` 表达意图；`tool_result` 是 middleware 链式处理，而不是每个事件类型一套独立代码路径。
- **mode 是运行时状态 mutation，不是独立枚举**：vibe mode 就是一条 slash command → 缩减 toolset + 注入指令 + 装载 tools，mode 本身没有独立的类型系统。

一句话：**OMP 的 workflow 形式是"命名组合，不是新类型"。**

VoidCode 不需要复制 OMP 的机制数量；它需要的是把"同一套组合机制"用在所有 guidance 注入上——这正是本仓库 `RuntimeContextTransformRegistry` 已经证明可行的方向。

## 目标形式

删除 `/start-work` 与 continuation loop 之后，剩下的 workflow 相关代码按下述方向收口：

1. **policy / guidance 类槽位全部收进 `RuntimeContextTransformRegistry`**：`workflow_mode_prompt_context`、`skill_prompt_context`、memory-usage 指导（`_STRICT_MEMORY_USAGE_GUIDANCE`）、workspace memory recall（`workspace_memory_context`）都改为 registry 里的 provider；file-rules、directory-readme、hook preset guidance 保持现状（它们已经在 registry 里）。每个 provider 自带 priority / ordering / injection metadata，通过现有 `RuntimeContextTransformInjection` 结构进入 prompt。
2. **`WorkflowMode` 降级为命名 transform 组合**：`mode = refs: [...] + description`，经 registry 解析成 injection，不再有独立 `workflow_mode_prompt_context` 槽位。mode 只声明"引用哪些 provider、附带什么描述"，不再携带注入位置与顺序知识。
3. **`build_prompt_assembly_plan` 对 policy tier 只做遍历 `context_transform_result.injections`**：删除 6 个命名槽位参数和对应的固定 `append_system` 调用，policy tier 的内容与顺序完全由 registry 决定；`prompt_assembly.py` 不再需要知道"有哪些 policy"。
4. **结构性槽位保留，不进 registry**：`base_safety`（`_BASE_SAFETY_GUIDANCE`）、`env_card`（stable / dynamic）、`identity header`（`agent_identity_header`）、capability block、tool policy summary、dynamic boundary、todo / task 状态、continuity summary 等属于运行时结构性内容，不是 policy 注入，继续由 `build_prompt_assembly_plan` 直接拼装。

## 残留问题

以下问题在收口时必须诚实面对，不能假装不存在：

- **`HookPreset` 的 `event_scopes` / `allowed_actions` / `kind="guard"` 目前是空标签**。preset materialize 之后只是文本：`service.py::_hook_preset_context_from_metadata` 调用 `ResolvedHookPresetSnapshot.guidance_context()` 渲染成一段 system 文本，`_build_hook_preset_snapshot` 的 payload 明确标记 `materialization: "guidance_only"`、`authority: "non_authoritative"`；没有任何事件总线消费 `event_scopes` / `allowed_actions`，`delegation_guard` 声明的 `("observe", "report", "cancel", "guidance")` 不会拦截任何事件。
- **收进 registry 时必须二选一**：
  - (a) 承认 `HookPreset` 就是 guidance 文本 provider，去掉 `guard` / `cancel` 语义与 `event_scopes` / `allowed_actions` 字段；
  - (b) 真的把 `event_scopes` 接到事件总线去拦截事件，让 `allowed_actions` 具有实际语义。
- **与 OMP 的对照**：OMP 的事件总线 handler 返回 `{block}` 可以真正拦截 `tool_call`；VoidCode 目前的 `kind="guard"` 只是"纸上的 guard"。如果选 (a)，文档与代码里都不应再声称存在 guard 能力。

## 非目标

沿用仓库现有边界（口径见 `docs/deliberate-omissions.md` 与 `docs/oh-my-pi-comparison-priorities.md`）：

- **不复制 OMP 的功能数量**：不追平 OMP 的 60+ provider、plugin / marketplace、fork / branch / export / share 等产品成熟度。
- **不做任意拓扑 multi-agent**：运行时只拥有 leader + 固定 child presets 的委托执行，不做任意 orchestration graph。
- **不做 agent-to-agent bus**：agent 之间不建立直接通信通道，协调继续走 runtime-owned 的 parent / child session 关联与背景任务契约。
- **不引入 Rust native core**：不复制 OMP 约 80k 行的 native 层，Python runtime 与外部系统工具的边界保持不变。
- 遵循 `docs/deliberate-omissions.md` 的指导原则：新增 tool / agent role / config knob 的准入门槛保持高位；本设计是在**减少** runtime 的类型与通道数量，而不是增加。

## 落地影响面（删改清单）

执行"已定决策"时按以下清单删改；实现以 `src/` 下实际代码为准。

| 文件 | 删除 / 修改内容 |
| --- | --- |
| `src/voidcode/command/loader.py` | 删除 `/start-work`、`/continuation-loop`、`/intensive-loop`、`/cancel-continuation` 四个 `CommandDefinition`；清理 `/plan` 模板中的 handoff 引导语 |
| `src/voidcode/runtime/command_effects.py` | 删除 `hydrate_start_work_prompt`、`apply_runtime_command_effects` 中的 start-work 与 continuation-loop 分支、`continuation_loop_metadata`、`render_intensive_loop_prefix`、`_cancel_requested_continuation_loop`、`INTENSIVE_LOOP_MAX_ITERATIONS`；`RuntimeCommandEffectHost` protocol 同步瘦身；`session_with_command_artifacts` 中 `workflow_plan` 写入 |
| `src/voidcode/runtime/task.py` | 删除 continuation loop 类型族与辅助函数 |
| `src/voidcode/runtime/storage.py` | 删除 `continuation_loops` 表 / 索引 / sequence 与全部相关方法（protocol + SqliteSessionStore） |
| `src/voidcode/runtime/service.py` | 删除 9 个 continuation loop 暴露方法与 `workflow_plan` 相关 plumbing |
| `src/voidcode/runtime/contracts.py` | 删除 `RuntimeContinuationLoopMetadata` 与 `workflow_plan` / `continuation_loop` metadata 校验 |
| `src/voidcode/runtime/resume.py` | 删除 `_response_with_refreshed_workflow_plan` |
| `src/voidcode/runtime/__init__.py` | 清理相关导出 |
| `src/voidcode/command/README.md` | 删除三个 continuation 命令的表格行；`workflow_mode_prompt_context` 槽位描述随目标形式更新 |
| `tests/unit/command/test_command_registry.py` 等 | 同步删除 / 改写相关用例 |

目标形式的收口（mode / skill / memory-usage 进 registry、`build_prompt_assembly_plan` 去槽位化）作为删除之后的下一步单独执行，不在本次删除范围内。
