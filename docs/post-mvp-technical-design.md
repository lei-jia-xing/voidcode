# VoidCode Post-MVP 技术设计

## 文档状态

本文档定义 VoidCode 在 MVP 基线之后进入 WIP 阶段的目标技术设计。

它不是一份脱离现有代码的“重做方案”，而是建立在当前仓库已经交付的真实边界之上：

- `runtime/` 仍是产品级执行边界
- `graph/` 仍负责编排，而不是承载产品逻辑
- `tools/` 仍通过运行时统一治理
- CLI / HTTP / Web / 后续 TUI 仍通过运行时契约消费执行与会话状态

本文档位于 `docs/current-state.md` 与 `docs/contracts/` 之间：

- `docs/current-state.md` 描述**现在已经实现了什么**
- `docs/roadmap.md` 描述**阶段性方向**
- `docs/contracts/` 定义**稳定的客户端契约和 schema**
- 本文档描述**从当前基线演进到 post-MVP/WIP 架构的技术路线与模块设计**

## 设计目标

进入 WIP 阶段之后，VoidCode 的目标不再只是“有一个稳定可演示的确定性单智能体循环”，而是向一个**本地优先、运行时中心、可恢复、可扩展**的真实 coding agent runtime 演进。

本阶段的设计目标是：

1. 保持当前运行时边界稳定，不推倒重来。
2. 在现有运行时内部引入一个**真实的、可恢复的、provider-backed 单智能体执行路径**。
3. 将 skill、LSP、ACP、provider 这些现在还停留在发现/配置/存根层的能力，提升为运行时可管理的能力面。
4. 继续强化本地持久化、事件流、审批与恢复，而不是让客户端承担执行状态。
5. 为后续可能出现的 subagent / ACP 控制面预留边界，但不把多智能体作为当前 WIP 的前置条件。

## 非目标

本阶段**不**包含以下内容：

- 不重写为新的 monorepo 或 Node/Bun 主体架构。
- 不绕过 `VoidCodeRuntime` 让客户端直接调用工具。
- 不立即把系统扩展成真实多智能体平台。
- 不引入云同步、托管协作、插件市场或大规模 IDE 生态。
- 不围绕尚未落地的 UI 愿景重新定义后端边界。
- 不在本文件中重复定义 `docs/contracts/` 已拥有的 schema 细节。

## 当前基线

当前仓库已经具备清晰的 MVP 基线，这些现实约束决定了 post-MVP 设计必须如何展开。

### 已稳定的部分

- `src/voidcode/runtime/service.py` 已经是明确的产品级运行时边界。
- 运行时已经拥有：
  - 请求/响应与流式输出
  - 工具注册与统一执行管线
  - 权限决策与审批恢复
  - hooks 执行
  - SQLite 会话持久化与恢复
  - HTTP 传输
  - skill discovery
- CLI 与 Web 已经消费统一的运行时边界；TUI 仍不完整，但方向一致。
- 事件流已经是系统的关键观测面，并支持恢复/重放。

### 仍然偏 MVP / 存根的部分

- `src/voidcode/graph/read_only_slice.py` 仍是一个小型的确定性解析式循环。
- skill 目前只有发现，没有真正的执行语义。
- LSP / ACP 目前是类型化配置载体和 disabled stub。
- provider/model 抽象仍然很窄，尚未形成真实执行引擎的统一入口。
- 前端和 TUI 仍然主要在“消费已有 runtime path”，还没有形成更成熟的产品交互层。

### 基线结论

因此，post-MVP 的第一步不应该是“增加更多表面”，而应该是：

1. 保持当前 runtime 为稳定控制面。
2. 在 runtime 内部增加一个新的真实 agent execution engine。
3. 让 provider、skill、LSP、ACP 都继续沿着 runtime-managed capability 的方向落地。

## 固定架构不变量

以下设计约束在 WIP 阶段默认保持不变，除非未来专门通过架构决策文档推翻。

### 1. Runtime 是系统控制平面

`VoidCodeRuntime` 是产品边界，而不是可有可无的适配层。所有执行、事件、审批、持久化和恢复都必须经过它。

### 2. Graph 负责编排，不负责产品治理

Graph 可以演进为更真实的 agent loop，但权限、hooks、工具治理、持久化、配置优先级和客户端传输，仍由 runtime 负责。

