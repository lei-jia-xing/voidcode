# VoidCode 与 Oh My Pi 重新调研记录

> 调研日期：2026-08-15（复核更新）<br>
> VoidCode：`a213a95ee1da5c818c63a25ddfc87d463763b4d3`<br>
> Oh My Pi：`45e12e5bb758198a920c6070e7e64cb33b21beac`，版本 `17.2.12`<br>
> 补充调研：OMP HEAD `ad318c7572abaeebd5cf8a7a16d350ff1d32a738`（约 `17.3.3`），复核确认原始结论，并新增 reasoning-effort 缺口（该缺口现已在 VoidCode 侧关闭）

## 目的与证据边界

本文重新比较 VoidCode 与 [Oh My Pi](https://github.com/can1357/oh-my-pi)，重点回答三个问题：

1. VoidCode 当前真正落后的是什么；
2. 旧调研中的哪些判断已经失效；
3. 当前最值得引入、且符合 VoidCode runtime-centric 架构的机制是什么。

本次结论优先来自两个项目的当前代码、测试、提交记录和项目内文档，不把 README 中的宣传数字当成独立 benchmark 结果。对“存在模块”“具备运行时契约”和“真实任务中成熟可用”分别判断，避免把基础设施存在误写成产品能力已经完成。

## 结论摘要

VoidCode 已不再只是一个有会话持久化和审批的运行时骨架。相较旧调研，它已经补上或推进了：

- `read` 的结构化行数据、continuation 和 `content_hash`；
- `edit`/`multi_edit`/`write`/`apply_patch`/`apply_workspace_edit` 的 required `expectedHash`（缺失/陈旧即拒绝）、seen-range 约束和写后 formatter/LSP/tree-sitter 语法 diagnostics；
- `apply_workspace_edit` 的多文件 hash 校验；
- provider 原生流式事件、缓存使用信息和错误归一化；
- deterministic / model-assisted context projection（后者仅有回退框架，未接模型 projector）；
- 持久化 tool replay、steer/follow-up interaction queue；
- structured delegated handoff、并行任务组完成事件和后台结果读取；
- CLI、TUI、Web 对同一 runtime truth 的消费。

因此，旧文档把“hash 防陈旧编辑”“结构化 compaction”“结构化子任务结果”整体列为尚未引入，已经过时。

需要补充的是，原始调研之后又识别并关闭了一个缺口：reasoning-effort / thinking level 语义。该语义现已是 runtime-owned 的规范枚举 `off | minimal | low | medium | high | xhigh | max`，在 4 个边界严格校验（`config.py`、`contracts.py`、`config_materializer.py`、JSON schema，不做向后兼容），并走 `normalize → clamp → map` 管线按 provider 映射：GLM 映射为 `extra_body.thinking.type=enabled|disabled` 二元开关；OpenAI/Anthropic/Google/Grok 映射为 `reasoning_effort` kwarg，其中 off→none、max 折叠。依据 `docs/reasoning-effort-decision.md` 与 `src/voidcode/provider/reasoning_effort.py`，提交 `e60af0b2` 与 `ecc504f4`。该缺口不在原始文档视野内（“当前不建议引入”一节未提及它）。

OMP 仍然显著领先，但领先方式也需要重新描述：它的核心优势不是单纯拥有更多工具，而是把工具协议、真实会话统计、专项 benchmark、模型适配、终端产品和发布反馈连接成了持续优化循环。

## 旧结论复核

| 旧结论 | 当前状态 | 新判断 |
|---|---|---|
| read/edit 缺少结构化输出和 stale 防护 | `read` 已返回行对象、raw content、hash 和 continuation；`edit` 已强制 `expectedHash`，并叠加 read-before-write 与 seen-range 约束 | 已解决；剩余差距转为自动 stale recovery（snapshot chain）与真实任务协议实测 |
| 写入未进入 LSP 闭环 | edit/write 路径已能返回 formatter 和 LSP diagnostics；另有 `apply_workspace_edit` | 已有闭环基线；差距转为覆盖率、rename lifecycle 和任务成功率 |
| context 只有确定性截断 | 已有确定性 projection 与 model-assisted 回退框架（无模型 projector 接入，实际始终回退确定性摘要） | 已落地第一版；差距转为质量评估、token/cost 数据和长会话回归 |
| child result 主要依赖自由文本 | child 必须通过 `submit_result` 形成结构化 handoff；parent 有结果读取与组完成事件 | 已明显推进；差距转为 JSON Schema、隔离 workspace 和合并语义 |
| TUI 只是早期壳层 | 已有真实流式、thinking、工具卡片、question 和后台事件排序 | 仍不及 OMP，但不应继续描述为纯壳层 |
| 最大问题是缺少机制 | 多项机制已经存在 | 最大问题变为缺少可测量的产品反馈循环 |

## 当前能力对比

| 维度 | VoidCode 当前状态 | OMP 当前状态 | 判断 |
|---|---|---|---|
| 架构控制面 | Python runtime 持有会话、策略、审批、事件、恢复、工具、父子关系和客户端真相 | TypeScript/Bun agent harness 与 Rust native core 深度集成 | VoidCode 边界更规整；OMP 产品集成更深 |
| 会话持久化 | SQLite、严格版本契约、checkpoint、bundle、replay、审批与交互队列恢复、terminal seal 与 shutdown drain | JSONL session tree、fork/branch/export/share；terminal seal 和事件 drain 防止结束后复活 | VoidCode 治理强，生命周期竞态处理已补齐；OMP 的 fork/branch/export/share 仍领先 |
| 读取协议 | 结构化 `lines`、`raw_content`、hash、offset/limit、附件和 artifact retrieval | hash-tagged read、seen-range snapshot、内部 URI、多源 read | VoidCode 已实用；OMP 的 edit 联动更强 |
| 编辑协议 | replace/patch/multi-edit、required expected hash（缺失/陈旧即拒绝）、read-before-write、seen-range 约束、near-match、formatter/LSP/tree-sitter 语法 diagnostics、per-model strict/flexible 匹配 | Hashline 默认协议、snapshot chain、seen-range 限制、block anchor、register、stale recovery、语法校验和 veto | OMP 仍明显领先，差距缩至自动 stale recovery 与协议实测 |
| 编辑安全 | hash 已是必填协议不变量（缺失/陈旧拒绝）；workspace edit 可多文件校验；seen-range 与 tree-sitter 语法校验已接入 | 写前解析全部 section，拒绝重叠/no-op/未读范围；最新加入语法验证与边界修复 veto | 差距收窄；剩余为自动 stale recovery、no-op/重叠拒绝与真实任务实测 |
| LSP | definitions/references/symbols/rename/code action、workspace edit、写后 diagnostics 基线 | LSP wired into every write，rename/file lifecycle 与工具展示成熟 | 能力面接近中，稳定性和覆盖率仍有差距 |
| Shell/进程 | shell、interactive shell、后台进程、超时与 artifact output | PTY、持久 shell、Rust builtins、跨平台 process tree、minimizer | OMP 明显领先，尤其 Windows 与长进程体验 |
| 上下文管理 | runtime-owned window policy、稳定 prompt prefix、continuity facts、确定性 projection（model_assisted 仅有回退框架，未接模型 projector）、恢复持久化 | compaction、handoff、checkpoint/rewind、非压缩重试策略、prompt cache 优化 | VoidCode 已有正确骨架；OMP 策略与实战迭代更丰富 |
| 委派执行 | 固定 preset、深度/预算治理、后台任务、通知、取消、重试、结构化 handoff、并行组完成、子会话续跑（task `session_id`）、`voidcode://artifact/<id>` / `voidcode://transcript/<id>` URI | batch fan-out、动态 agent、并发 semaphore、JSON Schema output、隔离 worktree/container、patch/branch、agent/history URI、可复活 agent | VoidCode 治理骨架可靠；JSON Schema output 与隔离 workspace 仍是真实缺口 |
| 模型与 provider | 直连适配、LiteLLM/custom provider、catalog、fallback、cache usage、错误归一化 | 60+ provider/大量模型、OAuth/订阅、角色路由、模型 quirks 和 schema/tool conversion | OMP 大幅领先；VoidCode 不应以数量追赶 |
| 工具质量工程 | 单元、集成、fuzz、契约测试较强；已有 `voidcode stats tools` aggregate-only effectiveness projection（成功率/错误分类/重复读取/token/cache，per-model edit 效果驱动 edit schema 选择）；仍无独立 agent task benchmark | TypeScript edit benchmark、metaharness、session-stats、tool error/token/cost dashboard、read/search/edit 分析脚本 | 差距收窄（aggregate 基线已落地）；真实任务 benchmark 仍缺，但不以 benchmark 为当前优先级 |
| 扩展 | skills、hooks、local tools、MCP、ACP、commands 均有受控边界 | Extension API 可注册 tool/command/provider/renderer/event；插件、marketplace、MCP lifecycle 完整 | OMP 产品成熟度明显更高 |
| Memory | session continuity 为核心；长期 memory 明确后置 | local/Hindsight/Mnemopi，多阶段提取、consolidation、recall/retain/reflect/learn | 战略不同，不应直接追平 |
| 客户端 | CLI + TUI + Web 都接 runtime；Web 强调 review/approval，TUI 已进入真实流式路径 | 强 TUI、one-shot、SDK、RPC、ACP、collab Web | OMP 交互成熟度明显领先 |
| Native/跨平台 | Python 与外部系统工具，开发维护成本低 | 约 80k Rust native 层，覆盖 shell/search/media/desktop 等 | OMP 性能与 Windows 更强，复制成本过高 |

## OMP 当前真正值得学习的机制

### 1. 工具质量反馈循环，而不只是 benchmark 命令

OMP 仓库里同时存在三层反馈：

- `packages/typescript-edit-benchmark/`：从真实 TypeScript 源码制造 identifier、mutation、structural edit 任务，并验证最终文件；
- `scripts/session-stats/`：从历史 session 分析工具调用、edit follow-up、重复读取、search relevance、token residency 等；
- `packages/stats/`：展示每个工具的调用次数、错误率、参数/结果大小、模型分布、token 和成本。

这三层分别回答：

1. 固定任务是否退化；
2. 真实用户会话在哪里浪费或失败；
3. 改动是否在版本和模型维度产生长期收益。

VoidCode 当前测试主要证明契约正确、恢复可靠和边界安全；`voidcode stats tools` 已提供 aggregate-only 的工具成功率/错误分类/重复读取/token/cache 投影，并据此按模型选择 edit schema。但还不能回答“哪个模型在真实改码任务上成功率更高”“compaction 后完成率是否下降”这类 benchmark 级问题。这是最值得优先补齐的机制。

### 2. 编辑协议不变量与可恢复失败

VoidCode 已把 hash 从可选字段升级为协议不变量：`edit`/`multi_edit`/`write`（覆盖已有文件）/`apply_patch`（每个受影响已有文件）/`apply_workspace_edit`（每个 edit）都强制 `expectedHash`，缺失报 `missing_expected_hash`、陈旧报 `stale_edit`，并叠加 read-before-write 与 seen-range 约束；写后同时跑 formatter/LSP 与 tree-sitter 语法 diagnostics，失败返回结构化 error_kind/reason/retry_guidance/details。OMP Hashline 把以下规则变成协议本身：

- 只能基于最近 read/grep/edit 暴露的 snapshot tag 修改；
- 未展示范围不可修改；
- stale anchor 只有在 snapshot chain 能证明唯一安全结果时才恢复；
- 重叠、越界、no-op、错误 block anchor 直接拒绝；
- patch 应用前进行语法验证，边界自动修复也受 parse/veto 检查约束；
- 失败结果包含下一次调用可直接使用的恢复信息。

VoidCode 不需要复制 Hashline 语法，但应借鉴其“不变量优先”和“失败可恢复”设计。

### 3. 子任务的隔离产物协议

VoidCode 已解决 child lineage、生命周期、通知和 handoff；当前短板是 worker 是否能安全产出可合并变更。OMP 的关键机制包括：

- 每个 task 可声明 JSON Schema 输出；
- session-scoped 并发上限；
- 可选隔离 workspace/worktree/container；
- 完成后返回 patch、branch、artifact 和 transcript URI；
- parent 读取结构化结果，不依赖复制完整 transcript；
- finished agent 可 parked/revive，isolated agent 则明确不可恢复。

值得学习的是产物协议和隔离语义，不是开放任意 agent 拓扑。

### 4. 终态封口与异步 drain

OMP 最近的 session/agent 修复集中在 terminal seal、in-flight disk/event drain 和 parked agent disposal：会话一旦终止，迟到事件不能重新激活状态；释放内存前必须完成需要持久化的工作。

VoidCode 已实现 terminal seal 与 shutdown drain：存储层通过 `SessionSealedError` / `_assert_terminal_session_events_allowed` 拒绝终态会话上的迟到非 lifecycle 事件（`storage.py`），runtime 层通过 `_sealed_session_status` 把 `interrupted`（无活跃 run）一并封口并门控 interaction queue（`service.py`）；后台任务对封口父会话的迟到通知直接丢弃（`background_tasks.py`）；bundle 导入的终态会话同样封口（`bundle.py`）。`tests/unit/runtime/test_session_lifecycle_seal.py` 与 `test_session_bundle.py` 已覆盖以下竞态并验证通过：

- cancel 与 tool result 同时到达（迟到 tool result 被丢弃，`test_cancel_lands_while_tool_result_in_flight_drops_late_result`）；
- terminal event 后迟到的 provider/tool delta（`test_cancel_mid_provider_stream_drops_remaining_deltas`）；
- parent 结束时 child completion 正在入库（`test_child_background_completion_cannot_mutate_sealed_parent`）；
- approval/steer 交错（`test_steer_landing_after_approval_resolution_is_rejected` 等）；
- 进程退出时后台结果与 checkpoint 的 drain 顺序（`test_runtime_shutdown_drains_background_worker_results_before_teardown`）与 bundle/replay 封口往返（`test_session_bundle_import_seals_terminal_session_against_late_events`）。

仍待专门验证的剩余竞态：compaction × resume 在真实 provider 长会话中的交错，以及进程崩溃（非正常 shutdown）场景下的 drain 完整性。

这类机制与 VoidCode 的 runtime 治理优势高度一致，优先级高于新增工具。

### 5. reasoning effort / thinking level 语义

OMP 的 thinking-level 模型：`Effort` 枚举 `minimal|low|medium|high|xhigh|max`，另加 `off`/`inherit`/`auto`，默认 `high`。按 provider 映射：OpenAI 为 `reasoning_effort` identity passthrough；Anthropic 走 adaptive 的 `output_config.effort`，旧模型回退 `thinking.budget_tokens`；Google 走 `thinkingLevel`，xhigh/max 折叠为 HIGH。模型不支持时 clamp 到最近的受支持值，并在收到 400/422 时以调整后的 effort 自动重试。来源：`packages/catalog/src/effort.ts`、`packages/catalog/src/model-thinking.ts`、`packages/ai/src/providers/openai-shared.ts`、`packages/ai/src/providers/anthropic.ts`、`packages/ai/src/providers/openai-reasoning-fallback.ts`（OMP HEAD `ad318c7572abaeebd5cf8a7a16d350ff1d32a738`）。

对 VoidCode 的 provider 层可直接复用的是 LiteLLM 各 provider 的 `reasoning_effort` 行为表：

| Provider | `reasoning_effort` 行为 |
|---|---|
| OpenAI | 原生支持，取值 none/minimal/low/medium/high/xhigh |
| Anthropic | 原生支持，映射到 thinking `budget_tokens`；Claude 4.6+ 走 adaptive 与 `output_config.effort` |
| Google / Gemini | 原生支持，映射到 thinking budget/level |
| xAI/Grok、Groq、Fireworks | 原生 passthrough，受 supports_reasoning 开关约束 |
| DeepSeek | 原生 `thinking`，none→disabled，其余→enabled |
| GLM / zai | 静默丢弃：不在 supported params，只接受 `thinking={"type":"enabled"}`，需走 `extra_body` |
| Kimi / moonshot、Qwen | 静默丢弃：不在 supported params |

## 推荐实施顺序

### P1：用数据强化编辑协议

以下各项均已落地，不再作为待办：

1. ~~对已读取文件默认要求 hash~~ → 已完成且无兼容期：`edit`/`multi_edit`/`write`（覆盖已有文件）/`apply_patch`/`apply_workspace_edit` 均强制 `expectedHash`，缺失或陈旧直接拒绝；
2. ~~记录并限制模型可编辑的已展示范围~~ → 已完成：read-before-write 与 seen-range（`enforce_seen_lines` / `enforce_seen_whole_file`）在全部写入工具上生效；
3. ~~为 apply_patch/edit 统一 stale、ambiguous、no-op 和 parse-error taxonomy~~ → 部分完成：`stale_edit`、`missing_expected_hash`、`ambiguous_match` 与 tree-sitter parse diagnostics 已统一为结构化 diagnostic；no-op / 重叠拒绝仍是 OMP 特有；
4. ~~对支持 tree-sitter 的语言增加可选语法验证~~ → 已完成：tree-sitter 语法诊断已接入 `edit` / `apply_patch` 写后路径（`VOIDCODE_SYNTAX_VALIDATION=0` 可关闭）；
5. ~~让失败结果返回稳定的 recovery payload~~ → 已完成：诊断包含结构化 `error_kind` / `reason` / `retry_guidance` / `details`；
6. ~~根据不同模型的表现选择 edit schema~~ → 已完成：`EditSchemaResolver` 依据 effectiveness 报告中的 per-model edit 表现选择 strict / flexible 匹配。

仍保留的差距：基于 snapshot chain 的自动 stale recovery（当前要求模型重读后重试）、no-op/重叠拒绝、以及用真实任务 benchmark 对 edit schema 成功率做协议实测。

### P1：验证终态与恢复竞态

以下各项均已落地（详见上文“终态封口与异步 drain”一节及 `tests/unit/runtime/test_session_lifecycle_seal.py`）：

1. ~~增加 terminal seal，终态后拒绝改变会话真相的迟到事件~~ → 已完成；
2. ~~明确 shutdown/dispose 前必须 drain 的事件、存储和 child result~~ → 已完成（`BackgroundTaskManager.shutdown` 强制 drain 顺序）；
3. ~~为 cancel/tool-result、approval/steer、parent/child completion 建立并发测试~~ → 已完成；
4. ~~把这些场景纳入 session bundle/replay 验证~~ → 已完成。

仍保留的差距：compaction × resume 在真实 provider 长会话中的交错回归，以及进程崩溃（非正常 shutdown）场景下的 drain 完整性验证。

### P2：补齐隔离 child 产物闭环

1. task 支持 invocation-level JSON Schema output —— 仍未落地（`task` 工具的 input_schema 不含 output schema），保持为真实缺口；
2. worker 支持只读或隔离 worktree —— 仍未落地，保持为真实缺口；
3. 结果返回 patch/artifact/verification 元数据 —— 部分落地：`submit_result` 已返回结构化 `files_touched` / `verification` / `completed_work` / `blockers`，且 `voidcode://artifact/<id>` 与 `voidcode://transcript/<id>` URI 已可被 parent 消费；patch/branch 级产物与合并语义仍缺；
4. parent 通过 runtime API 合并或拒绝，不让 child 直接绕过治理 —— 仍未落地（parent 只能读取后台任务结果），保持为真实缺口；
5. 用真实任务验证并行执行在哪些场景真正更优 —— 仍未落地。

### P2：客户端与首次使用

- TUI/Web 展示 tool error taxonomy、context pressure、token/cost 和恢复状态 —— 部分落地：Web composer 已展示 context usage（token/cost）与 runtime status；tool error taxonomy、TUI 对等展示仍缺；
- doctor 提供 provider 登录、模型校验和自动修复路径 —— 已完成：`voidcode doctor` 输出结构化 capability report 与 `first_task_readiness` next step；
- diff、approval、question 和 child result 保持跨客户端语义一致 —— 继续推进；
- 先优化首个真实改码任务，不追求 OMP 全部客户端形态。

## 当前不建议引入

- 大规模 Rust native core；
- 以 provider 或模型数量为 KPI；
- DAP、桌面控制、Slack、TTS、图片生成等长尾工具；
- OMP 的全部 memory backend；
- 插件 marketplace；
- 云协作 relay；
- 任意 agent topology 或直接 agent-to-agent bus。

这些能力在 OMP 中有真实价值，但不能优先解决 VoidCode 当前最关键的问题：已有机制是否真的提高了编码任务成功率。

## 最终判断

VoidCode 与 OMP 的差距已经从“运行时骨架对成熟产品”缩小为“治理能力较强但缺少实战优化循环的产品，对一个经过大量真实使用数据打磨的成熟 harness”。

VoidCode 不应复制 OMP 的功能数量。更合适的路线是：

> 保留 SQLite session truth、严格恢复、审批和跨客户端一致性，继续用真实使用中的反馈强化编辑协议、终态竞态和隔离子任务产物。

## 主要证据索引

### VoidCode

- `src/voidcode/tools/read.py`
- `src/voidcode/tools/edit.py`
- `src/voidcode/tools/apply_workspace_edit.py`
- `src/voidcode/runtime/context_projection.py`
- `src/voidcode/runtime/context_window.py`
- `src/voidcode/runtime/interaction_queue.py`
- `src/voidcode/runtime/tool_replay.py`
- `src/voidcode/tools/task.py`
- `src/voidcode/tools/submit_result.py`
- `docs/contracts/background-task-delegation.md`
- `tests/unit/` 与 `tests/integration/`

### Oh My Pi

- `packages/hashline/`
- `packages/coding-agent/src/edit/hashline/`
- `packages/coding-agent/src/task/`
- `packages/coding-agent/src/session/`
- `packages/typescript-edit-benchmark/`
- `packages/metaharness/`
- `packages/stats/`
- `scripts/session-stats/`
- `docs/tools/edit.md`
- `docs/tools/task.md`
- `docs/compaction.md`
- `docs/handoff-generation-pipeline.md`
- `docs/extensions.md`
- `docs/memory.md`
