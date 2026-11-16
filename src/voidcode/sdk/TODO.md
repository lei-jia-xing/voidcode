# TODO

SDK 表达力：只记录有具体消费方、不能靠现有公开契约完整实现的缺口。中立契约归 SDK，治理、调度和持久化实现归 Core，产品能力仍归插件；不以插件访问 Core 私有对象补洞。优先级与对照证据口径见[总待办](../../../docs/TODO.md)。

## P0：执行预算与工具编排的前置契约

- [x] **Optional provider-turn caps and explicit inherited root-action grants.** Consumers: primary/child agents, workflows, scripts.
  - SDK contracts use optional `RunSpec.max_steps`/`ResumeSpec.max_steps` solely for provider-turn caps, plus optional `root_action_budget` for a finite root action grant. Fresh `RunSpec.max_steps=None` is uncapped; `ResumeSpec.max_steps=None` retains the checkpoint's assigned cap, while an explicit resume value sets a new total cap for that resumable action and must exceed already-consumed steps to permit more dispatches. There is no implicit way to remove a captured finite cap. `BudgetExhaustion` reports dimension, source, limit, consumed/remaining, and continuation authority; `RunResult`/`ContinuationResult` retain it.
  - Child spawn defaults to no local cap; profiles only narrow a caller-assigned cap. Core captures root identity and initial optional cap at child birth/queued-send reservation, shares finite authorization across descendants, rejects child-created roots, and appends explicit idempotent resume grants. SDK carries neutral semantics and no default policy.
  - No schema migration or guessed authority: legacy records without captured root identity fail visibly. Core implementation and boundary risks are tracked in [Core TODO](../core/TODO.md).

- [x] **Lease-free same-session tool waits for background actions.** Consumers: scripted plugins and background workflows.
  - `PluginServices.invoke_tool` remains receipt-only and commits its request with the callback result. The long-lived `BackgroundContext` adds `wait_tool(receipt)`, which waits only after the callback lease/dispatch guard is released; it never retains `PluginServices`, calls a handler directly, or polls.
  - `ToolAwaitResult` correlates exact owner/action/request/session/revision/tool identity. It returns the actual complete `ToolResult` (canonical body/reference hydration included) or an explicit no-result terminal such as interrupted, cancelled, kernel-lost, or typed `BudgetExhaustion`. Only committed `user_stop`/`owner_cancel` is cancellation; missing captured root/visible-tool/stance authority is refused, and current policy can only narrow the captured origin.
  - Evidence: `uv run pytest -q tests/unit/core/test_background_tool_wait.py` — 12 passed in 44.88s, including real Core external-SDK read→branch→edit on a >65,536-byte canonical result, approval/API responsiveness, allow/deny/late-cancel, queued approval wake, captured-root exhaustion, active-callback rejection, hidden/stale/legacy authority refusal, close/reload park, and abrupt subprocess loss without replay. Existing nested/native pairing and approval compatibility: 9 passed in 21.08s.

- [x] **Authenticated, scoped byte-paged artifact reads.** Consumer: terminal full-output and image viewers.
  - Original evidence: the terminal viewer used `getattr(client, "read_artifact", None)` plus catch-all preview fallback while SessionClient/CoreRpcClient/RPC had no artifact reader; the old pager checkbox did not prove an end-to-end contract.
  - Contract: typed byte pages, workspace authentication, digest-only identity, honest base64 wire encoding, no writable backing paths/general VFS. Core `read_artifact`, local and authenticated RPC `SessionClient.read_artifact`, and `artifact.read` enforce exact integer bounds/lowercase digest, distinguish not-found, and return empty/eof past EOF.
  - Evidence: artifact client API conformance reported 6 passed; the test file also imports viewer code and was excluded from the first Core/API foundation stage. Authenticated Chromium Core/RPC artifact/image surface reported 1 passed/14.1s; Kitty CLI surface group reported 6 passed/17.70s, including a valid 346,073-byte 384×300 PNG and unsupported-terminal explicit open/export cleanup. Remote user-image→native-provider forwarding passed separately. Physical Kitty pixels remain unobserved; no Sixel or non-PNG terminal image support is claimed.