### 3. 客户端不直接调用工具

CLI、Web、TUI、后续可能的 IDE client 都只能消费 runtime contracts、runtime events 和 session state，不能形成各自独立的执行路径。

### 4. 本地持久化是运行历史的真相来源

`.voidcode/` 下的本地状态（尤其是 SQLite session store）继续作为运行历史、审批状态和恢复检查点的权威来源。客户端缓存只能作为渲染加速层，不能成为真相源。

### 5. 配置优先级必须集中且确定

所有 provider、engine、tool、skill、LSP、ACP 的运行时行为都必须消费 runtime 解析后的配置结果，而不是自行解析来自 CLI、环境变量或客户端输入的原始值。

### 6. 事件流必须可重放、可恢复、可演进

运行时事件继续承担观测、客户端渲染和恢复的三重职责。任何事件扩展都必须保持顺序稳定、语义明确，并尽量兼容已有消费方。

## OpenCode 参考后的目标形态

OpenCode 提供了几个值得借鉴的成熟模式：本地优先 session truth、transport 与 client 分离、typed event flow、provider abstraction、以及 ACP / agent role 的控制面思路。

对 VoidCode 而言，正确的吸收方式不是复制实现语言或仓库结构，而是将这些模式映射到现有 Python runtime 边界中。

### 应保留的核心形态

- **Local-first**：所有关键运行状态仍优先保存在本地。
- **Thin clients**：CLI / Web / TUI 是 runtime 的消费者，而不是执行中心。
- **Typed events**：事件词汇表是客户端与恢复逻辑共享的稳定协议。
- **Config layering**：配置优先级必须统一管理。
- **Control-plane mindset**：agent execution、tool execution、approval、resume 应由 runtime 控制。

### 不直接照搬的部分

- 不引入基于 Node/Bun 的整体重构。
- 不立即把 agent/subagent 关系产品化。
- 不在当前阶段拆出独立本地 daemon，除非未来出现多客户端并发附着或运行脱离调用进程的明确需求。

## 目标运行时模型

Post-MVP 的目标不是“一个更复杂的 graph”，而是“一个更完整的 runtime-managed execution model”。

### 逻辑分层

目标分层如下：

1. **Clients**
   - CLI
   - HTTP/SSE transport
   - Web frontend
   - TUI

2. **Runtime boundary**
   - run / stream / resume 入口
   - session persistence
   - event emission
   - config resolution
   - permission / approval
   - hooks
   - capability registry and lifecycle

3. **Execution engines**
   - `DeterministicReadOnlyGraph` 继续作为 reference engine
   - 新增一个 provider-backed single-agent engine，作为首个真实 WIP engine

4. **Managed capabilities**
   - tool providers
   - skill execution
   - provider/model abstraction
   - LSP lifecycle
   - ACP lifecycle

5. **Persistence and contracts**
   - SQLite session truth
   - runtime events
   - client-facing runtime contracts

### 关键变化

最关键的变化是：runtime 不再只驱动“确定性工具循环”，而是能够在同一个治理边界内管理多个 execution engine，并把它们都接到统一的工具、审批、事件和恢复管线中。

这使得系统能够在不破坏已有客户端和 session 语义的前提下，从 MVP 的 deterministic engine 演进到真实 agent execution。

## 关键能力面设计

### 1. Execution Engine 抽象

当前 `graph/read_only_slice.py` 更像一个“参考实现”。WIP 之后，运行时应显式支持 engine selection。

建议抽象：

- `deterministic`：当前 deterministic parser/graph engine
- `agent`：新的 provider-backed single-agent execution engine

运行时负责：

- 选择 engine
- 为 engine 提供 resolved config、session state、tool metadata
- 接收 engine 产出的 step / event / tool intent
- 通过 runtime-owned permission + tool pipeline 执行实际副作用

这样可以保证：即使 execution engine 更真实、更复杂，工具调用与审批恢复的治理仍然完全一致。

### 2. Provider / Model 抽象

Post-MVP 必须把 model invocation 从 ad-hoc config carrier 提升为运行时内部的明确能力边界。

该抽象的职责应仅限于：

- provider 选择
- model 选择
- provider capability 描述
- 推理调用与流式结果适配

它**不**负责：

- 工具执行
- session persistence
- approval / permission
- hooks
- 客户端 transport

