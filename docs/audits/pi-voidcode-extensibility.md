# pi agent × VoidCode 可扩展性架构审计

## 结论先行

**当前 VoidCode 还不是“高度可拓展的 agent framework”；它是一个治理能力很强、但执行内核与组合边界仍然耦合的 single-agent runtime。**

现在继续增加 agent preset、hook、delegation、context transform 或 provider 特例，会把新能力继续压进 `VoidCodeRuntime` / `RuntimeRunLoopCoordinator`，短期能交付，长期会让下层接口越来越不能复用。目标如果是：

> 上层机制可以轻松利用下层接口组合，而不是每个上层功能都改 runtime 控制面，

那么当前仓库必须进行**结构性重构与迁移**。不建议从零重写持久化与治理；建议：

1. 保留已经验证过的 runtime ownership、approval、append-only session events、checkpoint、execution lease、redaction 与 replay 不变量；
2. 抽出一个真正独立的 agent execution core；
3. 把 graph/provider/tool/context/event-store 的依赖方向反转；
4. 再把现有上层机制迁移到新 core；
5. 最后删除旧的 runtime-specific graph/tool path，而不是长期保留双控制面。

一句话：**要破的是执行内核和依赖方向，不是已经成立的运行时真相。**

## 审计范围与证据规则

审计对象：

- 当前工作树的 `src/voidcode/`、`tests/`、`docs/contracts/`；
- pi upstream 的 `pi-agent-core`、`pi-ai`、`pi-coding-agent` 文档与源码；
- 源码与测试优先于 README、契约文档和历史记忆。

审计不把“功能数量”当作成熟度指标，重点看：

- 下层是否可以独立运行；
- 上层是否只通过稳定接口组合；
- 新扩展是否需要修改中央 runtime；
- state、event、policy、tool、provider 是否有单一 owner；
- replay、resume、approval、cancel、failure 是否仍然可解释。

### 0. 语义准确性优先，避免无消费者的防御性机制

本审计不把“更多校验、更多 hash、更多 snapshot”默认当成可靠性。下层 contract 已经建立并由测试覆盖后，上层应信任它；重复检查只有在跨越不可信输入边界、阻止数据损坏或阻止权限越界时才保留。

具体例子：审计发现原先的 `hook/percall.py::percall_messages_sha256()` 会遍历所有 persistent message，序列化 `role/content`，计算 SHA-256，再取前 16 个 hex 字符写入 `assembled_context.metadata["percall_cache_prefix"]`。当前工作树没有任何 cache lookup、provider request、wire adapter 或 persistence consumer 读取这个字段；它只被同一函数和测试自证。因此本次审计已删除这条无消费者的 hash seam、对应 helper 与测试：

- **没有实际 cache 语义，hash 没有必要存在；**保留它只会用额外计算制造“cache prefix 已经接入”的假象；
- `role/content` 也不是可靠的 provider-wire identity，tool-call id、tool name、arguments、system-message 折叠和 provider-specific serialization 都可能改变真实请求；
- 如果将来确实需要 provider prompt-cache identity，应由实际 wire materializer 对 canonical wire bytes 计算，不能由通用上层 message helper 猜测；
- 当前 reminder 不写 transcript 的真实原因是 provider context assembly 是本次调用的临时投影，不是因为需要先 hash 再排除它。

这条规则适用于整个重构：**信任已验证的 lower contract；删除没有消费者的校验、hash、snapshot 和 facade；安全边界、数据完整性和跨进程输入校验不在此简化范围内。**

## 1. 当前 VoidCode 的真实形状

### 1.1 目标架构与实际依赖方向不一致

文档设定的方向是：runtime 拥有治理，graph 只推进步骤，tools 只实现工具。但实际依赖已经反向穿透：

- `src/voidcode/graph/contracts.py` 直接导入 `runtime.context.window.ToolResultView`；
- `deterministic_graph.py`、`provider_graph.py` 都直接依赖 `runtime.context.window`；
- tools 通过 `runtime_context` 的 `ContextVar` 读取 runtime 注入的 LSP、artifact、transcript、tool catalog、abort signal；
- `runtime/execution/tool_facades.py` 仍直接访问 runtime 私有字段，例如 `_tool_catalog_lookup`、`_session_store`、`_workspace`。

因此现在不是严格的：

```text
runtime -> graph -> provider/tools
```

而更接近：

```text
client
  -> runtime.service / run_loop
      -> graph
          -> runtime.context
          -> provider
          -> tools
      -> storage / hooks / background / capability managers
```

`graph` 名义上是下层，实际上已经消费 runtime-specific context；`tools` 名义上是能力层，实际上也依赖 runtime execution context。这个方向会阻止第三方 host 复用 graph、tool 或 provider。

### 1.2 中央控制面仍然过重

当前热点规模：