- [x] **Streaming immutable artifact publication.** Consumer: ordinary file/raster reads.
  - The old `spill_artifact(bytes | str)` ingress forced whole-image allocation and the media branch introduced an arbitrary 50 MiB cutoff. `PluginServices.spill_artifact_stream(Iterable[bytes], media_type=...)` now validates chunks under the current lease, checks cancellation/revocation, discards incomplete spills, and publishes only at EOF. File/raster and long-line recovery use this ingress, with no separate image-size limit; the actual existing ArtifactStore append bound is enforced visibly.
  - Proof: real Core raster reads above 256 KiB reach native SDK HTTP requests as valid, decoded RGB PNG pixels; malformed chunks, producer exceptions, revocation, cancellation and store-bound failures leave no incomplete artifact.

- [ ] **Per-dispatch model projection headroom.** Consumer: Core tool-result/context assembly.
  - Provider discovery/catalog exposes `context_window` and `max_input_tokens`, and committed window summaries already exist. The missing contract/computation is remaining per-call token/image occupancy and a projection allocation derived from that active model/window, not the existence of all context control.
  - `project_tool_result(max_bytes=...)` now strictly accounts for metadata, errors, Unicode, identities and partial markers. Core still passes the existing chosen `TOOL_OUTPUT_BYTES` text policy; it is not adaptive headroom or a provider law. The serialized-message 256 KiB reject is removed, not replaced with another guessed global limit.
  - Acceptance: derive a neutral caller allocation from known active model/context accounting, preserve complete-resource identities when shrinking, and report unknown limits/usage honestly rather than infer tokens from base64 or JSON byte counts.

## P1：URI、富结果与用户交互

- [ ] **Foreground suspension and the complete `eval` product.**
  - The background-action wait is not a foreground suspend API: ordinary `PluginServices.invoke_tool` remains receipt-only, and a normal tool/command/hook callback cannot block waiting for work it has not committed. No SDK contract retains or reconstructs an active Python frame across foreground suspension.
  - Full `eval` remains P1 product work: expose a separately governed foreground composition lifecycle only if its owner, approval, lease, cancellation, persistence, and restart semantics can be represented without bypassing the ordinary tool pipeline. This bridge does not claim remote resources or arbitrary-code sandboxing.

- [ ] **Discoverable tool-result/body schemas and content capabilities.** Consumers: eval/tool bridges and generated typed proxies.
  - Evidence: `ToolDefinition` declares `input_schema`, effects, path keys, replay policy and icon, but no result/body/output schema or returned content capability. A typed `ToolBody` and canonical `result_reference` preserve data; they do not let an unrelated consumer discover or validate that shape.
  - Projection selection is explicit: `TextOutput.text` remains the selected model page while machine bodies are recoverable at the canonical result URI; `EmptyOutput` bodies represent structured-only content, and its typed textual artifact reference opts into paging. This removes duplicate selected-page/body projection without field-name or tool-name heuristics. Discoverable result schemas/capabilities remain the separate gap.
  - Acceptance: discover schemas for existing typed bodies/resources and validate peer returns using SDK-only contracts; do not add a second JSON body, peer imports or a parallel tool registry. This API is not part of the media change.

- [ ] **Streaming/raw process-output capture.** Consumers: large/binary shell output and artifact-backed process results.
  - Evidence: `ProcessResult.stdout/stderr` promise bounded decoded text. Core `_StreamCapture` buffers per-stream bytes, then `text()` joins, decodes with `errors="replace"` and normalizes CRLF. Current shell artifacts preserve all **captured text**, not byte-identical raw stdout/stderr.
  - The shell's 128 MiB per-stream request is a chosen in-memory capture policy under the process service's 256 MiB ceiling, not an inline/UI limit; join/decode/UTF-8 re-encoding can allocate additional copies, and replacement expansion can exceed the artifact store bound. Streaming artifact ingress alone does not replace this process capture subsystem.
  - Acceptance: expose raw/streamed capture when required without pretending decoded text is original binary, with honest per-stream capture/truncation state and actual storage failures; preserve process lifetime/cancellation governance.

- [x] **Typed child budget exhaustion.** Consumers: `task`, `task_batch`, review, and workflows.
  - `ChildOutcome.budget_exhaustion: BudgetExhaustion | None` carries the committed run's dimension/source/limit/consumed/remaining/continue-with fields. Core reads a recoverable pause from that exact run's committed `RUN_INTERRUPTED` payload and terminal outcomes from its `CHILD_FINISHED` fact; reopen and join do not infer usage from current root counters.
  - Subagent task/batch and review projections retain the typed value. Workflow nodes persist a recoverable budget pause as `interrupted`, not success or cancellation. The pause has no `CHILD_FINISHED`; public resume starts a distinct run identity, so its later terminal fact is separate and does not inherit the old pause's budget data.
  - Regression coverage: exact-stop reopen/resume in `test_child_budget_stop_is_typed_from_exact_event_across_reopen_and_resume`; review and workflow consumer state in `test_budget_interrupted_child_preserves_typed_stop_in_review_result` and `test_budget_interruption_parks_with_typed_stop_without_acknowledging`; real-Core task output coverage is in the subagents integration tests.

