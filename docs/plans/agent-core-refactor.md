# Agent Core 重构实施计划

状态：计划已制定，架构迁移尚未实施。本轮仅清理已证实的冗余计算、错误版本说明和测试策略；不能把这些清理当作独立 core 已完成。

依据：[pi × VoidCode 可扩展性审计](../audits/pi-voidcode-extensibility.md)。审计是历史快照，不改写；本计划的当前状态须以源码和实际行为为准。目标覆盖审计 P0-1～P0-6、P1-7～P1-8、§5 的组合问题和 §8 全部十项验收，不把 roadmap 当成增加产品功能的授权。

## 目标与不可移动的边界

选择单 agent 的 **turn engine**，不把现有 `graph` 包装成不存在的 DAG/workflow 系统。

```text
provider adapters / tool implementations → core contracts + turn engine
                                            ↑
runtime host：policy / approval / redaction / persistence / recovery /
             lease / capability lifecycle / task supervision / composition
                                            ↑
                              CLI / TUI / HTTP / ACP
```

- core 必须实际完成 provider → tool batch → results → 下一轮 → 结束；没有 `VoidCodeRuntime`、SQLite、workspace、CLI、TUI、MCP 或 LSP 也能运行。无 workspace 不等于文件工具不需要 workspace：纯 core 和非文件工具不依赖它，文件工具显式要求它。
- runtime 仍是唯一的 authorization、approval、session truth、event persistence、recovery、execution ownership 和 capability lifecycle authority。core 只推进执行，不自行授权、写数据库、启动后台 manager 或恢复历史 side effect。
- provider 负责 normalized request/stream/result 和真实 wire 的解析；retry/fallback/recovery 由 runtime 决定。tool 通过显式 context 获得实际依赖，不从全局 `ContextVar` 读取 runtime truth。
- 原始 append-only history 与本次 model-facing transcript/projection 分开；provider view 不反写历史。保留 reasoning、tool-call/result pairing、分支和 continuity 语义，不复制 pi 的存储格式。
- clean cutover：每阶段迁移真实消费者并删除被替代的旧路径；最终无旧 graph aliases、兼容 re-export、私有 facade 或双控制面。运行中只能有一条 authority 路径，不能隐式双写。
- 上层信任已经建立的 typed lower contract；同进程不重复 serialize→parse、hash、复制或防御性校验。HTTP/JSON/SQLite、provider wire、工具输入、凭据、安全权限和数据完整性边界的校验保留。
- 只提取已有实现和确有消费者的接口。内存 host/store 是独立运行的实际实现；fake provider/tool 是确定性的验收 fixture，不是交付占位实现。

## 当前源码证据与复用起点

| 当前证据 | 影响与迁移落点 |
| --- | --- |
| `provider/protocol.py` 导入 `runtime.context.window.ToolResultView`，`graph/contracts.py` 和两种 graph 也依赖 runtime context | 只移动 graph 不会独立；P1 必须一起迁移 provider/tool/transcript 类型和所有消费者，处理 package import cycles |
| `graph/provider_graph.py::step` 持有 batch、session/run identity、approval-resume 判断并组装 provider turn；`pending_tool_call_count` 决定 safe boundary | P3 提取真实 turn/batch 算法，不是给旧 graph 加一个 engine facade |
| `runtime/run_loop.py::execute_graph_loop` 负责 context、steering、typed tool input、permission、intent、hook、execution、result、checkpoint 和 fallback | 抽出执行推进；治理、durable intent、safe checkpoint 和故障策略保留为 runtime host 行为 |
| `runtime/run_loop.py::_execute_resolved_tool_call` 已汇聚 native、approval-resume、`invoke_tool` 的 executor/progress seam；`runtime/tool_execution.py` 仍 bind hidden context | P2 复用 canonical execution boundary，避免为三条调用路径各造一套 ToolContext/executor |
| `tools/runtime_context.py` 使用 `ContextVar`，实际工具 context 隐藏；`ToolDefinition.read_only` 承担默认权限输入 | 显式 context + effect facts 全量迁移；default approval 语义不能因改字段而放宽 |
| `runtime/storage/sqlite.py` 的 `SessionStore` 同时覆盖 events、sessions、approvals、tasks 和维护；已有 SQLite owners/mixins | P4 按实际消费者拆窄 ports，复用存储算法和事务，不先造远程/JSONL backend |
| `runtime/tool_materializer.py` 已组合 base/MCP/local provenance，`RuntimeToolMaterialization` 可 scope registry | 复用 materialization seam；generation 有恢复消费者，不能因它是 hash 就删除 |
| `runtime/service.py::_agent_capability_snapshot` 组装 agent/prompt/tools/skills/hooks/MCP/delegation/runtime/execution；`config_materializer.py` 管 persisted config | P5 收敛 intent、authority、frozen execution plan，保持 env/user/repo/request/persisted/parent precedence |
| `runtime/agent_capability.py` 本轮修改前 snapshot version 为 3，但部分 shape 错误说明硬编码 v2；现已使用动态版本 | 只修复错误说明；持久化 shape/version 校验保留，其余契约版本漂移仍按 P4/P5 处理 |
| `provider/registry.py::with_defaults` 仍有 builtin adapter/table 组合，custom entry 主要是 endpoint-shaped，未知 id 会拒绝 | P5 支持真实 provider package entry；不能退化成未知 provider 静默 fallback，也不能重写已成立 wire adapters |
| `runtime/context/transforms.py` 已有 typed request/result、ordering、failure trace，但 scope 只有 `provider_context` | 复用已有 seam；P5 统一已有 typed phase，不提前增加所有可能的 transform 功能 |
| `runtime/context/provider.py` 的 debug projection 接受公开 `ProviderAssembledContext`，metadata 是开放 `dict[str, object]` payload；只读 helper 现接受 Mapping | custom/persisted metadata 仍不可信，parser 校验保留；只去掉已证实的同函数重复计算和不必要复制 |

