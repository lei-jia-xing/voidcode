# TODO

吸收 OMP 的工具与 Agent 产品设计，不以插件数量或“能调用一次”作为完成标准。SDK 前置缺口见[SDK TODO](../sdk/TODO.md)，终端表现见[TUI TODO](../tui/TODO.md)，优先级与证据口径见[总待办](../../../docs/TODO.md)。

## P0：从 demo 到完整任务

- [ ] **为官方插件建立真实任务的生产化门槛，先覆盖 files、exec、mcp、lsp、subagents、workflows。**
  - 现状：已有[插件编写与诊断契约](../../../docs/contracts/plugin-authoring.md)以及[命令交互验收](../../../docs/command-ux.md)；缺的不是另一套 factory smoke，而是跨工具完成真实任务的产品验收。下面各项列出的断点作为首批整改对象，不笼统宣称所有插件都没有可用实现。
  - 目标：按插件记录“已完整可用 / 明确受限 / 待补”的能力与可运行场景；覆盖能力发现、结构化输入/结果、长输出/媒体、进度、取消、审批、禁用/重载、断线/重启、错误恢复。复用现有 SDK 和验收清单，不维护第二份命令表。
  - 验收：首批插件各有一个普通用户可完成的真实场景及关键失败场景，检查实际产物、持久化事实与 UI 状态；例如读真实截图、启动并停止服务、MCP 混合结果、LSP 重构落盘、Agent 跟进、workflow 中途暂停恢复。空结果、只印 ID、只检查“不抛异常”均不能算通过；缺依赖和不能恢复的边界须在入口说明。
  - 已完成的 review 长输出整改（`95f7c298`）：review 数据改从 `outcome.as_payload()` 派生，保留精确 run、`output_sha256`/`output_bytes`/`output_preview`、history 引用与 capture cause；仅在真实 sha 下加 `output_uri`。长 review 经共享 artifact pager 返回首个有界页，URI + run 印在页前，完整字节走普通 read/byte cursor；超大 inline 经 scoped spill 取不可变 continuation，fitting inline 原样直出。无全文的 preview-only 显式失败但 data 保持 `completed`，不编造 cause/URI；缺/空/preview/capture-error 四态分明，capture 失败不改 run status。回归：`tests/unit/plugins/test_review_plugin.py` 49 passed；`tests/integration/test_review_plugin.py` 10 passed，含 real-Core 长 review 全字节重组与 follow-up 后旧 URI 不变。

- [ ] **让正常截图/图片读取真正可用，贯通工具结果和客户端显示。**
  - 现状：[`files/read`](../capabilities/files/read_tool.py) streams reference-backed raster originals without the former 50 KiB refusal. Core/native provider tests cover valid decoded PNGs above the 256 KiB page size; Core, local/authenticated RPC byte reads, real Chromium image paging, and Kitty CLI protocol/reflow/reopen checks have independently passed. The separate live-progress PTY proof now also passes; this is not an all-plugin audit.
  - 目标：keep stored originals, model-readable content, and client display as separate consumers; page the original by digest without arbitrary business cutoff, and retain explicit behavior for unsupported providers, corrupt formats, missing pages, and clients without image support. Multi-part output and adaptive context allocation remain independent SDK gaps.
  - 验收：Core/native, browser, and headless Kitty PNG/error/fallback scenarios have observed proof. Physical Kitty pixels and non-PNG terminal formats are not claimed; the checked-in viewer/Kitty UI remains a separate client-stage commit. No generic image-size or rendered-pixel guarantee is claimed.

