# Mode 组合设计

## 状态

- 状态：proposed
- 范围：design-only（本文件只记录设计与决策，不包含实现代码，执行删除时以 `src/` 下实际代码为准）
- 目标仓库：`voidcode`
- 关联文档：`docs/workflow-composition-design.md`（workflow 收口的前置文档）、`docs/oh-my-pi-comparison-priorities.md`、`docs/deliberate-omissions.md`、`docs/contracts/workflow-presets.md`、`docs/contracts/background-task-delegation.md`、`docs/contracts/runtime-lifecycle-hooks.md`

## 背景与动机

VoidCode 当前存在三个撞名的 "mode" 概念，各自持有"这个请求处于什么模式、应该有什么行为"的部分事实，彼此之间没有共享契约：

1. **`RuntimeMode`**（`src/voidcode/runtime/mode.py`）：`Literal["normal", "analyze", "plan"]`。这是唯一真正接行为的内核——`analyze`/`plan` 都只强制 `read_only=True`（`runtime_read_only_from_metadata` 里 `mode in {"analyze", "plan"}` → True），`analyze` 是死值；`permission.py::is_plan_mode_blocked`（约 line 86）在 `mode == "plan"` 时 deny 所有非只读工具。
2. **`WorkflowMode`**（`src/voidcode/runtime/workflow.py`）：5 个枚举值 `default / deep_work / review / product / sustain`，每个只有 `id + description + hook_preset_refs`。materialize 后只产出 `service.py::_workflow_mode_prompt_context`（约 line 1254）里的一行文本 "Workflow mode: X. ... Guidance only; does not expand tool permissions or agent scope."，**零行为、空转**。删掉 `/start-work` 等命令后，`deep_work` / `review` / `sustain` 已无触发源，只剩 `product`（/plan）和 `default`。
3. **`workflow_snapshot.py`**：v2 版本化持久化快照，校验 `requested.workflow_mode == effective.mode == snapshot.mode`（`validate_workflow_snapshot`），存储于 metadata `workflow` / `runtime_config.workflow` / `agent_capability_snapshot.workflow` 三处。

调研结论（依据 `docs/oh-my-pi-comparison-priorities.md` 与 OMP 源码结论，见下文"OMP 参考"）：OMP 的 mode 不是枚举 + switch，而是**命名的正交开关组合**——mode 名只是状态标记，效果由一个聚合点（`effectiveAgent` / `buildSessionContext`）在解析时一次性翻转多个正交开关。VoidCode 应当收敛为同一个形态：**声明 + 聚合 + 生命周期 + 持久化四者解耦**。

## 已定决策（原样记录，执行时以本节为准）

以下决策已定，本设计文档不修改它们，只负责把目标形态、聚合点、影响面写清楚。

1. **删除 `RuntimeMode.analyze`**：`mode.py` 的 `RuntimeMode` 只留 `"normal"` / `"plan"`；`parse_runtime_mode`、`runtime_mode_from_metadata`、`runtime_read_only_from_metadata`、`contracts.py` 的包装函数与错误消息、`policy.py` / `session.py` 的快照校验集合、`cli/app.py` 的 `--runtime-mode` Choice、`service.py::_stricter_runtime_mode` 的 rank 表全部同步收窄。
2. **删除 `WorkflowMode` 全族**：`src/voidcode/runtime/workflow.py`（`WorkflowMode` / `WorkflowModeResolution` / `resolve_workflow_mode` / `_BUILTIN_WORKFLOW_MODES`）与 `src/voidcode/runtime/workflow_snapshot.py` 整体删除；`service.py` 的 `_workflow_mode_prompt_context` 硬编码槽位、`_workflow_mode_resolution_for_request_metadata`、`_workflow_snapshot_for_resolution`、`_workflow_snapshot_with_effective_mode`、`_metadata_without_workflow_mode`、`_validate_explicit_workflow_mode_metadata`、`_restore_explicit_workflow_mode`、`_config_workflow_mode_resolution` 缓存、`_assemble_context` 里从 snapshot 重建 workflow 文本的回退分支一并删除；`hook_preset_metadata.py::hook_preset_refs_for_mode_and_agent` 的 mode 部分删除（只留 `hook_preset_refs_for_agent`）；`contracts.py` 的 `workflow_mode` / `workflow` metadata 字段与校验删除。
3. **`/plan` 命令绑定改设 `RuntimeMode.plan`**：`src/voidcode/command/loader.py` 内置 `plan` 命令的 `workflow_mode="product"` frontmatter 绑定移除，改设 `RuntimeMode.plan`（命令声明新增 `mode` 字段或等价机制，见"落地影响面"）。
4. **不引入 OMP 的 append-only session tree**：VoidCode 用 SQLite + session metadata 持久化，不用 tree 结构。
5. **方向：组合形式**——mode 是"声明 + 聚合 + 生命周期 + 持久化"四者解耦的命名正交开关组合，对照 OMP 的 `effectiveAgent` 聚合点原则。