已有可迁移的行为测试包括：`tests/unit/runtime/test_typed_tool_hooks.py`（rewrite 后真实执行参数、resume 不重复 handler、inner invoke 权限）、`tests/integration/test_process_crash_tool_resume.py`（hard kill/resume 不重放 side effect）、`tests/unit/runtime/test_tool_execution_timeout.py`（timeout/cancel/progress）、`tests/unit/storage/test_session_fork.py`、`test_session_checkout.py` 和 `tests/unit/runtime/test_checkout_provider_context.py`。这些是已有用例定位，不表示本轮已经运行，也不表示当前具有 standalone real-provider core 测试。

## 阶段与依赖

顺序为 P0 → P1 → P2 → P3 → P4 → P5 → P6。P0 是冻结和行为基线；P1 是第一份可执行代码 change set。每阶段是可独立 review/落地的内聚迁移，不按文件行数拆空壳 PR；每份代码 change set 同步更新受影响契约及消费者。

### P0：固定行为边界，停止继续扩张控制面

**进入：** 当前审计和源码已对照，尚未抽取 core。

**变更：** 暂缓新增 preset、hook surface、task topology、event family、中央 config section 和 provider-specific runtime 特例；允许 correctness/security/replay 修复。整理已有行为基线；删除只 pin 常量、文案、字段搬运、实现细节或 mock echo 的测试，不把它们重新 pin 到新架构。CLI/TUI 不保留永久自动测试，改为实际程序的人工 smoke。

**退出：** 下述治理场景有明确的可重复步骤和已有 backend 行为用例；当前失败、已验证结果和未验证项分开记录。不得以旧 graph event 精确列表或源代码文本作为“语义冻结”。

**验证场景：** 允许/拒绝工具；rewrite 后重新验证并对最终参数授权；approval 后只执行一次；cancel 后不出现晚到 completion；persisted replay 不执行 side effect；fork/checkout 不拆 tool pair。

**风险：** 删除过度测试时误删数据/权限行为基线。保留真实输出、副作用、权限结果、durable 状态和可见顺序的断言。

### P1：解开 lower types 的 runtime 依赖——第一个具体 change set

**进入：** P0 的 authority 和验证范围已确定。

**变更：**

1. 从当前 `ToolResultView`、context segment 和 provider request/result 中抽取真实使用的中立 message/tool-result/transcript contracts；在一个 lower owner 定义，区分 raw history 与本次 provider view。不要同时添加无人消费的 custom message/compaction/edit API。
2. 迁移 `provider/protocol.py`、`graph/contracts.py`、`graph/provider_graph.py`、`graph/deterministic_graph.py`、`tools/contracts.py` 及 runtime context/provider assembly 的全部现有消费者；类型引用和 package 初始化一起切换。
3. runtime-specific projection、session metadata/replay、redaction 和 policy 留在 runtime adapter；删除被迁走定义、obsolete imports/re-export。既有 provider 和 graph 立刻使用中立类型，不留“以后接入”的 facade。

