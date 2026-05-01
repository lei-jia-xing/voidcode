# Session Continuity Memory 设计

## 状态

- 状态：partially implemented
- 范围：design + initial runtime slice
- 目标仓库：`voidcode`

## 目的

定义 VoidCode 当前最值得优先设计的 memory 层：**Session Continuity Memory**。

这层 memory 的目标不是跨 session 记住越来越多事情，而是让一个**正在进行中的 workstream** 在以下场景里仍然保持可工作：

- context window 收缩 / compaction
- approval wait 与 approval resume
- session replay / resume
- provider-backed execution 的长会话推进

## 背景

当前仓库已经具备：

- runtime-owned `run / stream / resume` 主路径
- session persistence / replay / resume truth
- approval resume checkpoint
- provider-backed execution 中的最小 context window compaction
- `runtime.memory_refreshed` 事件词汇

但今天的“memory”仍然仍处于早期阶段：

- `RuntimeContextWindow` 已经具备最小 continuity state 字段，并在 compaction 时生成第一版 runtime-owned continuity summary
- compaction 触发时会发出带 continuity payload 的 `runtime.memory_refreshed`
- continuity state 已经进入 `session.metadata["runtime_state"]["continuity"]` 与 provider-backed `context_window`
- 但 continuity shape 仍然非常克制，仍未扩展成更完整的 distilled summary / reinjection 设计

因此，当前最合理的下一步不是 long-term memory，而是先定义：

> compaction 之后，什么信息必须以 runtime-owned 的方式被保留下来，才能让当前 session 继续工作。

## 核心目标

Session Continuity Memory 应满足以下目标：

1. 在 compaction 之后保留当前任务主线，而不是只做机械截断。
2. 继续服从现有 session truth、replay、resume 与 approval 语义。
3. 不再发明第二套 execution truth。
4. 为未来 provider-backed execution 的更长工作流提供稳定 continuation substrate。

## 非目标

本设计**不**覆盖：

- cross-session long-term memory
- user / project / team memory retrieval
- multi-agent handoff contract
- leader-facing background result summary
- agent-to-agent transport
- 独立的 memory daemon 或外部 memory service

这些方向要么属于后置层，要么属于另外的设计对象。

## Ownership Boundary

Session Continuity Memory 必须继续由 **runtime** 拥有。

这意味着：

- compaction 的输入/输出语义属于 `runtime/`
- distilled continuity state 的生成与注入边界属于 `runtime/`
- replay / resume compatibility 属于 `runtime/`
- provider context reinjection 属于 runtime execution path

它**不能**由以下位置拥有：

- provider prompt 自行推断
- hook 脚本
- Web / TUI / CLI 本地状态
- future agent preset 文档
- 外部 memory worker（至少在当前阶段不应作为 authority）

## 当前代码基线

今天最接近这层能力的代码是：

- `src/voidcode/runtime/context_window.py`
- `src/voidcode/runtime/service.py`

当前行为可概括为：

1. 当前 runtime 的 provider context-window preparation path 会接收 prompt 与全部 tool results。
2. 它根据 `ContextWindowPolicy.max_tool_results`、总 token budget 与默认/按工具 token cap 收缩 provider-facing tool feedback。
3. 如果发生截断，则 `RuntimeContextWindow.compacted == True`。
4. `runtime/service.py` 在图执行前发出 `runtime.memory_refreshed`。

这里的关键现实是：

> `runtime.memory_refreshed` 目前只是 compaction 发生的信号，不是 memory retrieval 成功的信号。

## 设计对象

Session Continuity Memory 需要补出的不是“记住更多”，而是一个更明确的 **continuity state shape**。

### 建议数据形状

最小 continuity state 应只保存与当前活跃 workstream 直接相关、且在 compaction 后必须保留的内容，例如：

```python
@dataclass(frozen=True, slots=True)
class SessionContinuityState:
    objective_summary: str | None
    active_constraints: tuple[str, ...]
    open_questions: tuple[str, ...]
    completed_milestones: tuple[str, ...]
    retained_tool_result_refs: tuple[str, ...]
    distilled_tool_result_summary: str | None
    source_event_sequence: int | None
```

### 字段意图

- `objective_summary`
  - 当前任务的主目标摘要
- `active_constraints`
  - 当前仍然必须被遵守的限制条件
- `open_questions`
  - 尚未解决、会影响后续执行的问题
- `completed_milestones`
  - 已完成且不应在 compaction 后丢失的关键进展
- `retained_tool_result_refs`
  - 仍然保留在上下文窗口中的工具结果引用
- `distilled_tool_result_summary`
  - 被截断工具结果的压缩摘要，而不是全文重复