## 现状核实与耦合诊断（证据）

以下符号均经实际代码核实（行号为核实时的位置，执行时以代码为准）。

### 证据 1：`mode.py` 是唯一行为内核，且 `analyze` 是死值

`src/voidcode/runtime/mode.py`：

- `type RuntimeMode = Literal["normal", "analyze", "plan"]`；`parse_runtime_mode` 三个分支；
- `runtime_read_only_from_metadata`：`mode in {"analyze", "plan"}` → 返回 `True`。`analyze` 与 `plan` 行为完全相同，无任何独立触发源（CLI `--runtime-mode` 可选，命令/前端不产生 `analyze`）。

`analyze` 的死值散布在 6 处独立校验/推导副本里：`contracts.py`（包装函数与错误消息 "must be 'normal', 'analyze', or 'plan'"）、`policy.py::_validate_runtime_policy_snapshot`（line 467）、`session.py::normalize_persisted_session_metadata`（line 138）、`tool_scope.py::runtime_mode`（line 123）、`cli/app.py`（`--runtime-mode` Choice，line 2713）、`service.py::_stricter_runtime_mode`（rank 表，line 5903）。

### 证据 2：read-only 语义有两套平行的推导与执行点，且语义不一致

- **approval 层**：`permission.py::is_plan_mode_blocked`（line 86）只认 `runtime_mode == "plan"`，不认显式 `read_only` metadata。唯一调用链：`resolve_permission`（line 122）→ `service.py::_resolve_permission`（line 3135 传 `runtime_mode=runtime_mode_from_metadata(session.metadata)`）。
- **tool policy 层**：`tool_scope.py::RuntimeToolScopeResolver` 自己复制了一份推导（静态 `runtime_mode` / `runtime_read_only` / `effective_read_only`，line 122-140），按 `read_only`（mode 或显式 metadata）deny 非只读工具，且对 `shell_exec` 有显式放行特例（line 54-58）。执行点在 `run_loop.py`（line 696 / 1878 / 2627）经 `service._tool_policy_denial` 消费，另有 `service.py::_tool_registry_for_policy`（line 1610）、`_tool_policy_decision`（line 1638）、`_hook_execution_policy`（line 2944-2946）等消费者。

即：同一份 "mode → read_only" 推导逻辑存在于 `mode.py` 与 `tool_scope.py` 两份；且显式 `read_only=True` 的请求在 approval 层不被 deny、在 tool policy 层被 deny。这是本次重构要消灭的散点。

### 证据 3：`WorkflowMode` 是零行为的空转声明

`workflow.py` 的 `WorkflowMode(id, description, hook_preset_refs)` 与 `_BUILTIN_WORKFLOW_MODES` 5 个值（`deep_work` / `review` / `product` / `sustain` 携带 `hook_preset_refs` 元组）。唯一行为产出是 `service.py::_workflow_mode_prompt_context`（line 1254-1258）：

```python
return f"Workflow mode: {mode.id}. {mode.description} Guidance only; does not expand tool permissions or agent scope."
```

该文本经 `build_prompt_assembly_plan` 的 `workflow_mode_prompt_context` 槽位（`prompt_assembly.py` line 367，`append_system` source=`workflow_mode_prompt`、layer=`mode_policy`，line 498-503）注入。**零行为**：不改变权限、不改变工具可见性、不改变 spawn 策略。

### 证据 4：`service.py` 的 workflow mode 解析是五段式多点耦合

`_workflow_mode_resolution_for_request_metadata`（line 1206）从 4 条来源解析（command payload `workflow_mode` → `CommandDefinition.workflow_mode`（`command/loader.py` 内置 `plan` 命令绑定 `workflow_mode="product"`，line ~50）→ metadata `workflow_mode` → 继承的 workflow snapshot），产出 `WorkflowModeResolution` 后流向：