**退出：** 真实 lower provider/tool contract 消费者已运行在新类型上；导入和构造现有 normalized provider request/result 不加载 runtime/SQLite/UI；runtime 的 context projection 和实际 provider turn 行为仍成立。此阶段**不是** standalone engine 完成。

**验证：** 干净 Python 进程 import + request/response smoke，实际调用现有 deterministic/provider adapter 的一轮（本地确定性 wire fixture），检查 tool-call/result identity 和 reasoning 不丢；运行受影响 backend 行为用例。导入隔离检查只是架构 smoke 的补充，不建立 source-text 永久测试。

**风险：** provider → runtime 的隐含依赖和 package init cycle；neutral 类型只做形状，不夹带 workspace policy 或存储算法。

### P2：显式 ToolContext，清除隐式执行依赖

**进入：** lower contract 已中立，P1 消费者已 cutover。

**变更：** 将已有 invocation context 变为 `Tool.invoke(..., context=...)` 的真实参数，迁移 builtin、MCP、LSP、local custom tools、runtime executor、test fixtures 和所有直接调用者。workspace、abort、progress 和实际需要的 resource/capability handles 显式传入；只给需要它的工具提供 dependency，不造无消费者 resource hierarchy。runtime command/resource adapter 不暴露 `_session_store` 或任意私有 runtime state。

以现有工具的实际行为建立 `read/write/execute/network/spawn/session` effect facts，policy 据此决策；迁移默认 read-only approval 规则和所有定义消费者，完成后删除旧 boolean 决策路径、ContextVar binder、obsolete facades。effect 是事实，不是许可；缺少能力必须诚实报错，不静默 fake fallback。

**退出：** 同一真实文件工具可以由显式 in-memory host context 调用；runtime 下 native/resume/inner invoke 仍走同一治理边界，cancel/timeout/progress/lease 均由 runtime 掌管；工具不必依赖 hidden ContextVar 获得身份。

**验证：** 临时 workspace 读写真实文件；write 等待 approval 时文件不存在，allow 后出现且只执行一次；deny 不执行且返回可供模型调整的 tool feedback；timeout/cancel 之后晚到 progress/result 不被持久化。MCP/LSP 用可控本地进程验证生命周期，不需要在线服务。

**风险：** effect 翻译放宽默认权限、嵌套 invoke bypass、线程/progress 生命周期遗漏；不用 `session_id=""` 等伪默认值掩盖缺少真实依赖。

### P3：真实 turn engine 与 runtime host 单路径切换

**进入：** provider/tool/transcript contracts 已中立，真实工具依赖已显式。

**变更：** 从 `ProviderGraph` 和 run loop 提取 provider turn、stream/tool batch assembly、pairing、结果推进、continue/end、steering/follow-up 和 abort 的实际状态机。provider wire 解析归 provider adapter，normalized batch/lifecycle 归 core。deterministic 路径迁到同一 core contract 的实现或输入 adapter，不保留旧 graph public aliases。

runtime host 注入 context builder 和 governed tool executor，处理 approval pause/resume、permission、hooks、redaction、durable intent、checkpoint、provider retry/fallback/recovery 和 lease。已批准的最终参数不再经过会变更输入的 handler，也不能重复执行 side effect；runtime 仍执行 resume 必需的 approval identity、policy 和 execution ownership 校验，不借重构跳过治理。core 不知道 SQLite event 名称。`VoidCodeRuntime`/run loop 改消费 core 结果，删除旧 `GraphRunRequest`/`RuntimeGraph` 和被替代算法，而不是保留两个循环开关。

**退出：** 无 `VoidCodeRuntime`、SQLite、workspace、UI/capability manager 的实际内存 host 跑完两轮、有多工具 batch 的完整会话；同一 core 在现有 runtime host 执行 run/stream/resume。runtime 不再实现 provider turn/tool-call parsing。

**验证：** 确定性 fixture provider 第一轮给两个 tool calls，真实纯工具返回不同可断言结果，第二轮 provider 消费配对结果后结束；steering/follow-up 在定义的 safe boundary 生效，abort 停止下一调用。另用真实 wire adapter + 本地可控响应运行同一场景，不能声称仅 fake 证明 real-provider 兼容。恢复/批准/重试场景见总验收表。

