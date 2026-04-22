# VoidCode Agent 架构草案

## 文档状态

**状态：proposed**

本文档定义 VoidCode 后续的 agent 架构方向，但它不是当前仓库已经实现的功能说明。当前仓库仍然以 **runtime-centric 的单 agent 执行路径** 为现实基线；真正的 multi-agent execution semantics 仍未落地。

## 目标

本文档的目标不是给系统增加“更多 agent 名字”，而是为 VoidCode 定义一个**现代、可治理、可分阶段落地**的 agent 架构。它需要满足三个前提：

1. 不破坏 `runtime/` 作为系统控制面的现实边界；
2. 不夸大当前单 agent 路径，明确哪些角色只是未来 preset；
3. 为 skill、hook、MCP、LSP、ACP 等能力面预留清晰的 agent 组合层，避免未来把 agent preset、运行时治理和客户端表现层重新混在一起。

## 当前现实

在定义 agent 架构之前，必须先明确今天仓库已经实现了什么。

- `runtime/` 仍是产品级控制面；
- `graph/` 仍负责执行步骤推进与编排，而不是产品治理；
- provider-backed 的单 agent 执行路径已经存在；
- `src/voidcode/agent/` 目录现在已经存在，但当前只是文档化的声明层；
- ACP 已进入最小的 runtime-managed transport / lifecycle 路径，但仍然不是当前可用的 agent-to-agent 控制面；
- 真正的 multi-agent delegation、handoff、shared execution topology 仍未实现。

另外，还必须补一句更容易被忽略的现实：

- 当前 runtime 还没有 first-class background task / child-session / async notification substrate；
- 当前 hooks 也仍然只覆盖 runtime-owned 的 `pre_tool` / `post_tool`；
- 因此，“leader 异步调用其他 agent 并被通知回来”现在仍然不能被当成已经接近落地的执行语义。

因此，新的 agent 架构文档必须建立在这个现实之上，而不能写成“我们已经是一个成熟的多 agent 系统”。

## 设计原则

### 1. `agent/` 是声明层，不是新的 runtime

`voidcode.agent` 的职责应当是声明“一个 agent 是什么”，而不是接管执行、治理或恢复逻辑。

### 2. `runtime/` 继续拥有执行与治理真相

会话、审批、权限、工具执行、事件发射、持久化、恢复、transport、以及 MCP/LSP/ACP lifecycle 仍然必须由 `runtime/` 持有。

### 3. 小角色集优于角色泛滥

在当前阶段，VoidCode 不需要十几个 agent 名称。更好的方案是先定义一组**职责边界清楚、未来可扩展**的现代角色。

### 4. 先对齐单 agent 主路径，再演进到协作

新的 agent 架构必须先能映射到今天的 provider-backed 单 agent 路径；只有当 skill execution、managed LSP、ACP 等能力面变得真实可用时，才应向 delegated / multi-role execution 扩展。

### 5. 异步 agent 需要 capability substrate，不只是 hook surface

如果未来要支持：

- `leader` 异步调用 `worker` / `explore` / `researcher`
- 主会话继续推进或等待
- 子 agent 完成后再可靠通知主 agent

那么至少需要先补齐这些底层能力：

- background task 对象与状态机
- parent / child session truth
- 结果读取与 transcript 回收路径
- 主 agent 的通知注入路径
- 完成、失败、取消、超时等生命周期语义
- approval / permission 的继承边界
- restart 后的持久化与 resume 语义

hook 在这里很重要，但它更多是通知与干预层，而不是异步 agent 本身的执行模型。

这也意味着：如果为了敏捷开发而“借用 LangGraph”，它最适合借来加速 workflow / branching / supervisor-worker orchestration 的实验，而不是拿来替代 background task substrate。本质上，LangGraph 可以帮助我们更快搭建**编排层**，但不能单独补出 task truth、child-session、notification、result retrieval 或 approval/resume governance。

## 建议的角色集

### 1. `leader`

`leader` 是主用户入口，也是当前阶段唯一应当映射到真实执行路径的 agent 角色。

职责：

- 理解用户意图；
- 决定是直接执行、先分析还是先收集上下文；
- 为当前任务提出希望启用的 tool / skill / capability 组合；
- 负责输出面向用户的进度与结论；
- 在未来 delegation 落地后，负责把任务拆给其他角色。

在当前实现阶段，`leader` 实际上就是对现有单 agent provider path 的**命名和 preset 化**，而不是新的执行引擎。

### 2. `worker`

`worker` 是未来的 focused executor，用于承担更窄、更具体的执行任务。

职责：

- 接收一个更小的原子任务；
- 在受限工具集内完成实现或修改；
- 返回结构化执行结果；
- 不拥有产品级治理权。