- `_workflow_mode_prompt_context` → prompt 槽位（line 2618-2621）；
- `_build_hook_preset_snapshot`（line 6749-6754）→ `hook_preset_refs_for_mode_and_agent`（`hook_preset_metadata.py`）合并 mode refs + agent refs；
- `_workflow_snapshot_for_resolution` / `_workflow_snapshot_with_effective_mode` → 持久化 `workflow` snapshot（含 `runtime_config.workflow`，line 6881-6884）；
- `_config_workflow_mode_resolution` 实例缓存（line 690）。

新增一种 workflow mode 要同时动 `workflow.py` + `hook_preset_metadata.py` + `service.py` 解析 + `prompt_assembly.py` 槽位 + snapshot 校验，外加 `contracts.py` 的 metadata 校验（`validate_runtime_request_metadata` 里经 `resolve_workflow_mode` 校验 `workflow_mode`，line 490-504）与委派继承 `_stricter_runtime_mode` rank 表（line 5902-5904）。`docs/contracts/workflow-presets.md` 甚至把这套硬编码路径写成了对外契约。

### 证据 5：组合缝已经存在，却被绕过

`src/voidcode/runtime/context_transforms.py` 已具备完整组合机制：

- `RuntimeContextTransformRegistry`：按 `(priority, provider_id)` 排序（`ordered_providers`），支持 `filtered(provider_ids)` 裁剪与统一 `build_result`；
- 已有 3 个 provider：`HookPresetGuidanceTransformProvider`（priority 100）、`RuntimeFileRulesTransformProvider`（200）、`DirectoryReadmeContextTransformProvider`（250）；
- `RuntimeContextTransformInjection` 携带 `role` / `content` / `metadata`；`validate_runtime_context_transform_refs` 已能按 registry 校验 refs 合法性；
- hook preset guidance 已经走这条通道（`build_provider_context_transform_result` 注入 `build_prompt_assembly_plan`）。

矛盾在于：hook preset 走 registry，而 `workflow_mode`、`skill`、`memory`、`agent` 各自绕开它走 `build_prompt_assembly_plan` 的硬编码命名槽位。mode 重构只需把 `workflow_mode` 这一条收进来；skill / memory 的收口由 `docs/workflow-composition-design.md` 的目标形式单独覆盖，不在本文范围。

### 证据 6：持久化底座已经具备

- `contracts.py::_STABLE_RUNTIME_REQUEST_METADATA_KEYS`（line 91-109）已含 `"mode"`（line 98）与 `"read_only"`（line 99）；`RuntimeRequestMetadata` TypedDict 已含 `mode` / `read_only`（line 57-58）。
- `session.py::session_metadata_for_persistence`（line 209-229）已把 `persisted["mode"] = mode`、`persisted["read_only"] = read_only` 写入 session 顶层 metadata。
- 委派继承已按"取更严格者"合并：`service.py::_metadata_with_inherited_child_policy`（line 5887-5897）同时合并 mode 与 read_only。
- 删除 `workflow_snapshot.py` 后，`mode` 的持久化落在既有的 session metadata `mode` 字段（+ 派生 `read_only`），不需要任何新存储机制。

### 证据 7：委派相关的两处设施

- `src/voidcode/runtime/delegation_routing.py`：实际内容是 delegated child 的 model / provider fallback 路由（`delegated_model_for_route_from_configs`、`provider_fallback_for_agent_selection`），**不是** spawn 控制。固定 child preset 的枚举在 `agent_capability.py`、`policy.py::_ALLOWED_CHILD_PRESETS`、`service.py::_EXECUTABLE_SUBAGENT_PRESETS`、`task.py::_CALLABLE_SUBAGENT_PRESETS`（advisor / explore / researcher / worker / product）。`RuntimeToolScopeResolver.delegation_policy_error`（`tool_scope.py`）按 child 的 manifest allowlist 校验工具。
- `src/voidcode/runtime/background_tasks.py`：`RuntimeBackgroundTaskSupervisor` 已存在（队列、并发、重试、取消、shutdown drain），是 Phase 2 生命周期设施的基础。

## OMP 参考（结论引用，不展开）

OMP 的 mode 是会话树上的 `mode_change` entry（append-only，tree-aware，可回放），效果由一个聚合点（`effectiveAgent` / `buildSessionContext`）在解析时一次性翻转多个正交开关：