- [ ] **资源能力发现/枚举与更多只读资源服务。** 消费方：`read`、资源检索、Agent/进程状态 URI、客户端资源查看器。
  - 已完成的 SDK/Core 前置：`ResourceReaderServices.read_child_snapshot(child_session_id)` 返回 caller-scoped `ChildSessionSnapshot`；`list_processes()` 与 `read_process_output()` 返回 path-free `ResourceProcessStatus`/`ProcessOutput`，Core 强制 reader owner + caller session。资源视图只读，不暴露 process control；process 读取不绑定服务取消、不暂存 effects。共享已有 artifact byte page 与 `ResourceReadRequest` offset/byte offset。
  - Agent snapshots come from current `DescendantNode` plus committed `CHILD_FINISHED` facts: only completed nonempty inline output or a full output artifact replaces the latest result; failed/interrupted/cancelled/empty/preview-only follow-ups do not erase it. Inline output is materialized to a deduplicated immutable artifact for generic paging without changing the event or join result. Current status/run/stop cause and published run id remain separate; the snapshot does not join, ack, or consume notices. Missing and invisible ids both return `None`.
  - 仍缺通用资源能力发现、枚举/搜索契约；不假设所有 URI 是本地文件。VoidCode 的 child fact 没有 OMP accepted-yield 标记，上述选择是显式近似而非宣称等价。当前 `agent://<id>` 不支持 `/key/index` JSON path，也不支持新查询游标；结构化 child publication schema 是单独 SDK 缺口。
  - 边界：不交出完整 `PluginServices`、peer state、凭据、任意 SQL 或可变宿主路径；process owner 在 spawn 时取自 lease，缺 owner/session 的 Core lookup 不作 unrestricted fallback。读者不能 join、ack、send、cancel 或 replay。
  - 剩余验收：增加与真实消费者对应的资源能力声明/搜索前，先复用同一注册入口与分页 identity；provider disabled 立即移除 scheme，未知 scheme、不可见/不存在资源、非法页和 handler/storage 错误保持各自语义。外部进程只读物化能力仍未定义，不得直接提供 artifact 可写 backing path。

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

- [x] **First-settled owned child wait and atomic result delivery.** Consumers: Subagents `wait`, parallel `task`, batches, and scripts.
  - `PluginServices.wait_children(child_session_ids)` uses the existing Core condition and durable event cursors, not timed polling or a new job registry. It first drains every eligible, unconsumed exact `(child_session_id, run_id)` fact for the selected owned children, then binds outstanding reservations/current runs at entry. It returns exact run identities (including an older unconsumed run beside a live follow-up) without child-id collapsing; current live/pending states are a separate snapshot.
  - The no-argument Subagents `wait` only selects child IDs recorded by that parent-owned plugin state. `task_batch.output` first performs its nonblocking current-run read and waits first-settled only when no current outcome is reportable; it returns full output/reference plus pending current runs. Paged outcome continuation uses the immutable output URI and byte cursor, never a new current-run join. `notify_on_completion=False` does not exclude durable child outcomes.
  - Delivery stages the exact source stream/session and event sequence receipt and matching pending-notice cancellation in the same successful invocation transaction. Read-only snapshots do not consume. A valid callback-returned `ToolFailure` (`status="error"`) also commits explicitly staged delivery; a thrown exception, invalid result, or aborted invocation does not. Steering wins a race without consuming the child result; cancellation/shutdown propagate rather than returning an empty success; approval parking remains pending.
  - Evidence: native real-Core provider proof returned three gated child results, preserved 52,000 UTF-8 bytes through URI paging/read/reread, and verified deduplication/reopen. Focused regressions cover event-snapshot races, reserved-run binding, exact stop receipt across resume/re-stop, consumer rollback, failed child delivery, approval resume, steering/cancel/close wake, and foreign-parent refusal; the changed Subagents unit module passed 68 tests.
  - Adjacent Core gap (unchanged): single-current-run `Core.join_child` still performs its existing 0.2s `Condition.wait`/SQL status checks; this first-settled wait does not change that path. There is no generic peer/process/job wait, no accepted-yield marker, and no top-level shared context in `task_batch`.
