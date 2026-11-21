# TODO

本文件集中跨模块取舍与导航；具体待办、现状证据和验收条件放在所属模块。新增项不代表功能已实现或已经实测。

- [Core](../src/voidcode/core/TODO.md)：可选 provider-turn cap 与显式共享 root-action grant 已实现；adaptive model-headroom 已实现（per-dispatch 自适应收缩）；输出投影剩余事项及真实生产化边界继续按专属 TODO 跟踪。
- [SDK](../src/voidcode/sdk/TODO.md)：`BackgroundContext.wait_tool` 的后台 action 结果等待已实现并经 Core/外部插件验证；普通前台 callback 的 suspend/恢复与完整 `eval` 产品仍属 P1。adaptive headroom 已实现，结果 schema 等表达力缺口仍开放；typed child budget outcome 与 owned first-settled child wait/atomic delivery 已实现，含精确事件来源、重开恢复与相邻 `join_child` polling gap 已修复（事件驱动）；typed child output/history capture errors 及 review/task/batch/wait/agent:// 透传与 workflow 持久化亦已实现，snapshot 当前-run 诊断为显式剩余缺口，见 SDK TODO。
- [插件](../src/voidcode/plugins/TODO.md)：仅记录已验证的具体整改与仍未收敛的图片/UI、生产化、URI、Agent、进程和检索任务，不宣称全插件审计。
- [TUI](../src/voidcode/tui/TODO.md)：独立记录图片渲染与实时进度/交互边界；viewer/API 不替代每条路径的真实 PTY 验收。
- [Web UI](../frontend/src/core/TODO.md)：artifact 查看/复制/导出与真实浏览器验收。
- [ast_grep](../src/voidcode/plugins/ast_grep/TODO.md)：只读查询边界。
- [命令交互验收](./command-ux.md)：逐命令客户端验收清单。

## 取舍

要吸收的不是一批同名工具，而是 **可组合的工具调用、统一可寻址的资源、可持续交互的 Agent，以及消费这些契约的 TUI**。只补皮肤、增加 shell 包装器或堆插件数量，不能补上这条链路。

当前已有 typed tool body、`OutputBounds`、内容寻址 artifact、可注册 `ResourceReader`、工具进度回调、子 Agent 跟进/完成通知、动态 workflow、命令表单，以及两端长输出分页；这些不是从零重建的待办。已完成项保持勾选，不把“图片输入已通”混同于“终端可以显示工具返回的图片”。

已核实的关键断点：普通 SDK `invoke_tool` 仍只排队；后台 action 可用 `BackgroundContext.wait_tool` 安全等待完整、request-correlated 结果，但这不是普通前台 callback 的 suspend API。`ResourceReaderServices` 已提供 caller-scoped child/process read projections；Subagents `agent://` 与 Exec `proc://` 复用注册、artifact 分页和 owner/session 边界。通用资源能力发现/枚举/搜索仍开放；`ToolOutput` 组合输出（有序 parts + 有界声明式视图 + 两端渲染）已实现，见 SDK TODO。Core 已删除隐式 32-turn 默认和普通图片文件 50 KiB 拒绝，root-action grant 已持久共享并通过重启/并行 child 验收；`ArtifactPage` 已贯通 Core、SDK 本地/RPC。active-model 自适应投影 headroom 已实现，仍开放结果 schema。Typed child budget 与 owned first-settled wait/atomic result delivery 已由精确 durable facts 贯通；native Core proof 覆盖三个 gated child、52,000 个 UTF-8 字节、不可变输出 URI/read/reread、去重与 reopen。等待事务只在有效 tool result 提交时保存精确 source event receipt；读快照不消费。Typed child output/history capture errors 已贯通 SDK outcome、review/task/batch/wait/agent:// 与 workflow 持久化，preview 独立于 artifact 发射；单 run `join_child` 已改为事件驱动、无 polling；accepted-yield、shared batch context、peer/process waits、snapshot 当前-run 诊断与真实 UI 交付仍开放。

## 优先级

| 优先级 | 落地点 | 归属 |
|---|---|---|
| P0 | 自适应模型投影 headroom（已实现）、资源完整读取与真实 UI/生产任务验收 | Core、插件、TUI/Web |
| P1 | 前台 suspend/完整 `eval` 工具桥、URI 跨工具复用、Agent 持续交互、富结果与结构化问答、后台执行收尾 | SDK/Core、插件、TUI/Web |
| P2 | 多格式读取、语义检索、结构化模型查询、语义重构闭环 | 插件、SDK |

后台 action 的 SDK 工具等待协议已实现；完整 `eval` 的普通前台 suspend/恢复仍属 P1。混合图文结果与完整问答分别依赖 SDK 的组合输出、用户问答契约。按模块待办中的依赖落地，不先造一个覆盖所有场景的平台。