`worker` 在当前阶段应被视为**未来 preset**，而不是当前仓库已经支持的 delegation runtime。

### 3. `advisor`

`advisor` 是只读或近只读的判断型角色，负责提供：

- 架构建议；
- 风险判断；
- 设计 review；
- 计划 review；
- 失败后的方向修正。

这个角色对标的是“咨询型 agent”，而不是执行型 agent。它的关键价值是**工具权限更窄、输出责任更清楚**。

### 4. `explore`

`explore` 是仓库内部探索角色，负责在本地 workspace 边界内收集代码结构与实现上下文。

职责：

- 仓库内部搜索；
- 文件与目录定位；
- 代码模式与调用链探索；
- 为 `leader`、未来的 `worker` 或 `advisor` 返回精炼的仓库内上下文。

这个角色应当保持只读，并尽量绑定本地探索类能力，而不是承担外部资料研究。

### 5. `researcher`

`researcher` 是收集上下文的只读角色，负责：

- 外部文档/示例检索；
- 外部仓库与公开实现调研；
- 上下文材料整理；
- 将调查结果返回给 `leader`、未来的 `worker`、`advisor` 或 `product`。

它和 `explore` 的区别在于：`explore` 面向**本地代码库内部结构理解**，`researcher` 面向**外部资料与公开实现研究**。

### 6. `product`

`product` 是需求对齐与验收口径角色，用于补上"实现正确，但不一定做的是对的东西"这一层空缺。

职责：

- 把用户原始需求收敛成更稳定的问题定义；
- 识别需求中的隐含验收标准、边界与非目标；
- 评估当前方案是否偏离产品目标；
- 在实现前或实现后，从需求一致性角度提出修正意见。

`product` 不应成为新的 orchestrator，也不应替代 `leader`；它更像是一个偏只读/近只读的产品判断角色，负责确保任务没有在技术实现中偏离用户真正想要的结果。

在 v1 中，`product` 的语义是**同步规划与对齐**：它由 `leader` 在同一执行路径内同步调用，不拥有独立 session、background task 或异步通知路径。`product` 的输出是结构化判断文本，直接注入当前会话上下文，不触发 delegated execution。

## 为什么保留这六类角色

这六类角色已经足够覆盖当前和 post-MVP 的主要协作语义：

- `leader`：面向用户的主编排者；
- `worker`：受限执行者；
- `advisor`：高价值判断者；
- `explore`：仓库内部探索者；
- `researcher`：外部资料研究者；
- `product`：需求对齐与验收口径把关者。

如果后续真的出现新的稳定任务类别，再在这个基础上增加更多角色才更合理。当前不宜一开始就引入过多角色，否则容易让文档先于系统真实能力膨胀。

## `agent/` 层应该承载什么

根据当前 `docs/agent-boundary.md` 的边界约束，`agent/` 最适合承载**声明式 agent preset / manifest**。建议包括：

- agent id / name
- role / mode metadata
- prompt / profile
- tool allowlist 或默认 tool set
- skill 引用
- hook preset 引用
- MCP profile 引用
- provider / model preference metadata
- 可选 routing hints（仅元数据，不是执行逻辑）

这些内容应当被 runtime 解析和消费，而不是由 `agent/` 自己执行。

当前建议的文档化落点是：

- `src/voidcode/agent/README.md`：角色层总览
- `src/voidcode/agent/<role>/README.md`：每个角色自己的职责、权限、建议 skills / hooks 与当前状态

这里还要明确一个边界：这些 README 里写的“建议 hook / 建议能力”是 preset intent，不代表 runtime 今天已经支持对应 lifecycle phase。

## 哪些能力必须继续留在 `runtime/`

新的 agent 架构不能削弱 runtime 的控制面职责。以下内容仍然必须留在 `runtime/`：

- session 真相与持久化
- approval / permission 决策
- runtime event emission / routing
- transport 与 client-facing truth
- tool registry 与 tool invocation
- hook 实际执行时机
- config resolution
- persistence / resume / replay correctness
- provider fallback 与 execution governance
- MCP / LSP / ACP lifecycle truth

这意味着 `agent/` 能描述“一个 agent 默认要带什么配置”，但不能拥有“系统最终如何执行、如何治理、如何恢复”的权力。

## 与 `graph/` 的关系

`graph/` 继续负责步骤推进和执行编排，但不应被当作 agent 配置层。

更准确地说：

- `agent/` 负责声明角色 preset；
- `runtime/` 负责解析 preset、治理能力面、发射事件；
- `graph/` 负责执行步骤推进与 orchestration path。

未来即使 multi-agent workflow 真的扩展了 graph 编排范围，也不应改变这三层的分工。

## ACP 在这套架构中的位置

当前 `src/voidcode/runtime/acp.py` 只暴露了：

