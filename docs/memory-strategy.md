# VoidCode Memory 策略

## 状态

- 状态：proposed
- 范围：design-only
- 目标仓库：`voidcode`

## 背景与动机

在讨论 VoidCode 的后续演进时，“memory”很容易被混成一个过于宽泛的词。实际上一套 coding agent runtime 里至少存在三类不同的问题：

1. **会话连续性**：如何让一个正在进行中的工作流在长上下文、压缩、恢复、审批或 subagent 交接之后仍然保持连贯。
2. **项目级持久上下文**：如何在新 session 开始时，为 agent 提供稳定的项目约定、用户偏好和长期有效的事实。
3. **长期可检索记忆**：如何在多次会话之后，从历史中检索少量真正值得复用的知识，而不是把所有历史噪音重新塞回上下文。

目前 VoidCode 已经有早期的 context window groundwork，但还没有成熟的 memory subsystem。因此在进入更真实的 agent runtime 演进之前，必须先明确：**什么才是现在最重要的 memory 问题。**

本文档给出的结论是：

- VoidCode 当前应当**优先学习 oh-my-opencode 的 session/compaction/continuation 模型**
- 而不是优先复制 Claude Code 的 cross-session memory 模型
- Claude 风格的长期 memory 仍然有价值，但它解决的是**另一个阶段的问题**

## 核心结论

VoidCode 当前的 memory 优先级应当是：

1. **先保证单个 session 的稳定连续工作**
2. **再保证 subagent 之间的 handoff / continuation 不丢主线**
3. **最后才考虑 selective 的 cross-session long-term memory**

换句话说，当前最应该解决的是：

> 如何保持一个活跃 workstream 在长时间运行中仍然干净、可恢复、可压缩、可继续。

而不是：

> 如何尽快让系统学会长期记住越来越多事情。

## 当前判断依据

### VoidCode 当前基线

从当前代码与设计文档出发，VoidCode 已具备的基础更接近以下方向：

- runtime 是系统控制平面
- session persistence / replay / resume 已经是核心语义
- approval continuity 属于 runtime-owned
- 当前只有早期 context window compaction groundwork
- 还没有完整的长期 memory 存储、检索和治理体系

这意味着 VoidCode 现在最贴近的问题不是“长期知识记忆”，而是“长会话执行与恢复”。

### Claude Code 提供的启发

从源码看，Claude Code 已经有一套明确的 memory layer：

- `src/memdir/`
- `MEMORY.md` 作为索引
- topic memory files
- memory 类型划分（user / feedback / project / reference）
- relevant memory retrieval
- private / team memory 区分
- daily log → distilled memory 的长期沉淀路径

这是一种典型的 **cross-session、file-based、retrieval-oriented memory** 设计。

它的价值在于：

- 新 session 开始时可以复用稳定项目知识
- 用户偏好与长期约定不需要重复解释
- 团队级别事实可以跨会话保留

但它的主要解决对象是：

- **长期可复用的知识**
- **跨 session 的 recall 问题**

而不是正在进行中的单个 workstream 如何持续推进。

### oh-my-opencode 提供的启发

从源码看，oh-my-opencode 更强调：

- session storage
- transcript / message / part truth
- compaction
- continuation
- resume / recovery
- compaction 前后的 checkpoint 与注入
- subagent/session 间的衔接与工作流组织

这是一种典型的 **session-truth-first、compaction-aware、continuation-oriented** 模型。

它关注的是：

- 一个会话如何在上下文变长后继续工作
- context 被压缩后如何不丢掉主线
- session 恢复时如何保住 agent configuration 和工作状态
- subagent 之间如何进行受控 handoff，而不是各自形成孤岛

这与 VoidCode 当前最迫切的问题高度对齐。

### anomalyco/opencode 提供的启发

anomalyco/opencode 的核心也更偏：

- session/message/part persistence
- compaction
- instruction files (`AGENTS.md` / `CLAUDE.md` / `CONTEXT.md`)
- replay / resume

它说明了一件事：对于 coding agent runtime 来说，**高保真 session truth + compaction + instruction context** 本身就已经是一条非常强的主线，不必一开始就把长期 memory 做成核心卖点。

## 为什么现在不优先做 Claude 风格 memory

Claude 风格的长期 memory 不是没有价值，而是它当前不应优先于 session continuity。

