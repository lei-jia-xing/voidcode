# VoidCode 多智能体架构规范（草案）

## 文档状态

- 状态：proposed
- 范围：design-only
- 目标仓库：`voidcode`

本文档是当前仓库关于 post-MVP 多智能体软件开发架构的**规范性参考**。后续涉及 multi-agent、delegation、runtime-managed orchestration、phase governance、artifact gate、rollback 的 issue 和 PR，都应先与本文档对齐，而不是各自重新定义术语。

## 为什么需要这份文档

当前仓库已经有不少与 agent、runtime、ACP、delegation 相关的设计讨论，但如果只有零散 issue，而没有一份统一的架构规范，不同人和不同模型很容易读出不同结论：

- 有人会把目标理解成开放式通用 agent 平台
- 有人会把目标理解成自由分裂 subagent 的实验系统
- 有人会把 ACP 误读成当前已经成型的 agent bus
- 有人会把 `agent/` 误读成新的 runtime

这些理解都不利于后续 issue 的稳定推进。

本文档的目标，就是先把系统级真相收口成一句清楚的话：

> VoidCode 的 post-MVP 多智能体方向，是一套运行在 `voidcode` 内部、面向软件开发全流程、遵循瀑布式阶段门控与强审计/强回滚原则的三层执行架构：`leader -> manager -> worker`。

## 当前现实

在定义目标架构之前，必须先明确仓库今天已经实现了什么。

- `runtime/` 仍是系统控制面，拥有 session、approval、permission、persistence、event、transport 与 capability lifecycle truth。
- `graph/` 仍负责 execution loop / step orchestration，而不是产品级治理边界。
- provider-backed single-agent path 已存在；deterministic engine 也已存在。
- `src/voidcode/agent/` 当前仍是声明层，不是独立执行 runtime。
- 真正的 multi-agent execution semantics 仍未落地。
- runtime 还没有完整的 manager/worker child-session substrate、可靠通知路径、完整的阶段治理状态机，以及面向三层架构的 artifact/gate contract。
- ACP 仍然只是 runtime-managed control-plane boundary 的早期基础，不是当前现成可用的 agent-to-agent messaging bus。

因此，本文档描述的是**目标架构与约束**，不是“仓库已经实现的现状说明”。

## 目标

本文档约束的目标架构，应满足以下前提：

1. 多智能体编排发生在 `voidcode` 内部，而不是依赖一个外部 orchestrator 仓库。
2. 架构服务于软件开发全流程，而不是面向开放式一般任务。
3. 流程遵循瀑布式阶段治理，而不是完全自由的自治协作。
4. 系统必须有明确的阶段门禁、结构化工件、可追踪审计和显式回滚路径。
5. runtime 仍然保持系统控制面地位，agent preset/template 不能反客为主。

## 非目标

本文档明确**不**主张：

- 把 VoidCode 定义成开放式通用 agent 平台
- 默认允许 agent 自由、无限制地继续创建更多 agent
- 把多智能体编排外包给另一个仓库或另一个长期驻留 orchestrator
- 让 `agent/` 接管 runtime 的治理职责
- 把 ACP 写成当前已经成熟可用的多智能体通信总线
- 把 issue 文本、prompt 文本或 PR 说明当成比本文档更高一级的架构真相

## 核心术语

### `phase`

软件开发流程中的阶段。本文档的规范性阶段集合为：

- `requirements`
- `architecture`
- `design`
- `implementation`
- `testing`
- `review`

第一轮实现如果因为范围控制需要合并 `architecture` 与 `design`，必须由上层治理逻辑显式声明，而不能在不同 issue 中隐式各自处理。

### `artifact`

阶段产物。包括需求文档、架构决策、设计说明、代码变更、测试报告、评审结论等。阶段是否完成，不由“写了很多自然语言”决定，而由工件是否满足约束决定。

### `gate`

阶段门禁。某一阶段必须通过的检查集合。未通过 gate，不能进入下一阶段。

### `stage report`

阶段报告。阶段执行结束时提交给上层治理逻辑的结构化报告，不是普通聊天总结。

### `rollback`

阶段失败后回到更早阶段重新工作的显式治理动作。rollback 必须带有原因、目标阶段和工件处理规则。

### `leader`

全局治理者。更像显式 phase-state-machine，而不是开放式人格化 agent。