- disabled / managed adapter state
- connect / disconnect / fail / request envelope
- 少量 runtime 事件

这说明 ACP 当前的合理定位仍然是：

> 一个保留中的、runtime-managed 的控制面能力边界。

它现在还不是：

- agent-to-agent messaging bus
- multi-agent routing plane
- 可恢复 delegated execution infrastructure
- 产品级 supervisor / worker transport

因此，在新的 agent 架构文档中，ACP 应被写成**未来可承载协作控制面的受管能力边界**，而不是当前现成可用的多 agent 基础设施。

同时，对 async agent 设计来说，ACP 也不应该被误写成唯一前提。真正更靠前的阻塞项通常是：

- runtime 是否已有 background task truth
- session 是否支持 parent / child 关系
- leader 是否能被可靠通知
- background result 是否可检索 / 可恢复

只有这些运行时能力先成立，ACP 才更像是协作控制面的放大器，而不是空中楼阁。

## 分阶段推进建议

### Phase 0：先定义角色与边界

在文档层面明确：

- 角色集只有 `leader` / `worker` / `advisor` / `explore` / `researcher` / `product`
- `leader` 对应今天的真实路径
- 其余角色是未来 preset
- `agent/` 只负责声明层

### Phase 1：让 runtime 支持 `leader` preset

第一步不是多 agent，而是让 runtime 能够解析并应用 `leader` 的 preset 到现有 provider-backed 单 agent 路径中。

这一步的意义是：先把 agent 从“概念”变成“可被 runtime 消费的结构”。当前实现已经把 `leader` preset 接入 runtime config / request metadata / provider-backed single-agent path；`prompt_profile`、`model`、`execution_engine`、`tools`、manifest `skill_refs`、`skills` 与 `provider_fallback` 都会影响当前 single-agent runtime truth 并随 session 持久化。同时，runtime 会显式拒绝把 future role preset 当作 active execution agent。

### Phase 2：引入只读辅助角色

当 skill execution、事件语义和基础能力面更稳定后，再优先引入：

- `advisor`
- `explore`
- `researcher`
- `product`

原因是它们的工具权限更窄、风险更低，也更容易先在 runtime 内形成受控的辅助调用路径。其中：

- `explore` 先承接仓库内只读探索；
- `researcher` 承接外部资料与公开实现调研；
- `product` 承接需求澄清、验收标准与范围对齐。

在这一阶段，比较现实的目标仍应是**同步或受限的辅助调用语义**，而不是直接许诺可靠的异步 subagent orchestration。其中：

- `product` 在 v1 中仅作为同步规划与对齐语义存在，由 `leader` 在同一执行路径内调用，不产生独立 session 或 background task。

### Phase 3：再评估 `worker` delegation

只有当：

- runtime-managed skill execution 已真实可用；
- managed LSP 更成熟；
- ACP 或等价控制面能力开始变得有意义；
- 恢复 / replay / approval 语义能覆盖 delegated work；
- background task / child-session / leader notification / result retrieval substrate 已经成立；

才适合引入 `worker` 这类真正执行型的 delegated role。

如果这些能力还没有成立，那么文档里最多只能把 async delegation 写成明确的 post-MVP 研究方向，而不能写成一个只差几个 hook 的短期落地点。

## 明确非目标

本文档明确**不**主张：

- 当前已经实现多 agent 协作
- 当前已经有 category routing runtime
- 当前 ACP 已经能承载 agent handoff
- 让 `agent/` 吞掉 runtime 的治理职责
- 在当前阶段就定义十几个角色 preset

## 为什么 `product` 值得单独存在

在当前阶段，最容易出现的问题并不总是“代码写不出来”，而是：

- 需求被技术实现稀释；
- 任务被不自觉扩大；
- 验收标准没有被提前收敛；
- 做出了技术上正确但产品上不够对齐的方案。

因此，`product` 角色是有必要的，但它不应该是一个“会写代码的 PM 替身”，而应是一个**以需求对齐和验收口径为中心的判断角色**。这类角色最适合在实现前做范围收敛、在实现后做结果对齐，而不是直接进入主执行链路。

## 结论

VoidCode 最合理的 agent 架构，不是照搬外部项目的命名或角色数量，而是在当前 runtime-centric 架构上，先建立一个**薄的、声明式的 agent preset 层**。

当前建议的现代角色集是：

- `leader`
- `worker`
- `advisor`
- `explore`
- `researcher`
- `product`

其中只有 `leader` 应当映射到今天真实可运行的单 agent 主路径，其余角色是 post-MVP 阶段为了 delegation、review、仓库内探索、外部研究、需求对齐和协作而预留的 preset。这样做既能吸收外部成熟系统的结构经验，又不会脱离 VoidCode 现在的真实边界与实现状态。