### 原因 1：它解决的是另一个问题

Claude 风格 memory 主要解决：

- 新开 session 时如何恢复稳定背景知识
- 如何跨会话记住偏好、项目事实、经验规则

而 VoidCode 当前更需要解决：

- 长会话如何继续运行
- 压缩后如何保持工作连续性
- approval / resume / replay / subagent handoff 如何仍然成立

### 原因 2：长期 memory 很容易污染上下文

如果没有严格的记忆边界，长期 memory 很容易把以下内容错误地长期化：

- 当前 session 的临时计划
- 任务过程中的中间判断
- 已经过时的项目状态
- 只对某次会话有意义的工作细节

这会让“长期记忆”反过来变成上下文污染源。

### 原因 3：多 agent 的第一问题通常不是长期 recall

即便目标是多 agent，第一阶段最常见的问题通常仍然是：

- 当前 authoritative state 在哪里
- subagent 之间怎么交接
- 被压缩后的上下文如何恢复
- 哪些信息必须进入 handoff summary
- 失败、审批和恢复之后谁来继续工作

这些问题更像 **session continuity / orchestration**，而不是 **long-term memory retrieval**。

## 应当优先学习的模型

VoidCode 当前更应该学习的是：

> **oh-my-opencode 式的 session/compaction/continuation 优先级**

而不是：

> **Claude Code 式的长期 memory 优先级**

这并不意味着否定 Claude Code 的 memory 设计，而是意味着在演进顺序上应当更克制：

- 先把 session truth 做稳
- 先把 compaction 做成可继续工作的机制，而不是简单裁剪
- 先把 subagent handoff 做成可恢复、可验证的流程
- 在这些已经稳定之后，再追加一层 selective、受控的长期 memory

## 建议采用的分层思路

为了避免以后再次把各种 persistence 语义混在一起，建议从一开始就按层理解 VoidCode 的 memory：

### 第 1 层：Session Continuity Layer

这是当前最重要的一层。

它负责：

- 活跃 session 的执行真相
- replay / resume / approval continuity
- context compaction 后的继续执行
- 运行中的主线保持

这层的核心不是“多记住一些东西”，而是：

> **让当前会话持续可工作。**

### 第 2 层：Subagent Handoff Layer

当系统进入多 agent 方向之后，这层会变得重要。

它负责：

- 父 agent 与 subagent 之间的交接摘要
- 当前任务状态、边界、结论、未完成项的传递
- compaction 或 resume 之后继续保持 agent 间上下文一致

这层本质上仍然更接近 **session-scoped coordination memory**，而不是长期知识记忆。

### 第 3 层：Long-Term Memory Layer（可选，后置）

这是后续可以引入的一层，但不应成为当前优先级。

它应只保存：

- 用户稳定偏好
- 长期有效的项目约定
- 不易从代码直接推导出的项目事实
- 多次会话后仍然值得复用的经验知识

它不应保存：

- 当前任务计划
- todo 列表
- 短期会话中间态
- 临时探索结果

## 长期 memory 的真正价值

长期 memory 当然有价值，但应明确它的价值边界。

### 有价值的地方

- 用户反复开启新 session 时，不必重复解释稳定偏好
- 项目级事实可以跨时间复用
- 多 agent / 多会话场景下，稳定背景知识可以共享

### 不应被高估的地方

- 它不能替代 session continuity
- 它不能自动修复 compaction 造成的信息损失
- 它不能替代真实 session truth
- 检索质量不够时，它会比不用更糟

因此长期 memory 更适合作为：

> **session continuity 之外的一层增益**

而不是当前 runtime 的首要基础设施。

## 对 VoidCode 的建议顺序

当前更合理的演进顺序应当是：

1. 强化 session truth、resume、approval continuity 和 replay
2. 将 context compaction 做成真正可继续工作的机制
3. 为 subagent handoff 定义稳定的 session-scoped 摘要与恢复边界
4. 等到跨 session 的稳定知识复用真的成为高频需求，再引入长期 memory 层

## 下一步应产出的设计对象

如果当前阶段决定继续推进 memory，最合理的目标不是实现长期 memory，而是把 **Layer 1 / Layer 2** 设计收敛到可执行的 runtime design。

更具体地说，下一步设计对象应当是：

### Design A：Session Continuity Memory

它解决的问题是：