### `manager`

阶段内编排者。负责将阶段目标拆解为 worker 任务，组织并行、汇总结果并提交阶段完成候选包。

### `worker`

受约束的、产物导向的、阶段专用的执行单元。worker 不是“给一段 prompt 就自由发挥”的通用代理。

### `worker_type`

worker 的类型定义，描述一个 worker 是什么、能做什么、必须产出什么。

### `task_instance`

某个具体 worker 这一次被分配的任务实例，包含输入材料、约束与目标产物。

## 规范性拓扑

本架构的规范性三层拓扑是：

```text
leader -> manager -> worker
```

进一步约束如下：

- `leader` 负责全局阶段治理
- `manager` 负责阶段内任务编排
- `worker` 负责受约束的任务执行与产物生成
- 默认不允许 `worker -> subworker`
- 人工确认是 leader phase governance 的一部分，而不是完全脱离系统的外部偶发事件

这意味着 VoidCode 未来如果支持 delegated execution，也应首先服务于这条拓扑，而不是先做一个开放式 agent spawning playground。

## 角色职责边界

### `leader`

`leader` 的核心职责是阶段治理，而不是深入执行细节。它应当是整个系统的全局决策者和状态机持有者。

`leader` 负责：

- 接收初始需求报告
- 判断当前是否可进入下一阶段
- 判断是否必须停机等待人工确认
- 审核阶段门禁是否通过
- 决定失败后是否回滚，以及回滚到哪一阶段
- 输出阶段总结与全局执行报告

`leader` 不负责：

- 直接执行大量细粒度任务
- 直接管理大量底层 worker 交互
- 决定某个具体文件如何修改
- 跳过 gate 直接宣布阶段完成

`leader` 的输入应是阶段完成候选包，输出应至少覆盖：

```yaml
phase_status: passed|blocked|failed|rollback_required
next_phase:
required_artifacts:
review_findings:
rollback_phase:
rollback_reason:
stage_summary:
stage_report:
```

### `manager`

`manager` 是阶段内项目经理 / 编排器。它接受 `leader` 的阶段任务，并在该阶段内部组织工作。

`manager` 负责：

- 将阶段目标拆解成多个 worker 任务
- 判断哪些任务可以并行
- 选择合适的 worker type 并实例化任务
- 汇总 worker 结果
- 处理 worker 结果之间的冲突
- 执行本阶段的强制质量检查
- 向 `leader` 提交阶段完成候选包

`manager` 不负责：

- 宣布全局进入下一阶段
- 自己修改阶段治理规则
- 任意创造新的顶层角色
- 绕过强制 gate 直接提交通过

`manager` 的结构化输出应至少覆盖：

```yaml
phase:
status: success|blocked|failed
artifacts:
quality_gates:
worker_results:
risks:
open_questions:
recommended_next_action:
```

### `worker`

`worker` 是受约束的执行单元，不是通用自由代理。

规范性原则是：

```text
worker = worker_type + task_instance
```

这意味着：

- worker 的能力边界来自 `worker_type`
- manager 只能实例化任务，不能任意重写 worker 的本质能力
- worker 的输入输出必须结构化
- 默认不允许 worker 再创建 worker

`worker` 的输出应至少覆盖：

```yaml
task_id:
worker_type:
status: success|blocked|failed
summary:
artifacts:
findings:
risks:
open_questions:
self_check:
confidence:
recommended_next_action:
```

## Worker 类型化约束

为了避免输出不可预测、审查困难、并行冲突和回滚失控，worker 必须以类型化目录的方式定义，而不是直接等于一段 prompt。

每个 `worker_type` 至少应定义：

- `name`
- `purpose`
- `phase`
- `allowed_tools`
- `disallowed_tools`
- `input_schema`
- `output_schema`
- `artifact_template`
- `quality_checklist`
- `max_turns`
- `can_write_code`
- `can_modify_docs`
- `can_call_subworkers`（默认 `false`）
- `memory_scope`
- `retry_policy`
- `failure_mode`
- `handoff_rules`

这类设计可以借鉴外部成熟系统的 agent type 结构，但不能照搬其开放式自由度。对 VoidCode 而言，更合理的 worker 形态是：

> 预定义类型 + 有限工具 + 固定产物格式 + 明确验收标准 + 默认不继续分裂子代理