| 文件 | 当前行数 | 说明 |
| --- | ---: | --- |
| `src/voidcode/runtime/service.py` | 5,110 | composition root、run/stream、resume、tool registry、background、capability lifecycle、session surface 汇聚于此 |
| `src/voidcode/runtime/run_loop.py` | 4,490 | provider retry、context recovery、permission、hook、tool execution、checkpoint、event persistence 汇聚于此 |
| `src/voidcode/runtime/background/supervisor.py` | 3,334 | task queue、worker、keep-alive、notification、result、hook、ownership 汇聚于此 |
| `src/voidcode/runtime/config.py` | 1,936 | config load、merge、precedence、agent、provider、tool、skill、MCP 等组合规则 |
| `src/voidcode/runtime/storage/sessions.py` | 1,409 | session row、event append、tree/leaf、checkout、resume 边界等持久化语义 |
| `src/voidcode/runtime/contracts.py` | 1,272 | request metadata、session/API/debug/background/provider/client shapes 混合在一起 |

这些数字本身不是问题；问题是**新上层行为的变化面集中在这些文件**。当前新增一类 agent 行为，通常会同时触及 config、policy、service、run_loop、events、storage、tool facade 与 contract。

### 1.3 已经存在的有效基础

不应该推倒重来。当前值得保留并作为迁移的行为基线：

- runtime 是 approval、permission、session truth、persistence、resume、streaming 的 authority；
- SQLite event append、session seal、dedupe、checkpoint、fork/checkout 已有大量边界测试；
- `execution_ownership.py` + storage write gateway 解决了 background execution 被夺权后的 late write；
- provider 层已经有 normalized turn request、stream event、usage、error classification；
- `ToolRegistry`、`RuntimeToolMaterializer`、MCP/LSP manager、skill snapshot、agent capability snapshot 已经形成若干可抽出的 seam；
- context transform registry 已经有 typed provider、priority、failure policy、trace 与 bounded metadata；
- 测试重点覆盖 governance、resume、crash、approval、same-session re-entry、provider recovery 等真正的 runtime 不变量。

**这些是 runtime 的成熟部分，不是应该被新架构替换的部分。**

## 2. pi agent 的结构性优点

pi 不是安全治理模型，但它在“下层接口如何支撑上层组合”这一点上更成熟。

### 2.1 分层

pi 的核心分层可以简化为：

```text
@earendil-works/pi-ai
  provider/model catalog、wire API、normalized transcript、stream、usage
          ↓
@earendil-works/pi-agent-core
  Agent state、agent loop、tool execution、steering/follow-up、lifecycle events
          ↓
@earendil-works/pi-coding-agent
  AgentSession、SessionManager、ResourceLoader、ExtensionRunner、compaction、CLI/TUI/RPC
          ↓
interactive / print / JSON / RPC / SDK hosts
```

关键点不是 TypeScript，而是 ownership：

- `pi-agent-core` 不负责 SQLite、CLI、TUI、MCP discovery 或 workspace policy；
- provider 只实现 normalized model/stream boundary；
- `AgentSession` 把 agent loop 与 session persistence、compaction、resource、extension runtime 组合起来；
- host 可以通过 SDK 替换 `SessionManager`、`ResourceLoader`、model runtime、tool set；
- CLI、TUI、RPC 消费同一个 session/runtime，而不是各自复制执行路径。

### 2.2 pi 的可组合 extension seam

pi 的 `ExtensionAPI` 是显式的组合面，包含：

- typed lifecycle events；
- `registerTool`；
- `registerCommand`、shortcut、CLI flag；
- `registerProvider`、`registerVirtualModel`；
- `registerMcpServer`；
- `context` / `context_with_system`；
- `tool_call` / `tool_result`；
- `before_agent_start`、`turn_end`、`agent_before_settle`；
- `appendEntry`、`sendMessage`、custom renderer、shared event bus。

pi 的底层 `Agent` 也把几个决定性 seam 直接暴露出来：

- `transformContext`；
- `prepareRequest`；
- `prepareNextTurn`；
- `beforeToolCall` / `afterToolCall`；
- `finishTurn`；
- steering/follow-up queue；
- sequential/parallel tool execution；
- `streamFn` 与 normalized `AgentMessage`。

这使“plan mode”“todo extension”“provider router”“custom tool”“subagent extension”可以主要通过组合已有接口完成，而不是修改 agent loop 源码。

### 2.3 pi 的边界不能照搬

pi 的 extension 是**进程内、可信代码**：

- extension 与主进程拥有相同 OS 权限；
- extension 可以看 prompt、tool call、credentials、session history；
- `tool_call` 可以直接 mutate input，文档明确说明该 mutation 后不会再次 validation；
- custom provider 可以处理 credentials、prompt、tool definitions 与 provider responses。