**风险：** batch resume 重复执行、stream 已可见输出被静默 retry、失败被当成功结束。保留 externally meaningful 顺序，而非整个内部事件 golden dump：最终参数校验/授权 → durable intent → hook/execute → durable result → 下一 provider turn；progress 在 completion 之前，safe checkpoint 不拆 batch/pair。若现有 resume 特例顺序不同，先用副作用/恢复场景证明其含义，再统一，不能借 refactor 偷改行为。

### P4：拆 events/repositories，保留已有 SQLite 真相

**进入：** core 不依赖 runtime events，runtime host 已唯一执行入口。

**变更：** 建立 typed domain execution facts 与明确 codec/version、replay policy/owner；runtime 内完成必要 redaction 后持久化，再投影为现有 client delivery events。live-only stream delta 保持 live-only，不为抽象整齐增加持久化。不能把未经 redaction 的 core data 直接写 store/客户端。

按已有 consumers 从 `SessionStore` 拆 event append/read、session identity/branch、approval、task 和 bounded artifact ports。SQLite 实现继续复用已有 owners/mixins、事务、dedupe、terminal seal 和 write gateway；内存 EventStore 实际实现 append/read/branch 需要的语义，供无数据库 host 使用。事务/ownership 只在需要处提供，不为每个 repository 复制完整 mega-port。

优先保持现有 durable rows 和公共 wire。若 codec/snapshot 确需变更，交付显式 database/bundle migration 和旧数据 fixture：先备份/验证、明确版本拒绝、一次 cutover，不隐式双写、不允许 replay 重执行工具；迁移后的 obsolete shape reader 删除。

**退出：** 同一 core 行为 contract 可以使用内存或 SQLite event adapter；domain facts 与 client delivery payload 有不同 owner；现有 approval/crash/fork/checkout/append-only 行为保持。

**验证：** 真 SQLite 临时库完成 run、关闭/reopen、replay、resume；side-effect counter 不增加；checkout/fork 原事件未改、pair 不拆，fork provenance 与 delegated parent 不混用；夺权后 late append/progress/result 被 write gateway 拒绝；必要 migration fixture 失败不破坏原库。

**风险：** ports 拆分破坏原子事务、未经 redaction 的 event 落库、旧 session 不可读、client wire/replay 语义混淆。

### P5：注册与组合收敛，不建立任意权限 plugin bus

**进入：** core、explicit tools、typed facts 和 repository adapters 已切换，恢复输入具有明确 version policy。

**变更：**

- provider package entry 组合 wire adapter、catalog/model metadata、auth 和 endpoint declaration。迁移 builtin 注册到同一机制；复用当前 normalized boundary。未知 provider 仍拒绝，runtime 保留 retry/fallback policy，metadata 来自 discovery/generator 而非手改生成 JSON。
- 复用 ToolMaterializer、agent/skill registry、MCP declaration 和 context transform seam，建立 `discover → validate → register → materialize`。已有内置能力成为实际注册消费者；可信进程内 package 能定义声明/生命周期，但不能自行 grant、写 session truth 或绕过 runtime lifecycle。
- 将 manifest、request overrides、parent binding 等 intent 收敛为 `CapabilityBinding`；policy 单独 materialize authority；最终冻结 `ExecutionPlan`。保留 recovery-critical 值和 provenance，删除重复 feature snapshot/中央 preset 判断；materialized plan 必须在可能发生第一个 side effect 前持久化。
- 核心 config 只保留 runtime execution/policy/storage 必需规则，extension-specific declaration 由所属 package 验证；中央 loader 只组合已注册 section。保持 env/user/repo/request/persisted/parent precedence，不改变现有 config 输入的权限意义。schema 由 source models 生成；如果格式需要改，按 P4 的显式版本 cutover，不永久保存旧入口。
- typed context transforms、tool-input handlers 和已有 typed lifecycle consumers 使用收敛 phase contract，明确 lifecycle/ordering/failure/snapshot/replay；argv hooks 保留外部 command adapter，guidance-only hook preset 不变成可授权插件。`observe/transform/gate/schedule` 只实现有现有消费者的 phase：gate 只缩权，schedule 只提交 runtime command。不顺带新增任意 prompt mutation、compaction strategy 或 provider transform feature。

**退出：** 在单一 extension package 内添加 provider/tool/agent/context extension 的 declaration、validator、materializer 和 phase 语义，只需注册该 package，不修改 `service.py`、`run_loop.py`、`config_models.py`、`events.py`。同一 registry 不意味不同能力拥有同一 authority；MCP/LSP 的资源创建/refresh/shutdown 仍由 runtime owner 处理。