- [ ] **删除没有边界依据的 PREVIEW/MAX，以及同一数据链路的重复截断。**
  - 整改前样本：`web_fetch` showed an 8000-character duplicate preview after saving the full snapshot; `shell_exec` selected first/last 25 KiB per stream and Core then imposed another result view; `monitor` truncated labels/activity at several layers; AST match body cut paths at 256 characters. The shell producer now stores every captured text stream behind a typed reference, web fetch keeps one immutable rendered-content reference, and Core preserves selected `TextOutput.text` without duplicating its machine body. The 67-case projection/producer command passed before the narrow JSON MIME addition; the changed JSON web-fetch/Core/provider consumer proof then passed 2 tests in 5.56s. Monitor/AST/goal have separate verified changes; this sampling is not an all-plugin/MAX audit.
  - Goal 切换：旧 `spill_objective_if_needed` 经命令直接写工作区 `.voidcode/attachments/...`，而 `SESSION` 模型工具拒绝超过 4000 字符目标；若只删除拒绝并复用旧 helper，会引入未声明的工作区写。当前所有新写入只接受显式 scoped services，较长目标存入不可变 `artifact://`；仅保留历史附件的 containment/hash 读取。4000 已命名为每轮重复上下文的 inline 字符 allowance，超过后转引用、不拒绝；原始空白和 Unicode 保留。平台入口/传输约束仍适用，不宣称无限输入。
  - 当前资源读取边界：`Core.read_artifact`, `SessionClient.read_artifact`, and authenticated `artifact.read` provide exact workspace-scoped byte pages. The API conformance group reported 6 passed; real Chromium artifact/image rendering 1 passed/14.1s and Kitty CLI/renderers 6 passed/17.70s. The gated-shell live-progress PTY also showed pre-release stdout/stderr, preserved a running draft, one durable completion, and exact full output via artifact pages. Web `ToolResultCard` consumes the same API/reference. These samples are not an audit of every plugin or every `MAX_*`.
  - AST 切片移除了路径 256 字符截断；声明工具的结果/完整 native JSONL 回归覆盖可继续使用的长路径。Monitor 按现有 SDK `MAX_VIEW_DATA_BYTES` 的实际 JSON 转义字节计费分页，不新增 label MAX；只有单行本身超过 payload 预算时才折叠，并显式进入保留完整 label/activity/父节点的 detail 游标页。刷新和取消仍携带完整 session/run 身份。孤立 prepare/invoke 消费者测试与真实 Core/客户端验收分开记录，不能把前者当成后者已通过。
  - 新的边界证据：`sdk/views.py:229-234` 先按 JSON 字节检查、再补 dataclass 默认字段；只计算原始 UTF-8 或忽略 `style`/`prepare_input` 默认值会低估最终页大小。Monitor 的预算计算包含这些默认字段。此 64 KiB 是当前 SDK 消费契约，不是外部协议硬限制的证明。
  - Monitor 实时一致性已收敛：detail 字符游标携带内容版本戳（`_detail_version`，渲染文本 SHA-256 前 16 hex）；同窗口翻页带版本，history 窗口推进因寻址新文本而不带版本、到达即重打戳；版本不符返回显式 stale notice（Back to roster / Re-open detail）而非错位内容。`detail_version` 进 schema（versionless 入口行为不变）。新 `test_monitor_consistency.py` 6 passed；集成 `test_subagents_monitor.py` 原 6 个预先存在失败已迁移转绿，模块 13/13：Group A 三个 census/parked/disarm 节点把 fence 门内的 racing `core.run` 改走内部 `core._run`（带 captured birth root，`send_child` 在 fence 内会被拒）；两个 fence 节点（spawn/send 参数）改走 `send_child`+`join_child` 并按 join 语义断言（`cancelled`/`owner_cancel`）；Group B 把 BIG marker 缩到 38,004 bytes（encoded 44,211），落在 40 KiB history page 与 50 KiB commit ceiling 之间、仍跨 2+ history 页且逐字提交。
  - 目标：逐项标注上限的消费者、单位、真实依据及达到后行为；无独立必要性则删除。展示按客户端视窗折叠，结果按调用者分页/按需读取，模型按上下文预算投影，保留完整原件与资源身份；可由现有 page/window 推导的数值不再单设常量。保留必要的解码/内存/协议保护，但不把每个常量改成配置，也不新造 LimitManager。
  - 验收：长标签、长路径、长 goal、长中英文输出及大量匹配仍可查看/复制/结构化读取完整内容；UI 的折叠不拒绝合法任务、不截短实际资源标识；没有连续多层“预览的预览”。原件已经可靠保存的 web/artifact 路径继续可用；真资源上限触发时明确报告，不宣称完整捕获。模型投影整改见 [Core TODO](../core/TODO.md)。