所以 pi 提供的是**高可扩展性参考**，不是 VoidCode 的 authorization/security 参考。VoidCode 应吸收 pi 的 lower-core 分层和 typed seam，但保留 runtime-owned policy、approval、redaction、lease、persistence，不应直接复制一个任意权限的 plugin bus。

## 3. VoidCode 与 pi 的对比

| 能力 | pi | VoidCode 当前状态 | 审计结论 |
| --- | --- | --- | --- |
| Agent loop | 独立 `Agent` state machine | graph step + runtime `run_loop` 分裂；graph 还依赖 runtime context | **P0 缺口** |
| Provider boundary | `pi-ai` normalized transcript/stream/provider registry | 有 `TurnProvider`，但 provider registry 主要是 builtin/table-driven，runtime 负责大量 dispatch/recovery | **部分具备，需抽离** |
| Tool boundary | tool 带 schema、execute context、signal、update callback | definition 有 schema/read-only，但实际 context 通过 `ContextVar` 注入，tool API 只收 `ToolCall + workspace` | **P0 缺口** |
| Session model | JSONL append-only tree，entry id/parentId，custom entry/message/context edit/compaction | SQLite event log + leaf pointer + runtime metadata，恢复语义强，但高度 runtime-specific | **强但不通用** |
| Extension | tools、commands、providers、MCP、virtual model、events、renderers | context transform、typed input handler、argv lifecycle hook、local JSON subprocess tool；没有通用可信 extension registry | **P0 缺口** |
| Policy | 主要由 extension/host 自己决定 | runtime policy、approval、tool scope、lease、redaction 很强 | **VoidCode 优势，必须保留** |
| Background/subagent | 可由 extension/tool 自己组合 | runtime-owned fixed child preset + task/keep-alive/yield 协议 | **可靠但不是通用 substrate** |
| Context pipeline | transform/context events 可按 extension 组合 | 仅 `provider_context` transform family，provider message validation/request transform 明确未实现 | **P1 缺口** |
| Provider plugin | extension 可注册 provider/custom wire | 可注入 registry，但新 provider 多数仍需改中央 tables/adapters；custom config 主要是 endpoint-shaped | **P1 缺口** |
| Alternate host | SDK 可替换 session/resource/model/services | `VoidCodeRuntime` 是主要 host，内部 surface 与 private state 仍大量耦合 | **P0 缺口** |

## 4. 当前下层少了什么

这里的“少”指缺少让上层自由组合的**原语和 ownership**，不是缺少某个产品功能。

### P0-1：独立的 agent execution kernel

当前 `RuntimeGraph` 的接口是 `step(request, tool_results, session)`；`GraphRunRequest` 已包含 `ProviderAssembledContext`、`ProviderContextWindow`、`ToolDefinition`、`ProviderAbortSignal`、runtime metadata 和 preview callback。`ProviderGraph` 自己还持有 pending tool-call batch、session id、run id 和 approval-resume 判断。

这不是通用 graph，也不是纯 agent loop，而是 runtime/provider execution adapter。工具治理和 persistence 又在 `run_loop.py`，导致一个 turn 被拆成两个控制面：

```text
ProviderGraph: provider response -> GraphStep
RuntimeRunLoop: GraphStep -> policy -> hook -> tool -> persistence -> next GraphStep
```

需要的新下层：

```text
AgentEngine
  state: AgentState
  input: TurnInput
  services: AgentServices(provider, tool_executor, context_builder)
  -> AgentEvent stream
```

它只负责：provider turn、tool-call batch、tool result、steering/follow-up、continue/end、abort 和 turn lifecycle。approval、persistence、lease、redaction 由 runtime 通过 services 注入，不进入 core。

### P0-2：统一的 transcript/message core

VoidCode 的历史事实是 runtime event log，provider context 是从 event log/metadata 投影出来的 `RuntimeContextSegment`。这适合 replay/debug，但不适合作为独立 agent kernel 的输入输出。

缺少一个明确的、provider-neutral 的 transcript model：

- user/assistant/tool-result/custom/system message；
- tool call 与 result 的 pairing；
- context projection 与 raw history 的区分；
- append-only branch/compaction/edit 的通用语义；
- model-facing transcript 与 runtime event delivery 的转换边界。

pi 的 `AgentMessage[] -> transformContext -> convertToLlm -> provider` 是可借鉴的最小形状。VoidCode 不应复制 pi 的 JSONL 格式，但应把“历史事实”和“本次 provider view”从类型上分开，而不是继续以大量 runtime metadata dict 传递。

### P0-3：显式 `ToolContext` 与 effect/capability 描述

VoidCode 已有 `ToolInvocation`，但 `RuntimeToolExecutor` 最终通过 `bind_runtime_tool_context()` 设置 `ContextVar`，再调用：

```python
tool.invoke(tool_call, workspace=workspace)
```