**验证：** package fixture 注入新 provider、实际执行的纯工具、agent declaration 和 context transform，完整 runtime run 得到可断言输出；在 config/request precedence、parent policy 限制、ordering/failure、frozen snapshot recovery 中验证行为；恢复时磁盘 declaration 改动不能偷偷替换已冻结执行语义，无法恢复则明确拒绝。argv hook timeout/failure 仍按原配置处理，gate 不可扩权。

**风险：** generic dict section 成为弱校验后门；agent selection 与 policy 混同；复用 snapshot 时丢失 skill scope/force load 或 MCP intent；phase 合并改变 rewrite/resume ordering。

### P6：任务 substrate / delegation adapter，完成最终切除

**进入：** P5 注册/组合和 frozen plan 已可被现有执行消费者使用。

**变更：** 从 `runtime/background/supervisor.py` 抽出 runtime-owned task lifecycle、ownership、persistence、cancellation、result channel（最小实际 `TaskSpec/Handle/Result`）；把 preset、parent/child session、`task/task_batch`、`yield` 和 leader notification 留为 delegation adapter。迁移现有 queued/running/idle/terminal、keep-alive、steer/retry/shutdown 路径，不重新发明 task engine。

用一个已有需求级别的内存/local workflow fixture组合 core + binding + task/context接口，证明无 service 中央分支；不新增用户产品 workflow。最终删除旧 aliases、private facades、mega-port、obsolete snapshots/events/config paths 和失效测试；契约只描述当前 authority，索引指向当前文档，历史审计保持不变。

**退出：** 现有 delegated feature 通过 substrate 运行，task/session lineage 和 fork provenance 仍分离；新上层组合不新增 service 分支；十项硬验收全部有行为证据，未完成项不能标记重构完成。

**验证：** 排队→执行→完成/失败/取消；keep-alive idle 后 steer；owner 被夺走后老 worker 不写状态/结果；yield/result/parent notification 只有一次；shutdown 不留活动 owner。CLI/TUI 人工实际程序 smoke 检查 run、approval、resume、cancel、fork/checkout 及流式 projection；不新增永久 CLI/TUI 自动测试。

**风险：** substrate 抽取意外扩大 preset/topology 权限、通知重复、child session ownership 与 fork lineage 混合；最终 cleanup 不得删除仍有消费者的 contract。

## 审计覆盖映射

| 审计项 | 落地阶段 |
| --- | --- |
| P0-1 独立 kernel | P1/P2 前置，P3 完整运行和 cutover |
| P0-2 transcript/message | P1 定义并迁消费者，P3 pairing/turn，P4 durable projection |
| P0-3 explicit tools/effects | P2 全调用路径，P5 binding/policy 组合 |
| P0-4 domain/store/client events | P3 脱离 wire 输入，P4 typed codec/projection |
| P0-5 narrow stores/repositories | P4，P6 task consumers 最终迁移 |
| P0-6 extension/resource registry | P5 注册实际消费者，P6 上层组合证明 |
| P1-7 task/supervision substrate | P6，runtime ownership 不下放 core |
| P1-8 provider/plugin boundary | P1 neutral contract，P3 wire/turn owner，P5 package registry |
| §5.1 graph 名称/抽象 | P3 单 turn engine；不造 DAG |
| §5.2 declaration/capability/policy 重叠 | P5 binding→policy→frozen plan；P4 版本基础 |
| §5.3 hook/phase | P5 已有 typed seam 收敛 + argv adapter |
| §5.4 task/delegation | P6 substrate 与 delegation owner 分离 |
| §5.5 config composition | P5 package validated declarations + precedence 保留 |
| §5.6 event owner | P4 事实→durable→delivery |
| §5.7 context pipeline | P1 transcript 前置，P5 已有 phases 收敛；新 transform 功能非本轮要求 |

## §8 十项硬验收：未来必须实际执行的检查

以下是实施验收计划，不是本轮已通过报告。

