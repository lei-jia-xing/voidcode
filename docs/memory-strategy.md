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
