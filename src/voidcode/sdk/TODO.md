# TODO

SDK 表达力：只记录有具体消费方、不能靠现有公开契约完整实现的缺口。中立契约归 SDK，治理、调度和持久化实现归 Core，产品能力仍归插件；不以插件访问 Core 私有对象补洞。优先级与对照证据口径见[总待办](../../../docs/TODO.md)。

## P0：执行预算与工具编排的前置契约

- [x] **Optional provider-turn caps and explicit inherited root-action grants.** Consumers: primary/child agents, workflows, scripts.
  - SDK contracts use optional `RunSpec.max_steps`/`ResumeSpec.max_steps` solely for provider-turn caps, plus optional `root_action_budget` for a finite root action grant. Fresh `RunSpec.max_steps=None` is uncapped; `ResumeSpec.max_steps=None` retains the checkpoint's assigned cap, while an explicit resume value sets a new total cap for that resumable action and must exceed already-consumed steps to permit more dispatches. There is no implicit way to remove a captured finite cap. `BudgetExhaustion` reports dimension, source, limit, consumed/remaining, and continuation authority; `RunResult`/`ContinuationResult` retain it.
  - Child spawn defaults to no local cap; profiles only narrow a caller-assigned cap. Core captures root identity and initial optional cap at child birth/queued-send reservation, shares finite authorization across descendants, rejects child-created roots, and appends explicit idempotent resume grants. SDK carries neutral semantics and no default policy.
  - No schema migration or guessed authority: legacy records without captured root identity fail visibly. Core implementation and boundary risks are tracked in [Core TODO](../core/TODO.md).