换句话说，provider abstraction 是 runtime 的一个子系统，而不是新的系统中心。

### 3. Skill Execution

当前 skill 只有 discovery，下一阶段最值得优先落地的是 runtime-managed skill execution。

目标方向：

- runtime 发现 skill
- runtime 决定当前 run 中哪些 skill 被启用
- engine 在规划时能看到 skill 提供的上下文/约束
- skill 的执行或注入过程由 runtime 记录事件并持久化必要元数据

这一能力可以先限定在单智能体路径中完成，不要求同时引入 subagent。

### 4. LSP 作为运行时管理能力

LSP 不应只是配置占位符，而应成为 runtime 可启停、可观测、可注入给 engine/tool 的能力面。

第一阶段不需要完成完整的 IDE 级 LSP 体验，但应明确：

- 生命周期由 runtime 管理
- 配置由 runtime 统一解析
- tool / engine 通过 runtime 请求 LSP 能力
- LSP 相关事件进入统一事件流

### 5. ACP 作为未来控制面能力

ACP 在当前阶段不应被理解为“必须马上做多智能体”。更合理的定位是：

- 先作为 runtime 内的受管控制面能力保留接口
- 后续可用于内部 agent role 隔离、外部代理连接或更强的生命周期管理
- 是否独立为本地 daemon，取决于多进程/多客户端附着需求是否真实出现

换句话说，ACP 是边界预留，不是当前 WIP 的首要交付物。

### 6. Typed Runtime Event Protocol

事件系统需要从“当前 MVP 已够用的事件流”演进到更稳定的 runtime protocol。

在语义层面，post-MVP 事件至少应覆盖：

- run started / resumed
- assistant output delta / finalized output
- tool planned
- permission resolved
- approval requested / resolved
- tool started / completed / failed
- skill loaded / skill applied
- provider selected / provider failed
- checkpoint persisted
- run completed / failed

具体 schema 应在稳定后下沉到 `docs/contracts/`，而不是在本文件中直接定稿。

### 7. Client Parity

客户端继续沿“统一 runtime truth”推进：

- CLI 作为最强参考客户端
- Web 继续建立在真实 runtime transport 之上
- TUI 追平同一套 session / event / approval 语义

任何客户端增强都应建立在 runtime event 和 runtime session 语义已经稳定的前提下。

## 配置模型与优先级

WIP 之后，配置不应继续由各子模块分别解释，而应进一步集中到 runtime resolution。

建议优先级顺序：

1. runtime internal defaults
2. user config
3. project config
4. environment overrides
5. request / CLI overrides

所有 execution engine、provider、skill、LSP、ACP 子系统都应消费 resolved config，而不是重复解释原始输入。

这样能确保：

- session resume 时行为稳定
- 客户端之间的行为一致
- 测试可以覆盖确定性的 precedence 行为

## 会话、恢复与检查点

进入真实 agent execution 后，session persistence 不能只存“最终结果和少量事件”，而必须继续强化为可恢复的运行历史。

关键要求：

- SQLite 仍为 canonical session store
- approval interrupt 后可恢复
- transport 中断后可重放
- engine 状态至少要持久化到足以恢复 run 的粒度
- 客户端不负责重建真实执行状态

对当前架构的直接要求是：新的 provider-backed engine 必须重用现有 session / event / approval / resume 机制，而不是建立自己的旁路状态。

进一步说，checkpoint 在这里必须被视为 **resume anchor**，而不只是给客户端展示的 UI summary：它至少要能够支撑 approval resume、transport replay 之后的继续执行，以及 provider-backed 单智能体路径在 compaction 之后的恢复。

这里还应吸收 OpenCode 已暴露出来的一个现实教训：SQLite 作为本地真相源是合理的，但当前阶段不应为了 shared-volume、多进程附着或 daemon 化场景过早引入更复杂的存储拆分。优先顺序应当是先在现有 runtime + SQLite 边界内把 retention / checkpoint / archive 语义建模清楚，再根据真实瓶颈决定是否需要 file-backed archive 或独立服务进程。

### 当前需要锁定的 retention / compaction / checkpoint invalidation 语义

在 `#70` 的第一落地切片之后，runtime 已经具备了用于 approval resume 的内部 persisted checkpoint anchor；`#82` 需要在此基础上锁定后续实现必须遵守的最小语义边界。

#### 1. Checkpoint 的职责边界

