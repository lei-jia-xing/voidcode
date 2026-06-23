# Runtime 架构重构计划

## 背景

当前 runtime 边界方向是正确的：CLI、Web、TUI、ACP 等客户端都应通过 runtime 进入执行闭环，graph 只负责推进执行步骤，tools 只负责具体能力，storage 只保存 runtime 拥有的事实。

风险在于实现正在向三个中心吸附：

- `VoidCodeRuntime` 承担过多控制面和子系统实现细节。
- `SqliteSessionStore` 同时承载 session、event、pending interaction、background task、continuation loop、memory、notification、revert 与清理语义。
- `metadata: dict[str, object]` 正在成为隐形控制面，承担 request config、session fact、turn-local state、observability 和 legacy compatibility 的混合职责。

本计划的目标不是追求文件变小，而是阻止架构继续靠特殊字段、兼容分支和横向堆叠维持。

## 重构目标

1. 把 runtime 收敛成稳定的内核 API，而不是功能堆叠入口。
2. 用少数明确状态对象替代开放式 metadata 控制面。
3. 让事件流成为客户端观测和 replay 的主轴。
4. 让 storage 回到事实持久化职责，业务生命周期由 runtime service 层表达。
5. 拆分 collaborator 时使用明确端口，而不是让子系统继续持有整个 `VoidCodeRuntime`。
6. 不新增 legacy 向后兼容层；旧路径只能被隔离、冻结和删除，不能继续获得新语义。

## 非目标

- 不把 runtime 治理迁移到 graph、CLI、HTTP、Web/TUI、ACP、hook 或 tool 实现里。
- 不扩展任意拓扑 multi-agent、agent marketplace、workspace-scoped MCP、通用 workflow DSL 或通用 policy DSL。
- 不为了保持历史内部实现形状而增加新的兼容 wrapper。
- 不为旧 metadata key 增加新的 fallback parse、best-effort migration 或静默降级逻辑。
- 不做一次性大爆炸重写；每个阶段都必须保持主路径可运行。

## 硬性原则

### 不新增 legacy 兼容

从本计划开始，新增能力必须走新的明确对象和端口。禁止新增以下类型的兼容层：

- 新旧字段同时写入。
- 新字段缺失时自动回退到旧字段。
- 为旧 workflow preset、旧 metadata key 或旧 event payload 添加新的行为语义。
- 为了旧测试或旧客户端新增长期 private wrapper。
- 在解析失败时 best-effort 猜测用户意图。

已有 legacy 路径允许短期保留，但只能做三件事：

- 冻结：不再承载新功能。
- 隔离：收束到单独 adapter/materializer。
- 删除：在计划阶段内移除或降级为显式错误。

### 类型优先

开放式 `dict[str, object]` 只能用于最终 payload 边界。runtime 内部控制语义应逐步迁移到明确类型：

- `RuntimeRequestContext`
- `RuntimeSessionFacts`
- `RuntimeTurnState`
- `RuntimePolicyFacts`
- `RuntimeWorkflowSelection`
- `RuntimeToolScope`

这些对象应表达“谁拥有状态”和“状态是否可持久化”，而不是只做字段搬运。

### 事件是观测，不是第二状态机

客户端可以从事件渲染 UI，但不应复制 runtime 状态机。runtime 应提供明确 projection，让客户端直接知道：

- 当前 session 状态。
- pending approval/question。
- active/background child status。
- last tool status。
- revert marker。

客户端可以容忍未知事件，但不应通过反向扫描事件来决定核心交互所有权。

## 目标架构

重构后的 runtime 应大致收敛为以下边界：

```text
client adapters
  CLI / HTTP / Web / TUI / ACP
        |
        v
RuntimeKernel
  request validation
  session lifecycle
  run/resume/cancel facade
        |
        +-- RequestContextBuilder
        +-- RuntimePolicyService
        +-- WorkflowSelectionService
        +-- ToolScopeResolver
        +-- RunLoopCoordinator
        +-- ResumeCoordinator
        +-- BackgroundTaskService
        +-- CapabilityManagers
        +-- EventLog
        +-- Repositories
```

`RuntimeKernel` 仍是唯一对外入口，但子系统不应持有整个 kernel。它们依赖小端口，例如：

- `SessionRepository`
- `EventLogRepository`
- `PendingInteractionRepository`
- `BackgroundTaskRepository`
- `HookRunner`
- `ChildRunExecutor`
- `ToolExecutor`
- `Clock`

## 阶段计划

### Phase 0: 冻结架构债入口

目标：先停止继续扩大 legacy 和 metadata 债务。

任务：

- 将本计划作为后续 runtime 重构 PR 的约束文档。
- 在代码审查标准里明确：新 runtime 行为不得新增 legacy fallback。
- 列出当前 legacy metadata/workflow/event 入口，并标记 owner。
- 对新增 runtime metadata key 建立审查规则：必须说明 owner、生命周期、持久化策略和删除条件。

验收：

- 文档明确“不新增 legacy 向后兼容”。
- 后续 PR 如果新增 metadata key，必须能指向 typed owner 或计划迁移点。

### Phase 1: 切分 metadata 语义

目标：先建立类型对象，不急着移动所有实现。

新增或整理：

