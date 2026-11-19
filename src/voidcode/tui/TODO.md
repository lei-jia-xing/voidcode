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
  - 证据：
    1. `bootstrap.py:140-194` `admit_live` 将同一份 `model/policy/plugins` 同时投影成 `snapshot` dict、`payload` dict 和 `ExecutionConfig` 三份结构，并对每个 plugin config 反复拷贝塞入 `providers`；`restore_admission`（`:224-244`）先把 `payload` 和 `snapshot` 各自 `json_wire_object` + `canonical_json` 哈希比对，再调 `admit_live`，再用 `replace(...)` 覆盖回原字段。
    2. `bootstrap.py:196-222` `config_from_snapshot` 保留 `version != 2` 与注释 `old snapshots without sandbox` 的向后兼容分支，把字典重新拼回 `{"schema_version": 1, ...}` 再调一次 `parse_config`。
    3. `bootstrap.py:411` `open_core` 先调用 `_assert_workspace_free` 获取并立刻释放一次 `flock`，随后 `Core.__init__` 又锁一次（TOCTOU 仪式）；`:429` 在已有 `admission` 的情况下又重新 `config_from_snapshot(load_private_snapshot(...))` 读盘解析一次传给 `sandbox_backend_for`；`locate_sandbox_helper`（`:302-328`）在生产代码中硬编码搜索 cargo 开发目录 `native/sandbox-adapter/target/{debug,release}`。
    4. `cli.py:74-102` `_normalized_argv` 手写 30 行 argv 扫描重排以支持子命令后置 `--workspace`；`_tui_prefs`（`:403-430`）在启动 TUI 时把 `voidcode.json` 重新读盘并 `parse_config` 解析第二遍，异常还用 `except Exception: file_tui = {}` 静默吞掉；`_show`（`:335-401`）在 CLI 层直接手写 raw SQL 查 `sessions`/`checkpoints`/`session_events`，与 `Core.show` 及 `transport/projection.py` 重复。
  - 整改：删除 `config_from_snapshot` 的旧版本兼容分支、重复读盘解析和 `_assert_workspace_free` 空锁；`Admission` 直接持有强类型 `Config`；CLI 使用标准 `argparse` 层级并复用统一只读查询入口，禁止静默吞配置异常。
  - 部分：`version != 2`/`old snapshots` 分支、`_assert_workspace_free` 空锁、cargo 硬编码路径、`cli` 静默吞配置异常与手写 raw SQL 已删；`ExecutionConfig` 改由 `_execution_config` 单次构建。保留（已接受）：snapshot/payload 双字典（凭据边界：私有快照 vs 去敏载荷，`admit_live` 已注释）。未动：`cli._normalized_argv` 手写 argv 扫描（`:73`）仍在，未切标准 argparse 层级。

- [x] **削除 `transport/` 与 `tui/{client,local_client}.py` 的死参数、字符串匹配定错、进程内字典往返与 100ms 轮询流。**
  - 证据：
    1. `transport/http.py:68-94,284-330` 中的 `control_token`、`fenced`、`generation`、`upgrade_id` 是彻底的死代码（`control_token` 存入属性后全仓零读取，`serve_core` 从不传这四个参数）。
    2. `transport/projection.py:145-173` `core_error_status` 靠 `if "is stale; refresh the catalog" in message:` 等子串匹配来决定 HTTP/RPC 错误码；`validated_session_id` 在 `projection.py:139`、`models.py:25`、`local_client.py:109` 重复实现 3 遍；`model_payload`（`projection.py:60-116`）在每次 `/health` 请求时都重新加载静态 catalog 与 thinking rules（而 TUI 高频轮询 health）。
    3. `tui/local_client.py:104,115,144,164,287,294,311` 作为**同进程**客户端，竟然把 `Core` 返回的强类型 dataclass 先转成 dict（`session_payload(record)` / `result.as_payload()` / `event_payload(...)`），再立即调 `SessionRow.from_payload(...)` / `RunResult.from_payload(...)`（内部再走一遍 `json_wire_object → json.dumps → TypeAdapter.validate_json`）转回 dataclass！
    4. `transport/rpc.py:397`、`tui/local_client.py:300` 与 `tui/client.py:347-363` 的事件流全部依赖 `FOLLOW_POLL_SECONDS = 0.1` 的 `asyncio.sleep(0.1)` 或 `to_thread(sink.get, True, 0.2)` 轮询数据库，且客户端每收到一帧还要手发 `session.ack`。
  - 整改：删除 `http.py` 死参数与手工拼接的 50 行 `openapi`（`:179-228`）；用 Core 强类型异常取代 `core_error_status` 字符串匹配；`CoreLocalClient` 直接透传 Core 强类型对象，零 JSON/dict 往返；将 `events_stream` 改为 Core 提交时的 Condition/队列事件推送，彻底消灭 100ms/200ms 轮询。
  - 已落地：`http.py` 死参数与手拼 openapi 已删（`/api/openapi.json` 改为注册表构建，components-only）；`core_error_status` 改 typed exception（`projection.py:155`）；`validated_session_id` 收敛为单定义（`projection.py:149`）；`model_payload` 改调用方喂行；`CoreLocalClient` 零 dict 往返透传；事件流改推送，`FOLLOW_POLL` 清零。

