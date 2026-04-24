# Runtime-Owned Scheduler 设计

## 状态

- 状态：proposed
- 范围：design-only
- 目标仓库：`voidcode`

## 背景与动机

VoidCode 当前已经具备清晰的、以 runtime 为中心的执行边界：`VoidCodeRuntime` 负责 `run` / `stream` / `resume` 入口、审批连续性、事件发射、provider fallback，以及 session persistence。基于 SQLite 的存储也已经承担了 session、事件重放、pending approval 和 resume checkpoint 的本地真相源职责。

这意味着 scheduler 应当是一个 runtime concern，而不是 client concern。

本文档的目标，是定义一个第一阶段的 scheduler 设计：它在产品形态上尽量接近大家对 Claude Code 风格 scheduled runs 的直觉预期，但同时仍然严格服从 VoidCode 当前已有的架构边界：

- client 仍然保持轻量
- 执行仍然通过 runtime boundary 进入系统
- session truth 仍然是 local-first 且可 replay 的
- approval 和 recovery semantics 仍然由 runtime 拥有

这不是一个关于独立云调度器、通用 background-task framework，或者 daemon-first 重写的提案。

## 目标

Phase 1 的 scheduler 工作应当达成以下目标：

1. 为本地 scheduled runs 增加一个 runtime-owned scheduling model。
2. 让 schedule dispatch 继续走现有 runtime execution path，而不是发明一条平行执行路径。
3. 在与 session 相同的、workspace-owned 本地真相域中，持久化 schedule definitions 和 scheduler state。
4. 保持当前的 session、replay、approval 和 checkpoint 语义不被破坏。
5. 让第一版实现足够小，可以在不重写 `runtime/`、`graph/` 或 client contracts 的前提下落地。

## 非目标

Phase 1 **不**打算提供以下能力：

- 云端或分布式调度
- 通用 job queue 或 background-task 平台
- graph-owned scheduling
- 由 CLI、Web 或 TUI 拥有的 client-owned scheduling
- UI 直接执行 tool
- 进程停机期间所有 missed fires 的 backlog catch-up 与回放
- 同一个 schedule 的并发重叠运行
- 默认复用同一个 long-lived session 来承载 recurring scheduled runs
- 把 daemon-first 或多进程 scheduler coordination 当作基础前提

## 当前运行时基线

scheduler 设计必须贴合当前代码库，而不是围绕一个尚不存在的理想化重写方案展开。

### 已经存在的 runtime ownership

`src/voidcode/runtime/service.py` 当前已经集中负责：

- `run`、`run_stream`、`resume` 和 `resume_stream`
- permission resolution 与 approval pause
- hook execution
- event emission 与 event renumbering
- provider fallback 与 runtime config 应用
- 通过 `SessionStore` 实现 session persistence

### 已经存在的 storage truth

`src/voidcode/runtime/storage.py` 当前已经通过 `.voidcode/` 下的 SQLite 作为本地真相源，持久化以下信息：

- session metadata
- session event history
- pending approval state
- resume checkpoints

这点非常关键，因为 scheduler 不应为 execution history 再引入第二套真相源。它可以增加 schedule-specific state，但实际 run truth 仍应保持为 session-scoped。

### 已经存在的 client boundary

CLI 和 HTTP transport 当前都只是 runtime boundary 的消费者，而不是 execution semantics 的拥有者。scheduler 应当保持这个模型：通过 dispatch 正常的 runtime runs 进入系统，而不是在 transport-specific 的代码路径里绕开 runtime。

## Scheduler 的 ownership boundary

scheduler 应当是 **runtime-owned**。

这意味着：

- schedule definitions、due-run decisions 和 dispatch policy 属于 runtime layer
- scheduled execution 必须以正常 `RuntimeRequest` 的形式进入系统
- session persistence、approval handling、replay 和 checkpoints 都继续由 runtime 拥有

这也意味着 scheduler **不**应当由以下位置拥有：

- `graph/`
- CLI command handlers
- HTTP handlers
- Web 或 TUI clients
- 一个无关的通用 worker subsystem

### 一个重要细节