## 阶段治理模型

### 阶段推进

规范性推进路径为：

```text
requirements -> architecture -> design -> implementation -> testing -> review
```

阶段推进规则如下：

- 只有 `leader` 可以决定进入下一阶段
- `manager` 只能提交“阶段完成候选包”
- `worker` 不能自行宣布阶段完成
- gate 未通过时，阶段状态必须是 `blocked`、`failed` 或 `rollback_required`，不能伪装为通过

### 人工确认

人工确认点是治理的一部分，而不是例外情况。系统至少要支持：

- 进入高成本阶段前的人类确认
- 关键风险未消除时的人类确认
- rollback 前的人类确认
- review 阶段后的最终人类确认

### 回滚

回滚不是“重新做一遍”这么模糊，而应是显式矩阵：

- 哪类失败回滚到哪一阶段
- 哪类工件可以保留
- 哪类工件必须作废
- 哪类失败不能自动推进，必须等待人工确认

如果这层规则没有被写清楚，系统就无法形成真正可治理的多智能体开发流程。

## 工件、门禁与报告契约

这套架构的稳定性，最终取决于工件和 gate，而不是 role 名称本身。

规范性要求如下：

- 每个 phase 必须有明确的 artifact catalog
- 每个 phase 必须有明确的 mandatory quality gates
- manager 提交给 leader 的必须是结构化 stage candidate package
- leader 输出的必须是结构化 stage report
- rollback 时必须说明哪些 artifacts 复用、哪些作废、哪些重建

因此，未来 issue 不应只写“支持 manager”和“支持 worker”，而必须同时写清楚：

- 本阶段要交什么工件
- 哪些 gate 是必过项
- 哪些输出字段是稳定 contract

## Ownership 边界

### `runtime/`

以下内容必须继续由 `runtime/` 持有：

- session truth
- parent / child session relationship
- delegated task lifecycle
- approval / permission governance
- persistence / replay / resume correctness
- event emission 与 notification routing
- tool registry 与 tool invocation
- capability lifecycle truth（含 MCP / LSP / ACP）

### `graph/`

`graph/` 继续负责 execution loop 和 orchestration path，但不应成为产品级治理真相层。

### `agent/`

`agent/` 更适合承载：

- `leader` preset
- `manager` preset
- `worker_type` catalog / template
- prompt / profile metadata
- tool allowlist metadata
- skill / hook / provider / model references

但 `agent/` 不能取代 runtime 的真相职责。

## ACP 在这套架构中的位置

ACP 可以成为后续的 typed control-plane contract 载体，但它不是当前三层架构成立的前提。

更靠前的前提是：

- runtime 是否拥有 delegated task truth
- session 是否拥有 parent / child relationship
- leader 是否能被可靠通知
- manager / worker 结果是否能被恢复、回放和审计

因此，ACP 的合理定位是：

> 后续用于表达 typed control-plane request/response 的受管能力边界，而不是当前最先要补的系统基础设施

## 后续 issue / PR 的使用规则

为了减少不同模型对同一方向的自由解释空间，后续相关 issue 和 PR 应遵守以下规则：

1. 必须把本文档作为规范性参考之一
2. 不得在 issue 文本中重新定义 `leader`、`manager`、`worker` 的职责边界
3. 必须显式写出本次工作是：
   - 架构定义
   - 阶段治理
   - manager 编排
   - worker type
   - artifact / gate contract
   - runtime substrate
   - ACP contract
   中的哪一层
4. 必须写明 `In scope` / `Out of scope`
5. 必须写明结构化 acceptance criteria，而不是只写愿景
6. 如果某条 issue 与本文档冲突，应优先更新本文档，再改 issue，而不是让 issue 各自漂移

## 结论

VoidCode 的 post-MVP 多智能体方向，不应是“更多 agent 名字”或“更自由的 agent 自治”，而应是：

> 一个运行在 `voidcode` 内部、以 `leader -> manager -> worker` 为规范性拓扑、以 phase governance / artifact contract / mandatory gate / rollback matrix 为核心的三层瀑布式软件开发架构

这份文档的价值，不在于替未来实现写更多愿景，而在于先把术语、边界、职责和非目标钉死。只有这样，后续 issue 才能被不同的人和不同模型以更接近的方式理解与实现。
