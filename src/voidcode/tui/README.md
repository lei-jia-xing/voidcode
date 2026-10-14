# `voidcode.tui`

VoidCode 的终端客户端层：inline 渲染 + 原生 scrollback 的 TUI。

## 定位

`voidcode.tui` 是 runtime 的消费者，只负责终端 I/O、渲染、输入解码，以及把 runtime
事件投影成 transcript 块。它不持有会话真相，也不形成独立执行路径。

## 分层

| 模块 | 职责 |
|---|---|
| `app.py` | 事件循环、线程桥、键位分发、runtime 调用面 |
| `term.py` | 终端 I/O：raw mode、尺寸/能力探测、单写者、同步输出、提交行、alt-screen 借用；Windows 通过原生宽字符 console events 读取输入与协议回复，确认 bracketed paste 能力后才启用，未确认时显式报错 |
| `keys.py` | 字节流 → `Key`（含 bracketed paste 与修饰键） |
| `region.py` | typed transcript/chrome frame；只有显式 settle batch 写入 scrollback |
| `theme.py` | 调色板数据、严格解析与语义符号 |
| `transcript.py` | 块模型（user / assistant / thinking / tool / diff / notice） |
| `statusline.py` | 单行段式状态栏 |
| `composer.py` | 多行、Unicode grapheme/cell-aware 输入编辑器；native caret 映射到硬换行行列 |
| `overlay.py` | 内联浮层；需要时借用 alt screen |
| `events.py` | runtime `EventEnvelope` → 块更新 + 视图状态 |

## 契约

- **已提交行只写一次**：`Transcript.take_settled()` → `LiveRegion.commit()` →
  `Terminal.commit_rows()`，进入终端原生 scrollback，永不重绘。只有 transcript settle ledger
  明确交出的行才提交；`set_live(TuiFrame)` 只裁剪旧的 live transcript 行以适配高度，保留
  chrome 且不会提交溢出行。`ctrl+o` 展开若落在提交边界之后会如实报告不可重绘。
- **live 帧只含尾部**：未结块 + 状态行 + composer（或当前浮层），走 `paint_frame` 逐行 diff。
  transcript rows 与 status/composer/overlay chrome 有类型边界；resize 只清除 live viewport，
  不重放已提交历史。
- **native caret ownership**: `Terminal` tracks the painted caret relative to the live-region origin and reanchors before frame edits, commits, resets, close, and suspend. Entering the alternate screen first returns the main caret to that origin; leaving clears the overlay diff and schedules a main-buffer repaint.
- **resize**: `TuiApp` compares current terminal size with its last applied layout size. When they differ it rebuilds the live viewport geometry; settled transcript rows remain in native scrollback and are not replayed. This does not depend on cursor-position query bytes.
- **composable appearance**: typed config keeps theme/glyphs, statusline layouts, eight composer
  frames, native caret, keymap, and brand independent; actions remain user-configurable, but F1/F2/F3
  are not bound by default, and appearance previews do not change runtime authority.
- **帧节奏**：最短 33 ms 一帧（`MIN_RENDER_INTERVAL_MS`），上一帧慢时按 omp 的自适应退避
  放大到 200 ms 上限，事件队列积压时推迟成帧。
- **transcript 永不进 alt screen**：只有全屏浮层（会话选择器、放不下的问答向导）临时
  `\x1b[?1049h` 借用，正文的滚动历史始终可用。
- **单线程**：runtime 的阻塞流在 `threading.Thread` 里泵进 `queue.Queue`，循环从不阻塞在 runtime 上。
- **stream outcome**: only `completed`, `failed`, and `interrupted` settle a closed stream. `running`/`idle` closure preserves unsettled rows with an “outcome unknown” notice; transport loss is “disconnected” and does not imply failure. Tool/assistant spinners stop without committing those rows.
  An `interrupted` outcome is neutral; it does not assert who initiated it. Approval/question overlays remain pending until their matching resolution event arrives; a failed stream does not assume an answer was unapplied.

## 不负责什么

- 直接执行工具
- 会话真相存储
- 执行治理、权限和恢复控制面

## 入口

`voidcode tui [--workspace PATH] [--approval-mode MODE]` → `voidcode.tui.run_tui(...)`。

键位来自 `config.tui.keymap`（`app.help` / `app.tui.appearance` / `app.tools.detail` /
`app.tools.expand` / `app.session.new` / `app.session.resume` / `app.session.tree` /
`app.display.reset` / `app.history.search`）；默认只有 Ctrl+O expand。F1/F2/F3 仍可在用户 keymap 中
显式绑定。`app.tools.detail` 通过 runtime 的 transcript/artifact read API 查看获准的工具输出，Ctrl+O
仍只展开当前可重绘的 transcript。`app.session.tree` 打开条目选择器（checkout：把 leaf 移回某个条目，
之后的 turn 保留但不再进入模型上下文）；未知键名或动作会在启动前直接报错。