实际工具所需的 session、abort、progress、LSP、artifact、transcript、spawn budget、todo state 都不在 tool 方法参数中。这导致：

- tool 不能脱离 runtime 运行；
- tool contract 看起来比实际依赖更简单；
- 测试必须建立 hidden context；
- 一个新 host 很难复用现有工具；
- runtime capability 与 tool implementation 通过全局 context 间接耦合。

需要的最小接口是：

```python
@dataclass(frozen=True, slots=True)
class ToolContext:
    workspace: Path
    session_id: str
    abort_signal: AbortSignal
    emit_progress: Callable[[Mapping[str, object]], None]
    capabilities: CapabilityView
    resources: ResourceView


class Tool(Protocol):
    definition: ToolDefinition

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult: ...
```

`ToolDefinition.read_only: bool` 也不足以表达 runtime governance。至少需要稳定的 effect facts，例如 `read`、`write`、`execute`、`network`、`spawn`、`session`；policy 再决定这些 facts 是否允许，而不是从工具名和一个 boolean 反推风险。

### P0-4：领域事件、持久化事件、客户端事件的分离

`EventEnvelope` 的 payload 是 `dict[str, object]`，event type 是字符串 Literal 集合。当前事件系统已经很完整，但它同时承担：

1. graph/runtime/tool 的内部事实；
2. SQLite replay truth；
3. client streaming protocol；
4. hook/background/MCP/LSP 的 observability。

这会让新增一个上层机制时必须同时考虑 event vocabulary、payload redaction、terminal allowlist、context replay、HTTP schema、TUI/Web rendering 和旧 replay。

需要下层提供：

```text
DomainEvent -> EventStore -> Projector -> RuntimeDeliveryEvent
```

不要求所有 event 都做复杂 algebra；只要求每个 domain event 有类型化 payload、schema/version、replay policy 和 projection owner。客户端事件可以继续使用稳定 wire string，但不应反过来成为内部 domain state machine 的接口。

### P0-5：可替换的 EventStore / SessionStore ports

当前 `SessionStore` 同时包含：

- session snapshot；
- event append/read；
- fork/lineage/checkout；
- approval/question；
- background process；
- background task；
- effectiveness report；
- storage diagnostics/prune/reset。

SQLite mixin 已经缓解了文件大小，但 protocol 仍然是一个 runtime-specific mega-port。下层应至少分成：

- `EventStore`：append/read/subscribe/transaction/dedupe；
- `SessionRepository`：session identity、metadata、branch position；
- `TaskRepository`：task lifecycle；
- `ApprovalRepository`：pending/claim/resolve；
- `ArtifactStore`：bounded output/artifact。

SQLite 可以继续实现这些 ports；不需要马上换数据库。但内存 store、JSONL store、测试 store 和未来远程 store 才能成为真实可替换实现。

### P0-6：真正的 extension/resource registry

当前可注入的是少数 constructor 参数：`graph`、`tool_registry`、`model_provider_registry`、`skill_registry`、`lsp_manager`、`mcp_manager`、`context_transform_registry`、`tool_input_handler_registry`。这能测试替换，但不等于可扩展 runtime：

- 新 provider 仍主要依赖 `ModelProviderRegistry.with_defaults()` 的中央表；
- 新 builtin tool 仍要改 `BuiltinToolProvider`；
- agent preset、hook surface、config schema、event vocabulary 各自有中央 catalog；
- local custom tool 只是 workspace JSON manifest + subprocess，不是通用 extension module；
- 没有一个可发现、可验证、可 materialize、可冻结版本的 runtime extension package contract。

需要把 registry 变成 lower capability layer：

```text
discover -> validate -> register -> materialize(snapshot) -> execute through runtime policy
```

extension 可以声明 capability，不能自行授权。runtime 仍是最终 authority。

### P1-7：通用但收敛的 task/supervision substrate

当前 background task 已经解决了 queued/running/idle/completed/failed/cancelled/interrupted、keep-alive、steer、retry、parent notification、result retrieval、ownership lease。这是可靠的 runtime feature，但它仍然是固定 delegated child workflow：

- fixed child presets；
- fixed `task` / `task_batch` tool；
- fixed `yield` handoff；
- fixed parent/child session linkage；
- 没有通用 task input/output channel、dependency、supervisor policy 或 execution handle。

如果未来要组合 research、review、implementation、verification 等上层流程，下层至少需要一个通用 `TaskSpec -> TaskHandle -> TaskResult/TaskEvent`。不需要现在引入任意 agent-to-agent bus 或 marketplace；先把当前 background supervisor 的可复用生命周期抽出来。

### P1-8：provider/plugin boundary

当前 provider protocol 已经是不错的低层基础，但 provider registry 仍是 VoidCode 中的 builtin implementation registry。要新增一个真正的 provider wire，通常仍会涉及 provider adapter、provider config、provider table、catalog、auth、thinking rules、error mapping、schema 和 runtime dispatch。