- **plan mode**：翻转 toolset 限制 + prompt 前置 + 清空 child spawns + 清空 prewalk；
- **vibe mode**：翻转 toolset 缩减 + 注入 director 指令 + 装 worker scope + 互斥 + 退出 kill workers；
- **approval mode**：按 read / write / exec tier 决定 auto-approve / prompt。

核心：**mode 名只是状态标记，效果 = 聚合点翻转正交开关的组合**。VoidCode 不复制其机制数量，只采纳这一形态原则（与 `docs/workflow-composition-design.md` 的结论一致）。

---

## 1. 目标形态

单一 `mode` 概念：**命名的正交开关组合，不是枚举值**。mode 是一个声明对象，只描述"激活哪些可复用的机制开关"：

```python
# 概念声明（示意，非实现代码）
@dataclass(frozen=True, slots=True)
class ModeDefinition:
    name: RuntimeMode  # "normal" | "plan"
    description: str  # 人可读说明（也作为 guidance 文本来源）
    read_only: bool = False  # 开关 1：执行姿态
    transform_refs: tuple[str, ...] = ()  # 开关 2：context transform provider refs
    toolset_restriction: tuple[str, ...] | None = None  # 开关 3（可选/未来）：工具白名单/黑名单
    spawn_policy: ... = None  # 开关 4（可选/未来）：spawn/delegation 策略


MODE_DEFINITIONS: dict[RuntimeMode, ModeDefinition] = {
    "normal": ModeDefinition(name="normal", description="Balanced default execution stance."),
    "plan": ModeDefinition(
        name="plan",
        description="Plan mode is active: read-only stance; produce a plan before writing code.",
        read_only=True,
        transform_refs=("mode_guidance",),
    ),
}
```

四个正交开关各自对接已有的、或已预留的机制：

| 开关 | 机制 | 现状 |
| --- | --- | --- |
| `read_only` | permission 层（`permission.py`）+ tool policy 层（`tool_scope.py`）+ policy snapshot / hook execution policy | 已存在，但有两套平行推导（证据 2），收口到聚合点 |
| `transform_refs` | `RuntimeContextTransformRegistry`（priority 排序 + `filtered()` + injection metadata） | 已存在（证据 5），`workflow_mode` 首次收进来 |
| `toolset_restriction` | `tool_scope.py` / `tool_registry.py`（`allowed_by_policy`） | 机制在，未由 mode 驱动；Phase 1 不启用 |
| `spawn_policy` | 固定 child preset + `delegation_routing.py` + background task 基础设施 | 机制在，未由 mode 驱动；Phase 1 不启用 |

**"声明"与"聚合"解耦**：新增 mode = 新增一条 `ModeDefinition` 声明（声明哪些开关），不触碰任何解析/消费代码。"生命周期"（何时进入/退出/继承 mode）与"持久化"（metadata 字段）与声明本身无关。

## 2. 聚合点（核心）

### 2.1 解析流程

一次解析、各处消费。解析是纯函数：`resolve_mode(mode, agent) -> ModeResolution`，不依赖可变全局状态。

```text
request metadata（mode、read_only、agent、command…）
        │
        ▼
resolve_mode(mode, agent)                  ← 唯一聚合点（纯函数）
        │
        ▼
ModeResolution
  ├── read_only: bool                      ← 消费方 1..4
  ├── transform_refs: tuple[str, ...]      ← 消费方 5
  ├── toolset_restriction: ... | None      ← （可选/未来）消费方 6
  └── spawn_policy: ... | None             ← （可选/未来）消费方 7
```

```python
# 概念签名（示意，非实现代码）
@dataclass(frozen=True, slots=True)
class ModeResolution:
    mode: RuntimeMode  # "normal" | "plan"
    read_only: bool
    transform_refs: tuple[str, ...] = ()
    toolset_restriction: tuple[str, ...] | None = None
    spawn_policy: SpawnPolicy | None = None
    source: Literal["command", "metadata", "inherited", "default"] = "default"


def resolve_mode(
    mode: RuntimeMode,  # 已从 metadata 解析出的标量
    agent: RuntimeAgentConfig | None,  # 供 agent 级覆盖（可选，Phase 1 可不接）
) -> ModeResolution:
    definition = MODE_DEFINITIONS[mode]
    return ModeResolution(
        mode=mode,
        read_only=definition.read_only or explicit_read_only,  # 显式 read_only 叠加
        transform_refs=validate_runtime_context_transform_refs(
            definition.transform_refs,
            field_path=f"mode {mode} transform_refs",
        ),
    )
```

