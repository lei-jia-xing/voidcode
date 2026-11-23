# TUI TODO

## 目标

TUI 是服务端事件流之上的内联终端客户端：不持有业务真相，不维护客户端命令表，所见即持久事件的投影。

- **看得见**：任何时刻都能看出 Agent 在做什么、在等什么、已花多少——运行、工具、子 Agent、排队追问、审批、上下文与用量。
- **控得住**：任何时刻都能中断、决策、追问，草稿永不丢失。
- **失败留痕**：错误原因写进滚动历史，而不是一闪而过的状态行；不可用就明说，不静默降级。
- **找得回**：会话可辨认、输入历史跨重启可用，二者都来自持久事件而非客户端缓存。
- **终端可靠**：resize、滚动历史、多行输入、浮层、图片下光标与布局始终正确；模型/插件文本中的控制字符永不到达终端。
- **不越界**：工作区访问走插件与查询契约，嵌入/远程差异由客户端能力声明表达，不靠 `isinstance` 判断。
- **可维护**：小模块、强类型、渲染为可单测的纯函数。

非目标：全屏 alt-screen 应用；客户端命令表或点号斜杠别名；fork/undo/回退；客户端自行缓存的审批策略。

## P0：可见性与失败留痕

- [ ] **让后台子 Agent 状态可见，沿现有 monitor 进入跟进与结果。**
  - 按服务端真实投影显示运行/审批/排队/失败及最近活动，并可发现地进入现有 monitor。
  - live 区每个子任务一行：状态、最近活动、已用时间；不再只有 `running N tools`。
  - 依赖：`plugins/TODO.md` 子 Agent 状态投影。
- [x] **运行失败原因进入 transcript。**
  - `content_rows` 与 `CoreApp` 现复用持久 `TranscriptRun` 的 `error` / `reason` / `stop_cause`，与实时 `run_notice_rows` 保持一致，不再只显示泛化的 `Error: Run failed` / `Interrupted`。
- [x] **排队追问可见、可撤回。**
  - composer 上方实时显示服务端排队的追问列表，支持 `Alt+Up` 在认领前按 `expected_revision` 原子撤回并还原回输入框编辑（服务端事件留痕，无客户端假队列）。
- [x] **运行状态带时间与并行明细。**
  - 边框状态在运行及并行工具期间显示已用时间并列出并行工具名（最多 3 个及 `+N`）。
  - 上下文占用接近窗口时页脚变色提醒（`>=80%` warning 黄色，`>=95%` error 亮红）。

## P1：交互完整性

- [ ] **审批门支持会话级放行与带理由拒绝。**
  - 目前只有 `allow once` / `deny`；“本会话内总是允许该工具/路径”必须由 Core 策略契约承载，客户端不缓存策略。
  - 拒绝可附一句理由回传模型，避免原样重试。
  - 依赖：`core/TODO.md` 会话级审批策略、带理由拒绝。
- [ ] **接通模型交互式问答（`ask`）浮层。**
  - `tui/gates.py` 已定义 `AskGate`/`AskRequested`，但 `stream_handlers.py` 与 `terminal_app.py` 均未处理 `core.ask_requested` 与 `RunResult.pending_ask`。
  - 依赖：`sdk/TODO.md` `SessionClient` 问答方法。
- [ ] **支持在 Composer 中附加/粘贴图片（`ImageAttachment`）。**
  - `RunSpec.images` 与 `rendering.py` 的用户消息图片徽标已就绪，但 TUI 缺少剪贴板读图与文件路径附加入口。
- [ ] **修复流式思考输出丢失与落盘思考 trace 折叠。**
  - `_live_stream_rows` 仅读取 `_stream_buffers`，忽略 `_stream_reasoning`；而落盘后 `render_thinking` 全文展开且不可折叠。
- [ ] **会话列表可辨认。**
  - `_present_sessions` 只显示 `id[:8] (turn N, status)`；补充首条用户消息摘要与相对更新时间。
  - 依赖：`core/TODO.md` 会话摘要字段。
- [ ] **输入历史跨重启并可搜索。**
  - 当前历史仅驻内存（100 条）；从会话持久的用户消息事件恢复，并提供 Ctrl+R 反向搜索。
  - 依赖：`core/TODO.md` 用户输入历史分页读取。
- [ ] **`@path` 补全走工作区查询契约。**
  - `_path_candidates` 在 UI 线程逐键同步 `iterdir()` 本地文件系统，绕过 files 插件（插件禁用时仍可列文件），远程模式下以 `cwd`/服务端路径在本地解析。
  - 改为通过 `workspace_queries` 询问服务端，异步返回；支持递归/模糊匹配。
  - 依赖：`plugins/TODO.md` 工作区路径补全查询。

## P2：终端鲁棒性

- [ ] **以真实终端验证 resize、滚动历史和光标重锚定。**
  - 对流式输出、多行输入、浮层及图片验证宽高变化后的重定位。
- [ ] **修复客户端参数透传、`session.ack` 容错、Diff 展示与 `NO_COLOR` 判定。**
  - `CoreRpcClient.run`/`resume` 与 `CoreLocalClient.run`/`resume` 均未转发 `spec.interaction`；`CoreRpcClient.events_stream` 发送 `session.ack` 未捕获连接关闭竞态异常。
  - `_detect_color_system` 对 `NO_COLOR=""` 空值也禁用颜色；Diff 卡片缺少行号，且 `write` 覆盖文件无 Diff（依赖 `plugins/TODO.md` `WriteResultBody` 返回 diff）。

## P2：结构与类型收敛

- [ ] **拆分 `CoreTuiApp`（约 2200 行、约 50 个状态字段）。**
  - `active_overlay`、`pending_approval`、`queued_confirmations`、`_suspended_live` 合并为一个类型明确的 overlay 栈，消除互相同步的分支。
  - 斜杠/路径补全（`_completion_candidates`、`_sync_slash_completion`、`_pending_path_*`）收拢为独立补全控制器。
  - `_render_composer` 的 live 区布局抽为纯函数并单测。
- [ ] **消除 `Any` 与宽松 dict。**
  - `_handle_composer_key(key: Any)`、`_outcome(outcome: Any)`、`_live_input: dict[str, Any]`、`_suspended_live: tuple[Any, ...]` 改用 `Key`、`ComposerOutcome` 与冻结 dataclass；继续收敛 `views.py`/`artifact_viewer.py` 中 `handle_key -> dict[str, Any]` 及 `InteractiveForm.values: dict[str, Any]`。
- [ ] **以客户端能力声明替代 `isinstance(CoreLocalClient)`。**
  - `footer_context` 的函数内导入与类型判断、`/tui model`·`/tui thinking` 的 `embedded_only` 统一为客户端声明的能力。
  - 依赖：`sdk/TODO.md` 客户端能力声明。
- [ ] **删除重复与魔法数字。**
  - `_BUILTIN_BANNER` 在 `terminal_app.py` 与 `rendering.py` 各有一份；`rendering.py` 退出提示硬编码 `1.5`，应复用 `_QUIT_ARM_SECONDS`。

## 跨模块

- [ ] **外层产品层问题已并入 `src/voidcode/TODO.md`（含 `bootstrap.py`、`config.py`、`cli.py` 配置多重投影与重复解析）。**