- `RuntimeRequestContext`：一次请求解析后的 runtime 事实，包括 agent、provider、workflow、policy、skills、tool config、mode/read_only。
- `RuntimeSessionFacts`：可持久化并可 replay 的 session 事实。
- `RuntimeTurnState`：本轮临时状态，包括 run id、provider attempt、retry attempt、context transform emission state。

迁移策略：

- 新代码只读这些对象，不直接读裸 metadata key。
- 旧 metadata 解析集中在 builder/materializer 中。
- 不新增旧 key fallback；遇到缺失或无效字段时显式失败或使用新对象默认值。

验收：

- graph 不再直接解释新增 runtime 控制 key。
- 新 request/session/turn 状态有明确持久化边界。

### Phase 2: 收缩 `VoidCodeRuntime`

目标：让 `VoidCodeRuntime` 成为 facade/kernel，而不是所有子系统的实现容器。

优先改造：

- `RuntimeBackgroundTaskSupervisor` 不再持有整个 runtime。
- `RuntimeRunLoopCoordinator` 不再通过 runtime 私有字段获取权限、hook、tool execution 和 config。
- `RuntimeResumeCoordinator` 不再依赖 runtime 私有方法拼接 checkpoint/session/tool results。

做法：

- 为每个 coordinator 注入小接口。
- 保持 public runtime surface 稳定。
- 删除不再需要的 private compatibility wrapper，不新增 wrapper 过渡层。

验收：

- collaborator 构造参数能表达真实依赖。
- 子系统测试可以不用实例化完整 `VoidCodeRuntime`。

### Phase 3: 拆分 storage 职责

目标：SQLite 仍可统一存储，但业务生命周期不再堆在 `SqliteSessionStore` 一个类里。

拆分端口：

- `SessionRepository`
- `EventLogRepository`
- `PendingInteractionRepository`
- `BackgroundTaskRepository`
- `ContinuationLoopRepository`
- `MemoryRepository`
- `NotificationRepository`

约束：

- repository 只做事实读写和基本一致性约束。
- 状态机规则进入 runtime service 层或 domain object。
- 不添加旧 schema 自动迁移 fallback。schema 不匹配继续 fail fast。

验收：

- background task transition 不再散落在通用 session store 中。
- pending approval/question 不再作为 session row 的附属业务逻辑到处解析。

### Phase 4: 硬化事件和客户端 projection

目标：事件保持 append-only observability，客户端核心交互依赖 runtime projection。

任务：

- 明确 stable/shipped/experimental event 分类，代码和文档命名一致。
- 为 session replay/debug/result 提供 pending interaction projection。
- 前端停止本地补造 `runtime.approval_resolved` 这类权威事件。
- 保留客户端对未知事件的容忍，但核心状态不靠客户端扫描事件倒推。

验收：

- Web store 不再复制 approval/question 状态机。
- runtime 是 pending interaction ownership 的唯一来源。

### Phase 5: 清理 workflow/policy/legacy preset

目标：把 workflow/policy 从兼容解释层收敛为明确选择和治理事实。

任务：

- first-class workflow mode 只表达当前支持的字段。
- legacy preset 不再获得新字段、新行为和新 fallback。
- policy snapshot 只表达 runtime 治理事实，不变成通用 DSL。
- 删除或隔离 legacy materialization 中无法解释当前产品语义的字段。

验收：

- 新功能不需要同时修改 workflow、legacy preset、policy、metadata、prompt guidance 多处才能生效。
- legacy 路径缺失字段不会被静默补齐为新语义。

## PR 切分建议

### PR 1: 文档和守则

- 新增本计划。
- 在架构文档和 coding standards 中引用“不新增 legacy 兼容层”。
- 不改行为。

### PR 2: metadata owner inventory

- 增加当前 runtime metadata key 清单。
- 标注 request/session/turn/observability/legacy 分类。
- 不做迁移。

### PR 3: typed request context scaffold

- 新增 `RuntimeRequestContext` 和 builder。
- 让新代码路径优先消费 builder 结果。
- 不新增旧字段 fallback。

### PR 4: background task service port

- 把 supervisor 对 `VoidCodeRuntime` 的依赖改成小接口。
- 保持 public API 不变。
- 删除被替代的 private wrapper。

### PR 5: event projection hardening

- 增加 pending interaction projection。
- 前端消费 projection。
- 移除前端本地权威事件补造。

后续 PR 再处理 run loop、resume、storage repository 拆分和 workflow/policy 清理。

## 验证策略

每个 PR 至少运行 touched boundary 的测试。行为 PR 需要补 characterization test，文档 PR 不要求全量测试。

推荐基线：

```bash
uv run pytest tests/unit/runtime/test_runtime_service_extensions.py
uv run pytest tests/unit/runtime/test_session_storage.py
uv run pytest tests/unit/tools/test_background_task_tools.py
uv run pytest tests/unit/interface/test_cli_delegated_parity.py
mise run check
```

如果只是文档变更，可以只运行：

```bash
uv run pytest tests/unit/project/test_project_metadata.py
```

## 成功标准

重构完成后，应能做到：

- 新 runtime 行为能从 typed request/session/turn 对象解释，而不是从散落 metadata key 解释。
- `VoidCodeRuntime` public surface 仍稳定，但内部 collaborator 不依赖整个 runtime。
- storage 层不再拥有高层业务状态机。
- frontend 不复制 runtime pending/approval/question 权威状态。
- workflow/policy 不再继续吸收 legacy 语义。
- 新功能不需要添加兼容 fallback 才能进入主路径。
