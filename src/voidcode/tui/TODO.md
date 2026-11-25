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

- [x] **让后台子 Agent 状态可见，沿现有 monitor 进入跟进与结果。**
  - 通过通用 `PluginViewDescriptor(live=True, live_action=...)` 契约在输入框上方实时投影活跃子任务（每行显示标签、状态 `running`/`approval`/`queued`/`failed`/`interrupted`、最近活动、已用时间），并提示 `Alt+A monitor` 入口进入现有 monitor 交互（无活跃/等待/排队子任务时自动收起，TUI 与 `subagents` 插件零硬编码耦合）。
  - 依赖：`plugins/TODO.md` 子 Agent 状态投影（已完成）。
- [x] **运行失败原因进入 transcript。**
  - `content_rows` 与 `CoreApp` 现复用持久 `TranscriptRun` 的 `error` / `reason` / `stop_cause`，与实时 `run_notice_rows` 保持一致，不再只显示泛化的 `Error: Run failed` / `Interrupted`。
- [x] **排队追问可见、可撤回。**
  - composer 上方实时显示服务端排队的追问列表，支持 `Alt+Up` 在认领前按 `expected_revision` 原子撤回并还原回输入框编辑（服务端事件留痕，无客户端假队列）。
- [x] **运行状态带时间与并行明细。**
  - 边框状态在运行及并行工具期间显示已用时间并列出并行工具名（最多 3 个及 `+N`）。
  - 上下文占用接近窗口时页脚变色提醒（`>=80%` warning 黄色，`>=95%` error 亮红）。

## P1：交互完整性

- [x] **审批门支持会话级放行与带理由拒绝。**
  - TUI `InteractiveApproval` 现提供 `[a]` allow once, `[s]` allow for session, `[d]` deny, `[r]` deny with reason（带理由拒绝文字输入），并调用带 `grant_session_policy` 和 `reason` 的 `client.resume`。
- [x] **接通模型交互式问答（`ask`）浮层。**
  - `InteractiveAsk` 浮层完整支持表单文本输入与多选项切换（Space/x 切换，Enter 提交，Esc 取消），`stream_handlers.py` 与 `terminal_app.py` 完整接通 `core.ask_requested`、`pending_ask` 以及 `answer_ask` / `cancel_ask` 客户端方法。
- [ ] **支持在 Composer 中附加/粘贴图片（`ImageAttachment`）。**
  - `RunSpec.images` 与 `rendering.py` 的用户消息图片徽标已就绪，但 TUI 缺少剪贴板读图与文件路径附加入口。
- [x] **修复流式思考输出丢失与落盘思考 trace 折叠。**
  - `_live_stream_rows` 现将 `_stream_reasoning` 内容实时展示为 `Thinking… (N chars)`；落盘后 `render_thinking` 默认以 `💭 Thought (N chars) · <preview>` 紧凑单行折叠展示，避免输出刷屏。
- [x] **会话列表可辨认。**
  - `_present_sessions` 现展示格式为 `{session_id[:8]} · turn {turn} · {summary or status}`，支持从首条用户消息持久投影会话主题。
- [x] **输入历史跨重启可用。**
  - TUI 启动与会话切换时从持久事件流加载历史输入至 composer，实现跨会话重启持久还原（Ctrl+R 搜索交互保持后续讨论）。
- [x] **`@path` 补全走工作区查询契约。**
  - files 插件注册 `workspace.path_completion`（前缀/模糊、递归深度、忽略规则、确定排序、结果与字节上限、frozen 请求/结果）；插件禁用即 `QueryUnavailable`，TUI 不再读本地文件系统。
  - TUI 经 `workspace.complete_paths` 异步请求（本地与 RPC 同一契约），同一时刻仅一个在途请求 + 最新待发前缀；只有 serial、会话/代次、草稿与光标都匹配的回复才更新弹窗，旧回复/取消/切会话均丢弃，草稿与光标不被回复修改。不可用/契约失败在 transcript 明示一次。
  - 远程补全路径始终相对服务端工作区；`@` 编辑语义保持不变。

## P2：终端鲁棒性

- [ ] **以真实终端验证 resize、滚动历史和光标重锚定。**
  - 对流式输出、多行输入、浮层及图片验证宽高变化后的重定位。
- [x] **修复客户端参数透传、`session.ack` 容错、Diff 展示与 `NO_COLOR` 判定。**
  - `CoreRpcClient.run`/`CoreLocalClient.run` 现转发 `RunSpec.interaction`（`resume` 原本已转发）；`session.ack` 仅忽略 `ConnectionClosed` 连接关闭竞态，其他发送失败继续上抛。
  - `_detect_color_system` 以变量存在与否判定 `NO_COLOR`，空串同样禁用颜色；Diff 卡片与 `DiffViewData` 预览显示有界双列旧/新行号，`write` 覆盖 diff（旧文件 ≤64 KiB）与 `edit` 共用同一形状分发渲染。

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