- [ ] **拆分 `terminal_app.py`（3345 行）、`term.py`（1508 行）、`composer.py`（1152 行）、`views.py`（1096 行），清除跨层越权与渲染性能黑洞。**
  - 证据：
    1. **`terminal_app.py`（3345 行）**：`CoreTuiApp.__init__`（`:578-686`）塞了 ~40 个状态字段（含 `_generation`、`_reconcile_serial`、`_interaction_serial`、`_live_serial` 四重序号守卫）；`_main_loop`（`:826-833`）无论是否空闲都以 `time.sleep(0.033)` 固定 30fps 空转，且每帧在 `_footer_context`（`:861-909`）里执行函数内 lazy import 并查 catalog；`_drain_events`（`:926-1238`）是 312 行 20 分支的字符串 `if kind == ...`；每次操作（`CoreRun`/`CoreFollowup`/`CoreFormLoad`/`CoreCommandInput`/`ArtifactPage`）都裸起 `threading.Thread`；`_tui_switch_model`（`:2799-2867`）甚至在 TUI 客户端里直接读盘修改 `voidcode.json`、调 `replace_file` 再发 `core.reload`；同时硬编码 `/tui` 命名空间与 11 个本地命令（`help/output/sessions/session/new/cancel/views/model/thinking/quit/exit`），与 Web 端各自维护一套 slash 解析。
    2. **`term.py`（1508 行）**：`render_lines`（`:289-333`）在每次渲染单行文本时都重新实例化一个 `rich.Console(width=..., height=32768)`；`open()` 启动时同步阻塞 `0.1s + 0.12s` 做 DECRQM 与 Kitty 图形探测（`:1038-1107`）；`:1250-1320` 甚至藏着 70 行用 `getattr(ctypes, "Win" + "DLL")("kernel32")` 混淆调用的 Windows Console 后端。
    3. **`composer.py`（1152 行）**：`:98-297` 从零写了第二套 CJK/grapheme/word 分词与折行宽度计算引擎（`_is_cjk`/`_classify`/`_display_width`/`_wrap_display_line`），与 `term.py:170-253` 的 `grapheme_spans`/`visible_width` 完全重复。
    4. **`views.py`（1096 行）**：`render_tool_result`、`render_plugin_view`（11 分支 `if view_type == ...`）和 `InteractiveForm` 与前端 `frontend/src/core/toolOutput.ts`、`views/FormView.tsx` 重复实现同一套工具输出提取、artifact 引用扫描和表单校验。
  - 整改：
    - 删除 `term.py` 的 Win32 `ctypes` 后端，复用单一 `Console` 实例，将 Kitty 探测改为首次渲染图片时懒加载；
    - 删除 `composer.py` 重复的 CJK/宽度引擎，统一复用 `term.py` 的 `grapheme_spans`/`cell_len`；
    - `_main_loop` 改为事件/dirty 驱动渲染，消灭空闲 30fps 空转与每帧 catalog 查找；
    - 禁止 TUI 直接改写 `voidcode.json`；拆分 `CoreTuiApp` 的事件流跟随、浮层栈、slash 分发与视图渲染。
  - 部分：输入线程 + `SessionFollower`（4 重序号守卫已删）+ 到达时间戳 ESC 消歧 + kitty ack 生命周期 + 1s idle park；`_main_loop` 改事件驱动（`queue.get` 常驻，30fps 空转已删），footer 改读缓存 revision；`_render_frame` 拆为 4 个 `render_*`，composer if-chain 改数据驱动 `_KEYMAP`；`term.py` Win32 后端、逐调用 `Console` 已删，Kitty 探测懒加载（首图渲染时）；`views.py` 的 `view_type` if-chain 已重构。保留（已接受）：composer 自有宽度引擎（与粘贴块显示映射耦合，不与 `term` 渲染代码共享）；`/tui` + 11 本地命令双系统（slash 统一已证伪：12/13 无 catalog 对应项，产品决策保留）。未动：`_drain_events` 仍是 26 分支 kind 链；每操作裸起线程模式仍在；TUI 直接改写 `voidcode.json` 切模型仍在（`terminal_app.py:2928-2950`，有校验与 CAS）。