- [ ] **可挂起、可等待结果的受治理工具调用。** 消费方：`eval`、动态工具编排。
  - 现状：[`PluginServices.invoke_tool`](services.py) 返回 `ToolInvocationReceipt`，请求与当前回调结果一起提交，回调返回后才执行；不是返回 `ToolResult` 的同步调用，也没有公开的结果等待接口。不能把它直接包装成 `await tool.read(...)`，否则当前回调等待自己尚未提交的请求。
  - 目标：明确请求、结果关联、审批等待、失败、取消和恢复的中立协议；宿主在安全边界挂起并释放 invocation lease，继续时重新取得合法 lease。脚本拿到结构化结果与资源引用，而非从 transcript 猜返回值。参考 OMP [eval 的 tool bridge、host waits 与 cancellation](omp://tools/eval.md)。
  - 边界：沿用 session tool scope、revision、只读限制、pre-tool gates、审批和递归/预算计数；不直调插件 handler，不保留跨等待的 `PluginServices`，不把任意代码执行宣传成沙箱。明确同 session 可重入调度和递归限制，不用轮询解死锁。
  - 验收：脚本读文件后根据返回数据发起编辑；审批期间 UI 可操作，允许后同一逻辑调用只执行一次，拒绝/取消成为脚本可处理的结果；重启后已发生的外部变更不重放，丢失的内核状态明确报告，旧 revision/隐藏工具不能借桥访问。

- [ ] **Authenticated, scoped byte-paged artifact reads.** Consumer: terminal full-output and image viewers.
  - Original evidence: the terminal viewer used `getattr(client, "read_artifact", None)` plus catch-all preview fallback while SessionClient/CoreRpcClient/RPC had no artifact reader; the old pager checkbox did not prove an end-to-end contract.
  - Contract: typed byte pages, workspace authentication, digest-only identity, honest base64 wire encoding, no writable backing paths/general VFS. Core `read_artifact`, local and authenticated RPC `SessionClient.read_artifact`, and `artifact.read` are implemented with exact integer bounds, lowercase digest, distinct not-found, and empty/eof past EOF. The artifact API conformance command reported 6 passed; its file imports viewer code and is not part of the first Core/API-only commit. Current Chromium full-page image and Kitty CLI surface commands separately reported 1 passed and 6 passed; their viewer code remains a later client stage. Live-progress PTY is not included.

- [x] **Streaming immutable artifact publication.** Consumer: ordinary file/raster reads.
  - The old `spill_artifact(bytes | str)` ingress forced whole-image allocation and the media branch introduced an arbitrary 50 MiB cutoff. `PluginServices.spill_artifact_stream(Iterable[bytes], media_type=...)` now validates chunks under the current lease, checks cancellation/revocation, discards incomplete spills, and publishes only at EOF. File/raster and long-line recovery use this ingress, with no separate image-size limit; the actual existing ArtifactStore append bound is enforced visibly.
  - Proof: real Core raster reads above 256 KiB reach native SDK HTTP requests as valid, decoded RGB PNG pixels; malformed chunks, producer exceptions, revocation, cancellation and store-bound failures leave no incomplete artifact.

- [ ] **Per-dispatch model projection headroom.** Consumer: Core tool-result/context assembly.
  - Provider discovery/catalog exposes `context_window` and `max_input_tokens`, and committed window summaries already exist. The missing contract/computation is remaining per-call token/image occupancy and a projection allocation derived from that active model/window, not the existence of all context control.
  - `project_tool_result(max_bytes=...)` now strictly accounts for metadata, errors, Unicode, identities and partial markers. Core still passes the existing chosen `TOOL_OUTPUT_BYTES` text policy; it is not adaptive headroom or a provider law. The serialized-message 256 KiB reject is removed, not replaced with another guessed global limit.
  - Acceptance: derive a neutral caller allocation from known active model/context accounting, preserve complete-resource identities when shrinking, and report unknown limits/usage honestly rather than infer tokens from base64 or JSON byte counts.

## P1：URI、富结果与用户交互

- [ ] **Discoverable tool-result/body schemas and content capabilities.** Consumers: eval/tool bridges and generated typed proxies.
  - Evidence: `ToolDefinition` declares `input_schema`, effects, path keys, replay policy and icon, but no result/body/output schema or returned content capability. A typed `ToolBody` and canonical `result_reference` preserve data; they do not let an unrelated consumer discover or validate that shape.
  - Projection selection is explicit: `TextOutput.text` remains the selected model page while machine bodies are recoverable at the canonical result URI; `EmptyOutput` bodies represent structured-only content, and its typed textual artifact reference opts into paging. This removes duplicate selected-page/body projection without field-name or tool-name heuristics. Discoverable result schemas/capabilities remain the separate gap.
  - Acceptance: discover schemas for existing typed bodies/resources and validate peer returns using SDK-only contracts; do not add a second JSON body, peer imports or a parallel tool registry. This API is not part of the media change.

- [ ] **Streaming/raw process-output capture.** Consumers: large/binary shell output and artifact-backed process results.
  - Evidence: `ProcessResult.stdout/stderr` promise bounded decoded text. Core `_StreamCapture` buffers per-stream bytes, then `text()` joins, decodes with `errors="replace"` and normalizes CRLF. Current shell artifacts preserve all **captured text**, not byte-identical raw stdout/stderr.
  - The shell's 128 MiB per-stream request is a chosen in-memory capture policy under the process service's 256 MiB ceiling, not an inline/UI limit; join/decode/UTF-8 re-encoding can allocate additional copies, and replacement expansion can exceed the artifact store bound. Streaming artifact ingress alone does not replace this process capture subsystem.
  - Acceptance: expose raw/streamed capture when required without pretending decoded text is original binary, with honest per-stream capture/truncation state and actual storage failures; preserve process lifetime/cancellation governance.

- [ ] **Typed child-join budget exhaustion.** Consumers: task/workflow/script joins.
  - `ChildOutcome` carries `stop_cause` and `error`, but unlike `RunResult`/`ContinuationResult` has no `BudgetExhaustion` field for dimension/source/limit/consumed/remaining. The new independent-child regression exposed the separate snapshot bug that mapped all interruptions to cancellation; that mapping is fixed using committed stop cause, but child joins still cannot express the typed finite-cap state.
  - Acceptance: retain the actual exhausted dimension and counters across join/recovery without inferring a refill or inventing cancellation. API extension is deliberately not bundled into media projection.

- [ ] **可发现的资源能力与受限只读资源服务。** 消费方：`read`、资源检索、Agent/进程状态 URI、客户端资源查看器。
  - 现状：[`ResourceReader`](resources.py) 只有 `scheme` 与同步 `handler`；`ResourceReadRequest` 已有分页参数，但没有资源枚举/搜索能力描述。`ResourceReaderServices` 只开放 owner state、artifact、tool definition 和 history，不能取得 Core 的实时 descendant/process 投影；不能为了 `agent://`/进程 URI 交出完整 `PluginServices`。
  - 目标：按实际消费场景补最小的能力发现、列举/搜索与 caller-scoped 只读投影；明确文本、二进制、可枚举资源及可选的只读文件物化能力，不假设每个 URI 都是本地文件。继续使用同一注册入口和返回游标，不在各工具维护第二张 scheme 表。参考 OMP [read 的 internal URL router 与 locate/resolve](omp://tools/read.md)、[shell 的 URI filesystem 及外部进程限制](omp://bash-tool-runtime.md)。
  - 边界：读者拿到 provider-owner 的窄只读 lease；所有新增方法保留 lineage/workspace/revision 隔离，不暴露 peer state、凭据、任意 SQL 或可变宿主路径。变更操作走受治理的工具/命令，不能塞进 resource reader。没有能力、没有资源、非法页与 handler 错误保持不同结果。
  - 验收：第三方插件只依赖 SDK 即可提供子 Agent 状态/结果资源；`read` 与检索消费者看到同一资源身份及分页边界；禁用提供者立即失去能力，子 Agent 无法通过枚举或 URI 读取父、兄弟及无关会话；只读物化内容不能被外部进程改写原 artifact。

- [ ] **组合工具内容与有界声明式展示。** 消费方：`eval` 的表格/图像、MCP 混合媒体、TUI/Web 工具结果卡。
  - 现状：[`ToolOutput`](tools.py) 是单个 `TextOutput | EmptyOutput | AttachmentOutput`，不能表达按序混合的文本和多张图片；`ToolBody` 已能表达结构化业务数据，不缺第二套 JSON body。[`ResourcePage`](resources.py) 同样只容纳单个文本或附件；[`views.py`](views.py) 的有限视图没有 table/JSON tree/media 类型。MCP [结果投影](../plugins/mcp/mcp_tool.py) 仅在恰好一张且足够小时选用附件输出，其余回退文本和 artifact 引用。
  - 目标：以现有 `ArtifactReference`、typed body 和 output bounds 为基础，补有序组合内容及有限、纯数据的展示契约，复用已有 `DiffViewData` 而非再造平行的 diff 格式；区分模型投影、用户展示和完整数据引用。复用 `ToolContext.emit_tool_progress` 及已有 `stream/text`，跨工具确需共享阶段/进度字段时才规范可选载荷及未知值，不再建一套进度总线。参考 OMP [eval 的 views/images 与模型文本分流](omp://tools/eval.md)。
  - 边界：SDK 不引入终端协议、HTML 执行器或任意插件 renderer；图片原件留在现有内容寻址存储，不复制 OMP 的第二套 blob/artifact 存储。契约落地须贯通 codec、transcript、transport、provider 投影和两端客户端，迁移全部调用方，不靠丢字段兼容。
  - 验收：一次调用输出说明、表格、两张图和失败诊断，两端按序呈现，CLI/无图终端有可读降级；大图/大表走引用与分页，不因展示上限丢完整数据，也不把 base64/完整 JSON 重复塞进模型上下文；取消后的部分结果不显示为成功。

- [ ] **模型发起的持久化用户问答契约。** 消费方：结构化 `ask` 工具及其 TUI/Web 表单。
  - 现状：[`FormViewData`/`FormField`](views.py) 是命令 action 驱动的表单，只有基础字段及单选 options；[`ConfirmationRequest`](commands.py) 确认的是命令副作用。`ToolResult` 只有成功/失败，尚无模型问题与用户答案的关联、等待和恢复契约；不能把问答伪装成审批。
  - 目标：声明有类型的问题、上下文、单选/多选/自由输入、校验、可选媒体，以及 request/answer/cancel 的关联与生命周期；复用已有表单和客户端能力声明，只增加确实缺失的部分。参考 OMP [ask 的整表交互与结构化答案](omp://tools/ask.md)，不要求引入 HTML 作为 SDK 语言。
  - 边界：一次答案只消费一次；断线、会话切换、超时、无人值守和用户主动取消有明确状态，未回答不能冒充同意。问题不会授予工具权限，回答不会绕过审批；资源图片沿用资源访问边界。
  - 验收：Agent 在同一问题中展示方案差异、图片和三个有校验字段；用户可修改、取消、重连后继续，所得答案保留布尔/数值/数组类型；无交互能力的客户端显式报告不支持，不编造默认回答、不挂死模型轮次。

- [ ] **非阻塞子代快照与首个就绪等待。** 消费方：并行 `task`、批次聚合、Agent URI、monitor。
  - 现状：[`join_child`](services.py) 只等待指定 child；`list_descendants` 返回当前状态，但 `DescendantNode` 没有终态输出/错误引用，结果信息分散在 `ChildOutcome` 与完成通知中。缺少在多个 caller-owned child 中等待任意结果的公开契约。
  - 目标：在现有血缘与 run 身份下提供只读快照、首个就绪等待及结果消费/acknowledge 语义；复用 Core 完成条件与通知，不通过轮询或串行 join 实现。资源读者仅得到快照的窄只读部分，等待/消费不塞进 resource reader。参考 OMP [wait 的 first-settled 与去重](omp://tools/wait.md)。
  - 验收：三个 child 中后创建者先完成时立即返回该 run 的结果；取消/用户消息能中断等待，过期 run 不满足新等待；快照不消费通知，消费结果才原子去重；跨插件派生的可见 descendant 可读，但父/兄弟/无关会话不可读。

## P2：脚本化检索与批量判断

- [ ] **有 schema 的单次模型查询结果。** 消费方：语义 `find`、`eval` 批量分类/数据提取。
  - 现状：[`PluginServices.model_query`](services.py) 返回字符串，接口没有结果 schema 或结构化结果字段。已有模型查询、预算和 usage 归属应复用，不由插件私建 provider 客户端。参考 OMP [eval 的 completion/judge 与结构化结果](omp://tools/eval.md)。
  - 目标：补中立的结构化请求/结果及取消、预算、用量关联；批量调度仍由产品插件组合，不为模仿 OMP 引入固定模型角色、评分框架或新模型注册表。
  - 验收：声明 schema 的抽取返回经过校验的 JSON；非法结果、拒绝、取消与预算耗尽不可当作有效答案；批次内单项失败可定位，调用仍进入原有治理和用量统计。

- [ ] **子任务输出 schema 与结构化结果身份。** 消费方：`task`、workflow、脚本中的 Agent 结果。
  - 现状：[`spawn_child`/`ChildOutcome`](services.py) 没有输出 schema/已校验 JSON 结果字段，只有文本与 artifact；已有 typed tool body 不会自动解决 child 最终交付问题。
  - 目标：为子任务声明可选输出契约，结果保留 schema 校验状态、run 身份和完整数据/引用；模型的最后一句话、部分进度与正式交付区分清楚。具体交付工具属于产品插件，SDK 不引入 OMP 专用 `yield` builtin，也不为此开放任意动态提权。
  - 验收：结构化交付经过严格校验后被父任务和 workflow 同样读取；无效结果不能静默标为成功，修正仍遵守已授权预算、不暗设固定重试轮数；取消/超限保留部分成果但不满足成功 schema。
