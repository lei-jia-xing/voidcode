# `voidcode.tui`

VoidCode 的终端客户端层：inline 渲染 + 原生 scrollback 的 TUI。

## 定位

`voidcode.tui` 是 runtime 的消费者，只负责终端 I/O、渲染、输入解码，以及把 runtime
事件投影成 transcript 块。它不持有会话真相，也不形成独立执行路径。

## 分层

| 模块 | 职责 |
|---|---|
| `app.py` | 事件循环、线程桥、键位分发、runtime 调用面 |
| `term.py` | 终端 I/O：raw mode、尺寸/能力探测、单写者、同步输出、提交行、alt-screen 借用 |
| `keys.py` | 字节流 → `Key`（含 bracketed paste） |
| `region.py` | live region 账本：`commit(rows)` / `set_live(rows)` |
| `theme.py` | token 表 + 调色板（`resolve_theme(name, mode)`） |
| `transcript.py` | 块模型（user / assistant / thinking / tool / diff / notice） |
| `statusline.py` | 单行段式状态栏 |
| `composer.py` | 多行输入编辑器 |
| `overlay.py` | 内联浮层；需要时借用 alt screen |
| `events.py` | runtime `EventEnvelope` → 块更新 + 视图状态 |

## 契约

- **已提交行只写一次**：`Transcript.take_settled()` → `LiveRegion.commit()` →
  `Terminal.commit_rows()`，进入终端原生 scrollback，永不重绘。账本由 transcript 自己持有
  （记住最后一个已交出的块），所以 `ctrl+o` 与 resize 都不会重新交出已提交前缀，也不会
  错位后续批次；toggle 落在提交边界之后时（无法重绘）由 `set_expanded()` 的返回值如实报告，
  app 据此在 live 区给一行提示。composer 不提供任何 slash 命令补全；以 `/` 开头的输入不再被
  客户端拦截，而是作为普通 prompt 提交给 runtime，由 runtime 的 prompt command 解析
  （`/init`、`/plan`，以及用户/项目 `commands/**/*.md`）统一处理：可解析的命令渲染模板后进入
  graph（`/plan add X` 的 `X` 经 `$ARGUMENTS` 保留），未知 `/foo` 由 runtime 拒绝，其错误以
  error 块呈现。
- **live 帧只含尾部**：未结块 + 状态行 + composer（或当前浮层），走 `paint_frame` 逐行 diff。
- **帧节奏**：最短 33 ms 一帧（`MIN_RENDER_INTERVAL_MS`），上一帧慢时按 omp 的自适应退避
  放大到 200 ms 上限，事件队列积压时推迟成帧。
- **transcript 永不进 alt screen**：只有全屏浮层（会话选择器、放不下的问答向导）临时
  `\x1b[?1049h` 借用，正文的滚动历史始终可用。
- **单线程**：runtime 的阻塞流在 `threading.Thread` 里泵进 `queue.Queue`，循环从不阻塞在 runtime 上。

## 不负责什么

- 直接执行工具
- 会话真相存储
- 执行治理、权限和恢复控制面

## 入口

`voidcode tui [--workspace PATH] [--approval-mode MODE]` → `voidcode.tui.run_tui(...)`。

键位来自 `config.tui.keymap`（`session_new` / `session_resume` / `tools_expand`），
未配置时只有 `tools_expand = ctrl+o`；未知键名或未知动作会在启动前直接报错。