目标应是：

- `ModelProvider` / `WireAdapter` 是稳定 lower contract；
- catalog、auth、endpoint、wire adapter 可以独立注册；
- unknown provider 不会静默 fallback；
- provider 只负责 normalized request/stream/result；
- retry/fallback/recovery 由 runtime policy layer 决定。

VoidCode 已经实现了最后一条的一部分，但 provider graph 仍把 pricing、finish reason、stream tool-call assembly 与 provider execution 细节混在 graph。

### P2：执行隔离不是可扩展性原语，但不能伪装成安全边界

当前 approval-flow 明确声明没有 OS-level sandbox；`allow` 是 consent，不是 containment。这个边界是诚实的，但如果产品以后要处理不可信 agent/tool/plugin，必须增加独立的 process/OS isolation layer，不能继续在 command string、path glob 或 prompt policy 上堆规则。

这不是本轮扩展性重构的 P0；但必须继续保持文档中的明确非目标，不能把 runtime policy 描述成 sandbox。

## 5. 哪些上层机制还没有经过足够深思

### 5.1 `graph` 是误导性的抽象名

当前只有 deterministic graph 与 provider graph 两条实现，`GraphStep` 主要表示“下一次要调用哪个工具，或本轮已完成”。它没有：

- node/edge model；
- graph state reducer；
- pause/resume boundary；
- node retry policy；
- parallel branch semantics；
- graph-level persistence；
- graph composition API。

因此上层所谓“未来加入更复杂 graph/multi-agent orchestration”没有可复用下层。应做一个明确选择：

- 如果目标是单 agent turn engine，把 `graph` 降名为 `AgentEngine` / `TurnEngine`；
- 如果目标是真 graph，必须另建纯 graph runtime，不得继续把 provider turn adapter 叫 graph。

在完成选择前，不应继续往 `graph/contracts.py` 添加字段。

### 5.2 Agent declaration、capability snapshot、runtime policy 三套系统重叠

当前同时存在：

- `AgentManifest`：角色、prompt、tool allowlist、skills、hook refs、MCP intent；
- `RuntimeAgentConfig`：request/config overrides；
- `agent_capability_snapshot`：materialized declaration evidence；
- `RuntimePolicySnapshot`：authorization/explanation；
- `resolved_hook_plan`：hook execution declaration；
- `skill_snapshot`：skill binding；
- `runtime_config`：recovery-critical config。

这些快照有价值，但它们不是一个清晰的 pipeline。问题表现为：

- authority 与 intent 在多个 payload 中重复；
- manifest 的 `top_level_selectable` 与 runtime `_EXECUTABLE_AGENT_PRESETS = {"leader"}` 仍然是两处执行判断；
- agent capability 代码常以 `dict[str, object]` 组装十个 section；
- 当前代码常量 `AGENT_CAPABILITY_SNAPSHOT_VERSION = 3`，但校验错误文本仍写 `v2`，契约文档也写 snapshot version 2，说明 snapshot owner 已出现漂移。

建议收敛为：

```text
Declaration sources
  -> CapabilityBinding (intent only)
  -> PolicyMaterialization (authority only)
  -> Frozen ExecutionPlan (what this turn will execute)
```

历史 session 只保存这三层必要的 typed snapshot；不要继续为每个 feature 发明一个 dict snapshot。

### 5.3 Hook 已经被当成 plugin bus 使用，但又不允许成为 plugin bus

当前存在至少四种“扩展”语义：

- argv lifecycle hooks；
- typed tool-input handlers；
- context transform providers；
- agent hook preset guidance。

它们的 authority、failure、ordering、replay 语义不同，文档已经花费大量篇幅解释这种差异。这个设计对安全边界是谨慎的，但对组合性不够：

- 新行为不能只注册一个 typed extension；
- hook surface 通过配置字段与 hard-coded descriptor 增长；
- hook preset 只能 guidance-only；
- lifecycle hook 不能拥有 task/session/provider truth；
- provider request/message/tool-result transform 仍未形成统一 contract。

建议：保留 argv hook 作为一个外部 command adapter；其余 typed extension 统一进入一个 `ExtensionPhase` registry。每个 phase 明确标记：

```text
observe | transform | gate | schedule
```

其中 `gate` 只能缩小 authority，不能 grant；`schedule` 只能提交 runtime command，不能直接写 truth。这样可以保留当前安全原则，同时避免继续增加平行 hook family。

### 5.4 Background task 是可靠 feature，不是通用 async substrate

`RuntimeBackgroundTaskSupervisor` 已经承担 queue、thread、reconcile、notification、hook、result projection、ownership、shutdown、keep-alive 和 retry。它解决的是当前产品需求，但上层若要组合更复杂 workflow，会被迫继续把流程塞进 `task` tool、child preset、`yield` payload 和 parent notification。