解析来源收敛为一条简单链（替代证据 4 的四来源解析）：command 声明 `mode` → request metadata `mode` → 继承（child 从 parent 合并，见 2.3）→ 默认 `"normal"`。`workflow_mode` 字段及其全部来源不复存在。

### 2.2 消费点替换（散点 → 聚合点）

| 现在的消费方式 | 之后 | 位置 |
| --- | --- | --- |
| `is_plan_mode_blocked` 直接读 `RuntimeMode`（`mode == "plan"`） | 改读 `ModeResolution.read_only`（保留 operation_class 纵深防御：非只读工具 + 显式 write/execute 一律 deny） | `permission.py`；`service.py::_resolve_permission` 传 read_only |
| `tool_scope.py` 自己的静态 `runtime_mode` / `runtime_read_only` / `effective_read_only` 副本 | 删除静态副本，`RuntimeToolScopeResolver` 消费同一份推导（共享 `resolve_mode` 或传入的 read_only）；`shell_exec` 放行特例保留并显式记录 | `tool_scope.py` |
| `_workflow_mode_prompt_context` 渲染文本 → `workflow_mode_prompt_context` 槽位 | mode guidance 经 `transform_refs` → `RuntimeContextTransformRegistry.filtered(refs)` → `build_provider_context_transform_result` → prompt 注入；与 hook preset / file rules / directory readme 同一条通道、同一套 priority/ordering/injection metadata | `service.py`、`prompt_assembly.py`（删槽位）、`context_transforms.py`（新增 mode guidance provider） |
| `hook_preset_refs_for_mode_and_agent` 合并 mode refs + agent refs | mode 不再携带 hook preset refs；只留 `hook_preset_refs_for_agent`；`_build_hook_preset_snapshot` 去掉 mode 参数 | `hook_preset_metadata.py`、`service.py` |
| `_stricter_runtime_mode` rank 表（normal/analyze/plan） | mode 继承收敛为 read_only 合并（parent read_only OR child read_only，现有 line 5894-5897 已是这个形态）；mode 字段本身按同样规则取更严格者 | `service.py` |
| workflow snapshot 版本化校验（v2） | 删除；`mode` 是稳定标量，无需快照契约 | `workflow_snapshot.py`、`contracts.py` |

**mode guidance 的 provider 形态**：`mode_guidance` provider（priority 约 150，插在 hook_preset_guidance 100 与 runtime_file_rules 200 之间）沿用 `HookPresetGuidanceTransformProvider` 的既有模式——服务侧从 `ModeResolution` 预渲染 guidance 文本传入 `RuntimeContextTransformRequest`，provider 只负责注入槽位/顺序/metadata。`plan` mode 的 `transform_refs=("mode_guidance",)` 声明其激活；registry 的 `filtered()` 决定哪些 provider 运行。这样"加 mode 的 guidance"不需要新 provider 代码，只改声明。

**语义统一（需显式决定的行为变化）**：收敛后，`read_only` 成为两层共用的唯一门槛。显式 `read_only=True` metadata 的请求此前只在 tool policy 层被 deny（approval 层不拦），收敛后在两层一致。`plan` 模式语义不变（`read_only=True` 且带 guidance 注入）。这是把证据 2 的两套推导合并成一套的必然结果，Phase 1 实现时按此语义执行，不需要向后兼容旧行为（见决策约束）。

### 2.3 生命周期：mode 在请求路径上的位置

- **进入**：request metadata 规范化时（`validate_runtime_request_metadata` 已校验 `mode`，line 425-426）→ 命令绑定（/plan 设 `mode: plan`）或显式 metadata。
- **传播**：session 顶层 metadata `mode` + `read_only`（`session_metadata_for_persistence` 已写，证据 6）；委派时 child 继承 parent 的更严格者（`_metadata_with_inherited_child_policy`，mode 合并退化为 read_only 合并）。
- **消费**：每次工具调用从 session metadata 反解 `mode` → `resolve_mode`（纯函数，廉价）→ read_only 决策；prompt 组装路径在请求开始处解析一次，得到 transform refs 供 registry 裁剪。
- **回放**：`resolve_mode` 是持久化字段的纯函数，回放/恢复天然一致，无状态可漂移。

## 3. 组合形式 vs 枚举 + switch 的可维护性对比