runtime-owned **并不**意味着每个 `VoidCodeRuntime` 实例内部都要嵌入一个 long-lived timer loop。

更合理的做法是：runtime boundary 拥有 scheduler semantics 与 persistence，而真正负责 tick scheduler 的本地 host process 可以保持轻量。未来无论是 `serve` 邻近的 host loop，还是一个专门的本地 scheduler entrypoint，都只是驱动 polling 的外壳；权威的 scheduling model 仍然属于 runtime layer。

## Phase 1 形态

推荐的第一落地切片是 **internal-scheduler-first**。

这意味着 Phase 1 应该包括：

- 持久化的 schedule definitions
- 一个检测 due schedules 的本地 poller
- 由 runtime dispatch 的 scheduled runs
- 从 schedule 到 sessions 的最小 run indexing
- 明确的 overlap 与 missed-fire policy

Phase 1 应当避免：

- 先搭一个通用 async work platform
- 先做 permanently detached 的 daemon model
- 把多个本地 scheduler host 同时操作同一个 workspace 当作正常支持模式

## 调度生命周期

Phase 1 的 scheduling lifecycle 应当是：

1. schedule definition 被存入 workspace-local 的 scheduler state。
2. 本地 scheduler host 轮询是否存在 due schedules。
3. 当某个 schedule 到期时，scheduler 创建一个带有 schedule metadata 的正常 `RuntimeRequest`。
4. `VoidCodeRuntime` 通过现有 run path 执行该 request。
5. 运行结果作为正常 session 持久化，拥有正常的 events、output、approval state 和 checkpoint 行为。
6. scheduler state 在 schedule/run-index 这一层记录 outcome，但不替代 session truth。

这样 scheduler 扮演的是 runtime runs 的 initiator，而不是第二套 execution engine。

## 持久化模型

Phase 1 应当继续使用现有的 workspace-local SQLite 真相域。

持久化职责应拆分为：

- **sessions 继续作为 execution truth**
- **schedules 负责存储 future intent 与 dispatch state**

从概念上讲，schedule-oriented state 至少需要覆盖：

- schedule identity
- 用户定义的 prompt 或 request payload baseline
- schedule expression 或 interval policy
- timezone policy
- enabled / disabled state
- next due time
- last attempted fire time
- last successful fire time
- single-flight enforcement 所需的 overlap policy state
- 从 schedule 指向已发出的 session ids 或 recent run summaries 的关联信息

scheduler 不能把 schedule records 当成 session event history 的替代品。如果某个 scheduled run 需要 replay、inspection、approval resolution 或 debugging，其真相源仍然是该 session record 及其 events。

## Scheduled runs 的 session 语义

Phase 1 默认应当采用 **每次 schedule occurrence 都创建一个 fresh session** 的模型。

这是最合理的默认值，因为当前 storage、replay 和 approval 语义都是 session-centric 的。如果复用同一个 session 去承载 recurring runs，会立刻让以下问题变复杂：

- replay boundary
- approval continuity
- event ordering
- checkpoint meaning
- 多次触发之间的 failure diagnosis

### 必须明确的默认行为

- 每次 due schedule occurrence 都创建一个新的 `session_id`
- session metadata 记录 schedule provenance，例如 `schedule_id`、trigger timestamp 和 initiator
- replay 保持为 session-scoped
- session resume 保持为 session-scoped

### 明确延期的行为

“让 recurring work 持续在同一个 long-lived conversational session 中进行” 这个想法应当被显式延后。如果未来它变成产品需求，就应该把它视为一个更高复杂度的设计问题，而不是悄悄塞进 Phase 1。

## Approval、resume 与 replay 语义

scheduled runs 应当继续被视为正常的 runtime runs。

因此：

- 如果某个 scheduled run 走到了 approval boundary，它应进入现有的 `waiting` session state
- pending approval 应通过现有 runtime persistence path 存储
- approval resolution 应走现有的 resume / approval flows
- replay 应继续使用现有的 session event replay

Phase 1 **不**应当为了 scheduled work 再发明第二套 approval model 或 background-run model。

### 实际含义