- 单个活跃 session 在 context 变长后如何继续工作
- compaction 后如何保住执行主线
- approval / resume 之后如何保持同一条 workstream 的连续性

它不解决：

- 新 session 如何继承长期偏好
- 项目级长期知识如何跨 session 检索
- 团队共享知识如何沉淀

### Design B：Handoff / Coordination Memory

它解决的问题是：

- parent / child session 之间需要什么最小摘要
- background / delegated work 完成后，leader 应该看到什么结构化结果
- child transcript 如何继续通过现有 `resume(child_session_id)` 路径恢复，而不是复制进 leader session

它不解决：

- 完整 multi-agent orchestration runtime
- agent-to-agent transport
- 长期 memory retrieval

## 当前代码基线意味着什么

从当前实现出发，VoidCode 已经拥有以下 memory 相关基础：

- session persistence / replay / resume
- approval resume checkpoint
- background task truth 与 parent/child linkage 基线
- provider-backed execution 中的最小 context window compaction
- `runtime.memory_refreshed` 事件词汇

但当前缺失同样明确：

- 语义化 compaction（现在只是 last-N tool results 截断）
- compaction 后的 distilled memory 注入
- leader-facing background result retrieval / notification 真相
- 可执行的 skill-based memory distillation 机制
- cross-session long-term memory store / retrieval / governance

这意味着今天的“memory”不能被理解成一个已经存在的 subsystem；它更像是若干 runtime truth 的空缺交叉点。

## Layer 1：Session Continuity Memory 设计要求

这一层应当建立在现有 `run / stream / resume` 主路径之上，而不是建立第二套旁路状态。

### 目标

- 让长会话在 compaction 后继续可工作
- 让 provider-backed execution 在上下文收缩后仍能保住当前任务主线
- 让 approval / resume / replay 继续基于同一份 session truth

### 应拥有的数据

这层应只拥有 **session-scoped、可丢弃、与当前活跃工作直接相关** 的记忆材料，例如：

- 当前任务目标摘要
- 最近已完成的重要子结论
- 仍然开放的未完成项
- 必须保留的约束 / decision log
- tool results 的 distilled summary，而不是完整原文重复注入

### 不应拥有的数据

这层不应承载：

- 用户长期偏好
- 项目级长期事实库
- 历史 session 的完整摘要集合
- todo / transcript / plan 的全量镜像
- 与当前 workstream 无关的知识片段

### 刷新触发点

从当前 runtime 模型推导，Layer 1 最合理的刷新触发点应是：

- context window 触发 compaction 时
- approval wait 进入持久化 checkpoint 前
- resume 重新进入 provider-backed execution 前
- 明确的 runtime-owned memory refresh operation（如果后续需要暴露）

其中最关键的一点是：

> memory refresh 必须是 runtime-owned continuation machinery，而不是 provider prompt hack。

### 与当前代码的最小映射

今天最接近这个入口的是：

- `src/voidcode/runtime/context_window.py`
- `src/voidcode/runtime/service.py` 中对 `RUNTIME_MEMORY_REFRESHED` 的触发

但它们当前只表达“发生了截断”，还没有表达“保留了什么 distilled state”。

因此这一层未来最小实现切片的方向应是：

1. 先把 compaction 从“机械截断”升级为“截断 + distilled summary shape”
2. 再决定 distilled summary 是如何注入 provider-backed context
3. 保持 replay / resume / approval semantics 不变

## Layer 2：Handoff / Coordination Memory 设计要求

这一层本质上是 session-scoped coordination memory，而不是 long-term memory。

### 目标

- 为 parent / child session 交接定义最小摘要
- 让 leader 看见结构化结果，而不是完整 transcript copy
- 让 delegated/background work 在恢复后仍然可追溯

### 最小数据形状

从 `docs/contracts/background-task-delegation.md` 推导，这层至少需要：

- `task_id`
- `parent_session_id`
- `child_session_id`
- status / approval_blocked
- `summary_output`
- result_available / error

这层最重要的边界是：

- parent session 持有 leader-facing notification 与 result summary
- child session 持有完整 delegated execution history
- 完整 child transcript 继续通过 `resume(child_session_id)` 恢复

### 非目标

这一层不应演变成：

- 把 child transcript 复制到 parent
- prompt 文本承载的伪 handoff
- 客户端本地拼接的通知模型
- 脱离 runtime truth 的“memory helper”