- `resume_checkpoint_json` 是 **runtime 内部的 resume anchor**，不是客户端 replay 历史的替代物。
- 在当前阶段，客户端可见的 replay 语义仍然依赖完整的持久化事件流；checkpoint 只服务于 runtime 恢复与继续执行。
- 在 `#84` 之前，任何 retention / compaction 设计都不得默默改变客户端观察到的有序 replay 契约。

#### 2. Retention 的最小不变量

- waiting session 必须保留足够信息，以支撑 approval resume 与显式决策后的继续执行。
- terminal session 可以拥有内部 checkpoint，但这不自动意味着其完整事件历史可以被裁剪。
- 在 archive 语义正式落地前，runtime 必须继续把完整事件流视为当前 hot-path replay 的真相源。

#### 3. Compaction 可以做什么，不能做什么

- compaction 可以减少 runtime 为继续执行所需的重建成本，把部分恢复输入替换为已验证的 checkpoint anchor。
- compaction 不能在缺少明确 archive / replay 方案的情况下，把客户端仍需观察的历史事件静默丢弃。
- 当前阶段允许先定义触发点和前置条件，但不在 `#82` 中引入新的 archive 存储实现。

#### 4. Checkpoint 失效（invalidation）规则

checkpoint 至少应在以下情形下被视为 **invalid**：

- payload corrupt / unreadable
- checkpoint schema 或 version 不受支持
- resume-critical 的 runtime metadata / config shape 与当前期望语义不匹配
- checkpoint 引用的最小前置状态已经缺失，导致继续执行无法安全重建

invalid checkpoint 的默认处理策略应当是：**在可行时回退到 event-based / legacy reconstruction；只有在不存在安全恢复路径时才显式失败。** 这也是 `#83` 所收口的 correctness 方向。

#### 5. Issue 顺序

- `#70`：checkpoint groundwork 已完成
- `#82`：定义 retention / compaction / invalidation 语义
- `#83`：单独跟踪 corrupt / unreadable checkpoint fallback correctness
- `#84`：cold-session archive / replay strategy

## WIP 实施波次

### Wave 0：锁定不变量并补齐设计入口

目标：明确 post-MVP 的边界与首个实施切片。

工作内容：

- 新增本技术设计文档
- 明确 runtime 仍为控制平面
- 明确 deterministic engine 保留为 reference path
- 明确多智能体、云协作、插件市场不进入 immediate WIP

完成信号：

- 团队对 post-MVP 目标架构有共享理解
- 后续 issue / PR 能引用统一设计文档

### Wave 1：Provider-backed Single-Agent Engine

这是 post-MVP 的**最高优先级 WIP 切片**。

目标：在现有 runtime 边界内部，新增一个真实的单智能体执行路径。

工作内容：

- 引入 execution engine 选择机制
- 保留 deterministic engine
- 新增 agent engine
- 为 agent engine 接入 provider/model abstraction
- 复用 runtime-owned tool registry、permission、approval、hooks、session persistence 和 HTTP/CLI/Web transport

完成信号：

- 一个真实的单智能体 run 可以在 provider 支撑下完成
- 工具调用仍通过 runtime 治理
- 审批与恢复保持成立
- session replay 仍然可用

### Wave 2：Skill Execution 落地

目标：从 skill discovery 过渡到 skill execution。

工作内容：

- 定义 skill 在运行时中的启用与注入语义
- 将 skill 相关事件纳入 runtime event flow
- 让 agent engine 能消费 skill 提供的上下文或能力面

完成信号：

- skill 不再只是发现结果，而是影响真实执行行为
- skill 生命周期可观测、可测试、可恢复

### Wave 3：LSP / ACP Managed Capability 化

目标：把当前 carrier/stub 变成受 runtime 管理的可启停能力。

工作内容：

- LSP 生命周期管理
- ACP 生命周期管理
- 对 tool / engine 暴露统一访问接口
- 增补必要事件与配置开关

完成信号：

- LSP / ACP 不再只是配置结构
- 启用/禁用行为、失败行为与事件记录可验证

### Wave 4：Client Parity 加固

目标：让 CLI / Web / TUI 对同一套 runtime truth 拥有更高一致性。

工作内容：

- 补齐 TUI 对会话、恢复和审批语义的消费
- 强化 Web 对 richer runtime events 的渲染
- 保持 CLI 为 reference client