**枚举 + switch（现状）**：新增一种 mode 需要动 `workflow.py`（枚举 + 硬编码 refs）+ `hook_preset_metadata.py`（合并胶水）+ `service.py`（解析 + 快照 + prompt 渲染 + 缓存）+ `prompt_assembly.py`（槽位）+ `contracts.py`（metadata 校验）+ 委派继承 rank 表，外加文档契约（`docs/contracts/workflow-presets.md`）。任何一个环节的"这个 mode 是什么"的知识都是局部写死的，改一处漏一处。

**组合形式（目标）**：

| 维度 | 枚举 + switch | 组合形式 |
| --- | --- | --- |
| 新增 mode | 动 5+ 个文件的解析/渲染/校验/持久化逻辑 | 声明一条 `ModeDefinition`（开关组合 + 描述）；只有引入**全新开关类型**才动解析逻辑 |
| 开关复用 | switch 分支彼此不共享 | 每个开关独立可复用（OMP `effectiveAgent` 原则）：`read_only` 被 permission / tool scope / policy snapshot 共享一份推导；`transform_refs` 与 hook preset / file rules 共享 registry 通道 |
| 解析逻辑 | 每个消费点各自解析（现已有两套 read_only 推导） | 单一 `resolve_mode` 纯函数，一处解析、各处消费 |
| 行为可预期性 | "mode 到底做了什么"散落在 N 处，靠读代码拼图 | `ModeResolution` 一次说清：read_only? 哪些 transform? 工具限制? spawn 策略? |
| 可测试性 | 需要为每个散点写测试 | `resolve_mode` 纯函数 + `registry.filtered()` 组合可单测 |
| 代价 | 直观，但耦合随 mode 数线性增长 | 依赖 registry 已存在（已具备，证据 5）；多一个间接层 |

一句话：**"加 mode 不写代码"**（除非要新增开关类型），这是组合形式相对枚举的核心收益，也正是 `docs/workflow-composition-design.md` 已经确立的方向在本文件的延续。

## 4. 持久化与回放

**不引入 append-only session tree**（决策 4）。mode 的持久化完整落在既有 SQLite + metadata 体系上：

- **存储字段**：session 顶层 metadata 的 `mode`（已入 `_STABLE_RUNTIME_REQUEST_METADATA_KEYS`，line 98）与派生的 `read_only`（line 99，`session_metadata_for_persistence` 已写入）。这是唯一真相。
- **删除后的落点**：`workflow_snapshot.py` 删除后，`workflow` / `workflow_mode` 两个 metadata 键（line 106-107）与 `runtime_config.workflow`（`service.py::_effective_runtime_config_metadata` line 6881-6884）随之消失；`mode` 不需要新的 snapshot 契约——它是稳定标量，`parse_runtime_mode` 严格校验即版本契约（未知值直接报错）。
- **回放**：`resolve_mode(runtime_mode_from_metadata(metadata), ...)` 是持久化字段的纯函数。恢复/续跑/后台任务消费同一路径，解析结果与当初完全一致——不需要快照版本号、不需要 `requested/effective` 对齐校验，因为**存储的就是标量本身**，不存在"请求态与生效态漂移"问题（这正是 workflow snapshot v2 存在的原因，删掉它之后问题消失）。
- **旧数据**：沿用 `docs/workflow-composition-design.md` 决策 3 的硬约束——不需要任何先后兼容。旧 session 若持久化 `analyze` 或 `workflow` / `workflow_mode`，按新 schema 视为失效（parse 严格报错）；旧库按新 schema 初始化或重建。

## 5. 分阶段落地

### Phase 1（最小可落地，本次范围）