如果某个 scheduled run 因 approval 被阻塞，scheduler 不应自动“越过”这个边界。这个 run 此时已经成为一个正常 session，应继续使用与其他 session 一样的 runtime-owned approval semantics 来处理。

## Overlap 策略

Phase 1 应当采用 **每个 schedule 的 single-flight overlap semantics**。

推荐规则：

- 如果某个 schedule 的上一次 run 仍处于 `running` 或 `waiting`，那么同一个 schedule 的下一次 due tick 应被 skip 或 coalesce，而不是再启动第二个并发 run

这样做能让第一版模型足够直观，也能避免 schedule-level concurrency 把当前 runtime/session path 冲乱。

Phase 1 不应支持：

- 单个 schedule 的无界 backlog queue
- 默认允许同一 schedule 并发运行
- 把两个 due occurrences 隐式合并进同一个 session lineage

## Missed fires、停机与时钟行为

即使 Phase 1 刻意保持保守，这里也必须先定义清楚策略。

### 进程停机期间的 missed fires

Phase 1 **不**应承诺在停机后回补所有 missed scheduled occurrences。

推荐默认行为：

- 当 scheduler host 重启时，它基于持久化 schedule state 重新计算下一个有效的 due run
- 停机期间错过的 occurrences 默认不作为 backlog 被重放

这样可以让 Phase 1 保持小而明确，避免不知不觉变成一个 queueing system。

### Timezone 策略

schedule definitions 应携带明确的 timezone interpretation，或者至少在文档中定义清楚默认 timezone 行为。实现不应依赖隐藏的 process-local 假设。

### 时钟跳变

本地时钟跳变与时间不连续，应被视为未来实现中的 correctness concern。Phase 1 应先保守定义行为，而不是在 local best-effort polling 之上作出过强保证。

## Retry 与 failure 策略

Phase 1 的 scheduler failure policy 应保持克制。

### Dispatch-level failures

如果 schedule dispatch 在 session 创建之前就失败，scheduler 应在 schedule-oriented state 中记录失败信息，使其可见且可诊断。

### Runtime-level failures

如果 session 已经创建，而 run 随后失败，那么 failure 仍属于正常 session lifecycle：

- session 变为 `failed`
- 失败细节保留在正常 runtime events 以及 session output / history 中
- schedule-level state 可以记录 summary pointer，但不能替代 session truth

### Retry 范围

Phase 1 可以支持一个最小的 retry / backoff policy 来处理 scheduler dispatch failures，但不应扩展成复杂的策略矩阵。它不需要完整的 queue semantics、任意 retry orchestration，或 durable worker leasing。

## 事件与可观测性预期

scheduled runs 应尽量复用当前 runtime event model，而不是过早推动一轮新的 contract rewrite。

对于 Phase 1：

- scheduled runs 应发出与正常 runs 相同的 core session-scoped runtime events
- session metadata 应足以标识 schedule provenance
- scheduler-specific 的可观测性增强可以先停留在 runtime-internal 或 design-level guidance 层，再决定何时进入稳定的 client-facing contracts

这一点很重要，因为 `docs/contracts/runtime-events.md` 当前定义的是一个以 session 为作用域的稳定事件词汇表。scheduler 设计的第一落地切片应与这个模型兼容。

## Client 与 transport 的关系

client 仍然只是 runtime truth 的消费者。

这意味着：

- CLI、Web 和 TUI 应把 scheduled runs 观察为普通 sessions
- client 后续可以增加 schedule metadata 展示和 schedule administration features
- client 不应拥有 timer loops，也不应直接拥有 execution behavior

HTTP layer 和 CLI 将来可以暴露 schedule management surfaces，但它们仍应只是 transport / control entrypoints，而不是 scheduler 的真相源。

## 与 execution engines 的关系

scheduler 应 dispatch 到 runtime boundary 中，并且对最终由 resolved runtime config 选中的具体 execution engine 保持无感。

换句话说：

- scheduler 不应在 ownership model 上对 `deterministic` 和 provider-backed/delegated execution path 做特殊分支
- engine selection 仍然属于 runtime config 行为
- scheduled execution 仍然走 runtime 治理下的 tool、permission、hook 与 persistence path