## P1：工具、URI、Agent 形成一个工作面

- [ ] **提供面向模型的持久 Python `eval`，而不只是 shell 的另一层包装。**
  - 现状：[`workflows`](./workflows/README.md) 只执行显式配置的可信 `.py`，明确不接受 inline/model-supplied 代码；[`exec`](exec/README.md) 提供命令和后台进程，并非保留变量、等待工具结果的脚本运行时。不要改坏 workflow 的持久化与指纹复用语义来冒充 `eval`。
  - 目标：普通 Python 编写条件、循环、并发和数据处理，通过 `await tool.*` 调用当前 session 可见的工具并消费 typed body/资源引用；跨 cell 保留状态，提供重置、取消、流式输出和 artifact 溢出。采用项目已有 Python 栈，不为名称对齐先造双语言运行时。参考 OMP [eval](omp://tools/eval.md)。
  - 依赖：SDK 可等待工具调用；富表格/多图依赖组合输出。脚本中 MCP 的结构化结果须通过原始结构化数据或完整结果引用取得，不能解析被截断的 pretty text，不能把错误结果当成功 JSON。
  - 验收：两个 cell 复用变量，批量读取后按结果发起不同工具调用，审批可暂停/继续，输出表格和图片可查看完整数据；父子 Agent 内核隔离，取消/崩溃/重启明确宣布状态丢失，不自动重跑已产生副作用的 cell；`tool.*` 不可访问隐藏工具或越过审批，任意 Python 自身仍按执行能力治理。

- [ ] **让 URI 成为跨工具的资源身份，而不只是一条 `read` 特例。**
  - 现状：[`files`](files/README.md) 已注册 `tool://`、`artifact://`、`transcript://`，web 另有快照资源，`subagents` 现有 `agent://<child_session_id>`；`read` 已按注册者分发。当前 [`grep`](../capabilities/files/grep_tool.py)/[`glob`](../capabilities/files/glob_tool.py) 基于 `Path.walk`，没有同一资源的搜索/枚举路径；`ResourceReader` 本身也没有能力发现契约。
  - 目标：基于 SDK 资源能力统一 `read`、资源检索、脚本 helper 与客户端查看器；结果返回可继续使用的 URI 和真实分页游标。按需发现工具文档沿用已有 `tool://` 与当前可见 catalog，不复制 OMP 的整张 scheme/device 表，也不默认把所有工具永久塞进上下文。参考 OMP [read/router](omp://tools/read.md)、[find 的 URL scope](omp://tools/find.md)、[shell URI filesystem](omp://bash-tool-runtime.md)。
  - 边界：虚拟文本、不可变 artifact、本地可变文件不是一回事；外部命令确有需要时才用受限只读物化，不能直接交出 artifact 的可写 backing path。不给 mutation 加“读 URI”捷径，不用 shell 文本替换伪装 VFS。
  - 验收：同一份超长命令输出、网页快照或子 Agent 结果可以按 URI 阅读、检索和在脚本中处理，分页无丢字，UI 展开指向同一资源；禁用提供插件后明确不可用，不回退其他实现；不可枚举/不可搜索的资源说明能力限制，不静默给空结果。

- [ ] **把现有子 Agent 生命周期做成持续交互、可寻址结果的闭环。**
  - 现状：已有 `task`/`task_batch`、SDK `send_child`/`join_child`、按 run 的完成通知、lineage-gated history 和 alt+a monitor；[`workflows`](./workflows/README.md) 已支持动态节点/依赖与持久化恢复。不是再造一套 spawn/send/wait，也不能把静态 DAG 当作当前能力上限。
  - 已完成的生命周期底座：`wait_children`/无参 `wait` 提供 owned child first-settled result delivery；`task_batch.output` 返回准确 ready-run 输出/ref 与独立 pending 状态。`agent://` 仍是 latest eligible published output，不等价于某个当前或已等待 run；`task`/`task_batch`/`wait` 透传 typed `output_error`/`history_error`（text+body），`agent://` 仅展示 publication run 自身 cause。Core 没有独立 accepted-yield 标记，typed structured publication 与共享 context 仍未完成。
  - 边界：保留 parent/descendant 权限，不能照搬全局广播与任意 peer 读取；消息是带来源的数据而非 system 指令。batch 与 workflow 复用同一子会话机制、配额和只读限制，不失败后放宽 scope，不以进程存活或已有输出冒充本轮完成。
  - 验收：首个任务很慢、后续任务先成功/失败时，父 Agent 可立即取得该 exact run 的完整结果继续工作；长 artifact 按不可变 URI 读取，跟进新 run 不会换掉旧输出分页；取消、approval、failed、重连/重启状态不谎报成功或重复交付。first-ready/output/receipt 相关 Core 与插件窄 proof 已通过；accepted-yield、完整共享 context 和 generic peer/process waits 仍开放。
  - 前置整改：Agent 的默认 32 及预算归属单列为 [Core P0](../core/TODO.md) 与 [SDK P0](../sdk/TODO.md)，不把任意轮次截断当作生命周期设计，也不假定父子共享额度已正确实现。

- [ ] **统一有限命令、后台任务与长期服务的使用和收尾体验。**
  - 现状：[`exec`](exec/README.md) 已有受监督的进程组、取消、stdout/stderr artifact，`background_process` 也已有 ready pattern、logs、send、stop；不能把这些成熟底座重写成第二个进程管理器。
  - 目标：让耗时有限命令可显式或按策略交还前台，完成后可靠通知；服务的 started/ready/timeout/exited 明确分开，日志、stdin、stop 使用稳定句柄/资源入口；复用 `emit_tool_progress` 与 Core process ownership，按真实需要补 readiness/PTY，绝不把“启动成功”当“服务已就绪”。参考 OMP [bash runtime 的 async、managed service、proc、输出捕获](omp://bash-tool-runtime.md)。
  - 验收：主 Agent 启动服务后能继续做事，看到实时输出/明确 readiness，停止时清理所属进程组；有限任务超时/取消保留部分输出与终止是否确认，完成无需轮询且不重复通知；artifact 捕获失败不能宣称有完整日志。宿主重启后旧进程事实按现有真实语义展示，不伪装恢复了控制权。

- [ ] **增加模型可调用的结构化问答，而不滥用确认门。**
  - 现状：已有命令表单、choices、confirmation；它们不是通用的模型 `ask`。先补 [SDK 用户问答](../sdk/TODO.md)，不在插件里嵌入私有 TUI 回调。
  - 目标：一次呈现必要上下文和一组相关问题，支持推荐值、单/多选、自由输入、校验及可选资源图片，两端消费同一声明；仅在用户必须决定实质取舍时提问。参考 OMP [ask](omp://tools/ask.md)。
  - 验收：用户能在同一表单比较方案并提交有类型的答案，取消/断线/无人值守不产生伪造答案；模型读到的是答案而非一段供它猜测的聊天回显；问答不授予执行权限，也不替代工具审批。

- [x] **首个就绪等待与批次实际结果交付。**
  - Subagents 插件的无参 `wait` 只对该父 session 已记录 children 做新鲜结果交付；Core 用现有 condition/cursors 等待 undelivered durable `(child, run)` facts，返回精确 run 身份。旧 A 可在 B 当前运行时交付，B 的 pending live 状态独立列出。`notify_on_completion=False` 不排除事实；approval parked 不会被当成完成。
  - `task_batch.output` 先非阻塞读取各成员当前 run；无当前可报告结果时才首就绪等待。结果携带实际完整 output/ref 和 URI，pending 状态独立；大/内联分页继续走不可变 URI 与 UTF-8-safe byte cursor。显式 output reread 仍可读当前 run；`agent://` latest publication 不代替具体 run 输出。
  - Delivery receipt 与成功的 tool result 原子提交；只读 snapshot 不消费。有效 callback 返回的 `ToolFailure`（`status="error"`）仍提交显式暂存的 delivery；callback exception、invalid result、abort/revocation/shutdown 会回滚。Core proof 的三个 gated child、52,000 UTF-8 bytes、普通 URI read/reread、去重及 reopen 已通过；窄 lifecycle regression 覆盖 approval resume、cancel、steering、shutdown、foreign parent 与 failed outcome。
  - 未包含：accepted-yield、通用 peer/process waits；另外现有单 run `join_child` 的 0.2s SQL/Condition polling 未由此功能修复。
- [x] **task_batch 顶层共享 context。**
  - 声明：spawn 调用可选顶层 `context: string`，整组声明一次；严格解析（未知顶层字段拒绝），`operation=output` 携带 context 直接拒绝（spawn-only）。合并：固定模板 `Shared context:\n{context}\n\nTask:\n{prompt}` 在 profile 前缀之前拼入每个 item 的 spawn prompt；None/空白保持 item prompt 字节全等（identity no-op）。来源：仅当次 batch 请求，绝不持久化到 `DelegationState`/`ChildRecord`，跨调用不继承，output 读取不加宽。
  - 指纹：batch 本无 keyed retry（每次 fresh uuid group，spawn 不带 `idempotency_key`，`ChildRecord` 无指纹字段）；context 并入 prompt 字符串即自动进入 Core `_spawn_spec_hash`（prompt 为哈希字段之一，`core/delegation.py:387`），换 context 永不复用另一 context 的 settled 结果——与 `test_same_key_different_schema_is_refused` 同 bug 类。纯插件改动，无 Core/SDK/permission/lineage/scope 变化。
  - 验收：共同背景声明一次，每 child 结果保留精确 run/output 身份，batch output/ref/pending 语义与 permission/lineage 边界不变。7 新单元测试（合并、模板原文、空 no-op×3、校验、指纹分离、profile 顺序、不继承）+ real-Core e2e（child 实际收到的 user prompt 即合并模板，wait 保留精确 run/output 身份）；`test_subagents_plugin.py` 单元 80 passed，集成 subagents 模块 8 passed，全单元 2025 passed + 1 skipped，lint + typecheck green。

## P2：高频读写检索能力

- [ ] **扩展统一读取入口，覆盖常见工作材料而不是一律退回 shell。**
  - 现状：[`read` 的架构边界](../../../docs/architecture.md) 明确只读普通文件/资源，不列目录、不解析 notebook、PDF、SQLite 或 archive；保持一个可发现的入口有价值，不等于把格式实现塞进 Core。
  - 目标：由 capability/plugin 适配器提供目录、JSON/JSONL 查询、notebook 单元、PDF 文本/指定页图、SQLite 只读页和 archive 成员读取，复用资源身份、分页和媒体输出。参考 OMP [read 各格式与 selectors](omp://tools/read.md)，保留 VoidCode 现有显式游标习惯，不照搬全部路径后缀语法。
  - 验收：每种声明支持的格式有真实材料场景与明确限额；大文件/长行/UTF-8/扫描 PDF 可继续读到所需部分；archive 防路径逃逸及解压资源耗尽，数据库不执行变更；缺 converter、解码失败和不支持格式不可伪装为空内容。

- [x] **增加行为定位式 `find`，与 literal/AST/LSP 检索分工。**
  - 链路：有界 lexical 预筛（每关键词一次共享 `grep`，总匹配复用 grep 自身 `MAX_MATCHES`）→ 一次结构化 judge（`model_query_structured` + 严格 `verdicts` schema，只判预筛候选）→ 共享 `read` 校验行号并取证据。新包 `find/`（`find_tool.py` + README）+ `tests/unit/plugins/test_find_plugin.py` 17 passed（含 real-Core loopback judge round-trip）。
  - 类型化状态：`ok`/`no_hit`/`partial` 走 ToolSuccess（`ok` 永不空；`partial` 覆盖任何截断且保留已验证命中），`timeout`/`judge_unavailable`/`judge_error` 走带同体 `FindResultBody` + diagnostics 的 ToolFailure；幻觉行号进 `coverage.rejected`（7 种原因）永不静默修正；grep 层拒绝（超长行）以其 typed diagnostic 向上传播，不伪装 `no_hit`。
  - 组合 fence：直接调用共享 grep/read（无独立 pipeline ledger）仅因二者皆为 READ-effect 且中间页不进 session model（judge 侧转 prompt + 本地校验；证据只存 body 不进 output.text，零 ReadObservation 正确）——fence 注释 + effects tripwire 测试锁定；judge prompt 的 snippet 截断（12 matches/file、500 chars/line）经 `coverage.snippet_cuts` 逐文件可见，不再静默。
  - 未包含：向量数据库/后台索引；符号导航仍归 LSP。

- [ ] **把 LSP 语义重构、安全编辑和诊断连成一次可靠操作。**
  - 现状：[`lsp`](lsp/README.md) 已能返回 rename/codeAction/willRenameFiles 计算出的 edits，[`files`](files/README.md) 已有 `apply_workspace_edit` 与 `expectedHash`/document-version 校验；不是缺一个能做字符串替换的“rename”。
  - 目标：以符号定位减少模型猜 character offset，支持可检查预览及受治理应用，沿用已有文档状态 query 与 workspace edit；明确跨文件部分失败/竞态，不把工具预览当已落盘。参考 OMP [lsp 的符号定位、rename_file/code_actions 与 freshness](omp://tools/lsp.md)、[edit 的 snapshot 与错误类别](omp://tools/edit.md)。
  - 验收：重命名带跨文件引用的符号或文件，预览可取消，应用后引用与诊断可核对；stale hash/version 拒绝而不覆盖用户修改，失败不谎报全成功；只读语义继续受实际进程权限约束，不为模仿 OMP 偷改效果标签。

- [x] **支持可选 schema 的子 Agent 结构化交付。**
  - `task`（含 batch）与 workflow 同读 SDK 契约：spawn 声明 `result_schema`，读取侧严格校验后返回 `data` 及其 run 身份；schema 不符/未交付/预算耗尽/正常完成严格区分（`data`/`parse_error`/`unavailable` 三态），完整数据按引用读取。无 `yield` builtin、无固定提醒/重试 MAX。
  - task↔workflow parity：声明/未声明、合法/违例/不可解析、preview-only、预算/取消/失败、复用与指纹拒绝两侧一致；schema 进入 workflow 有效规格指纹，`test_same_key_different_schema_is_refused` 锁定换契约不复用旧结果。
  - 验收：5 新文件 43 passed + e2e 子 spawn→交付全链路；`test_subagents_plugin.py` 4 处断言迁移到首行结构化 JSON。

## 清算：插件巨石、遗产移植与 Capabilities 膨胀（P0，零向后兼容）

- [x] **删除 `capabilities/providers/native/tokenizer.py`、`claude_tokenizer/` 与 `tokenizer_data/*.bz2` 整套离线分词器工程。**
  - 证据：`tokenizer.py:1-240`、`claude_tokenizer/{engine,normalize,constants}.py`（~700 行）及 `tokenizer_data/` 下的 bzip2 词表从 OMP 移植了 6 套 tiktoken 正则、DeepSeek 13 分支 CJK 边界正则和 `ctok` 动态规划最小代价切分引擎；但 Core 的自适应上下文分配（`coverage.py`）只用 provider 实测 token/char 比率，离线分词器仅被 `pricing.py` 等边缘路径拿来做估算，`known_tokenizers()` 在 Core 中根本无人调用。
  - 整改：彻底删除 `tokenizer.py`、`claude_tokenizer/`、`tokenizer_data/`；所有计费与上下文窗口只认 provider 返回的真实 usage facts。
  - 已落地：`tokenizer.py`、`claude_tokenizer/`、`tokenizer_data/` 全部删除；`native/` 下无残留。

- [ ] **削平四大巨石插件（`workflows` 2352 行、`subagents` 1789 行、`goal` 1729 行、`todo` 1378 行）的重复造轮子与历史包袱。**
  - 证据：
    1. **`workflows/workflows_plugin.py`（2352 行）**：在 async 驱动器里自己建 `threading.Lock` 与 `threading.Semaphore(8)`（`:57-67`）去串行化 Core 本就用 CAS 串行化的状态提交；`WorkflowNode`（`:72-124,175-360`）持久化 7 个 sha256 字段（`source_sha256`、`source_artifact`、`input_sha256`、`effective_input_sha256`、`prompt_sha256`、`output_sha256`、`result_sha256`）并在反序列化时写 150 行 `len(sha) != 64` 校验；`_run_node`（`:753`）与 `_run_node_structured`（`:822`）把 80 行的 `1.0s` 轮询 `join_child` 状态机复制粘贴了两遍；在 `:289,412,503,527,966,1291,1368` 反复执行 `json_wire_object(own_json_object(...))` 三重往返。
    2. **`goal/goal_plugin.py`（1729 行）**：把一个目标插件写成了 23 字段的财务分类账（`KNOWN_GOAL_STATE_FIELDS:60-89`，含 8 个 token 计数器、`ActiveWindow` 列表、`blocked_turns`、`objective_edited` 等），`to_dict`/`from_dict` 达 200 行；`read_full_objective`（`:378-427`）至今保留读取工作区 `.voidcode/attachments/...` 旧文件的向后兼容分支；`render_context_prompt`（`:429-700`）拼接 5 套冗长提示词模板与 `<objective>` 伪 XML 标签，并在 observer 中散落 6 处 `services.set_budget(...)`。
    3. **`todo/todo_plugin.py`（1378 行）**：将派生统计 `summary` 持久化到状态中并在每次 `TodoState.from_payload`（`:158`）时重算比对；维护两套提醒状态机（`TodoReminderState` 与 `TodoMidRunState`，`:649-850`，含 `12` 次变更阈值、`2/cycle`、`3/cycle` 魔法常数和 `<system-reminder>` 字符串注入）；`todo_observer`（`:819-900`）在每次工具调用 fact 到达时都跑 `for _ in range(3): read_facts(limit=200)` 重建 `ToolEffect` 集合并无条件 `replace_state`。
    4. **`subagents/subagents_plugin.py`（1789 行）+ `monitor.py`（436 行）**：手写 10 个 `_parse_*` 函数（`:207-310`）替代 schema 校验；保留 `_CHILD_OPTIONAL_KEYS` 兼容“rows written before preset selection”；`monitor.py:114-252` 把按钮参数 `json.dumps` 塞进 `_choice_id` 字符串，并对超长行用 `json.dumps` 二分查找截断标签长度。
  - 整改：
    - 抽提共享的强类型插件状态读写 helper，删除 6 个插件（`workflows`/`goal`/`todo`/`subagents`/`cron`/`compaction`）各自手抄的 `_STATE_KEYS` + `from_payload`/`to_payload` + `replace_state(expected_version)` 样板；
    - `workflows` 删掉自建线程锁、合并 `_run_node` 与 `_run_node_structured`、砍掉冗余 sha256 账本（只留 `run_id`/`nodes`/`status`/artifact 引用）；
    - `goal` 彻底删除 `.voidcode/attachments` 旧工作区文件读取分支与 18 个冗余统计字段（直接读 Core `UsageFact`/`SessionUsageReport`），合并 5 套提示词模板；
    - `todo` 删除持久化 `summary` 和双提醒状态机，改为基于内存游标的单次增量读取，无状态变更时不写库；
    - `subagents` 删除 `_CHILD_OPTIONAL_KEYS` 旧行兼容与手写 `_parse_*`，`monitor.py` 删除二分序列化截断与 JSON-in-choice-id。
  - 部分：`workflows` 三重 JSON 往返清零、`_run_node` 双体合并、sha 账本 7→3（`source`/`effective_input`/`output`）；`goal` 删 `.voidcode/attachments` 双读、字段 23→19、提示词收敛为模块函数、`set_budget` 收敛至 1 处；`todo` 的 `summary` 改为派生属性、双提醒机合并为单 policy 解析、observer 的 3 页重扫 + 无条件写库已删；`subagents` 手写 `_parse_*`、`_CHILD_OPTIONAL_KEYS`、monitor JSON-in-choice-id 已删。未动：共享插件状态读写 helper——各插件仍手写 `from_payload`/`to_payload`。

- [x] **清理其余插件与 Capabilities 中的“verbatim legacy copy”、轮询锁与重复实现。**
  - 证据：
    1. **`cron/cron_plugin.py:1-32`** 与 **`compaction/projection.py:3-4`** 顶部直接写着“verbatim legacy copy” / “Ported from legacy runtime/context/window.py”，保留了 `_clock_ms` 模块级测试钩子、3×200 页事件扫描与 4 个硬编码字符截断常量。
    2. **`review/review_plugin.py:80-260`** 重新实现了一遍 `subagents` 的 child join 与 artifact 分页拼接逻辑。
    3. **`lsp/lsp_manager.py:1-1012`**（10 个文件）手写 stdio `Content-Length` 帧解析、3 把锁（`request_lock`/`_lock`/`_lifecycle_lock`）与 `time.sleep(0.01)` 轮询读循环；**`mcp/mcp_manager.py:1-710`** 维护两把 `RLock` + `BlockingPortal` + `json_wire_object(own_json_object(...))`，`mcp_tool.py:210-213` 用 `preview_bytes[:MAX_VIEW_DATA_BYTES].decode(errors="ignore")` 静默截断。
    4. **`capabilities/exec/background_process.py:406-445,625`** 用 `time.sleep(0.03)`（30ms 轮询，注释写“as legacy manager polled”）等待进程就绪与退出。
    5. **`capabilities/files/`** 存在三套重叠的编辑/补丁引擎（`edit_tool.py`、`apply_patch.py`、`apply_workspace_edit.py`），且 `grep_tool.py`、`glob_tool.py`、`find_tool.py`、`skills_plugin.py` 各自复制了一份 `DEFAULT_IGNORE_PATTERNS`、限制常量与非空字符串校验器。
    6. **`capabilities/providers/`** 中 `openai_native.py`（1189 行）、`anthropic_native.py`（826 行）、`google_native.py`（701 行）三处重复实现 `_usage`/`_done_reason`/`_merge_usage`/`_normalize_base_url` 与 Transport Protocol；`panels.py:87` `last_dispatch` 每次打开 `/model` 面板都用 `while True: read_transcript(limit=200)` 从头扫完整个会话历史。
  - 整改：删除所有 legacy 移植垫片；`review` 复用共享 child output 分页 helper；`background_process` 与 `lsp` 改为事件/管道唤醒，删除 10ms/30ms `time.sleep` 轮询；合并 `files/` 的 ignore 列表与校验器，收敛多余补丁实现；在 `providers/` 中提取统一的 HTTP/SSE 传输底座，并为 `last_dispatch` 提供逆序单条查询而非全量扫 transcript。
  - 已落地：`_wire_common.py` 统一传输底座 + `last_dispatch` 改 `latest_event` 逆序单查（`panels.py:84`）；MCP 预览超限改为显式抛错；`find`/`skills` 去掉自带 ignore 表；`files/` 方案 A 落地（`edit` 严格收敛为确定性单/批文本替换，移除 `_PatchEdit`，`apply_patch` 隔离为专用能力，`apply_workspace_edit` 专用于 LSP/重构，并在 `_common` 提取统一路径解析与 diff 计算原语）。保留（已接受）：`cron`/`compaction` 的 legacy 移植注释仍在（超出清算范围，不动）；`review` 保留本地 output pager（`_first_output_page`/`_paged_output_text`）；`background_process` 就绪轮询保留为命名常量 `_READY_POLL_SECONDS`（`:50`，进程就绪无事件可等）；`grep`/`glob` 各保留自有 ignore 表（按名 vs 按路径段匹配，语义不同）；files pager 的 4KiB 包络余量（`resource_pages.py:21`，注释已说明）。`lsp`/`mcp` 由专职 worker 重写（stdio 帧解析保留、`sleep(0.01)` 已删）。