完成信号：

- 多客户端消费统一 session / event truth
- 新能力不会只在单一客户端成立

## Immediate WIP 定义

如果只定义一个“现在立刻进入 WIP 的首批工作面”，建议范围严格限定为：

### WIP-1：单智能体真实执行闭环

必须完成：

- runtime 内的 engine selection
- 一个 provider-backed single-agent engine
- provider 抽象的最小闭环
- 基于现有 runtime pipeline 的工具执行、审批、恢复、持久化
- 对应的事件扩展与测试

明确不做：

- 多智能体调度
- subagent 产品化
- 独立 daemon 化
- 云端协作
- 大范围 UI 重写

这是当前阶段杠杆最高的一步，因为它会把仓库从“稳定 deterministic MVP”推进到“真实可扩展 agent runtime”，同时又不破坏当前边界。

## 验证与测试矩阵

每个实施波次都应遵循 contract-first / runtime-first 的验证方式。

### 1. 契约层

- 若事件、传输或配置语义变稳定，则更新 `docs/contracts/`
- 本技术设计文档只描述演进方向，不取代 schema 文档

### 2. 单元测试

优先覆盖：

- provider resolution
- config precedence
- skill loading / skill enablement
- LSP / ACP enablement guards
- event translation / normalization

### 3. 集成测试

优先覆盖：

- runtime run / stream / resume 主路径
- approval continuity
- session persistence
- richer engine 下的 event ordering
- provider failure / recovery path

### 4. 客户端冒烟测试

- CLI 继续作为 reference smoke path
- Web 验证 session replay、runtime event 渲染、approval handling
- TUI 在进入对应 wave 后验证相同语义

### 5. 手动 QA

每个新 capability 都必须进行实际运行验证，而不是只通过类型检查或单元测试判断完成。

## 风险与开放问题

### 1. Skill execution 与 subagent 的先后顺序

建议先完成 skill execution，再决定是否需要 subagent role。否则系统会在最基础的运行时能力还未落地时就被拉向更高复杂度。

### 2. ACP 是否需要独立 daemon 化

当前不需要预设为 daemon。只有在出现多个客户端/进程同时附着同一 live run，或运行必须脱离调用进程长期存活时，才值得将 runtime 提升为独立本地服务进程。

### 3. Event vocabulary 的扩展节奏

事件扩展过快会让客户端和恢复逻辑反复震荡；因此建议先稳定运行时语义，再把稳定部分正式写入 contract docs。

### 4. Provider abstraction 边界膨胀

必须防止 provider 层吞掉工具、session、permission 或 transport 职责。否则 runtime 将失去作为系统控制面的价值。

## 补充架构改进优先级

`docs/arch-improvement-notes.md` 提出了若干面向真实 LLM/provider 接入后的补充建议。它们不改变本文档已经确定的主方向，但应作为 post-MVP / WIP 阶段的细化优先级纳入规划。

### P0：接入真实 provider 前必须完成

#### 1. Context Window 管理

当前运行时在真实 provider 接入后会面临上下文持续增长的问题；如果没有窗口裁剪、压缩或 summarization 触发点，`single_agent` 路径很快会遇到上下文溢出或成本失控。

建议最低要求：

- 为 engine 提供受控的消息窗口输入，而不是无界历史
- 在 runtime 中预留 context compaction / summarization 触发点
- 将 provider 的上下文上限错误纳入可识别的错误分类

这一项应与 provider fallback 一起，在真实 provider 落地前完成。

#### 2. Provider Fallback Chain 与错误分类

当前 provider-backed single-agent engine 只是最小闭环，尚未具备真实 provider 接入后的容错能力。下一步必须补齐：

- preferred model → fallback chain 的降级机制
- rate limit / context limit / invalid model / transient provider failure 的错误分类
- runtime 对这些错误的统一处理入口

这项工作仍应保持在 runtime control plane 内完成，而不是把重试、降级和失败恢复下放给客户端。

### P1：近期高价值改进

#### 3. `max_steps` 可配置

当前单智能体路径的 step 上限不应长期硬编码。将其纳入 `RuntimeConfig` 或 request 级覆盖可以较低成本提升灵活性，并让不同任务类型拥有更合理的执行预算。

#### 4. 流式执行模型从同步 `Iterator` 演进到异步流

