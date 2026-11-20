# TODO

- [x] 中段 slash 补全只替换光标处 token，保留前后草稿和已有参数，不误提交；当前 `Composer._accept_completion` 整体替换文本，`CoreTuiApp._completion_candidates` 只在光标末尾给候选。
- [x] 为 composer 增加 workspace 内 `@path`/Tab 路径建议；接受后只插入路径文本，不隐式读取或附加文件，并保留光标两侧内容。参考 OMP [`@` 搜索、路径补全与 token 替换](https://github.com/can1357/oh-my-pi/blob/45c35e40c46166dc726ccc52f5d27f40f2449f99/packages/tui/src/autocomplete.ts#L751-L915)。
- [x] `/tui sessions` 提供可选列表而非只打印 ID，切换仍沿用 `_switch_session` 的 generation 与已提交游标保护。参考 OMP [session selector](https://github.com/can1357/oh-my-pi/blob/45c35e40c46166dc726ccc52f5d27f40f2449f99/packages/coding-agent/src/modes/controllers/selector-controller.ts#L1768-L1919)。
- [x] `_footer_context` 分开显示当前 dispatch context/window 与累计 session usage；不拿累计 token 伪装上下文占用，实测不足时保持未知，40 列下保留模型信息。参考 OMP [当前 context 与 token segments](https://github.com/can1357/oh-my-pi/blob/45c35e40c46166dc726ccc52f5d27f40f2449f99/packages/tui/src/status-line/segments.ts#L738-L895)。
- [x] 为工具结果提供只读完整数据入口；`TextOutput.text` 保持所选模型页面，外置 body/长内容经 canonical result reference 与 artifact page viewer 读取，不重写 append-only scrollback。参考 OMP [tool 输出展开输入](omp://tools/eval.md)，仅借鉴可发现性，不复制历史回放语义。

## 新一轮 OMP 对照

- 上述已完成项不重开。新增条目的对照证据口径及“不要到处添加 MAX”原则见[总待办](../../../docs/TODO.md)；SDK 缺口见 [SDK TODO](../sdk/TODO.md)，工具链整改见[插件 TODO](../plugins/TODO.md)。未勾选项仍需按各自验收观察；已完成的图片查看与实时进度路径，其实际终端/浏览器证据及限制分别记在下方。

### P0：图片与运行中的工作可见

- [x] **显示真实图片附件，并按终端能力提供查看入口。**
  - 实现：TUI 按不可变 artifact URI 分页获取完整原件；Kitty 只上传经完整分页及内容摘要验证的原始 PNG，并等待终端 decode acknowledgement。主机侧 IHDR 仅用于尺寸/布局，不作为完整 PNG 验证。resize/reopen 复用稳定 image identity。普通图片读取不再有 50 KiB 拒绝。
  - 当前支持范围：Kitty Graphics 与显式 external-open fallback；不实现或宣称 Sixel、JPEG/WebP/GIF 终端预览。`read_artifact` 分页、源引用、页错误与会话/revision 边界沿用 SDK 客户端契约。
  - 边界：终端控制序列只能由可信 renderer 产生，不能直接透传工具正文；稳定图像身份与缓存避免每帧重发整段 base64，resize 不擦除已提交历史。保留真实解码/图像内存保护，不再拿文本预览预算充当图片协议上限。
  - 证据与限制：真实 CLI/PTY Kitty 协议测试 6 passed/17.70s，覆盖 346,073-byte、384×300 的真实解码 PNG、分页/reflow/reopen、坏图错误；不支持终端的 explicit open/export 权限 0600 与清理通过。PTY 是 headless 协议 peer，不证明物理 Kitty 屏幕像素；Chromium 截图只证明 Web viewer。

- [x] **将活跃工具留在 Live 区域，消费已有工具进度，结束后只提交一次结果。**
  - 进展：`terminal_app.py` 移除了 `TOOL_INTENT` 过早提交 scrollback 的行为与 `_tool_headers` 去重 shim；活动工具按 `(session_id, run_id, invocation_id, generation)` 保持在 Live 区域（`_active_tools`），消费 Core `TOOL_PROGRESS` 事件。
  - 视窗优先布局：根据 composer/popup/footer 等需求计算可用视口，活动工具在 Live 区域按视口布局；超出部分由 overflow summary 表示，不承诺所有详细标头同时可见。可见工具保留标头（spinner、glyph、名称、耗时），剩余行分配给最新工具的可见尾部；窄屏（10 行）下 composer 输入和工具名均不被截断。
  - 增量分块缓冲区：`ActiveToolProgress.append_chunk` 维护未完结片段，正确拼合跨 chunk 的断行与 Unicode；可见尾部按当前视口/行列预算派生，不以不透明固定 MAX 裁剪。
  - 单次结算与隔离：`TranscriptTool` 到达时将标头与结果作为一个完整块提交到 append-only scrollback 恰好一次，并从 Live 区域移除；旧 run/错误会话的进度被严格拒绝，run 结束仅清理匹配该 run 的工具。
  - 已有单元证据：8 个进度行为回归与 2 个 Kitty 图标测试通过，覆盖无提前提交、分块/Unicode、窄屏、多工具、身份隔离、单次结算与 run 清理。
  - 真实 PTY/Core 烟测：真实 CLI、Core 与 loopback provider 在 60×10 PTY 中运行；socket-gated `shell_exec` 在释放前显示工具身份及 stdout/stderr marker，运行中输入的 draft 在完成后保留。释放后屏幕显示最终 provider 响应及 artifact 摘要；持久 ledger 恰有 1 个 tool intent 与 1 个 completion，`Core.read_artifact` 取回 stdout/stderr 完整字节，draft 未形成额外 provider 请求。一次性 smoke script 已删除。
  - 限制：烟测验证正常完成，不宣称此次实际 PTY 覆盖取消/重连；旧 run/session/revision 隔离仍由现有行为回归覆盖。

### P1：结果有语义，后台 Agent 可发现

- [x] **用语义结果卡呈现 diff、表格、结构化数据和完整资源入口。**
  - 实现：`views.py` 新增 `_render_diff_body`，仅按 body 的 `path`/`diff`/`additions`/`deletions` 严格形状分派（类型严格、bool-safe），不按工具名猜格式、不导入具体插件；成功路径接入通用卡，失败/残缺形状保持纯输出渲染。hunk 按 `+`/`-`/`@@`/上下文逐行复用 `DiffViewData` 着色槽位渲染，50-hunk 大 diff 全部保留（不截断、不新增上限），宽度沿现有 `render_lines` 视窗派生；diff 文本转义终端控制序列，不污染 scrollback。
  - 完整数据：完整结构化数据仍走既有 `result_reference`/artifact 条目（`Artifacts: Ctrl+O / /tui output`），不建第二份全文；表格/JSON/media 沿用既有 TSV/预览 + URI + `artifact_sources` 呈现，并由新测试锁定层次、引用与 CLI fallback 字符串。
  - 边界：多文件 body（`apply_workspace_edit`/ast_grep 的 files 列表）与无 stats 的单 diff 仍为纯输出渲染，不属本次范围；本次只覆盖带 stats 的单文件形状（`edit_tool` 焦点）。
  - 证据：新 `tests/unit/tui/test_diff_cards.py` 12 passed（文件+增删统计+hunk、输出/artifact 顺序、失败/残缺回退、控制字符转义、工具名无关、50-hunk 全保留与资源身份、表格/JSON 层次与引用、media CLI fallback、transcript harness）；`tests/unit/tui/` 全量 134 passed + 1 skipped；`ruff check`/`ruff format --check`/`ty check`（0.0.104）全绿。

- [ ] **让后台子 Agent 状态可见，沿现有 monitor 进入跟进与结果。**
  - 现状：已有 alt+a monitor 和 SDK `DescendantNode.activity` 等状态；不缺新的 task console，缺的是主会话工作面中的后台活动提示与结果入口。
  - 目标：按服务端真实投影显示运行/审批/排队/失败及最近活动，并可发现地进入现有 monitor；长 label/activity 保留原值，在视窗边界折叠而不是插件先截短。资源入口、首个就绪等待与状态数据依赖 SDK/插件待办，不用客户端窥探私有状态。参考 OMP [Agent Hub / pinned jump list](omp://agent-hub.md)。
  - 验收：用户留在主会话即可知道哪些 child 在工作、等待谁、哪些结果可读；切换/重连不混淆旧 run，不伪造百分比/ETA，不反复读取完整子会话历史刷新指示器，取消仍绑定所见 run 并经过现有确认。

### P2：输入与视窗稳态

- [x] **长粘贴只折叠展示，不截断或替换真实输入。**
  - 现状：[`composer.py`](composer.py) 的 paste 直接 `_insert(key.text)`；尚未有将粘贴块与其显示徽标分开的编辑模型。源码路径需用真实大文本观察，不把性能问题当作已复现事实。
  - 目标：多行粘贴可显示可展开/编辑/删除的紧凑块，提交时还原原文；按视窗选择折叠，不增加 PASTE_MAX。保留草稿、光标和已接收 follow-up/未接收原稿的现有区别。参考 OMP [composer 与 viewport](omp://tui-core-renderer.md)。
  - 验收：在当前入口支持的请求规模内粘贴长堆栈、中英文和多段文本，操作期间不会把 composer 撑出可用区域，复制/编辑/提交与原文一致；超过真实入口能力时明确说明或走全文资源方案，不能静默丢尾部、提交徽标文本或误重送。
  - 实现：`composer.py` 新增纯 `_PasteBlock` span 模型（exact true-text offsets + collapsed 标志），badge 由块原文单行派生（`_badge_for_text`），仅当多行粘贴高于可见输入高度时折叠（`_should_fold_paste`，按视窗派生，不新增 PASTE_MAX），display/true 双向光标映射（badge 内 snap 到边）；submit/copy/edit 一律走原文精确字节（alt+e 展开、alt+c 复制块、alt+x 删除块）。`terminal_app.py` 接入真实入口能力：enter 提交前 oversize 预检（`MAX_PROMPT_CHARS`/`MAX_PROMPT_BYTES`，超限 draft 保留、status 明确说明、不发送），`ComposerAction.COPY` 经 `copy_to_clipboard`（OSC52 上限）接线，失败/超限均有明确 status。
  - 证据与限制：新 `tests/unit/tui/test_paste_folding.py` 18 passed（块模型/badge/视窗折叠/双向光标映射/精确字节提交/复制/编辑/删除、超限预检与 COPY 接线）；`tests/unit/tui/` 全量 152 passed + 1 skipped；full unit 2010 passed + 1 skipped；`ruff check`/`ruff format --check`/`ty check` 全绿。unit-only，无 PTY/物理终端证明。

- [ ] **以真实终端验证 resize、滚动历史和光标重锚定。**
  - 现状：[`term.py`](term.py) 根据 `_painted_cursor` 的缓存相对位置回到 Live origin，resize 后会失效几何缓存；是否存在真实漂移必须实测，不能仅凭这段源码宣布已有擦历史 bug。
  - 目标：对流式输出、多行输入、浮层及图片验证宽高变化后的重定位；确有漂移才评估 CPR/DSR 等真实坐标反馈，并正确区分探测应答和用户按键。复用已有私有模式探测与恢复，不盲加探针/重试 MAX。参考 OMP [resize 的 preserve 模式](omp://tui-core-renderer.md)。
  - 验收：真实终端缩放/分屏、图形能力受限或无探测响应时，composer 可用、光标正确、旧历史不被擦除/复制。只借鉴 viewport 稳态；不引入 OMP rebuild、ED3 清历史或历史 replay。

结构化 `ask` 的两端表单闭环由 SDK/插件 TODO 统一验收，TUI 复用已有 form/choices，不再建设第二套问答或审批 UI。

## 清算：Outer / Transport / TUI 架构与代码质量问题（P0，零向后兼容）

- [ ] **清理 `bootstrap.py`、`config.py`、`cli.py` 的配置三重投影、死兼容分支与重复解析。**
  - 证据（审计基线；已落地项见“部分”）：
    1. `bootstrap.py:140-194` `admit_live` 将同一份 `model/policy/plugins` 同时投影成 `snapshot` dict、`payload` dict 和 `ExecutionConfig` 三份结构，并对每个 plugin config 反复拷贝塞入 `providers`；`restore_admission`（`:224-244`）先把 `payload` 和 `snapshot` 各自 `json_wire_object` + `canonical_json` 哈希比对，再调 `admit_live`，再用 `replace(...)` 覆盖回原字段。
    2. `bootstrap.py:196-222` `config_from_snapshot` 保留 `version != 2` 与注释 `old snapshots without sandbox` 的向后兼容分支，把字典重新拼回 `{"schema_version": 1, ...}` 再调一次 `parse_config`。
    3. `bootstrap.py:411` `open_core` 先调用 `_assert_workspace_free` 获取并立刻释放一次 `flock`，随后 `Core.__init__` 又锁一次（TOCTOU 仪式）；`:429` 在已有 `admission` 的情况下又重新 `config_from_snapshot(load_private_snapshot(...))` 读盘解析一次传给 `sandbox_backend_for`；`locate_sandbox_helper`（`:302-328`）在生产代码中硬编码搜索 cargo 开发目录 `native/sandbox-adapter/target/{debug,release}`。
    4. `cli.py:74-102` `_normalized_argv` 手写 30 行 argv 扫描重排以支持子命令后置 `--workspace`；`_tui_prefs` 已改用 `config.load_config`（不再二次解析/静默吞）；`_show`（`:335-401`）仍在 CLI 层直接手写 raw SQL 查 `sessions`/`checkpoints`/`session_events`，与 `Core.show` 及 `transport/projection.py` 重复。
  - 整改：删除 `config_from_snapshot` 的旧版本兼容分支、重复读盘解析和 `_assert_workspace_free` 空锁；`Admission` 直接持有强类型 `Config`；CLI 使用标准 `argparse` 层级并复用统一只读查询入口，禁止静默吞配置异常。
  - 部分：`version != 2`/`old snapshots` 分支、`_assert_workspace_free` 空锁、cargo 硬编码路径、`cli` 静默吞配置异常与手写 raw SQL 已删；`ExecutionConfig` 改由 `_execution_config` 单次构建。保留（已接受）：snapshot/payload 双字典（凭据边界：私有快照 vs 去敏载荷，`admit_live` 已注释）。已落地（2026-11-21）：`cli._normalized_argv` 删除，root `--workspace`/`--state-dir` 由标准 argparse 预解析器与 `SUPPRESS` 主解析器共同处理（`cli.py` 519 行）。

- [x] **削除 `transport/` 与 `tui/{client,local_client}.py` 的死参数、字符串匹配定错、进程内字典往返与 100ms 轮询流。**
  - 证据：
    1. `transport/http.py:68-94,284-330` 中的 `control_token`、`fenced`、`generation`、`upgrade_id` 是彻底的死代码（`control_token` 存入属性后全仓零读取，`serve_core` 从不传这四个参数）。
    2. `transport/projection.py:145-173` `core_error_status` 靠 `if "is stale; refresh the catalog" in message:` 等子串匹配来决定 HTTP/RPC 错误码；`validated_session_id` 在 `projection.py:139`、`models.py:25`、`local_client.py:109` 重复实现 3 遍；`model_payload`（`projection.py:60-116`）在每次 `/health` 请求时都重新加载静态 catalog 与 thinking rules（而 TUI 高频轮询 health）。
    3. `tui/local_client.py:104,115,144,164,287,294,311` 作为**同进程**客户端，竟然把 `Core` 返回的强类型 dataclass 先转成 dict（`session_payload(record)` / `result.as_payload()` / `event_payload(...)`），再立即调 `SessionRow.from_payload(...)` / `RunResult.from_payload(...)`（内部再走一遍 `json_wire_object → json.dumps → TypeAdapter.validate_json`）转回 dataclass！
    4. `transport/rpc.py:397`、`tui/local_client.py:300` 与 `tui/client.py:347-363` 的事件流全部依赖 `FOLLOW_POLL_SECONDS = 0.1` 的 `asyncio.sleep(0.1)` 或 `to_thread(sink.get, True, 0.2)` 轮询数据库，且客户端每收到一帧还要手发 `session.ack`。
  - 整改：删除 `http.py` 死参数与手工拼接的 50 行 `openapi`（`:179-228`）；用 Core 强类型异常取代 `core_error_status` 字符串匹配；`CoreLocalClient` 直接透传 Core 强类型对象，零 JSON/dict 往返；将 `events_stream` 改为 Core 提交时的 Condition/队列事件推送，彻底消灭 100ms/200ms 轮询。
  - 已落地：`http.py` 死参数与手拼 openapi 已删（`/api/openapi.json` 改为注册表构建，components-only）；`core_error_status` 改 typed exception（`projection.py:155`）；`validated_session_id` 收敛为单定义（`projection.py:149`）；`model_payload` 改调用方喂行；`CoreLocalClient` 零 dict 往返透传；事件流改推送，`FOLLOW_POLL` 清零。

- [x] **拆分 `terminal_app.py`（2,225 行）、`term.py`（1,269 行）、`composer.py`（1,210 行）、`views.py`（1,062 行），清除跨层越权与渲染性能黑洞。**
  - 证据：
    1. **`terminal_app.py`（审计基线 3,345；现 2,225 行）**：`CoreTuiApp.__init__`（`:578-686`）塞了 ~40 个状态字段（含 `_generation`、`_reconcile_serial`、`_interaction_serial`、`_live_serial` 四重序号守卫）；`_main_loop`（`:826-833`）无论是否空闲都以 `time.sleep(0.033)` 固定 30fps 空转，且每帧在 `_footer_context`（`:861-909`）里执行函数内 lazy import 并查 catalog；`_drain_events`（`:926-1238`）是 312 行 20 分支的字符串 `if kind == ...`；每次操作（`CoreRun`/`CoreFollowup`/`CoreFormLoad`/`CoreCommandInput`/`ArtifactPage`）都裸起 `threading.Thread`；`_tui_switch_model`（`:2799-2867`）甚至在 TUI 客户端里直接读盘修改 `voidcode.json`、调 `replace_file` 再发 `core.reload`；同时硬编码 `/tui` 命名空间与 11 个本地命令（`help/output/sessions/session/new/cancel/views/model/thinking/quit/exit`），与 Web 端各自维护一套 slash 解析。
    2. **`term.py`（审计基线 1,508；现 1,269 行）**：`render_lines`（`:289-333`）在每次渲染单行文本时都重新实例化一个 `rich.Console(width=..., height=32768)`；`open()` 启动时同步阻塞 `0.1s + 0.12s` 做 DECRQM 与 Kitty 图形探测（`:1038-1107`）；`:1250-1320` 甚至藏着 70 行用 `getattr(ctypes, "Win" + "DLL")("kernel32")` 混淆调用的 Windows Console 后端。
    3. **`composer.py`（审计基线 1,152；现 1,210 行）**：`:98-297` 从零写了第二套 CJK/grapheme/word 分词与折行宽度计算引擎（`_is_cjk`/`_classify`/`_display_width`/`_wrap_display_line`），与 `term.py:170-253` 的 `grapheme_spans`/`visible_width` 完全重复。
    4. **`views.py`（审计基线 1,096；现 1,062 行）**：`render_tool_result`、`render_plugin_view`（11 分支 `if view_type == ...`）和 `InteractiveForm` 与前端 `frontend/src/core/toolOutput.ts`、`views/FormView.tsx` 重复实现同一套工具输出提取、artifact 引用扫描和表单校验。
  - 整改：
    - 删除 `term.py` 的 Win32 `ctypes` 后端，复用单一 `Console` 实例，将 Kitty 探测改为首次渲染图片时懒加载；
    - 删除 `composer.py` 重复的 CJK/宽度引擎，统一复用 `term.py` 的 `grapheme_spans`/`cell_len`；
    - `_main_loop` 改为事件/dirty 驱动渲染，消灭空闲 30fps 空转与每帧 catalog 查找；
    - 禁止 TUI 直接改写 `voidcode.json`；拆分 `CoreTuiApp` 的事件流跟随、浮层栈、slash 分发与视图渲染。
  - 已落地（2026-11-21）：输入线程 + `SessionFollower`（4 重序号守卫已删）+ 到达时间戳 ESC 消歧 + kitty ack 生命周期 + 1s idle park；`_main_loop` 改事件驱动（30fps 空转已删），footer 改读缓存 revision；`_render_frame` 拆为 4 个 `render_*`，composer if-chain 改数据驱动 `_KEYMAP`；`term.py` Win32 后端、逐调用 `Console` 已删，Kitty 探测懒加载；`views.py` 的 `view_type` if-chain 已重构；逐操作裸线程改 `WorkGroup`，`create_session`/`list_sessions`/`views`/model reload 走工作线程；TUI 配置写入走 `config.update_model_section`；`_handle_stream_event` 移入 `tui/stream_handlers.py`（306 行），`terminal_app.py` 2,437→2,225，`test_idle_render` 改用共享 `wait_for`。保留（已接受）：composer 自有宽度引擎（与粘贴块显示映射耦合）；`/tui` + 11 本地命令双系统。无剩余待拆项。

## Help 与命令发现（2026-11-20 审计）

- [x] **Help 改为 catalog 驱动的声明式模型，删除手写本地命令清单与两套 help 排版。**
  - 已落地：
    - `sdk/commands.py:127-131,320-324` 为 `CommandDescriptor`/`CommandDefinition` 增加可选 `usage`/`examples`/`group`，`_validate_help_metadata:99-106` 在声明/入口边界校验；`as_payload` 携带三字段，`CommandDescriptor.from_payload` 已验证 round-trip。
    - 新增 `sdk/command_help.py`：`public_command_path:39-46`、`command_argument_hints:49-64`、`command_usage:67-79`、`help_entries:91-112`、`grouped_help_entries:114-121`，统一从 schema 的 `properties`/`required`/`text_argument`/`has_input` 与 `client_syntax` 派生；不再在 core/TUI 各写一份 `client_syntax` 拆解。
    - `core/plugins.py:562-564` 把插件定义的三字段带入 catalog；`core/engine.py:2759-2768` 的 `/help` 输出改为按 group + usage + examples 生成，仍返回完整 catalog payload。
    - 新增 `tui/help_view.py`（243 行）：`TuiControl:28-38` + `TUI_CONTROLS:40-51` + `TUI_CONTROLS_BY_NAME:53` 成为 `/tui` 分派、补全、help 的唯一清单；`render_help:198-243` 用 section 级 usage 列、悬挂缩进、窄屏堆叠、组内排序；`_table_rows:73-116`、`_command_rows:119-133`、`_key_rows:136-183`、`_control_rows:186-195` 均为纯函数。
    - `tui/terminal_app.py:2816-2869`：`_command_receipt_rows:2816-2821` 仅对 core `help` 的 receipt 走 `_help_receipt_rows:2825-2847`（从 `data` 反解 `CommandCatalog`），`_show_help:2849-2869` 只取 catalog 并一次 `commit_rows`；catalog 异常降级为 warning + 本地控制，不再从键处理路径抛出。`tui/term.py:594-596` 新增公开 `content_width`，help 用 `_frame_row_width()` 而非 `terminal.width`；`tui/term.py:366-396` 新增无 ANSI 的 `wrap_plain`。
    - `!` shell escape 按 catalog `client_syntax` 通用路由：`terminal_app.py:2366-2390` 增加 `_client_syntax_command`，不新增客户端命令表；`user_shell_plugin.py:46-48` 用 `usage`/`examples`/`group` 声明 `!` 与 `/shell` 两种写法。
  - 证据（整改前）：`_show_help` 在 `:2814-2848` 手写 11 条本地控制与 4 条按键提示；`:2839` 用 `f"  /{...:<16} {c.description}"` 固定 16 列；80 列下 `/goal load_objective` 与描述粘连、长描述由 `commit_rows` 在列 1 续行；本地控制清单在 `:2724-2757`、`:2332-2336`、`:2550-2556` 三处重复；描述未过 `_safe` 而快捷键描述过 `_safe`；`_load_catalog` 在 `:800-809` 抛 `RuntimeError`，`_show_help` 在 `:2834-2835` 无保护调用。
  - 验收：
    - 新增 `tests/unit/tui/test_help_view.py` 7 项（usage 派生、round-trip、40/80/120 列宽度、CJK/宽字符、注入字符、冲突、catalog 失败）；`tests/unit/tui/test_shortcut_bindings.py` 增加 catalog 失败降级；`tests/unit/test_core_tui.py` 增加真实 catalog receipt 走分组 help 的测试；`tests/unit/tui/test_tab_completion.py` 用 `len(TUI_CONTROLS)` 替代硬编码 11。
    - 已运行（`-n0`，串行，符合本轮“禁止 xdist”约束）：`uv run pytest -n0 tests/unit/tui tests/unit/test_core_tui.py -q` = 234 passed；`uv run pytest -n0 tests/unit/tui tests/integration/test_command_browsers.py tests/integration/test_core_first_boot.py -q` = 173 passed；`uv run python -m pytest -q` = **2430 passed / 2 failed in 26:29，本次全量串行不是全绿**（`test_sandbox_backend.py` 的 sqlite `disk I/O error`、`test_tui_monitor.py[40-18-0;15]` 的 8s PTY 退出超时；两条均单独串行重跑通过，`test_tui_monitor.py` 全文件 6 passed，判定为环境/资源 flake，非本次改动复现）；`ruff check .`、`ruff format --check .`、`ty check src`、frontend api/lint/typecheck/test 均通过。早前并行 `mise run check` 亦为 2432 passed（仅作参考，最终验证以上述串行为准）。
  - 限制：40/80/120 已覆盖，<40 列仅做了 header/group/key 的基础换行，未纳入参数化快照；core CLI `/help` 仍是纯文本（CLI 无宽度模型），TUI 使用同一 catalog 的富排版；examples 目前仅 `shell` 声明，其余命令按 schema 派生 usage。

- [x] **`!` shell escape 从“只画 gutter”变为真实 catalog 路由。**
  - 证据：`plugins/user_shell/user_shell_plugin.py:32-48` 声明 `client_syntax="!"`；`tests/unit/test_core_tui.py:1902-1941` 覆盖 `/shell echo hi` 与 `!echo bang` 均落到 `shell` 的 `command` 参数；`terminal_app.py:2381-2390` 只匹配非 `/` 的 catalog 前缀，未匹配时仍走普通 prompt。
  - 验收：`!` 与 `/shell` 行为一致；catalog 缺少 shell 时 `!cmd` 作为普通文本送出，不伪造执行。

- [x] **catalog 加载失败不再从 slash 分派/补全/表单路径抛出。**
  - 实现：catalog 生命周期已移到 `tui/catalog.py`（`load`/`get`/`cache`/`unavailable`/`displayed_revision`）：失败只记录 `app._catalog_error` 并 `logger.warning(..., exc_info=True)`，不再抛 `RuntimeError`；无 revision 时 `get` 返回空表并由 `unavailable` 统一写 `Catalog unavailable: ... (retry)`。slash 提交返回 `False` 时恢复原始 draft（含 `!` client-syntax 路径），补全、未绑定的修饰键、overlay prepare_input、`_action_schema`、`_maybe_load_form_data`、`/tui sessions|views` 的 revision 捕获均显式报错；`reconciled` 的 catalog 失败同样落入同一状态。成功 `cache` 清除错误并重建快捷键。
  - 证据：`tests/unit/tui/test_catalog_failures.py` 8 passed（slash draft、completion、shortcut、client-syntax/schema、form load、overlay、sessions/views、成功 reload 恢复）；`test_help_view.py` 断言 `Catalog unavailable: server unavailable (retry)`；`tests/unit/tui tests/unit/test_core_tui.py` = 242 passed；ruff/format/ty 通过。

- [x] **工具结果渲染改为 typed `ToolResult`，删除 wire-Mapping 防御分支。**
  - 实现：`views.render_tool_result` 直接消费 `ToolResult`/`TextOutput`/`EmptyOutput`/`AttachmentOutput`/`CompositeOutput` 与 typed parts；`_content_part_text` 只做类型分派，媒体字节/URI 来自 `ArtifactReference`；`_artifact_ref_uri`/`_artifact_ref_bytes`/`_composite_part_text`/`_composite_part_has_reference` 已删。diff body 仍是 `ToolBody.as_payload()` 后严格形状校验（body 契约本身不透明），失败/中断、bounds 后缀、控制字符转义与 artifact 行语义保持不变。`_content_rows` 直接传 `content.result`，不再 `encode_tool_result`。
  - 证据：`test_tool_previews.py`、`test_composite_views.py`、`test_diff_cards.py` 共 29 passed（含 typed 构造边界拒绝非法 part）；TUI 目标全量 242 passed。

- [x] **TUI 不再静态依赖具体 capability，RPC 与后台线程统一托管。**
  - 实现：新增 `tui/work.py::WorkGroup`，全 app 唯一有界执行器（6 workers、命名 future、异常按任务名记录 `exc_info`）加受跟踪生命周期线程（输入、stream）；`terminal_app.py` 的 12 个逐操作裸线程与 `session_follower.py` 的 health/reconcile 全部改走 `submit`，stream/input 走 `start`，`run()` 退出统一 `shutdown()`。`create_session`、`list_sessions`、`views`、`core.reload`（含配置写入）改在工作线程执行，结果经 event queue 的 `session_created`/`sessions_loaded`/`views_loaded`/`model_switch_done` 帧回到 UI，沿用既有 session/generation 顶部守卫；`/tui sessions|views` 不再阻塞主循环。
  - 配置：`voidcode/config.py::update_model_section(workspace, field, value)` 是唯一写入入口：整份 payload 经 `parse_config` 校验后以 `replace_file` CAS 落盘，失败原文不动；返回 `Config`。TUI 删除 `capabilities.providers.native.reasoning_effort`、`capabilities.files._replace`、`capabilities.providers.native.model_catalog` 全部导入（`grep -R "capabilities\." src/voidcode/tui` 为空）。
  - Health：`sdk/client.py::HealthModel` 已增加严格校验的 `context_window: int | None`，`transport/projection.model_payload` 从 catalog metadata 填充；footer 直接读 typed 字段，未知窗口显示 `ctx`。
  - 证据：`tests/unit/tui/test_work.py` 5 passed；`test_config_update.py` 4 passed；`test_async_ui_rpc.py` 6 passed；`test_statusline_segments.py` 13 passed（真实 `HealthStatus`/`HealthModel` + known/unknown window）；TUI 目标全量 257 passed；ruff/format/ty 通过。
  - 替换文件写入：`replace_file` 已移到 stdlib-only `sdk/atomic_file.py`，`config.update_model_section` 与三个 file tool 统一导入；私有 `capabilities/files/_replace.py` 已删除，`grep -R "capabilities.files._replace\|_replace import" src` 为空。
  - 残留：`tui/client.py` 的 RPC socket reader 是连接生命周期线程，不属 app 逐操作工作，未改。

- [x] **notice/error/status 前缀与列宽实现收敛到 `tui/formatting.py`。**
  - 实现：新增 `formatting.py`（`safe`/`json_safe`/`tone_prefix`/`tone_slot`/`notice_row`/`prefixed_rows`/`pad_visible_row`/`command_result_rows`/`command_result_text`/`shortcut_bindings`/`footer_row`/模型 segments）；`views.render_notice` 与命令 receipt 共用 tone 映射，`composer._input_row`/`_status_row` 共用 `pad_visible_row`，宽度仍只经 `term` 的 `clamp_row`/`crop_row_right`/`visible_width`。`terminal_app` 以别名导入，测试可见符号不变。
  - 证据：TUI 目标全量 257 passed；ruff/format/ty 通过；无新增 `MAX_*`。

- [x] **typed 事件泵、模块拆分与死符号清理。**
  - 事件：`tui/events.py` 为每种帧定义冻结 dataclass（`TuiFrame` union，artifact 按操作拆成 7 个类）；`tui/event_pump.py` 用单个 `match` 分派，generation/session 守卫与 stream-ready reconciliation 保持在 drain 边界；worker 全部构造 typed 帧，queue 类型为 `Queue[TuiFrame]`。
  - 拆分：新增 `tui/catalog.py`（生命周期）、`tui/operations.py`（sessions/create/views/model reload/command input/live input 的请求与响应构造）、`tui/rendering.py`（transcript/frame/footer/border 渲染）。`terminal_app.py` = **2,225 行**（目标 <2500，已达成）；`CoreTuiApp` 仍是唯一状态所有者，`_drain_events`/`_render_frame` 保留为一行入口。
  - 死符号：删除 `term.set_cursor_shape` 及 cursor-style 查询/恢复内部、`alt_screen`、`reset_live`、`composer.set_enabled`（含 `_enabled` 状态）、`gates.ApprovalGate.from_fact`；`_reset_live`/`enter_alt_screen` 仍为内部实现，非公开入口。
  - 证据：`tests/unit/tui tests/unit/test_core_tui.py` = **257 passed**；`tests/integration/test_tui_monitor.py` 可正常 collect（6 tests）；ruff/format/ty 全绿；`frontend:api` 已重生成并通过 `--check`。