## 为什么它还不能被当成统一实现项

虽然 Layer 1 / Layer 2 都已经值得设计，但它们的前置条件并不相同，不能被写成同一组依赖。

### Layer 1 的主要前置条件：更真实的 compaction contract

Layer 1 已经拥有部分 substrate：

- session persistence / replay / resume
- approval resume checkpoint
- `runtime.memory_refreshed` 事件词汇
- provider-backed execution 中的最小 context window compaction

但当前 `RuntimeContextWindow` 仍只是 last-N tool result retention。只在这之上谈“memory refreshed”还不够，因为它还没有 distinguished summary shape，也没有 durable injection semantics。

因此，Layer 1 当前真正缺的不是长期 memory store，而是：

- 更语义化的 compaction output
- 可恢复的 distilled summary shape
- 与 replay / resume 兼容的 reinjection boundary

### Layer 2 的主要前置条件：Leader-facing background result truth

Layer 2 直接依赖 `docs/contracts/background-task-delegation.md` 中仍处于 proposed 状态的那些能力：

- leader notification
- background result retrieval
- parent / child linkage 之上的结构化 summary truth
- restart / dedupe correctness

当前仓库已经有 raw parent / child linkage 与 `resume(child_session_id)` 路径，但还没有 leader-facing structured result truth。因此 Layer 2 现在可以继续设计，但仍应保持 design-only 表述。

### Skill execution 的位置

如果未来希望 memory refresh / distillation 通过 skill 或相似 capability 参与，那么 `#153` 这类 runtime-managed skill execution 仍然非常重要。

但它更像是：

- Layer 1 的增强器 / 执行载体候选
- 而不是 Layer 1 设计文档本身的硬前提

也就是说，Layer 1 可以先把 shape 设计清楚；真正实现自动 distillation 时，skill execution 才会成为更直接的依赖。

## 推荐的设计顺序（更新版）

结合当前仓库状态，更稳妥的顺序应当是：

1. 完成 runtime-managed skill execution（让 skill 不再只是 discovery / event / static payload）
2. 完成 leader-facing background result / notification contract
3. 基于现有 resume / replay truth，为 Layer 1 设计可持续工作的 compaction memory
4. 基于 parent / child linkage，为 Layer 2 设计 handoff / coordination memory
5. 最后才考虑 selective 的 long-term memory

也就是说，memory 设计现在可以继续，但应当**严格以 Layer 1 / Layer 2 为范围**，并且不应被误写成当前主路径已经准备好立即实现 long-term memory。

## 当前阶段最值得产出的文档结果

如果继续推进本方向，当前最值得补出的不是长期 memory API，而是以下两份更具体的设计：

1. `Session Continuity Memory Design`
   - compaction 输入/输出 shape
   - refresh trigger
   - replay / resume compatibility
   - provider context reinjection boundary
   - 参考文档：[`session-continuity-memory-design.md`](./session-continuity-memory-design.md)
   - 详见 [`docs/session-continuity-memory-design.md`](./session-continuity-memory-design.md)

2. `Handoff Memory Contract`
   - leader-facing summary shape
   - parent / child ownership
   - result retrieval vs transcript replay boundary
   - restart / dedupe / approval-blocked behavior

这两份设计会直接服务于未来 agent runtime 的真实连续性，而不是把精力提前消耗在长期 memory retrieval 上。

## 非目标

本文档当前**不**主张以下方向：

- 立即引入 Claude Code 风格的完整 memdir 子系统
- 立即将长期 memory 作为多 agent 的前置条件
- 把 task / plan / todo / transcript 一股脑都归类为 memory
- 用长期 memory 替代 session persistence、resume 或 replay

## 总结

对 VoidCode 来说，当前最重要的 memory 问题不是“如何长期记住更多”，而是：

> **如何让一个正在进行中的 session 在长上下文、压缩、审批、恢复与 subagent 交接之后，仍然稳定、干净、连续地工作下去。**

因此，当前阶段最值得学习的不是 Claude Code 的 cross-session memory 优先级，而是 oh-my-opencode 的 session/compaction/continuation 优先级。

Claude 风格的长期 memory 仍然值得未来吸收，但应作为后置、受控、选择性开启的能力层，而不是现在抢占主路径优先级的核心设计。