1. **删** `RuntimeMode.analyze`：`mode.py` 收窄为 `Literal["normal", "plan"]`；同步收窄 `contracts.py`（包装函数、错误消息、校验）、`policy.py`（line 467 集合）、`session.py`（line 138 集合）、`tool_scope.py`（line 123 集合）、`cli/app.py`（line 2713 Choice）、`service.py::_stricter_runtime_mode`（rank 表，随 mode 合并退化一并删除）。
2. **删** `WorkflowMode` 全族：`workflow.py`、`workflow_snapshot.py` 整体删除；`hook_preset_metadata.py` 只留 `hook_preset_refs_for_agent`；`contracts.py` 删 `workflow_mode` / `workflow` 字段与校验；`prompt_assembly.py` 删 `workflow_mode_prompt_context` 槽位（`context_window.py` 同名参数同步删）；`service.py` 删第 2 节列出的全部 workflow mode 方法与快照写入。
3. **改** `/plan` 绑定：`command/loader.py` 内置 `plan` 命令去掉 `workflow_mode="product"`，改设 `mode: "plan"`（`CommandDefinition` 字段由 `workflow_mode` 改为 `mode: RuntimeMode | None`，`command/registry.py::get_workflow_mode` → `get_mode`，markdown frontmatter 解析 `_optional_string(metadata.get("workflow_mode"))` → `mode`）；`service.py::_resolve_prompt_command_for_request` 相应改为写 `command_metadata["mode"]`。
4. **建** 聚合点：`mode.py`（或同层新模块）新增 `ModeDefinition` / `MODE_DEFINITIONS` / `resolve_mode`；`context_transforms.py` 新增 `mode_guidance` provider（priority 约 150，沿用 `HookPresetGuidanceTransformProvider` 的预渲染文本模式）。
5. **换** 消费点：`permission.py::is_plan_mode_blocked` 改读 read_only；`tool_scope.py` 删静态推导副本、消费共享推导；`service.py::_resolve_permission` 与 `_build_hook_preset_snapshot` 相应改传参；prompt 注入改走 `registry.filtered(resolution.transform_refs)`。
6. **收尾**：`docs/contracts/workflow-presets.md` 与 `command/README.md` 中 workflow mode 契约描述更新/删除；`runtime/__init__.py` 如有相关导出一并清理；相关测试同步改写。

**内容级注意**：`WorkflowMode` 携带的 `hook_preset_refs`（如 `product` → `role_reminder`）随删除消失；若 `/plan` 路径仍需要 role-reminder 类指导，应作为 mode 的 `transform_refs`（guidance provider）显式声明，而不是在代码里偷偷加回 hook refs 合并。Phase 1 按决策 2 原样删除，不主动补回。

**Phase 1 验收形态**：`mode` 只有 `normal` / `plan`；`plan` 的 read_only 与 guidance 全部经 `resolve_mode` + registry 生效；仓库中不再存在 `WorkflowMode`、`workflow_mode`、`workflow` snapshot 任何符号；`/plan` 进入 `RuntimeMode.plan`。

### Phase 2（终态，明确暂不做）

vibe 式 director / worker 生命周期：进入/退出 mode、mode 互斥、退出时 kill workers。**不在 Phase 1 范围**。它的前提（现状与缺口）：

- **已有**：background task 基础设施（`background_tasks.py::RuntimeBackgroundTaskSupervisor`：队列/并发/重试/取消/shutdown drain）、固定 child preset（advisor/explore/researcher/worker/product）、parent/child session 关联、生命周期 hooks（`docs/contracts/runtime-lifecycle-hooks.md` 的 session_start 等）。
- **缺口**：① mode 进入/退出需要生命周期事件（现有 hooks 面是否覆盖 mid-session mode 切换需评估）；② "退出 kill workers"需要 session → 活跃 background tasks 的反向索引与强制终止路径；③ "互斥"需要单 mode 活跃不变量（进入新 mode 前先退出旧 mode）；④ spawn_policy 开关需要接入 `delegation_routing.py` / `agent_capability.py` 的 child preset 选择。这些都需要先在 Phase 1 的 `spawn_policy` / `toolset_restriction` 开关占位上建立契约，再逐步接入。
- **范围提醒**：OMP 的 plan mode 会清空 child spawns；VoidCode 的 `/plan` 模板明确允许委托 `product` 子代理产出计划，Phase 1 不采纳"plan 禁 spawn"，spawn 限制留给未来模式按需声明。

## 6. 非目标

- **不引入 OMP 的 append-only session tree**：持久化继续走 SQLite + session metadata（决策 4）。
- **不引入任意拓扑 multi-agent**：运行时仍只拥有 leader + 固定 child presets 的委托执行（`_EXECUTABLE_AGENT_PRESETS` / `_EXECUTABLE_SUBAGENT_PRESETS`），不做任意 orchestration graph。
- **不复制 OMP 的 vibe / plan 全部语义**：不追平 director 指令、互斥、kill workers 等机制数量；Phase 2 按需评估，Phase 1 只做 read_only + guidance 注入。
- **mode 不变成规划状态机**：沿用 `docs/deliberate-omissions.md` 口径——"Plan mode as a runtime concept" 指不引入专用 planning 执行引擎/plan-state machine；`RuntimeMode.plan` 只是只读执行姿态 + guidance，规划产物仍是 agent 写出的文件/计划文本，两者不冲突。
- **沿用指导原则**：新增 tool / agent role / config knob 的准入门槛保持高位；本设计在**减少** runtime 的类型与通道数量（删 2 个类型族 + 1 个快照契约 + 1 个 prompt 槽位），而不是增加。