- `source_event_sequence`
  - 该 continuity state 基于哪个事件序列/上下文片段生成

## 什么能进 continuity state，什么不能进

### 可以进入

- 当前 workstream 的目标与子目标
- 影响后续执行的关键决策
- 仍未解决的问题
- 工具结果中真正会影响下一步动作的 distilled facts

### 不能进入

- 用户长期偏好
- 项目长期事实库
- 全量 transcript 镜像
- todo / plan / tool result 的全文复制
- 与当前 session 主线无关的探索结果

## 触发点

Session Continuity Memory 最合理的刷新触发点应当是：

1. **Compaction 触发时**
   - 当前最核心触发点
2. **Approval wait 进入持久化 checkpoint 前**
   - 避免 approval resume 后丢失主线
3. **Resume 重新进入 provider-backed execution 前**
   - 确保恢复时 continuity state 与当前上下文重新对齐
4. **显式 runtime-owned refresh operation（后置）**
   - 只有当 runtime 真正需要显式刷新入口时才考虑暴露

这里最重要的约束是：

> refresh 触发点必须继续服从现有 runtime 主路径，而不是通过独立 worker / client-side helper 私自生成。

## 与 replay / resume 的兼容规则

Session Continuity Memory 不能破坏现有的 replay / resume 契约。

因此它必须满足：

1. `resume(session_id)` 仍然以 session truth 为准。
2. continuity state 只能作为 session truth 的派生补充，而不是新的 authority。
3. approval resume checkpoint 的语义不能被 continuity state 替代。
4. replay 时看到的事件流仍应保持现有模型；continuity refresh 如果被事件化，也必须作为 runtime-owned 事件进入同一条序列。

## 与 provider-backed execution 的边界

Session Continuity Memory 的价值最终必须体现在 provider-backed execution 上，但注入边界必须非常克制。

### 应做的事

- 在 provider 执行前，为当前上下文提供 distilled continuity state
- 保留仍在窗口中的原始 tool results
- 让模型在上下文收缩后仍理解“当前在做什么、哪些问题仍开着”

### 不应做的事

- 把 continuity state 当作新的系统 prompt 全量覆盖原上下文
- 把被截断的 tool results 再完整塞回去
- 让 provider 自己决定哪些 continuity facts 属于真相

## 建议的演进切片

### Phase 1：定义 continuity state shape

先回答：

- runtime 到底要保留哪些最小 continuity 字段
- 哪些字段是可恢复且值得进入 session metadata / runtime state 的
- 哪些字段必须继续只留在完整 transcript 中

### Phase 2：把 compaction 从“截断”升级为“截断 + summary”

让 `RuntimeContextWindow` 或相邻的 runtime-owned data structure 能表达：

- 截断前数量
- 截断后数量
- continuity summary payload

### Phase 3：定义 reinjection boundary

明确 continuity state 何时、以什么 shape 进入 provider-backed execution path。

### Phase 4：保持 replay / resume / approval 兼容

任何实现都必须证明：

- replay 语义不变
- resume 语义不变
- approval checkpoint 语义不变

## 当前不应做什么

这一层设计当前不应演变成：

- long-term memory store
- memory retrieval ranking / search
- team/user/project memory taxonomy
- future multi-agent handoff summary 的实现替代物
- background worker 驱动的 memory pipeline

## 与其他工作的关系

### 与 skill execution 的关系

skill execution 不是本设计存在的硬前提，但如果未来希望自动 distillation 通过 skill 参与，它会成为重要增强器。

### 与 handoff memory 的关系

Session Continuity Memory 只解决**当前 session 内部继续工作**的问题。

当问题变成“parent / child session 之间怎么交接”时，就已经进入另一份设计对象：`Handoff / Coordination Memory`。

### 与 long-term memory 的关系

long-term memory 仍然是后置层。它的责任是跨 session recall，而不是当前 session 的 continuity。

## 完成标准

当这层设计足够成熟时，至少应能回答以下问题：

1. compaction 之后，到底保留哪些最小 continuity facts？
2. 这些 facts 属于 session truth 的哪一层？
3. 它们何时刷新、何时持久化、何时注入？
4. 它们如何不破坏 replay / resume / approval？
5. 为什么这仍然不是 long-term memory？

## 相关文档

- [`memory-strategy.md`](./memory-strategy.md)
- [`contracts/approval-flow.md`](./contracts/approval-flow.md)
- [`contracts/background-task-delegation.md`](./contracts/background-task-delegation.md)
- [`runtime-owned-scheduler-design.md`](./runtime-owned-scheduler-design.md)