应把它拆成：

```text
Task substrate: lifecycle, ownership, persistence, cancellation, result channel
Delegation adapter: parent/child session, preset, yield handoff, leader notification
```

不要把固定 delegated semantics 继续扩大成“通用多智能体平台”的伪装。

### 5.5 Config 已经成为第二个 composition engine

`RuntimeConfig` 同时描述 model/provider、approval、permission、hooks、tools、skills、context window、LSP、ACP、background task、reminders、MCP、TUI、agent、agents。来源又有：

- environment；
- user config；
- repo config；
- request metadata；
- agent manifest；
- persisted session metadata；
- parent policy/capability snapshot。

当前配置 owner 的划分比早期清楚，但上层新增机制仍然通常需要把字段加进 `config_models.py`、generated schema、loader、serializer、materializer、effective config 与 contract tests。配置不应继续成为所有 extension 的入口。

建议：核心 runtime config 只保留 execution/policy/storage 需要的字段；agent/tool/provider/resource extension 自己拥有 validated declaration，runtime 只 materialize 已注册的 section。这样新增 extension 不必修改中央 config model。

### 5.6 Event contract 过早承担了所有上层语义

现在 event 既是事实源、client wire、debug projection、background notification、MCP/LSP lifecycle、hook outcome 和 context observability。新 workflow 必须先设计 event 名称，再设计执行模型，导致“为了可观测而把上层状态变成全局事件”。

正确顺序应是：

```text
纯状态/命令模型
  -> domain event
  -> persisted event
  -> client projection
```

客户端仍可消费现有 `runtime.*` 事件；但未来内部扩展不应因为要存一个事实就直接扩大公共 event vocabulary。

### 5.7 Context transform 目前是安全的窄 seam，但不是完整上下文组合层

当前 `RuntimeContextTransformRegistry` 是审计中最接近正确方向的 extension seam：typed request/result、priority、failure policy、trace、bounded event。问题是 scope 只有 `provider_context`，并且注入能力仍被 runtime-specific `ToolResultView`、rulebook、hook guidance、mode guidance 约束。

这满足“不要让插件任意修改 prompt/tool/approval”的安全要求，但不足以支撑：

- provider request adaptation；
- message validation；
- model-specific transcript conversion；
- custom compaction/summary strategy；
- extension-owned context state。

这些能力可以后续增加，但必须基于统一 transcript/phase contract，而不是继续在 `RuntimeContextWindow` metadata 上增加字段。

## 6. 目标架构：先把下层变成可复用内核

### 6.1 依赖方向

目标依赖图：

```text
core
├── message / transcript
├── agent engine
├── provider contract
├── tool contract
├── event contract
└── small resource interfaces

provider adapters ───────┐
tool implementations ────┼──> core contracts
resource extensions ─────┘

runtime host
├── policy / approval
├── event store / repositories
├── context builder / redaction
├── execution ownership / cancellation
├── task supervisor
└── composition/materialization
        └── assembles core + adapters

CLI / HTTP / TUI / ACP / SDK
        └── runtime host only
```

硬规则：

1. `core` 不得导入 `runtime`、SQLite、CLI、MCP、LSP、TUI；
2. provider/tool implementation 不得通过 `ContextVar` 读取 runtime truth；
3. runtime 是 composition root 和 authority，不是每个 turn 细节的唯一实现文件；
4. event store 不知道 provider/tool implementation；
5. client 不直接调用 core tool/provider；
6. extension 只能声明、观察、变换或提交 runtime command，不能直接 grant authority。

### 6.2 最小 lower contracts

不要先设计一个大而全的 plugin SDK。先稳定五个 contract：

```python
class AgentEngine(Protocol):
    def run(
        self,
        request: TurnRequest,
        state: AgentState,
        services: AgentServices,
    ) -> Iterator[AgentEvent]: ...


class Provider(Protocol):
    def stream(self, request: ProviderRequest) -> Iterator[ProviderEvent]: ...


class Tool(Protocol):
    definition: ToolDefinition

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult: ...


class EventStore(Protocol):
    def append(self, events: Sequence[DomainEvent]) -> Sequence[StoredEvent]: ...
    def read(self, stream_id: str, *, after: int = 0) -> Sequence[StoredEvent]: ...


class CapabilityRegistry(Protocol):
    def discover(self, scope: DiscoveryScope) -> Sequence[CapabilityDeclaration]: ...
    def materialize(self, request: CapabilityRequest) -> MaterializedCapabilities: ...
```

实际命名可以不同；不能缺少这些 ownership。

### 6.3 `Graph` 的处理决定

本次迁移必须做一个 breaking decision：