| # | 完成条件与可观察证明 | 阶段 |
| --- | --- | --- |
| 1 | 新 provider/tool/agent/context package 注册后完整运行产出预期结果，四个中央文件无需变更；review diff 是补充，不以 source-text 测试替代运行 | P5/P6 |
| 2 | 干净进程无 runtime/SQLite/workspace/UI/MCP/LSP，内存 host 完成 multi-tool 两轮会话及 abort；必须运行而非仅 import | P3 |
| 3 | 插件/core 尝试扩权限无效；approval 阻塞副作用；lease 夺权阻止晚写；所有 durable truth 由 runtime/store owner 写 | P2/P4/P5/P6 |
| 4 | 同一真实工具使用显式 context 在内存 host 和 runtime host 都执行；身份、abort、progress、capability 不通过 ContextVar 获取 | P2 |
| 5 | core typed facts 可被内存 host 消费；runtime 同事实先治理/持久化再投影稳定 client wire；live-only delta 不落库 | P4 |
| 6 | 同一参数化 core 行为 contract 使用 in-memory/SQLite event adapters、fake provider 和真实 provider wire adapter（本地确定性 transport fixture）；有凭据时再做在线 smoke，不能拿 fake 替代 real adapter 证据 | P3/P4/P5 |
| 7 | approval resume、fallback、timeout、hard kill/crash resume、late write、fork/checkout 真实副作用/持久化场景不回退；复用已有 backend 行为用例 | P2/P3/P4/P6 |
| 8 | reopen/replay/恢复已完成 tool result 时实际副作用计数不增加；未确定完成的 in-flight call 不被假报完成或偷偷重放 | P3/P4 |
| 9 | 单 package 定义 phase 生命周期、ordering、failure、snapshot/replay；改变 package 后恢复沿 frozen semantics 或明确拒绝，不修改中央 runtime | P5 |
| 10 | 真实 provider response 的 parsing/normalized batch continuation 由 provider/core 完成；runtime host 在 run/stream/resume/fallback 中只选择与治理 | P3/P5 |

## 验证策略与交付规则

- 永久测试只留 backend 可观察行为：权限/错误、状态转换、边界、precedence、side effect 与数据完整性。不要为常量、错误文案、copy/forward、内部字段名或新 helper 再建 suite；更不能把旧 incidental assertions 改名保留。
- 每个阶段至少一条实际 smoke：执行真实变更路径并观察结果。provider 网络可用本地 transport fixture，文件/process/SQLite 用临时资源；throwaway 脚本验证后清除。涉及 UI 的验证只启动实际 CLI/TUI，观察输出、交互及 durable state，不增加永久自动测试。
- scoped backend checks 在 change set 集成后运行；最终 `mise run check`、完整 pre-commit 和 schema check（schema 受影响时）一次执行。实现阶段若发现当前 task 配置引用被删除 CLI/TUI tests，同步删除过时入口，不用空目录/空 suite 保绿。没有运行的检查不得报告通过。
- 每份阶段交付包括：真实消费者 cutover、旧路径删除、相应契约/索引更新、已执行行为证据与剩余 blocker；不交付 stub、fake fallback 或“后续接线”的 skeleton。

## 本轮已落地的有限清理

- `runtime/context/provider.py`：`blocking` 已包含所有 error diagnostics，删除再合并其子集 transform failures 的重复计算；仍然保留 transform failure 对 `mode=off` 的硬 block。debug transform projection 直接读取 Mapping，不再为只读 helper 复制 dict。开放 custom/persisted metadata 的 parser 校验保留，不把它误判为过度防御。
- `runtime/agent_capability.py`：shape 错误说明使用 snapshot owner 的真实版本；不测试错误文案，不删除持久化输入验证。
- 独立前置修复：首次 smoke import 发现 `runtime/reminders.py` 的重复原始 doc 段落和多余三引号导致 `SyntaxError`；只移除重复段落/引号，保留原 docstring 和所有 runtime 逻辑。
- 已执行：before/after 九组 context transform failure × policy mode 的实际 projection/policy smoke 输出一致；重复 blocking code 保持去重、无效 persisted snapshot 保持拒绝；`uv run pytest tests/unit/runtime/test_context_window_diagnostics.py -q` 为 **2 passed**。本轮未执行 standalone core/real-provider 验收或全仓检查，没有创建永久文案/source-text 测试。
- CLI/TUI 专用测试及其他非行为断言的清理由独立工作流按 [`testing.md`](../testing.md) 完成；这一策略调整不等于架构迁移或 UI 行为已经验证。

## 明确延期：审计 §9

任意 agent-to-agent bus、marketplace/dynamic remote plugin、cloud execution、scheduled runs、完整任意拓扑 multi-agent planner、OS sandbox 均不做。也不为它们提前造协议、config、事件或 stores。它们不影响本计划十项硬验收；进程内可信 extension 不是安全隔离，approval consent 也不是 OS containment。