当前同步 `Iterator` 路径足以支撑 MVP 与首个 single-agent slice，但在真实 provider 流式输出、并发工具执行和更丰富的 transport 场景下，异步流模型会更合理。

这项工作应在真实 provider 路径稳定之后推进，而不是与首个 single-agent engine 同时耦合实现。

### P2：中期能力增强

#### 5. 并发工具执行

当前工具执行仍然是串行模型。中期可以利用已有 `read_only` 元数据，将只读工具批量并发，而继续保持写工具串行与审批治理不变。

建议边界：

- 只读工具可并发
- 写工具与高风险 shell 继续串行
- 并发不应绕开现有 runtime 事件、审批与恢复语义

#### 6. 工具动态加载与插件式发现

当前内置工具提供方已稳定，但后续可以把工具路径配置和动态发现能力做成更正式的 runtime capability。该项与 Skill / MCP / 扩展生态方向相邻，但不需要在当前阶段与真实 provider 接入绑定推进。

#### 7. 会话事件日志增长控制

随着真实 provider 进入系统，SQLite 中的 session/event 数据会增长更快。中期需要补齐：

- 事件保留策略（由 `#82` 先定义语义）
- 会话压缩 / summarization checkpoint（仍需服从当前 replay 不变量）
- 长会话归档与恢复策略（由 `#84` 承接实现）

这项工作直接关系到长期运行稳定性，应与 richer runtime event protocol 一起设计。

### P3：功能完善与长期评估

#### 8. 进程内 Hook

在保留 subprocess hook 的同时，可以逐步增加进程内 hook 接口，以支持更细粒度的运行时扩展与状态感知。

#### 9. MCP 集成

这与 LSP / ACP 一样，属于受 runtime 管理的外部能力接入方向。应在运行时边界稳定后推进，而不是先把核心执行路径建立在 MCP 之上。

#### 10. 重新评估 LangGraph 的必要性

当前 deterministic 与 single-agent graph 都还比较轻量，因此长期需要重新评估 LangGraph 是否继续提供足够价值。该评估应以实际复杂度为准，而不是预先假设“必须保留”或“必须移除”。

评估标准应包括：

- 当前 graph 层是否已经承担真实状态机复杂度
- plain async loop 是否能更直接表达单 agent 执行模型
- 移除或保留 LangGraph 对 runtime boundary 的影响

这是一项长期架构判断，不应在当前 WIP 主路径中抢占优先级。

## 建议的后续 issue 方向

在 provider-backed single-agent path、checkpoint groundwork 和配置持久化语义已经落地之后，下一批更适合直接启动的 issue 应收敛到以下几类：

1. retention / compaction / checkpoint invalidation semantics (`#82`)
2. corrupt / unreadable checkpoint fallback correctness (`#83`)
3. runtime-managed provider config hardening
4. runtime-managed skill execution semantics
5. read-only managed LSP vertical slice
6. tool contract hardening with formatter hook presets
7. archive and replay strategy for cold sessions (`#84`，但仍应晚于前述语义与 contract 收口)

其中需要明确的优先级判断是：

- provider config、skill execution、read-only LSP、tool contract/formatter hooks 可以直接进入下一轮实现
- ACP 继续作为 control-plane boundary 预留，不在当前批次产品化
- MCP 继续保持为 runtime-managed external capability 的后续方向，但不作为当前 MVP 主路径的优先实现

formatter hook presets 的建议边界：

- Python → `ruff format`
- TypeScript / JavaScript / JSON / Markdown / YAML → `prettier`
- Rust → `rustfmt`
- Go → `gofmt`

它们应建立在现有 runtime-owned hook surface 之上，而不是把 hook 体系扩展为不受控的任意脚本平台。

## 总结

VoidCode 的 post-MVP 演进应当围绕一个核心原则展开：**强化 runtime 作为本地优先 coding agent control plane 的地位，而不是增加更多绕开它的执行表面。**

这意味着下一阶段最值得做的不是多智能体平台化，而是：

- 保留 deterministic engine 作为 reference path
- 新增一个真实的 provider-backed single-agent engine
- 让 skill、LSP、ACP、provider 都通过 runtime-managed capability 的方式逐步落地
- 继续把 session、event、approval 和 resume 保持为统一真相源

这就是 VoidCode 从 MVP 进入 WIP 的正确起点。