## 实施约束

- 保留 plugin-only Core、SDK 依赖方向、owner/lineage/revision 隔离、可撤销 lease、显式审批和 append-only 事件；产品机制不下沉为 Core 内置工具。
- 优先用已有 body、资源页、进度、子会话与 background action 契约；公开契约不够则先补 SDK TODO，不导入 peer 插件、不解析私有 `core.*` 事件、不维护第二套能力或权限注册表。
- 不照搬 undo/fork/历史重写、OMP 的双存储体系，或把所有 scheme 都做成可写文件。任意脚本执行是执行能力，工具桥和审批不是 OS 沙箱。
- 以完整任务验收：发现能力 → 调用 → 等待/交互 → 查看完整结果 → 跟进/取消 → 断线/重启后的真实状态；UI 要在真实客户端观察，缺依赖、未知状态和不能恢复必须显式呈现。

## 对照证据口径

历史条目的 OMP 源码基线固定到 [v18.12.6（commit `45c35e40c46166dc726ccc52f5d27f40f2449f99`）](https://github.com/can1357/oh-my-pi/tree/45c35e40c46166dc726ccc52f5d27f40f2449f99)；当时隔离运行实际打开过 `/model` 选择器，其他对照仅为源码核对。

Agent `agent://` 对照固定到 OMP v18.20.2 源码 commit [`7c49c11aa5bbaf3f418406583096168ad5b8c6dc`](https://github.com/can1357/oh-my-pi/tree/7c49c11aa5bbaf3f418406583096168ad5b8c6dc)，并用当前官方 runtime 文档核对 `agent://` 最新 published output 与 `history://` 跨轮 transcript 的区别；不宣称本轮实跑 OMP。其他本轮新增条目核对 VoidCode 当前源码/契约与当前会话可读取的 OMP 官方运行时文档，不宣称这些新文档对应旧版 v18.12.6，也不宣称本轮实跑 OMP 图像终端或 `eval` 场景。

## 不以添加 MAX 代替设计

| 限制的性质 | 决策 |
|---|---|
| 真实协议、解码、内存或外部输入风险边界 | 只在负责该边界的一层保留有依据的保护；说明单位、来源与触发行为 |
| 用户/宿主明确授予的费用、token、时间或轮数预算 | 明确授权与继承；不能偷换成所有任务默认都有的寿命 |
| UI 预览、折叠、分页与模型上下文投影 | 属于消费策略；原件、资源身份及完整读取能力不变，按视窗/请求/上下文选择 |
| 无依据的业务 MAX、重复预览、预览截断原始标识 | 删除或改成按需读取；不是把常量改大，也不是每个数字都加配置项 |

本轮只抽查并逐条记录 shell/web/child 输出、monitor、AST、goal 及 Core 投影的行为与证据；不宣称审完所有插件或删除所有 MAX。已有完整 artifact、UTF-8 游标分页和 UI 展开是应复用的能力，不能为了删常量而删掉资源保护。

OMP 自己也有 request budget 与 preview 阈值；它们不自动构成 VoidCode 的合理默认值。新待办中的“有界”必须对应实际消费者或资源风险，不能成为继续机械添加 `MAX_*` 的理由。

项目自己写下的帧大小、preview 或输入常量，不能反过来作为“这是协议硬限制”的证据；保留限制需要可核查的来源或资源风险，默认值也需要独立理由。

## Hook 与 staged-tree 隔离观察

首次 Core/SDK 阶段提交时，repo-wide pre-commit 会自动暂存 tracked unstaged diff，但 Ruff/ty 仍扫描工作树中的 untracked 新模块。实际失败是 `artifact_viewer.py` 已导入 `KittyImagePlacement`，而未暂存的 `region.py` 尚无该纯 frame placement 数据契约。通过把经审阅的 `region.py` 作为完整 foundation 文件提交，并修复有限格式问题后，所有原配置 hooks 均通过；未绕过或弱化检查。

这是 staged-tree/worktree 隔离脆弱点，不是运行时或 SDK 缺陷。后续可研究 exact-staged-tree/isolation，但必须保留完整 Ruff、ty、API 检查；不得靠抑制导入或降低 hooks 规避依赖。

## 架构与代码质量清算（零向后兼容）

全仓 ~71.4k 行 Python/SQL 存在大量为绕开 Schema 变更而塞入 JSON 的私货、无迁移路径却长期保留的兼容分支、7000 行级上帝文件、未接线却先铺满协议的空架子、跨层 3–4 次重复深拷贝与校验，以及用 `time.sleep` 轮询/每次调用新建线程代替事件驱动的反模式。**清算原则：零向后兼容，直接改表/删字段/砍死代码，绝不保留兼容分支或过渡 shim。**

| 清算领域 | 核心症状与代表证据 | 详单归属 |
|---|---|---|
| **Schema 仪式感与 JSON 偷渡** | `schema.sql:6-10` 专门建单行表 `kernel_identity` 存 magic/version，`store.py:399-528` 每次打开库额外开只读连接跑 9 条 SQL/PRAGMA 探针（含检查已废弃的 `"generation" in receipt_cols`）；`wakeups` 表因不愿改 DDL 把 `root_id`/`root_action_budget` 偷渡进 `payload_json._voidcode_core_wakeup`（`store.py:72-94,1209-1215`）；`continuations` 表靠 `source='@core/background'` 与 `json_extract(payload_json, '$.kind')` 混装后台任务、子会话消息和普通续跑，并在 claim/fail 时原地覆写 `payload_json`（`store.py:1397,3167,3819-3882`）；`_session_record` 仍在兼容 schema v1 的 7 列短元组（`store.py:374-380`）。 | [Core TODO](../src/voidcode/core/TODO.md) |
| **Core 上帝对象与全表扫描/线程泛滥** | `engine.py`（6986 行）、`store.py`（4031 行）、`delegation.py`（2081 行）、`plugins.py`（1828 行）、`services.py`（1535 行）。每次 `_get_session_usage`、`measured_tokens_per_character`、`_drain_deferred_requests`、`_child_fact_payload` 全量 `store.events(session_id)` 线性扫描并反序列化全部历史；`PluginRuntime.invoke` 每次工具调用都启一个 20Hz `watcher` 线程并在 `finally` `join(timeout=5)`（`plugins.py:814-827,1123-1130`）；`DelegationManager` 每次 spawn/send 裸启 `threading.Thread`、提交父事实时 `time.sleep(0.1)` 忙等（`delegation.py:579,1116,1847`）；`Core.wait_background_actions` 靠 `time.sleep(0.2)` 猜状态（`engine.py:4201,4241`）；`services.py:790-965` 对同进程 `DelegationManager` 刚产出的 `ChildOutcome` 转成 dict 再逐字段做 135 行类型重校验。 | [Core TODO](../src/voidcode/core/TODO.md) |
| **SDK 悬空契约与 JSON 克隆税** | `sdk/suspend.py`、`sdk/foreground.py`、`sdk/ask.py`（共 941 行）及 `PluginServices.suspend_foreground_composition`/`suspend_for_ask` 在 `core/` 中**零实现、零引用**；`PluginServices` 膨胀为 ~45 方法上帝 Protocol；每个 DTO `__post_init__` 递归 `own_json_object` 冻结，紧接着 `_decode` 又 `json_wire_object → json.dumps → TypeAdapter.validate_json` 递归拆冻、序列化再解析（`sdk/client.py:32`、`codec.py:69`、`views.py:317`、`transcript.py:356`）；`codec.py:88-91` 为“保持旧版字节一致”刻意 `pop("output_schema")`；`gates.py:175` 为迁就旧测试用 `compare=False` 篡改相等性。 | [SDK TODO](../src/voidcode/sdk/TODO.md) |
| **插件/Capabilities 巨石与遗产移植** | `workflows_plugin.py`（2352 行，异步驱动里套 `threading.Lock`/`Semaphore` + 每节点 7 个 sha256 字段账本 + 复制两遍 80 行 1s 轮询 `_run_node`）；`goal_plugin.py`（1729 行，塞入 18 个 token/cost 计数器与 `.voidcode/attachments` 旧路径双读兼容）；`todo_plugin.py`（1378 行，持久化派生 `summary` 并在读取时校验 + 两套 `<system-reminder>` 状态机 + 每条 fact 重扫 3 页历史）；`subagents_plugin.py`（1789 行，手写 10 个 `_parse_*` + 兼容旧行 `_CHILD_OPTIONAL_KEYS`）；`cron`/`compaction` 顶部直接标注“verbatim legacy copy”；`providers/native/tokenizer.py` + `claude_tokenizer/` + `tokenizer_data/*.bz2` 为估算价格塞入整套离线分词器与 bzip2 词表。 | [插件 TODO](../src/voidcode/plugins/TODO.md) |
| **Outer / Transport / TUI / Web 双栈重复** | `bootstrap.py` 三重投影配置、在无迁移承诺下保留 `snapshot.version != 2` 与 `old snapshots without sandbox` 分支，`http.py` 留着从不读取的 `control_token`/`fenced`/`generation`/`upgrade_id`；`tui/client.py` 与 `local_client.py` 无共享 ABC 复制 16 个方法，进程内 `local_client` 把 Core 对象 `.as_payload()` 转 dict 再 `.from_payload()` 解析回来；`rpc.py` 与 `local_client.py` 靠 `0.1s` `asyncio.sleep` 轮询事件流；`terminal_app.py`（3345 行）固定 30fps 空转 sleep、手写第二套 `/tui` 命令系统、在 TUI 里直接读写 `voidcode.json` 切模型；`composer.py` 与 `term.py` 各自实现一套 CJK/grapheme 宽度引擎，`term.py:289` 每次调用新建 `rich.Console`，还留着 obfuscated `Win32 ctypes` 终端后端；`CoreApp.tsx`（2601 行）单组件 30+ hooks，与 TUI 重复实现 slash 解析、gate reducer 与 tool output 渲染。 | [TUI TODO](../src/voidcode/tui/TODO.md)、[Web UI TODO](../frontend/src/core/TODO.md) |

### 清算结果（2026-11-19，以上各表详单已逐项核验落点）
- Schema/JSON/兼容：三项全落（`kernel_identity` 删除、`wakeups`/`checkpoints` 建列、`background_actions` 独立成表、`json_extract` 清零、v1/v5 兼容分支删除）。用户可见：旧库拒绝不迁移（`ForeignDatabaseError`）。
- Core 上帝对象：`ChildOutcome` 直传、`activation.py` 抽出、`store.py` 拆为 facade + records/sessions/state/continuations/writes、索引 + `events_by_types` + `payload_bytes`、watcher/轮询/`sleep` 同步、线程池收敛、`child_history` 删除全部落地；`per-turn rebuild` 的全量拉取属模型所需而保留。2026-11-21 补齐：`PluginRuntime` 单 builder、`LeaseIdentity`/`LeaseStaging` 拆分、background execution 抽出、artifact `.media` fsync。
- SDK：悬空契约走闭环保留；`validate_json` 保留（`validate_python(strict)` 已证伪）；兼容分支、双 Wire 定义、`ObservedChange`、碎片模块、`sdk`→`tui` 的 gates 迁移、`PluginServices` 拆分全部落地；`icons.py` 与快捷键归一化保留。
- 插件：tokenizer 整套删除；四巨石各自修剪（共享状态 helper 未动）；`cron`/`compaction`、`review` pager、`background` 就绪轮询（命名常量）保留；`grep`/`glob` 已共用唯一 ignore 表；`files/` 三引擎未收敛。
- Outer/TUI/Web：死参数、手拼 openapi、dict 往返、轮询流、30fps 空转、Win32 后端、`views` if-chain、`CoreApp.tsx` 上帝组件全部落地；逐操作裸线程改 `WorkGroup`、TUI 配置写入走 `config.update_model_section`、typed event pump 与 `catalog`/`operations`/`rendering` 拆分落地；`_handle_stream_event` 已拆到 `tui/stream_handlers.py`（306 行），`cli` root 选项已改标准 argparse。`snapshot`/`payload` 双字典（凭据边界）、composer 宽度引擎、`/tui` 双系统保留。

### 2026-11-20 TUI Help/命令发现复核
- 已落地：`CommandDescriptor`/`CommandDefinition` 增加可选 `usage`/`examples`/`group` 并贯通 payload 与插件 catalog；新增 `sdk/command_help.py` 统一 `client_syntax`→public path、schema→usage、group 排序；core `/help` 输出和 TUI `/tui help` 都改为该投影；新增 `tui/help_view.py`，`/tui` 控制、补全、help 共用一张表；`/help` receipt 由 TUI 从其 catalog `data` 渲染；`!` 前缀按 catalog `client_syntax` 路由。详见 [TUI TODO](../src/voidcode/tui/TODO.md)。
- 跨模块剩余（不重复下方维护性清点）：core `/help`、CLI `invoke help` 与 TUI help 现已共用 `sdk/command_help.py`；`HealthModel.context_window` 与 web `client_syntax` 派生仍分别归属 SDK/Web，详见本文件“维护性清点”与 [TUI TODO](../src/voidcode/tui/TODO.md)。

## 维护性清点（2026-11-21，当前树复核）

口径：行数为当前工作树 `wc -l` 实测；`ruff check .`、`ruff format --check .`、`ty check src` 全绿。旧审计的方法数、类区间与 `row[N]` 计数已不再沿用。

### 体量与职责

| 模块 | 行数 | 职责 |
|---|---|---|
| `core/engine.py` | 5,633 | `Core`：生命周期、run/resume、ask/approval、命令 invoke/confirm；activation/usage/projection/cancellation/background registry/background execution/context projection/fact delivery 已抽为协作者 |
| `core/execution.py` | 87 | 共享执行投影：`_Cursor`、`_Execution`、`committed_event`、`wire_object` |
| `core/context_projection.py` | 305 | `ContextProjector`：已提交前缀到 provider 请求、window 前缀、activation note、adaptive 收缩 |
| `core/fact_delivery.py` | 371 | `FactDelivery`：observer 结算候选、有序交付、receipt、gap 诊断与未知 dispatch 结算 |
| `core/background_execution.py` | 746 | `BackgroundExecution`：background driver invoke、tool wait 收支、lost-wait reconcile 与 deferred drain |
| `core/services.py` | 1,500 | `RuntimeServices`：state/event/continuation/child/artifact/process/background 服务 |
| `core/delegation.py` | 2,142 | `DelegationManager`：spawn/join/send/cancel、父子 facts、notice |
| `core/plugins.py` | 1,909 | `PluginRuntime`：导入/catalog/lease/dispatch/hook/view/resource/query；lease 由 `LeaseIdentity` + `LeaseStaging` 组成 |
| `core/storage/store.py` | 215 | facade：连接/生命周期/re-export |
| `core/storage/records.py` | 1,086 | typed row decoders、SELECT 行类型与生命周期 StrEnum 单点解码 |
| `core/storage/sessions.py` | 1,128 | sessions/events/checkpoints |
| `core/storage/state.py` | 1,108 | revision/activation/adoption/owner state/confirmation/budget/overlay/projection |
| `core/storage/continuations.py` | 1,393 | wakeup/continuation/background action/root budget/child-message claim |
| `core/storage/writes.py` | 131 | 跨域写 helper |
| `tui/terminal_app.py` | 2,212 | `CoreTuiApp` 状态所有者；事件泵/stream handler/catalog/operations/rendering 已拆到同级模块 |
| `tui/event_pump.py` | 540 | 输入/stream/queue 泵；stream frame 调用 `stream_handlers.handle_stream_event` |
| `tui/stream_handlers.py` | 294 | 按事件族拆分的 stream 处理（session/cursor 守卫、transcript、tool、provider、gates、run notice） |
| `tui/events.py` | 327 | typed frame 与事件投影 |
| `tui/catalog.py` | 72 | catalog 生命周期 |
| `tui/operations.py` | 149 | sessions/create/views/model reload/command input 请求与响应 |
| `tui/rendering.py` | 338 | transcript/frame/footer/border 渲染 |
| `tui/term.py` | 1,269 | 终端控制、宽度/裁剪、`render_lines`、`wrap_plain` |
| `tui/composer.py` | 1,210 | 编辑器状态机、布局、粘贴块、自有折行 |
| `tui/views.py` | 1,062 | 纯渲染器 + 交互视图；`render_tool_result(result: ToolResult, ...)` 直接消费 typed 结果 |
| `plugins/workflows/workflows_plugin.py` | 2,327 | 脚本加载/执行、节点账本、上下文、命令/视图 |
| `plugins/subagents/subagents_plugin.py` | 1,855 | child 投影、task/batch/wait、状态与监控 |
| `sdk/services.py` | 1,617 | wire DTO + 服务子 Protocol |
| `plugins/goal/goal_plugin.py` | 1,630 | goal 账本/提示词/命令/视图 |
| `plugins/todo/todo_plugin.py` | 1,378 | todo 状态、提醒策略、命令 |
| `capabilities/providers/native/openai_native.py` | 1,146 | HTTP/SSE wire、tool-call 累积、usage、错误映射 |
| `plugins/lsp/lsp_manager.py` | 1,103 | LSP 进程、帧协议、请求路由、生命周期锁 |
| `core/processes.py` | 1,031 | 进程注册表、捕获、supervised services/jobs |

### 已落地（当前证据）

计划与验收记录见 [Maintainability cleanup plan](plans/2026-11-20-maintainability-cleanup.md)。

- **C1–C5 + engine seams 1–2（2026-11-21）**：`PluginRuntime` 由唯一 `Core._build_runtime` 构造；`LeaseContext` 拆为不可变 `LeaseIdentity` + `LeaseStaging`；background driver invoke 与 tool wait 移入 `core/background_execution.py`（746 行），`engine.py` 6,697→6,058（C1–C5）→5,633（seam 1–2；−425 对 6,058）。`core/execution.py`（87 行）own `_Cursor`/`_Execution`/`committed_event`/`wire_object`；`core/context_projection.py`（305 行）own provider context 与 adaptive 收缩；`core/fact_delivery.py`（371 行）own observer 结算/交付。`Core` 仍保留 157 个方法定义。剩余：`_commit_tool_wait_events`（`engine.py:3301`）仍在 `Core`，因为 `cancel_background_tool_requests` 与 tool wait 共用同一条 commit/notify 路径。
- **公开 fact 路由与顺序（2026-11-21）**：`Core.commit_public_staged`/`commit_public_fact` 为 child spawn、command invoke/confirm、background invocation 等非 turn 提交统一执行 frozen-owner stamping 与 live delivery；同 session 重入 delivery 由 thread-local 深度判定，嵌套提交追加到活动队列，observer 可见顺序等于 durable sequence。回归：`test_sdk_facts` live/restart 顺序、`test_compaction_plugin` reopen 后交付、`test_collaboration` spawn commit 失败可见。
- **类型化生命周期状态（2026-11-21）**：`SessionStatus`/`CheckpointKind`/`ContinuationStatus`/`ConfirmationStatus` StrEnum 在 `storage/records.py` 经 `_decode_closed_enum` 单点解码，非法列值抛 `StoredValueError`；Core/outer 比较改用 enum，不再比对裸字符串。`StagedStateUpdate.schema_version` 改为必填，private snapshot 的 `version` 字段与 `logical_turn` 默认回退删除。
- **零兼容删除批次（2026-11-21）**：compaction state v2 要求 `schema_version=2` 且 `summary_kind="model"`（旧 state 显式拒绝，无迁移）；无 captured root 的 fact delivery 抛 `FactDeliveryContractError`，不再 rootless 交付；TUI 审批 payload 解码收敛为 `tui/gates.py:approval_gate_from_payload`；`BudgetExhaustion.from_payload` 为唯一 budget 解码；`background_live_actions`、重复 background cancel 调用、重复 `end_activation`、`wire_object` 副本、dead `getattr`/`Mapping` 回退全部删除。
- **T1–T2（2026-11-21）**：stream 事件族移入 `tui/stream_handlers.py`（294 行），`terminal_app.py` 2,437→2,212；`test_idle_render` 改用共享 `wait_for`。
- **M1–M5（2026-11-21）**：provider 行迁到 `capabilities/providers/registry.py`（122 行）+ `provider_table.json`（499 行），删除 `native/provider_table.py`；CLI root 选项改标准 argparse 两段式（`cli.py` 519 行）；artifact `.media` 临时文件先 `fsync` 再 hard-link；共享 loopback/Core registry 继续覆盖剩余测试套件。
- **Typed store rows**：`storage/records.py:633/651/686` 单点解码 session/info/event；`ChildOutcome` 与 spawn/ack/send 结果已强类型直传。残留 `row[N]` 仅 6 处（`continuations.py` 4、`records.py` 1、`state.py` 1），不再是全量逐字段解包。
- **`render_tool_result` typed**：`tui/views.py:263` 直接收 `ToolResult`；`terminal_app` 不再经 `encode_tool_result`→dict→`isinstance` 还原；TUI view dispatch 也是 typed union。
- **Config ingress**：`cli._tui_prefs` 走 `config.load_config(..., allow_missing=True)`；`bootstrap.config_from_snapshot` 只保留“5 个 section 必须存在”后交给单一 `parse_config`；TUI 配置写入唯一入口为 `config.update_model_section`，`grep "capabilities\." src/voidcode/tui` 为空。
- **TUI 线程/事件/配置**：`WorkGroup` 是唯一有界执行器；`event_pump`/`events`/`catalog`/`operations`/`rendering` 已从 `terminal_app` 拆出；30fps 空转、逐操作裸线程、TUI 直写 `voidcode.json` 均已删除。
- **Help**：`sdk/command_help.py` + `tui/help_view.py` 统一 core/CLI/TUI 的 usage、分组与 `/help` receipt。
- **Ignore/constants**：唯一 ignore 表在 `capabilities/files/_common.py`（glob/grep 共用）；`RASTER_IMAGE_MIMES` 在 `sdk/tools.py`；`PAGE_BYTES` 在 `sdk/services.py`；`MAX_PAGE_BYTES` 单定义于 `core/artifacts.py`（`processes` 引用）；provider wire 常量统一在 `native/_wire_common.py`。保留：`_MAX_EVENT_PAGES`/`_EVENT_PAGE_SIZE` 按插件隔离各自定义。
- **死符号**：4 个无实现的资源发现/枚举/搜索 `ArtifactServices` 方法；零读取 SDK 常量；TUI `set_cursor_shape`/`alt_screen`/`reset_live`/`set_enabled`/`ApprovalGate.from_fact` 均已删除。
- **测试脚手架**：`tests/fixtures/core_factory.py`（`loopback_server`/`open_core_for_test`/`write_loopback_config`/`register_core`/`discard_core`/`close_cores`）与 `tests/fixtures/polling.py:wait_for` 已落地；12 个 integration 文件迁移 loopback、25 个文件迁移 core registry、7 个文件迁移 `_wait_for`。
- **运行期 ownership 字面量**：事件 source/actor、artifact owner、overlay/continuation owner、`kernel_lost`→`core_lost` 已完成（见下节）；无兼容归一化。

### 重复实现核查（此前待验证项，逐条结论）

| 命题 | 结论 | 证据 |
|---|---|---|
| 宽度/ANSI 在 `term.py`、`views.py`、`terminal_app.py` 重复 | **反驳** | `term.py:170-288` 是唯一实现；`views.py:40`、`terminal_app.py:81`、`composer.py:27`、`artifact_viewer.py:26` 全部 import。残留仅 `composer.py:203-297` 的编辑器折行（保留每行逻辑列起点，复用 `cell_len`/`grapheme_spans`），不是第二套宽度引擎 |
| web/TS 重复 ANSI/宽度 | **反驳** | `frontend/src` 无 strip/width 工具；HTML 渲染不需要 ANSI。跨端重复在 view 契约（`frontend/src/core/views/*.tsx` vs `tui/views.py:488-1066`），已由 TUI TODO 跟踪 |
| config 解析/校验在 `config.py`/`cli.py`/`bootstrap.py` 重复 | **确认（原有 4 处）→ 2 处已收敛** | 权威仍为 `config.py:201`/`:251`。**已落地 2026-11-20**：`cli._tui_prefs` 改用 `load_config(..., allow_missing=True)`，删除自带 `json.loads`+`parse_config` 与 `except Exception: file_tui = {}`；格式错误改为带路径的显式 `ValueError`（经 `main` 输出 `status=failed`）。`bootstrap.config_from_snapshot` 删除逐 section `isinstance`+`json_wire_object` 复制，保留“5 个 section 必须存在”的显式检查后交给单一 `parse_config`；仍只解析 digest-verified 私有快照，不读 live 文件/环境，凭据边界不变。验证：`test_cli_arguments.py` 10、`test_provider_passthrough.py`+`test_config.py` 25 串行通过。已全部落地；TUI 配置写入走 `config.update_model_section`。 |
| artifact 读取在 viewer 与 capabilities 重复 | **确认** | `core/coverage.py:71-85`（`read_artifact_content`）与 `tui/artifact_viewer.py:110-138`（`read_artifact_bytes`）同一分页/偏移/摘要校验算法，各约 28 行；TUI 版额外做逐页 reference 相等与取消检查，core 版做 EOF 一致性检查 |
| markdown 渲染 TUI/web 重复 | **反驳（Python 侧）** | 仅 `tui/views.py:348` 一处 `Markdown` + `tui/styles.py:178` 主题；`core/history_renderer.py` 是给模型/`show` 的纯文本投影，不是第二渲染器 |
| JSON guard 跨 `sdk/json_values.py` 使用方重复 | **反驳（helper）/确认（样板）** | 快照只有一套，且只剩 3 处 `json_wire_object(own_json_object(...))`（`cli.py:39`、`engine.py:3514`、`mcp_manager.py:472`）；但“must be an object”类手写形状检查仍是样板，不构成第二套 helper |

新增重复（当时发现；除分页策略外均已落地，历史行号不再保证当前）：

- `_response_payload` 两个 provider 逐字节相同：`anthropic_native.py:475-484` vs `openai_native.py:712-721`；`_response_metadata` 共享 6 行：`anthropic_native.py:218-229` vs `openai_native.py:1156-1164`。应移入已存在的 `native/_wire_common.py`。**（已落地 2026-11-20：`decode_response_payload` + `response_metadata(..., thinking_blocks=)`；provider/compaction 套件 227 passed。）**
- **增量 UTF-8 分页解码（有意不同，不合并）**：`capabilities/resource_pages.py` 用 `replace` 做流式展示/进度，永不因半个字符失败；`capabilities/exec/process_reader.py` 用 `strict`+hex 游标，字节寻址日志的非法字节以 hex 呈现；`core/coverage.py` 用严格前缀，模型投影预算内绝不注入非法字节；`tui/artifact_viewer.py` 用 strict+state 缓存的视窗解码器。四种错误策略对应四种消费语义，强行抽共享 helper 会改变其中至少一端的可见行为，因此不视为重复缺陷。
- 同包重复：`_INTEGER_PATTERN`/`_NUMBER_PATTERN` 在 `sdk/views.py:454-455` 与 `sdk/ask.py:29-30`；`PAGE_BYTES` 在 `core/history_renderer.py:23` 与 `sdk/services.py:1281`（注释自认相等，因禁止 sdk→core 而内联）。**（已落地 2026-11-20：页预算单一定义为 `sdk/services.py:PAGE_BYTES`，core renderer 引用；服务默认参数同源。）**
- 常量分叉：`DEFAULT_IGNORE_PATTERNS` 在 `capabilities/files/glob_tool.py:27`（17 项）与 `grep_tool.py:36`（5 项）——同名不同义，`grep` 会搜 `target/.venv/vendor`；`RASTER_IMAGE_MIMES` 3 份（`core/coverage.py:38`、`capabilities/resource_pages.py:22`、`plugins/mcp/mcp_tool.py:40`）；`WEB_URI_PREFIX` 2 份（`plugins/web/web_fetch.py:42`、`web_reader.py:16`）；`_MAX_EVENT_PAGES`/`_EVENT_PAGE_SIZE` 4 份（`todo:43-44`、`cron:83-84`、`skills:628-629`、`compaction:59-60`）；`MAX_PAGE_BYTES` 2 份（`core/artifacts.py:28`、`core/processes.py:37`，注释写“matching”）；provider wire 常量 `_DEFAULT_TIMEOUT_SECONDS`/`_OUTPUT_CAP_WHEN_UNKNOWN`/`_PLACEHOLDER_API_KEY` 在 openai/anthropic 各一份。**（2026-11-20 已落地：ignore 表唯一为 `capabilities/files/_common.py:DEFAULT_IGNORE_PATTERNS` + `is_default_ignored`，glob/grep 共用；`RASTER_IMAGE_MIMES` 移入 `sdk/tools.py`；`web_reader` 删除未读副本；`MAX_PAGE_BYTES` 由 `processes` 引用 `artifacts`；provider 三常量移入 `native/_wire_common.py`。保留：`_MAX_EVENT_PAGES`/`_EVENT_PAGE_SIZE` 按插件隔离决定各自定义。）**

### 防御式检查（已收敛）

- TUI `_mount_interactive_view`/`_dispatch_catalog_command` 的恒真 `assert` 与 `Mapping` 降级路径已删；`render_tool_result` 直接收 typed `ToolResult`。
- `bootstrap.config_from_snapshot` 去掉逐 section `isinstance`+`json_wire_object` 复制；store 读路径单点解码，残留 `row[N]` 仅 6 处。
- 其余 `isinstance`/`cast` 仅保留在真实 ingress 或值形状检查；不再有对 `ChildOutcome`/spawn/ack/send 的字段级重验。
- `except Exception` 静默点仅余 UI/插件边界的显式降级。

### 死代码与空契约（已清理）

- SDK 零读取常量、`CHUNK_BYTES`、`ArtifactStore.discard_all`、`CORE_TOOL_NAMES`、`COMPACTED_FACT_TYPE` 已删除。
- 4 个无实现、0 调用的资源发现/枚举/搜索 `ArtifactServices` 方法及其 `FakeServices` 桩/断言已删除；`resources.py` DTO 与 `ResourceReader` slots 保留为未实现设计。
- TUI `set_cursor_shape`/`alt_screen`/`reset_live`/`set_enabled`/`ApprovalGate.from_fact` 已删除。

### 测试脚手架（已收敛）

- `tests/fixtures/core_factory.py`（`loopback_server`/`open_core_for_test`/`write_loopback_config`/`register_core`/`discard_core`/`close_cores`）与 `tests/fixtures/polling.py:wait_for` 是唯一共享入口；conftest autouse teardown 关闭所有注册 Core。
- 12 个 integration 文件迁移 `loopback_server`（最近一批 42 passed），25 个文件迁移 core registry，7 个文件迁移 `_wait_for`（`test_tui_monitor` 现可正常 collect）。
- 剩余见下方“剩余（open）”中的测试 fixture 项。

### 运行期 ownership 字面量去 kernel（2026-11-20，破坏性）
- 事件 `source`/`actor`、artifact `owner`、session overlay owner、continuation owner（含 SQL 谓词）统一为 `"core"`；StopCause/status `"kernel_lost"` 改为 `"core_lost"`。测试与当前 docs/contracts 同步。
- 不做读时归一化/兼容：旧库中已写入的 `"kernel"`/`kernel_lost` 行不会被识别；按零向后兼容策略，跨此改动应从新 state 目录开始。
- 保留：`docs/CHANGELOG.md` 历史条目，以及 `core/TODO.md`/`docs/TODO.md` 中描述已删除 `kernel_identity`/旧 `owner='kernel'` 行的历史审计原样。

### 剩余（open）

1. `_commit_tool_wait_events` 仍在 `Core`（`core/engine.py:3301`）：`cancel_background_tool_requests` 与 background tool wait 共用同一条 commit/notify 路径；抽出它会复制取消结算的原子提交顺序，故保留并记录为已知共享点。其余 background driver invoke/tool-wait 收支已在 `core/background_execution.py`（746 行）。
2. 测试 fixture 例外：9 个本地 loopback context manager 仍在（C 自有 `test_background_actions.py`、`test_background_tool_wait.py`、`test_process_lease.py`；integration `test_child_structured_e2e.py`、`test_core_activation.py`、`test_core_vertical.py`、`test_goal_plugin.py`、`test_subagents_monitor.py`、`test_workflows_restart.py`）。共享 `loopback_server` 已接受 `replies=None`；这些文件按 workstream 边界保留本地副本，行为与共享实现一致。`test_background_actions.py` 也保留本地 `_CREATED` registry，因为测试中途 close/reopen 需要显式 `_CREATED.remove`。