- [x] **Typed child output/history capture errors.** Consumers: `task`, `task_batch`, review, `agent://`, and workflows. (`8824ba40` SDK/Core fields, `95f7c298` consumers, `c72f561d` workflow persistence.)
  - `ChildOutcome.output_error`/`history_error: str | None` carry the committed run's verbatim capture cause; `None` means no committed failure, including historical facts that predate the fields — never an inference. `as_payload` emits `output_preview` and both causes independently of `output_sha256`, so a preview-only outcome survives the wait path. Core populates `_child_outcome`, `wait_children`, and `join_child` only from that same run's committed `CHILD_FINISHED` fact, and completion notices carry the same cause hints.
  - Consumers distinguish missing/empty/preview/full output from capture failure: review derives data from `outcome.as_payload`, pages committed output through the shared artifact pager with exact run + immutable URI, and fails visibly with status `completed` preserved when full capture is unavailable; task/batch/wait propagate both causes in text and body without inventing URIs or relabeling the run; `agent://` shows only the publication run's own errors; workflows persist preview-only facts instead of empty success. No consumer infers a cause from later live state, and no current-run cause is attached to a prior publication.
  - Regression coverage: `tests/unit/core/test_child_capture_errors.py` 6 passed; `tests/unit/plugins/test_review_plugin.py` 49 passed; `tests/unit/plugins/test_subagents_plugin.py` 71 passed; `tests/integration/test_review_plugin.py` 10 passed, including real-Core long-review full-byte reassembly and old-URI immutability across a follow-up; workflow persist-then-raise durability proof in `tests/unit/plugins/test_workflows_nodes.py` (17 passed, 1 pre-existing HEAD failure). Current-run snapshot diagnostics remain a separate gap below.

- [ ] **Snapshot current-run capture diagnostics.** Consumer: `agent://` reader.
  - `ChildSessionSnapshot` has only descendant+latest_outcome: a current/newest terminal run's capture failure (ineligible preview-only/failed B) cannot be represented while latest eligible A is kept. Do not widen eligibility or borrow A's fields. Missing: an exact newest-terminal-run projection (identity + capture causes) distinct from latest eligible output; design the minimal contract before any agent:// surfacing.
  - Acceptance: add the projection only when a real consumer needs it; no new API now, no eligibility widening, and no inferring B's cause from live state.

## P2：脚本化检索与批量判断

- [ ] **有 schema 的单次模型查询结果。** 消费方：语义 `find`、`eval` 批量分类/数据提取。
  - 现状：[`PluginServices.model_query`](services.py) 返回字符串，接口没有结果 schema 或结构化结果字段。已有模型查询、预算和 usage 归属应复用，不由插件私建 provider 客户端。参考 OMP [eval 的 completion/judge 与结构化结果](omp://tools/eval.md)。
  - 目标：补中立的结构化请求/结果及取消、预算、用量关联；批量调度仍由产品插件组合，不为模仿 OMP 引入固定模型角色、评分框架或新模型注册表。
  - 验收：声明 schema 的抽取返回经过校验的 JSON；非法结果、拒绝、取消与预算耗尽不可当作有效答案；批次内单项失败可定位，调用仍进入原有治理和用量统计。

- [ ] **子任务输出 schema 与结构化结果身份。** 消费方：`task`、workflow、脚本中的 Agent 结果。
  - 现状：[`spawn_child`/`ChildOutcome`](services.py) 没有输出 schema/已校验 JSON 结果字段；`agent://<id>` 只读完整文本/既有 artifact，不能依据字符串或 transcript 解析 `/key/index`。Core 的 `CHILD_FINISHED` 也没有独立的 accepted-yield 标记，当前 URI 对非空 completed output 的选择是有限近似。
  - 目标：为子任务声明可选输出契约，结果保留 schema 校验状态、run 身份和完整数据/引用；模型的最后一句话、部分进度与正式交付区分清楚。具体交付工具属于产品插件，SDK 不引入 OMP 专用 `yield` builtin，也不为此开放任意动态提权。
  - 验收：结构化交付经过严格校验后被父任务和 workflow 同样读取；无效结果不能静默标为成功，修正仍遵守已授权预算、不暗设固定重试轮数；取消/超限保留部分成果但不满足成功 schema。准确区分 accepted yield 的协议须先由 Core/SDK 表达，再由 `agent://` 消费。