- **推荐**：把当前 `graph` 降为 `agent_engine` / `turn_engine`，把 provider stream/tool batch 逻辑迁入纯 core；
- 只有在真的需要分支 DAG、parallel node、checkpointed workflow 时，才另建真正的 `workflow`/`graph` layer；
- 不要用当前 `ProviderGraph.step()` 继续承载未来 multi-agent/workflow 的抽象名。

## 7. 迁移顺序

### Phase 0：冻结上层扩张

暂缓新增：

- 新 agent preset；
- 新 lifecycle hook surface；
- 新 task topology；
- 新 runtime event family；
- 新中央 config section；
- provider-specific runtime special case。

允许修复 correctness/security/replay 问题，但新产品行为必须证明能落在目标 lower contract 上。

### Phase 1：抽出 Agent Core

- 从 `ProviderGraph` / `run_loop.py` 提取 provider turn、tool-call batch、tool result、turn continuation、abort、steering/follow-up；
- 定义 provider-neutral message/transcript；
- 让 core 用 fake provider + fake tool + in-memory event sink 独立运行；
- 将现有 runtime event 作为 adapter output，而不是 core input；
- 保留现有 runtime execution order 的 golden tests。

验收：不导入 `voidcode.runtime` 的 core 可以完成一次 provider turn、工具调用、工具结果、下一轮和结束。

### Phase 2：反转 Tool 边界

- 将 `ToolInvocation` 变为真正的 `ToolContext` 输入；
- 迁移 builtin tools、MCP tools、LSP tools、local custom tools；
- 删除 `ContextVar` 作为必需 runtime context 的角色；
- runtime executor 负责创建 context、policy、lease、progress 和 resource views；
- 将 `read_only` 扩展为稳定 effect facts，但 policy 仍由 runtime 决定。

验收：同一个 tool 可以被 in-memory host 调用；runtime host 仍然能完整执行 approval/cancel/timeout/lease。

### Phase 3：拆 EventStore 与 projections

- 从 `SessionStore` 抽出 event append/read、session repository、task repository、approval repository；
- 给 domain event 增加 typed payload codec/version；
- 现有 `runtime.*` 事件通过 projection/adapter 继续对外；
- 为 event schema 明确 migration/version policy；
- 保留现有 SQLite 数据和 replay 行为，必要时做一次显式 bundle/database migration，不做隐式双写。

验收：内存 EventStore 可以跑 core；SQLite 仍通过同一组 approval、crash、resume、fork/checkout contract tests。

### Phase 4：provider 与 capability registry

- 将 provider wire adapter、catalog、auth、endpoint、model metadata 分成 registry entries；
- 新增一个 custom provider 不修改 `runtime/service.py`、`run_loop.py` 或公共 event vocabulary；
- 将 tool、skill、agent、context transform、MCP declaration 统一走 discover/validate/materialize；
- materialization 生成一个冻结、可 replay 的 capability binding。

验收：增加一个 fake provider、fake tool、fake context transform 只修改 extension package/test fixture，不修改中央 runtime。

### Phase 5：重建 runtime host

- `VoidCodeRuntime` 保留 public boundary、policy、session/task ownership 和 adapter wiring；
- run loop 变成 core engine 的 host adapter；
- background supervisor 只保留通用 task substrate；
- `task`/agent preset/`yield` 成为 delegation adapter；
- hooks 的 argv surface 变成一个外部 adapter，typed extension 归并到统一 phase registry；
- 删除旧 GraphRunRequest、runtime-specific core context、隐式 registry 组合和 obsolete private facades。

验收：新上层 workflow 只组合 `AgentEngine + CapabilityBinding + TaskSpec + ContextPipeline`，不新增 runtime.service 分支。

### Phase 6：文档与旧路径切除

- 更新 `docs/contracts/` 只保留当前 authority；
- 删除旧 aliases、compat facade、旧 event/metadata shape；
- 将历史设计放到明确的 audit/archive，而不是让实现者同时遵守多份契约；
- 修复 snapshot version、event vocabulary、lifecycle hook anchor 等文档-代码漂移。

## 8. 迁移完成的硬验收标准

以下标准比“新增了多少 extension point”更重要：

1. 添加新 provider/tool/agent/context extension **不修改** `runtime/service.py`、`runtime/run_loop.py`、`runtime/config_models.py`、`runtime/events.py`；
2. core 可以在没有 SQLite、workspace、CLI、MCP、LSP 的情况下运行；
3. runtime 仍然是唯一 approval、authorization、session truth、event persistence、execution ownership authority；
4. tool 的真实依赖出现在显式 `ToolContext`，不再依赖隐式 `ContextVar` 才能知道自己是谁；
5. domain event 与 client delivery event 分离；
6. in-memory EventStore、SQLite EventStore、fake provider、real provider adapter 都能跑相同 core contract tests；
7. approval resume、provider fallback、tool timeout、crash resume、late write、fork/checkout 的既有行为不回退；
8. replay 不重新执行 side effect；
9. 一个新 extension 的生命周期、ordering、failure、snapshot、replay 语义可以在一个 package 内定义；
10. `VoidCodeRuntime` 不再拥有 provider turn/tool-call parsing 的业务算法。