这样可以保证 scheduler 与现有 post-MVP 方向保持一致：runtime 可以治理多个 execution engines，但这些 engines 不应反过来接管 product control-plane 的职责。

## 配置与策略输入

scheduler 应消费 **resolved runtime policy**，而不是来自各个 transports 的原始 client 输入。

这意味着未来实现应继续遵守 runtime 当前已经在其他能力上采用的 ownership 规则：

- runtime defaults
- user config
- project config
- environment overrides
- request 或 command overrides

schedule definitions 可以携带 schedule-specific execution defaults，但这些值仍应通过 runtime-owned config resolution 解释，而不是发明一套平行配置系统。

## Phase 1 不支持的 multi-host 行为

Phase 1 应明确把 **多个本地 scheduler hosts 同时操作同一个 workspace database** 视为 unsupported。

在没有 claim / lease model 的前提下，多 host 会带来 duplicate firing、ownership overlap 或 scheduler state 不一致的问题。未来如果 multi-process attachment 真的成为需求，可以再增加轻量协调机制。

对于 Phase 1，“每个 workspace 仅一个本地 scheduler host” 是一个诚实且安全的边界。

## 明确保持在范围外的后续项

以下方向是合理的 future follow-ups，但不应折叠进这次第一阶段设计切片中：

- schedule CRUD transport 与 client UX
- 用于 multi-host coordination 的 durable lease 或 leader election
- 停机后的 catch-up backlog policy
- 有意延续同一 long-lived session lineage 的 recurring runs
- 进入稳定 client contracts 的 richer scheduler-specific event vocabulary
- 脱离调用者生命周期、always-on 的 daemonized local scheduling
- remote 或 cloud-managed scheduling

## 面向未来实现的验证计划

这个 PR 是 design-only，但设计文档仍然需要定义什么叫做“正确”。

后续实现至少应验证以下行为：

1. due schedule 会 dispatch 一个正常 runtime run，并持久化一个新的 session。
2. scheduled runs 会按顺序发出正常的 session-scoped runtime event sequence。
3. 一个请求 approval 的 scheduled run 会成为正常的 `waiting` session，并能通过现有 approval path 恢复。
4. 一个失败的 scheduled run 会成为正常的 `failed` session，且不会破坏 scheduler state。
5. single-flight overlap policy 能阻止同一 schedule 的并发运行。
6. 本地 scheduler host 重启后，默认不会回放无界 backlog 的 missed fires。
7. session replay 足以检查 scheduled execution，而不需要额外的 scheduler-specific replay pipeline。
8. scheduler 行为与当前 execution engine selection 兼容，而不会被绑定到单一 engine 上。

合适的验证落点，大概率仍然是 runtime-focused unit tests，以及围绕 storage、dispatch、approval continuity 和 replay 的 integration coverage。

## 开放问题

以下问题有意保留给后续实现或 follow-on design：

1. Phase 1 最先支持的 schedule expression format 应该是什么？
2. 本地 scheduler host entrypoint 应放在哪里：独立 command、`serve` 邻近模式，还是另一个 runtime-owned host surface？
3. 在第一版实现中，有多少 scheduler-specific metadata 需要进入稳定的 client contracts？
4. dispatch-level scheduler failures 只通过 schedule state 暴露就够了，还是也要发出 additive runtime events？
5. 当 multi-host 或 daemonized operation 真正成为需求时，基于 SQLite 的 claims 是否足够，还是需要更强的 coordination model？

## 总结

对 VoidCode 来说，第一版正确的 scheduler 设计应当是一个 **runtime-owned、internal-scheduler-first** 的模型：它把 **正常的 runtime runs** dispatch 到 **fresh sessions** 中，同时让 approval、replay、checkpoints 以及 execution truth 都继续留在它们当前已经归属的位置——也就是 runtime/session boundary 内部。

这样做能让设计尽量贴近当前架构，避免再发明第二套执行模型，也能在不破坏仓库中已经建立的 control-plane 原则的前提下，为 Claude Code 风格的 scheduled runs 提供一条可信的演进路径。