## 落地影响面（Phase 1 删改清单）

| 文件 | 删除 / 修改内容 |
| --- | --- |
| `src/voidcode/runtime/mode.py` | `RuntimeMode` 收窄为 `Literal["normal", "plan"]`；`parse_runtime_mode` / `runtime_read_only_from_metadata` 删 analyze 分支；新增 `ModeDefinition` / `MODE_DEFINITIONS` / `resolve_mode`（或同层新模块） |
| `src/voidcode/runtime/workflow.py` | 整体删除 |
| `src/voidcode/runtime/workflow_snapshot.py` | 整体删除 |
| `src/voidcode/runtime/hook_preset_metadata.py` | 删 `hook_preset_refs_for_mode_and_agent` 与 `WorkflowMode` import，只留 `hook_preset_refs_for_agent` |
| `src/voidcode/runtime/permission.py` | `is_plan_mode_blocked` 签名 `runtime_mode: RuntimeMode` → `read_only: bool`（保留 operation_class 纵深防御）；`resolve_permission` 相应调整 |
| `src/voidcode/runtime/tool_scope.py` | 删静态 `runtime_mode` / `runtime_read_only` 副本，消费共享推导；保留 `shell_exec` 特例并注释说明 |
| `src/voidcode/runtime/context_transforms.py` | 新增 `mode_guidance` provider（priority 约 150）；`RuntimeContextTransformRequest` 增加 mode guidance 字段（沿用 hook_preset_context 模式） |
| `src/voidcode/runtime/service.py` | 删 `_workflow_mode_resolution_for_request_metadata`、`_workflow_mode_prompt_context`、`_workflow_snapshot_for_resolution`、`_workflow_snapshot_with_effective_mode`、`_metadata_without_workflow_mode`、`_validate_explicit_workflow_mode_metadata`、`_restore_explicit_workflow_mode`、`_config_workflow_mode_resolution` 缓存、`_assemble_context` snapshot 回退分支、workflow 相关 import；`_build_hook_preset_snapshot` 去 mode 参数；`_stricter_runtime_mode` 删除（继承退化为 read_only 合并）；`_resolve_permission` 传 read_only；`_resolve_prompt_command_for_request` 写 `mode`；`_effective_runtime_config_metadata` 删 `runtime_config["workflow"]` |
| `src/voidcode/runtime/contracts.py` | `RuntimeRequestMetadata` 删 `workflow_mode` / `workflow`；`_STABLE_RUNTIME_REQUEST_METADATA_KEYS` 删两键；删 workflow_mode 校验（line 232-243、490-504）；错误消息收窄为 'normal' / 'plan' |
| `src/voidcode/runtime/prompt_assembly.py` | 删 `workflow_mode_prompt_context` 参数、`append_system` 槽位与 `_layer_for_source` 分支 |
| `src/voidcode/runtime/context_window.py` | 删 `workflow_mode_prompt_context` 透传参数 |
| `src/voidcode/runtime/session.py` | 校验集合收窄为 `{"normal", "plan"}` |
| `src/voidcode/runtime/policy.py` | snapshot 校验集合收窄为 `{"normal", "plan"}` |
| `src/voidcode/cli/app.py` | `--runtime-mode` Choice 删 `"analyze"` |
| `src/voidcode/command/loader.py` | 内置 `plan` 命令删 `workflow_mode="product"`，改设 `mode: "plan"`；frontmatter 解析 `workflow_mode` → `mode` |
| `src/voidcode/command/registry.py` / `models.py` | `get_workflow_mode` → `get_mode`；`CommandDefinition.workflow_mode` → `mode: RuntimeMode | None` |
| `src/voidcode/command/README.md` | workflow_mode frontmatter 说明更新为 mode |
| `docs/contracts/workflow-presets.md` | 契约已被删除，更新或删除该文档 |
| `src/voidcode/runtime/__init__.py` | 相关导出清理（如有） |
| 相关测试 | `tests/` 下 workflow mode / snapshot / analyze 相关用例同步删除或改写 |