## 9. 暂时明确不做的事情

这些不是本次 lower-core 重构的前置条件：

- 任意 agent-to-agent message bus；
- marketplace/dynamic remote plugin；
- cloud execution；
- scheduled runs；
- 完整任意拓扑 multi-agent planner；
- OS-level sandbox（除非产品安全目标明确要求）。

它们可以建立在正确的 lower contracts 之上。现在提前做，会把尚未稳定的 execution core 固化成更大的错误抽象。

## 10. 审计后的最终判断

### 当前能否继续当作框架扩展？

**只能有限扩展。** 新能力可以通过 constructor injection 和少数 typed registry 加入，但一旦进入真实产品路径，通常仍要修改中央 runtime、config、event、storage 或 run loop。它更像一个经过大量 runtime hardening 的产品内核，不是可供多个上层机制轻松组合的 framework core。

### 是否需要重构和迁移？

**需要，而且应该尽快开始。** 继续堆上层功能的代价不是代码行数，而是每个新机制都要穿过多个重复的 snapshot、policy、hook、event、metadata 和 persistence boundary。

### 是否应该全部推倒重写？

**不应该。** 现有 approval、session event、checkpoint、lease、redaction、resume 和 crash invariants 是最有价值的资产。正确方案是：

> **迁移下层执行内核；保留并测试 runtime 治理真相；完成后删除旧路径。**

## 参考来源

### VoidCode 当前实现

- [`runtime/service.py`](../../src/voidcode/runtime/service.py) — runtime composition root 与 public boundary
- [`runtime/run_loop.py`](../../src/voidcode/runtime/run_loop.py) — execution loop、tool governance、checkpoint、provider recovery
- [`graph/contracts.py`](../../src/voidcode/graph/contracts.py) — 当前 graph protocol
- [`graph/provider_graph.py`](../../src/voidcode/graph/provider_graph.py) — provider turn/tool-call streaming adapter
- [`runtime/tool_execution.py`](../../src/voidcode/runtime/tool_execution.py) — canonical tool executor 与 timeout/lease 处理
- [`tools/runtime_context.py`](../../src/voidcode/tools/runtime_context.py) — 当前隐式 tool context
- [`runtime/storage/sqlite.py`](../../src/voidcode/runtime/storage/sqlite.py) — SessionStore protocol 与 SQLite owner
- [`runtime/context/transforms.py`](../../src/voidcode/runtime/context/transforms.py) — 当前最完整的 typed context extension seam
- [`runtime/agent_capability.py`](../../src/voidcode/runtime/agent_capability.py) — capability snapshot materialization
- [`docs/contracts/execution-lifecycle.md`](../contracts/execution-lifecycle.md) — execution ownership/cancel/resume 不变量
- [`docs/contracts/runtime-extension-points.md`](../contracts/runtime-extension-points.md) — 当前 extension boundary 与明确非目标
- [`docs/testing.md`](../testing.md) — 当前 core 测试范围与 near-zero coverage 区域

当前工作树还存在一项文档漂移：[`docs/README.md`](../README.md) 链接 `docs/audits/omp-alignment.md`，但该文件在当前工作树中不存在。本审计文档补齐 `docs/audits/`，不把缺失历史审计当作当前代码行为证据。

### pi upstream

- [`pi-agent-core README`](https://github.com/badlogic/pi-mono/blob/main/packages/agent/README.md) — 独立 Agent state machine、event flow、tool execution 与 continuation seam
- [`pi-agent-core Agent`](https://github.com/badlogic/pi-mono/blob/main/packages/agent/src/agent.ts) — Agent state、queue、abort、lifecycle
- [`pi-agent-core types`](https://github.com/badlogic/pi-mono/blob/main/packages/agent/src/types.ts) — `transformContext`、`prepareRequest`、`beforeToolCall`、`afterToolCall`、`finishTurn`
- [`pi-ai README`](https://github.com/badlogic/pi-mono/blob/main/packages/ai/README.md) — provider/model/wire/transcript boundary
- [`pi extensions guide`](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/extensions.md) — extension lifecycle、tool/provider/MCP/command/context seams与 trusted-code 警告
- [`pi extension types`](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/src/core/extensions/types.ts) — typed events、registration API、tool/provider/session extension shape
- [`pi SDK`](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/sdk.md) — `AgentSession`、`SessionManager`、`ResourceLoader` 的可替换 host boundary
- [`pi session format`](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/session-format.md) — append-only JSONL tree、custom entries、context edits、compaction
- [`pi custom provider`](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/custom-provider.md) — custom provider registration 与 normalized stream contract
