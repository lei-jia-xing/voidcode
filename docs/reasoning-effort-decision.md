# 关于 reasoning effort 抽象的决策草案

## 文档状态

**状态：proposed**

本文档记录一个当前阶段的架构决策：**VoidCode 暂不引入通用的 reasoning effort / thinking level 抽象**（例如 `low` / `medium` / `high` / `xhigh`）。

这是一份决策草案，不表示仓库今天已经拥有该能力。

## 问题

在 OpenCode / oh-my-opencode 一类系统中，常见会提供一层 reasoning effort / thinking budget 抽象，用于控制模型在正式输出前花多少内部推理预算。典型目的包括：

- 为复杂任务提供更高的推理预算
- 为简单任务降低延迟与成本
- 在多模型、多 provider 场景中提供一个较统一的“思考强度”旋钮

问题在于：**VoidCode 当前是否有必要引入这样一层抽象？**

## 当前现实

在做这个判断之前，必须先明确今天仓库真正已经实现了什么。

- 当前 runtime 还没有通用的 reasoning effort 配置字段
- 当前 `RuntimeConfig` 不暴露 provider-agnostic 的 thinking / reasoning budget 控制面
- 当前 provider 层存在一些带有“thinking”倾向的模型别名，但它们本质上仍是**模型选择**，不是独立的预算旋钮
- 当前 provider 协议里已经存在 `reasoning` stream channel，但它表达的是**可观测性输出**，不是配置输入
- 当前仓库仍处于 pre-MVP，并且优先目标仍是单智能体 runtime 稳定性、默认体验与主路径产品化

因此，今天的仓库已经有“可观测推理输出”和“通过选不同模型来粗粒度控制思考能力”这两件事，但还没有一层统一的 reasoning effort 抽象。

## 决策

**现在不引入通用的 reasoning effort / thinking level 抽象。**

当前阶段继续采用更简单的控制方式：

- 通过模型选择表达“更快”还是“更会想”
- 把现有 `reasoning` stream channel 视为观测面，而不是配置面

## 为什么现在不做

### 1. 当前有更低成本的替代手段

对于今天的 VoidCode，用户已经可以通过模型选择来表达粗粒度的行为差异，例如选择更快的模型，或选择 reasoning-capable model alias。

这意味着：在当前阶段，reasoning effort 抽象并不是唯一手段，也不是最短板。

### 2. 这不是一个“小字段”，而是一层横切抽象

如果要正确实现 reasoning effort，它不会只是给 `RuntimeConfig` 多加一个字符串字段。

它会横切到：

- runtime config 解析
- CLI / client override
- session 恢复与持久化
- provider request contract
- provider adapter 映射
- 测试矩阵

换句话说，这层能力天然会进入 runtime-owned configuration 与 resume semantics，而不是孤立存在。

### 3. provider 语义目前并不天然统一

不同 provider 的 thinking / reasoning 控制语义并不等价：

- 有的提供显式 `reasoningEffort`
- 有的提供 `thinking` 或 budget token
- 有的只提供 `variant`
- 有的根本没有这层能力

因此，今天如果强行抽象成统一的 `low` / `medium` / `high` / `xhigh`，大概率只是一个“看起来统一、实际上靠猜测映射”的薄层包装。

### 4. 过早锁定 schema 成本很高

一旦 reasoning effort 进入：

- `RuntimeConfig`
- `.voidcode.json`
- `SessionState.metadata["runtime_config"]`

它就不再是一个随便可改的实验字段，而会变成恢复语义、兼容语义和配置演进语义的一部分。

如果在 provider 语义尚不稳定时提前锁定这一层，后续修改成本会很高。

### 5. 当前主线优先级不是能力扩张

当前阶段更重要的事情仍然是：

- 提升第一次真实任务成功率
- 收紧单智能体 runtime 主路径
- 完善默认配置与默认可用性
- 继续补齐 background task / notification / result retrieval 等 substrate

相比之下，reasoning effort 更像“值得拥有的高级调优旋钮”，而不是当前 MVP 主路径的短板。

## 当前替代策略

在不引入通用 reasoning effort 抽象的前提下，当前推荐策略是：

1. **继续使用模型选择作为粗粒度控制面**
2. **把 provider-specific thinking 能力留在 provider 侧，而不是提前提升为产品级统一抽象**
3. **把 `reasoning` stream channel 当作观测面，而不是配置承诺**

这能让系统继续保持：

- runtime 配置面更小
- provider 适配责任更清晰
- 当前 resume / persistence 语义不被提前扩张

## 未来何时再考虑

只有在以下触发条件之一出现时，才值得重新评估是否引入 reasoning effort 抽象：

### 触发条件 A：至少两个重要 provider 都稳定支持原生 reasoning budget 控制

如果至少两个主要 provider 已经稳定提供 thinking / reasoning 控制，并且语义足够清晰，那时引入一层统一 hint 才更有意义。

### 触发条件 B：产品明确需要“同一模型，不同思考预算”

如果真实用户需求已经不是“换个模型”，而是明确想要：

- 同一个模型
- 不同延迟 / 成本 / 质量档位

那时 reasoning effort 才会成为真正的一等产品配置，而不是内部实验字段。

### 触发条件 C：模型选择已经不足以表达 UX 需要

如果只靠模型选择已经不能满足：

- 快速模式
- 深思模式
- 成本受控模式

这种明显不同的运行体验，那么再引入 reasoning effort 抽象就有现实价值。

## 如果未来要做，正确边界是什么

如果未来真的引入 reasoning effort，正确边界应当是：

### 1. 它必须是 runtime-owned

这层能力应当由 runtime 拥有，而不是由：

- client 拥有
- graph 拥有
- prompt 约定拥有

原因很简单：它会进入配置优先级、恢复语义和 provider 调度语义，因此必须由 runtime 控制。

### 2. 它应是可选 hint，而不是强保证

未来更合理的形状应当类似：

- `reasoning_effort: str | None`

它表示一个 **runtime-level hint**，而不是对 provider 行为的强一致保证。

provider adapter 应当可以：

- 映射
- 降级
- 忽略

而不是要求所有 provider 都精确实现相同语义。

### 3. 它只应先作用于 provider-backed single-agent path

deterministic execution 不应受这层能力影响。

如果未来要做，首个作用范围应当仅限：

- provider-backed
- single-agent

而不是一开始就扩张到 graph orchestration、multi-agent 或客户端 UX 全面联动。

### 4. provider 映射应继续留在 provider 层

runtime 持有的是统一 hint；真正把它翻译成 provider 请求参数的责任，应继续留在 provider adapter 层。

这样才能避免：

- runtime 层被 provider-specific 细节污染
- client 直接依赖某个 provider 的私有 thinking 语义

## 非目标

本文档不主张：

- 当前立刻实现 reasoning effort
- 把 provider-specific budget 字段直接透出给所有客户端
- 把 `reasoning` stream channel 误当成配置能力已经存在
- 把 reasoning effort 作为当前 MVP 的必需能力

## 结论

结论很明确：

**reasoning effort 抽象是未来可能值得引入的能力，但当前阶段不应进入 VoidCode 的 runtime config 与产品主路径。**

现在更合理的策略是：

- 继续用模型选择做粗粒度控制
- 把现有 reasoning 输出留在观测面
- 等 provider 支持、产品需求与配置边界都成熟以后，再把它作为 runtime-owned optional hint 正式引入
