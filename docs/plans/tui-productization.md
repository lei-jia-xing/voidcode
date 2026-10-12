# TUI 产品化与 clean-cutover 实施计划

状态：P0–P5 实现与 clean cutover 已完成；完整本地 pre-commit、`mise run check`、VCS wheel 资源与 extracted-wheel Web consumer 均已通过。已观察的 native acceptance 仅覆盖 Linux；不宣称 Windows/macOS parity 或完整跨平台矩阵通过。

## 目标、基线与硬边界

把当前自研 inline TUI 收敛为一套完整、可配置、可组合的 VoidCode 外观与交互产品：丰富且严格的主题目录、透明且可布局的 statusline、八种可组合 editor/composer 形状、唯一 native cursor、品牌首屏、可发现的 action/help，以及小终端下完整可操作的审批和问题界面。主题、statusline、composer/frame、caret、brand/config/action/help 是独立的轴，用户可以组合；不能把它们绑成 provider-specific preset 或单体 UI framework。`codex` / `claude` 两个 statusline profile 是 VoidCode 必需的消费者目标，不是 OMP 已存在的 preset 名，也不是模型/provider 分支。

当前产品基线由 VoidCode 源码提交 `4b05d73` 与隔离真实 CLI/PTY 观察交叉核实（该提交是本次审计的源码基线，不宣称当前工作树版本）：TUI 是 `src/voidcode/tui/` 的单一 inline renderer；80×24 truecolor 初帧实际输出 `Idle`、审批模式、workspace、`lsp off`、不透明暗色 statusline、`Ask voidcode...` 与 `▏`；80×24 输入/Ctrl+J 后 resize 到 40×12 出现新 frame；无 API 凭证地通过真实 runtime/deterministic engine 提交过只读 `read sample.txt`，看到完整工具调用、输出和最终回复；真实 approval 已在 **36×8** PTY 打开 full-screen 目标/理由面板，Esc 经 runtime 拒绝、未执行 command，但面板底部 decision/help hints 被顶裁（capture `artifact://2951`）；真实 empty-session selector 确实借用 alt-screen。另一个真实隔离配置在 TUI terminal setup 前因 `app.session.tree` 被 runtime TUI enum 拒绝退出。**捕获是 PTY ANSI/文本和状态流，不是截图、实际终端像素、tmux 或字体渲染证据。** 以上场景均未调用外部模型/provider，真实 read/approval 的工具执行/拒绝仍经过 runtime。

不可移动边界：

- SQLite append-only 数据是 session truth；runtime 独占授权/审批/工具执行/取消/streaming/恢复和持久化。TUI 不增造或绕过执行语义；显示时只使用 runtime 提供、session 访问权限允许的数据。
- `Transcript.take_settled -> LiveRegion.commit -> Terminal.commit_rows` 一次性写入终端原生 scrollback；已提交行不可改色、重新折叠、展开、搬动或再写。resize、theme/profile change、banner、viewer 切换不从 committed prefix 重播。
- 所有永远保留的 UI chrome（statusline、composer、品牌、占位/帮助、overlay）必须与 session transcript 分开布局；这些行不得因为 terminal height cap 被 `LiveRegion` 当作旧 transcript 溢出行提交到 scrollback。只有 transcript 行可以进入 scrollback；当前未完成、会继续变更的 transcript 也不能仅因布局溢出而提前“定稿”。
- 所有风格/主题/UI state 都是展示数据：不得进入 `RuntimeRequest.prompt`、event、transcript、provider context 或 session runtime behavior config；不得更改已有 provider/permission/steering/approval 行为。
- 不引入 marketplace、theme/plugin 执行框架、二套 UI renderer、TUI 测试框架、历史格式兼容 alias、魔法配置或无消费者的上游 token/API。目录数据完整，数据架构小。

## 参考源与事实边界

- 当前上游设计参照是经过 `git ls-remote` 固定并读取 raw source 的 oh-my-pi commit [`b07a1c146d0d12cfc855a2c65d52f892ef319040`](https://github.com/can1357/oh-my-pi/tree/b07a1c146d0d12cfc855a2c65d52f892ef319040)；其 `packages/coding-agent/package.json` 为 **18.8.7**。源路径引用均使用这个 commit，例如 [theme schema](https://github.com/can1357/oh-my-pi/blob/b07a1c146d0d12cfc855a2c65d52f892ef319040/packages/tui/src/theme/schema.ts)、[theme registry](https://github.com/can1357/oh-my-pi/blob/b07a1c146d0d12cfc855a2c65d52f892ef319040/packages/tui/src/theme/defaults/index.ts)、[statusline presets](https://github.com/can1357/oh-my-pi/blob/b07a1c146d0d12cfc855a2c65d52f892ef319040/packages/tui/src/status-line/presets.ts)、[composer registry](https://github.com/can1357/oh-my-pi/blob/b07a1c146d0d12cfc855a2c65d52f892ef319040/packages/tui/src/components/composer/registry.ts)、[welcome](https://github.com/can1357/oh-my-pi/blob/b07a1c146d0d12cfc855a2c65d52f892ef319040/packages/tui/src/prompt/welcome.ts)。
- 必须分开陈述 binary 与 source：本机 `omp --version`/原生无模型 `omp gallery` 是 **18.6.1**；另一个已安装 npm source 是 `@oh-my-pi/pi-coding-agent` **17.3.3**。它们不是 b07 source 的行号或 gallery 行为证据。当前 source 独立验证了 7 个 status preset (`default/minimal/compact/full/nerd/ascii/custom`)、27 个上游 segment IDs、8 个 composer 样式 (`box/band/claude/pi/borderless/rule/field/rail`)。其 statusline 没有 `codex`/`claude` 命名 preset；`claude` 是 composer shape。VoidCode 仍必须实现自己清晰定义的 `codex` 与 `claude` statusline presentation。
- b07 `theme/defaults/index.ts` 机械计数为 **100** 个 theme JSON 名称：45 个 `dark-*`、39 个 `light-*`、16 个 unprefixed；这些完整名称和分类见该固定 source 文件。主题 token 是数据驱动 schema（`vars`, `colors`, symbols）；这证明可用的数据/结构范围，不等于 VoidCode 要携带上游所有无消费者的 tokens 或 terminal exporter。
- 固定源 commit 的根`LICENSE`是 MIT，适用于该repo的软件与数据文件（主题 JSON 包含在内），除非遇到实际具体冲突 notice；许可文本/版权行见 [`LICENSE`](https://github.com/can1357/oh-my-pi/blob/b07a1c146d0d12cfc855a2c65d52f892ef319040/LICENSE)。主题 JSON 没有逐主题 origin/license 字段，单凭缺少该字段**不是**许可例外证据。实施时保留上游 MIT copyright/notice 与每项 palette 的准确 repo/commit/source JSON path；当前审计未独立追溯每项数据历史，也不要求无事实基础的法律取证。100项原始数据保留身份/色值，不由VoidCode原创色值冒充补数；VoidCode原创dark/light/high-contrast family另列，brand/logo原创。
- OMP 明确以 `\x1b[49m` 表示 transparent/default background，且透明布局会去掉依赖背景色的 endcaps；这是真 ANSI 证据，不是像素保证。VoidCode 的初始 statusline 默认透明，真实支持的 term/tmux 组合要用实际 terminal capture 验收。

## 目标 UI 与唯一配置合同

### 一个严格、可组合的 `tui.*` typed contract

在 `src/voidcode/runtime/config_models.py` 中把 TUI appearance schema 做成唯一、Pydantic typed 的配置入口，`src/voidcode/runtime/config.py` 负责 user/workspace 来源解析与现有 precedence，不把渲染策略塞进 runtime。采用直接的 `tui.theme`、`tui.statusline`、`tui.composer`、`tui.keymap`、`tui.brand`；**删除旧 `tui.preferences.theme` shape，不保留旧名 alias、自动转换、silent fallback 或双份设置**。Workspace/user config 同一合同；按 field 保留现有的 user-global → workspace override 责任链；keymap 的 workspace 覆盖粒度明示为整个 keymap，不把未知 `app.*` 映射自动转译。若配置也经 public OpenAPI/API 暴露，则在同一 cutover 中通过现有 schema/type generator 更新其公开声明与前端消费类型；只修改 generator 所有的源并重生成产物，不手改生成声明、不新增 frontend UI。

最小可理解配置结构如下（键语义是合同，实施时用具体 `Payload`/Enum/可选嵌套 model，不接受宽泛 metadata）：

```json
{
  "tui": {
    "theme": {
      "mode": "auto",
      "dark": "voidcode-dark",
      "light": "voidcode-light",
      "custom": {},
      "symbols": {"preset": "unicode", "overrides": {}}
    },
    "statusline": {
      "layout": "codex",
      "transparent": true,
      "left": ["status", "model", "mode", "path"],
      "right": ["context_pct", "cost"],
      "separator": "dot",
      "options": {}
    },
    "composer": {
      "style": "band",
      "cursor": {"shape": "terminal"},
      "gutter": "prompt",
      "padding": "normal"
    },
    "brand": {"enabled": true},
    "keymap": {"ctrl+o": "app.tools.expand"}
  }
}
```

合同规则：

- `theme` 的唯一字段是 `mode: auto|dark|light`、`dark` slot、`light` slot、`custom`、`symbols`：`mode=dark|light` 选对应 slot；`mode=auto` 根据一次 terminal-default-background 探测选择 slot，暗背景选 `dark`、亮背景选 `light`。所有 slot name 必须精确解析为已知 built-in palette 或 `custom` key，且其声明 mode 必须与所在 slot 一致；**两个 slot 与所有 custom entries 始终都要验证，即使当前 inactive**。不设 `name` alias，不从名称猜测/替换slot内容，冲突或 mode 不匹配直接 fail-fast。
- `mode=auto` 仅在启动或用户明确请求 appearance refresh 时经唯一 Terminal response parser 执行一次有界 `OSC 11` terminal-default-background query；query 失败/拒绝/超时后仅接受有效 `COLORFGBG` 作为次级背景报告，均不可用时固定选配置中声明的 `dark` slot。按解析背景的线性相对亮度 `<0.179` 选dark，否则选light；不探测compositor pixels、不写terminal palette、不做OS-specific猜测、不轮询。query缺席走确定fallback，不会把坏配置降级为auto。
- 通过 `$schema` 暴露同一 typed contract；`scripts/generate_config_schema.py` 是唯一 schema artifact producer，运行 generator 更新 `schema/voidcode.config.schema.json`，**禁止手编辑生成 JSON**。更新 `docs/contracts/runtime-config.md` 说明实际 user/workspace precedence、字段、clean-cutover 和错误。若配置也经 public OpenAPI/API 暴露，按配置拥有的现有 generator 更新 API schema/前端声明；只有生成器拥有的公开 config artifact 才更新，不开始 frontend UI 产品化。
- `theme.custom` 是由精确 palette name 索引的 mapping；key 是唯一 custom identity，不能与任何 bundled name 重名；每个值为封闭对象 `{mode, vars, colors}`，`mode` 必须显式为 `dark|light`，被 slot 使用时必须匹配 slot。custom palette 的基色明确继承相同 mode 的 `voidcode-dark` 或 `voidcode-light`，`colors` 仅覆写 renderer-known keys，遗漏值沿此 base 继承。颜色值允许 `#RRGGBB`、ANSI index、声明变量引用及仅对 terminal-default surface keys 有效的 `"terminal-default"` sentinel；禁止任意 ANSI 序列。变量引用必须形成有向无环图，cycle/unknown reference 均拒绝；所有 custom entry 即使未选中也要完整验证。
- `theme.symbols` 仅接受 `preset: unicode|ascii|nerd` 和 `overrides` mapping；preset 规定默认 glyph family，override 只替换已知语义 glyph，不能接收控制字符/raw ANSI。
- `composer.cursor.shape`（`bar|block|underline|terminal`）是唯一公开cursor-shape字段；默认`terminal`完整继承shell原有shape与blink，不发自定义shape/query、不改terminal style。非法配置必须在raw mode前拒绝；只有显式选择`bar|block|underline`时，才可为能力确认使用有界、scoped、Terminal-owned probe mode。Terminal必须在报告unsupported前精确恢复所有终端flags，且probe失败时不得启动interactive UI；不伪造software caret。`composer.gutter`仅`prompt|none`，`padding`仅`compact|normal`；gutter glyph来自`theme.symbols.preset`/override。每项仅在对应consumer的phase同原子schema change接受。
- Statusline profile精确defaults `left`/`right` IDs、separator、空 options见下表；数组显式传入时替换该侧，separator替换profile值，options只合并已知key。所有builtin preset默认包含真实`mode` segment，并在宽度收缩时优先保留；custom数组可由用户显式省略`mode`或其他status fields，不因有效省略而强制重插或拒绝配置。未知segment/option/value fail-fast，真实源缺失不补占位/假值。
- 唯一 `app.tui.appearance` picker 在同一 overlay 选择 mode/dark slot/light slot/statusline layout/composer style/cursor shape并预览。Enter 只应用当前 TUI 进程内的未来/未提交 view rows，不重绘 scrollback、不改 session/config；Esc 回滚未应用的预览。单独显式 Save action 必须先询问 `user` 或 `workspace` scope，再调用现有配置写入路径；没有自动保存或隐式 scope。`app.help` 帮助列出当前 mode、slot/profile/style/cursor 实际值与 auto source/fallback；help和appearance均通过用户配置的keymap动作打开，F1/F2不是默认绑定。
- `brand.enabled` 仅为布尔view setting，默认true；它只控制单行fresh-start brand，无tagline、无prompt/runtime effect；此字段与brand renderer同在P5变更接受。

### 主题目录：交付完整数据而不是把两种 palette 换名字

- 在`src/voidcode/tui/themes/`随package安装100-entry upstream catalog（45 dark、39 light、16 other；完整名称见 pinned `theme/defaults/index.ts`）；直接复用每个上游palette的实际JSON数据与身份，不为凑数重画颜色或改名。每个data record保留**准确repo (`can1357/oh-my-pi`)、commit (`b07a1c146d0d12cfc855a2c65d52f892ef319040`)和source JSON path (`packages/tui/src/theme/defaults/<name>.json`)**。另加原创VoidCode dark/light + high-contrast家族，色值独立设计、填满实际renderer tokens；100 upstream themes范围完整，原创主题独立命名。
- wheel/sdist捆绑100项数据并保留该repo根`LICENSE`完整版权/MIT文本；如处理具体真实third-party exception notice则同步保留其实际要求。无需逐主题license metadata、未要求的法律取证或人为新造 `NOTICE`。`pyproject.toml`已有`src/voidcode` package tree，release acceptance核实主题JSON与上游`LICENSE`都进安装包；这不是marketplace、联网下载、动态import、Python callback或外部执行。
- 提供可核查的主题目录索引/可直接选择的 config values，让用户知道 dark/light/type/foreground/background 高对比方向；不需要新的主题 marketplace/自动预览框架。选定 palette 后仅对尚未进入 committed scrollback 的新 frame 生效。

### Statusline layout 与信息优先级

`src/voidcode/tui/statusline.py` 为唯一状态条渲染器与 segment registry；`app.py` 只传入真实 runtime/context 数据。所有 layout 分享一套 segment consumer/token width/overflow 路径，不做 `codex_*`/`claude_*` 两套 provider renderer。

| VoidCode `layout` | 默认 segment 顺序（空数据不渲染） | 视觉目标 |
|---|---|---|
| `codex` | left `[status, model, mode, path]`; right `[context_pct, cost]`; `dot` | 干净单行，model 与路径易定位，context/cost 在右侧，不画端帽色块 |
| `claude` | left `[model, path]`; right `[mode, context_pct, cost, session_name]`; `rule` | 更静的文本/rule 分隔，减少持久前缀，保留有真实数据的提示 |
| `minimal` | left `[status, model, mode]`; right `[]`; `none` | 超窄可读；approval mode 仍保留 |
| `compact` | left `[status, model, mode, path]`; right `[context_pct, cost]`; `pipe` | 常规紧凑 |
| `full` | left `[status, model, mode, path, session_name]`; right `[context_pct, cost, token_in, token_out, cache_read, cache_write, cache_hit]`; `dot` | 所有有来源的数据段；窄宽按固定优先级收缩/丢弃 |
| `ascii` | same segment order as `compact`; ASCII-only separator/glyph preset | 无 Nerd Font/unicode 环境 |
| `custom` | 使用配置中显式提供的 `left`/`right` arrays；用户可选择省略`mode`或其他status fields；`dot` separator，无 options | 同一segment engine，不接受不存在的IDs |

statusline 合同：

- 可配置 segment IDs 仅限真实来源：当前 projection 为 `status`（runtime/view state）、`model`（runtime metadata）、`mode`（实际 approval mode）、`path`（当前 workspace）、`context_pct`（`context_window.usage_tokens_after|usage_tokens_before` + runtime `provider_readiness.context_window`）、`cost`（`provider_usage.cumulative.cost_usd`）、`session_name` 和 `lsp`（client 的当前实际 LSP 状态）。再提供 `token_in/token_out/cache_read/cache_write/cache_hit`，直接映射现有 public `ProviderTokenUsage` 的 `input_tokens/output_tokens/cache_read_tokens/cache_write_tokens/cache_hit_rate`；仅当该 metric 在 runtime `provider_usage.latest` 有值时显示。`input_tokens` 已含 cache reads，cache reads 不可再加一次；context processed token 继续使用 runtime 的 canonical `input + cache_write + output` usage，不从 TUI 另算。未接通上述公开字段的消费端不得发布对应 ID，不得假造 OMP 的 git/pr/hostname/time/subagents/premium/collaboration 数据。`lsp off`/无 reasoning/空 status 不渲染；状态列 IDs 与 schema、renderer、picker/help 一致。
- `codex`、`claude`、`minimal`、`compact`、`full`、`ascii` 是精确已命名的 layout；statusline 自身仍可自定义段顺序与受支持 separator，separator/token color 也可配置。它们不选 model/provider，也不决定 composer shape/theme。
- `transparent=true` **为默认且可观察**：使用 terminal default background / background SGR `49m`（或省略背景色），不要输出 `statusLineBg` 固定黑色、不模拟 RGB alpha，也不保留只有邻近背景不同才能读的 powerline endcap。用户显式 `transparent=false` 才填 theme 的 solid status surface。
- Idle 没有进行中/决策时为空状态而不是 `Idle` 常驻段；`lsp off`、无 reasoning 的 disabled/off 不渲染。删除 `Ask voidcode...` 空输入 placeholder；空编辑器留给 native insertion cursor 和 composer frame。不能为清爽隐藏错误、取消、排队steer、进行中的加载/工具状态；状态条按builtin默认或custom显式所选字段展示，实际approval details与runtime authority不受statusline选择影响。steer 的含义仍是 runtime 当前 `queue_steering`/`Steering queued (N)`；Esc 仍是 active run cancel。
- builtin profile 窄宽缩减时真实 `mode`/approval label 按默认优先级保留，不换成假值或改语义；先省略 path details、按已声明优先级丢弃其他低优先 segment。custom layout 的overflow关键优先级仅适用于用户显式选择的字段，不强制补入省略字段；若当前终端仍小于支持的最小几何，则转为下述 `Resize to >=20x6` safe-hold。

### 八种 composer 形状和一个 terminal-owned native cursor

`src/voidcode/tui/composer.py` 继续是一份 editor state machine。引入封闭 compositional style registry/data，提供完整八种上游 gallery 形状：

- `box` 圆角四边框；`band` 无完整 frame、全宽 status/chrome band 与独立 prompt gutter；`claude` 上下 rule、左 prompt chevron、status/model 可读区；`pi` 规则线和紧凑元数据；`borderless` 纯 prompt/status；`rule` 顶部 rule；`field` 双竖边/侧栏；`rail` 单强调侧 rail。
- 这些是同一个 composer/editor 的 **frame geometry、rule/rail、gutter 和 status dock 槽位数据**，可从配置独立选择，不是八个 widget/editor renderer，不改变键盘、text、history、steer、cancel。statusline 在 editor frame 内只能有一个 host，不得 composer 再画重复 status data。
- composer style、palette/theme、statusline profile 和 cursor shape 是独立可组合的用户设置。frame/Gutter只使用 palette / glyph semantic styles；未知shape/glyph参数 raw config 立即拒绝。
- 把 Terminal native cursor 确定为插入点的 **唯一** painter。Composer 与可编辑 overlay 都向同一 typed frame 提交 cursor cell/row/visibility/shape；renderer不在行尾写 `▏`、不反显中间字符、不以底线、主题色块或software glyph覆盖。原先提到的“右侧竖线”是当前行尾软件 caret，“中间方块”是中间字符反显；两者均非输入框右侧边界。公开字段 `composer.cursor.shape=bar|block|underline|terminal`；默认 `terminal` 继承 shell原有光标，不触发能力query或shape设置。显式 bar/block/underline 需在此 terminal 报告支持且能精确保存/还原之前用户 shape/visibility，否则启动报不支持。
- 单一 Terminal writer 是设/读/清 cursor position/shape/visibility 的 owner。`Terminal.paint_frame` 消费 typed frame（完整行/布局分区 + 唯一 native cursor metadata）；`Term` 同步绘制/自动换行状态结束后定位到编辑插入 cell，而不是现在的 transcript region 锚点。cursor-only arrow moves 即便 `rows` 不变也必须 repaint定位；shape query 只发生在显式请求时，不是busy polling。
- Native cursor使用 UTF-8显示 cell 坐标：editor 持有逻辑光标；排版给出可见 grapheme 的 cell column/row；跨行、CJK/emoji宽度、combining/ZWJ、行尾满宽都不拆裂字符、不误触 terminal wrap、不出现零/二个caret。没有cursor shape/position能力或无法安全restore时不显示软件fallback；所有 exit/error/signal/overlay退出均恢复进入前cursor visibility/style、raw mode、protocol flags、auto-wrap/sync-output和主buffer。

### Native history、reserved chrome、短终端与复杂 overlay

现有 source 在 TUI启动时使用 `LiveRegion(max_rows=terminal.height)` 一次；resize 只更新width并 clear，`LiveRegion`没有动态height更新；composer 默认最大10行；Terminal frame按当前terminal高度bottom clip；alt-screen over-height overlays top crop；这些 geometry事实已由 `app.py:353-365,497-507,544-553`、`region.py:97-154`、`composer.py:162-175` 证明。高度重排是layout/scrollback所有权问题，不是 CSS或补padding：

1. 每帧在一个有序布局内明确 `transcript viewport`、唯一statusline、editor/frame chrome、输入行和overlay行。根据当前terminal rows先计算persistent chrome/editor min-height，剩余高度分给transcript viewport；低优先级 transcript tail可暂时隐藏，不裁composer caret/审批buttons、不把过高composer rows送入scrollback。
2. 完整 TUI 可交互的最小终端几何固定为 **20 columns × 6 rows**：到达此尺寸，普通composer可见编辑行/原生caret；approval/question overlay可分页body并固定显示目标/决策label、allow/deny或选择actions、help。小于此阈值时切换为唯一的`Resize to >=20x6` safe-hold frame，阻止普通编辑/submit及不可见的approval/question/config actions，不丢draft/pending request、不commit或裁出部分approval controls；pending state保持不变，除非用户显式cancel/deny/quit。safe-hold不得阻断既有context-defined的显式Esc/Ctrl+C cancel/deny/quit，它们仍经现有runtime/terminal lifecycle执行；不添加自动decision或新timer。恢复到最小尺寸后从未变的模型状态重建frame。
3. `Terminal.height` resize 更新同一纯布局和 region geometry，`region.clear()` 后完整重绘当帧；`LiveRegion`溢出只处理类型为transcript的rows。committed settled transcript超出viewport时进入原生scrollback一次；unsettled stream rows只能留在transcript model/viewport/滚动视图；live composer/status/brand/overlay/help永不 `commit`。
4. Resize使已提交行宽度不可逆；历史文本保留原始ANSI/宽度，不生成第二遍、不伪称按新theme重染。
5. Fullscreen selector、approval、question、history/detail/help overlay在支持的窄屏使用自己的viewport：mandatory decision/action/hint行固定可见，主体分页/滚动；拒绝 `rows[:height]` top crop。低于20×6时全部进入上述safe-hold，阻止普通编辑/submit和不可见的approval/question/config actions，不显示不完整control，且pending state保持不变，除非用户显式cancel/deny/quit；既有context-defined显式Esc/Ctrl+C cancel/deny/quit仍按现有runtime/terminal lifecycle工作。
6. 键盘discoverability显示真实当前配置的key bindings；`F1 -> app.help`、`F2 -> app.tui.appearance`、`F3 -> app.tools.detail`均可由用户显式配置，但没有function-key defaults。help列出appearance实际值、mode来源/fallback、临时预览或已显式保存状态；列明Esc cancel语义、`ctrl+o` scope限制和当前steer行为。session new/resume/tree、history search通过同一keymap/actions catalog；不偷偷绑定额外有状态副作用。
- 当前 `app.py:_ACTIONS`/parser/dispatch确有 `app.session.tree`，`tui/README.md`也建议其可配置，但 `runtime/config_models.py:VALID_TUI_COMMANDS/TuiCommand`拒绝此值。本轮真实CLI workspace config预检已证明退出码1、报错列值缺tree；同一完整输出还观察到此无效TUI command config向用户泄漏未捕获的多帧Python traceback，末尾为`ValueError`，发生在terminal activation之前。canonical typed TUI action catalog定义允许值，Pydantic/schema、parse/dispatch、help、按键hint、appearance picker、文档全部消费同一事实。每个公布action可配置/发现/运行；普通无效用户TUI配置应给出可操作的field/value/config scope/path诊断、非零退出且terminal未改变，而不是内部traceback；unexpected/internal failures保留诊断，不压制异常、不fallback、不自动修复输入。不创建自动插件命令注册框架或保留drift。

### Branding、状态与输入数据 integrity

首次新启动的 VoidCode brand lockup 只有一行：

```text
  ◇V  VOIDCODE
```

ASCII fallback:

```text
 [*V] VOIDCODE
```

不添加 tagline、提示或重复显示的 workspace/session 小字；若workspace/session identity已有其他chrome位置，则不在brand重复。原创 `◇V` monogram与wordmark用theme accent着色，monochrome和Nerd Font缺席照样成立；窄屏保留完整wordmark，不切断字符。仅在fresh interactive TTY、transcript还未产生时显示在live chrome；已恢复/非交互/非TTY不插splash。品牌从不构造成`UserBlock`/Notice/runtime event/assistant content/request metadata，不持久化、不进入provider payload；首次prompt提交时从未settled chrome消失。help键提示仅在用户为`app.help`配置绑定后显示实际按键。

Composer 保持纯用户文本而不做 shell/URL/path normalizer。对照当前 `_sanitize_paste` 会 NFC normalize、把tab改成3个space、删control；`_submit`会 trim，而 `Composer`逻辑caret/backspace以 code point 工作。本轮选定可接受文本的产品语义：

- 在 bracketed paste delim 去除终端帧之后保留 Unicode codepoint、空格、tab、行首尾空格和 tab数量原样；不要 NFC 归一、三格tab重写、`.strip()`重写或路径/shell/URL normalization。跨平台 CRLF/CR作为编辑器行边界统一变成LF；只用 `.strip()` 判定“全空不提交”，一旦是有效 prompt就把原draft**原样**交给 runtime。提交runtime内容必须和输入visual/editor value一致。
- tab按清楚一致的 display tab-stop rendering（默认4个 terminal cells）显示，但draft内仍是 `\t`；没有将tab退化成空格。Line wrap/cursor movement/backspace/delete按 grapheme boundary；CJK、combining mark、emoji modifier、ZWJ家庭/旗帜序列无法被左右移动/退格拆开，宽2字符、Unicode粘合符宽度受同一个 cell-width函数治理。
- bracketed-paste内/用户文件或工具结果里的ESC、OSC、CSI、终端C0/C1等不可信数据不能在Terminal被转成 control instructions；需要显示时经既有 Rich/ANSI-safe text renderer以普通escaped content展示，序列始终只来自Terminal style helpers。custom glyph/用户theme/label也走字段级text/glyph/colors validator，禁止 raw ANSI/富文本/terminal commands。保留安全的 tab/newline文本，不篡改可见普通 whitespace。
- 中间编辑光标/行尾光标在八种composer形状、无颜色、width边界都由同一`composer.cursor.shape`决定；不因为 editor shape自动偷换software caret风格。

### Tool details与runtime read authority

- 当前`ctrl+o`展开的是尚可重绘的transcript blocks；已commit rows展开时只能报告其已在scrollback、不可再绘。保留“只有 transcript 可提交”的合同，不谎称能展开immutable rows、不往native历史插一份重复输出。
- 真正完整的长工具输出使用现有 `VoidCodeRuntime.read_tool_output_artifact(session_id, artifact_id|tool_call_id, offset, limit, caller_session_id=...)` bounded read API和 `read_tool_transcript(caller_session_id, session_id, limit)` / replay/session surface确认caller与session。把caller/session/实际artifact标识交由这个runtime-owned公开入口执行现有authorization和lineage检查并透传拒绝结果；TUI不自己判断权限、不复制policy、不从SQLite、Store、inspection coordinator private API或raw artifact path取数据。授权读取后由只读detail overlay分页显示实数据；`TranscriptPage`仅是entry metadata，不等于tool body。
- 具体 artifact 内容保持 `Rich`安全的文本/Markdown/diff presentation；不要把返回的raw terminal bytes直接写到PTY。不存在artifact或该次tool未持久化detail时，仅显示runtime实际公开的可见result，不能捏造展开。

## 依赖有序的完整实施阶段

阶段是互相依赖的完整产品交付包，不是降低验收的 MVP，**P0→P5全部完成才叫Done**；无空壳基础设施 PR、未来preset、stubs、temporary mode。
本文上方 JSON 是最终完整产品的目标合同，不是 P0 即刻接受的配置。阶段是内部依赖顺序、不是分阶段发布许可：每个字段、enum、segment、action、schema/API声明必须与其完整真实renderer/dispatch/help及 caller migration 在同一原子切换中引入；消费者未落地前不得加入可接受schema/config，也不得留下no-op/“暂不可用”值、stubs或平行reader。每个阶段只交付可独立启用且行为完整的用户能力；全部P0–P5完成、迁移调用点后才发布最终目标合同。

### P0 — 一个 user-visible config/action合同，删除旧偏好路径

**Owner/files:** `src/voidcode/runtime/config_models.py`、`src/voidcode/runtime/config.py`、`src/voidcode/tui/app.py`、`src/voidcode/cli/commands/tui.py`中真实config消费者；`scripts/generate_config_schema.py`及其产物`schema/voidcode.config.schema.json`；若runtime config经public API暴露，则一并更新该schema的现有owner/generator和真实API consumers；需同步`docs/contracts/runtime-config.md`与`src/voidcode/tui/README.md`。

**Cutover:** 只清理当前已有偏好theme reader，迁移现有dark/light palette选择到即时可用的`theme.mode: dark|light` + 精确dark/light slot结构；P0 mode不接受`auto`，两个slots只接受当前dark/light built-ins；`custom/symbols`、theme catalog、terminal probe、statusline、composer/cursor、brand fields均不接受。修正`app.session.tree` allowlist与其现有dispatch；将真实`app.help` action及catalog落在同一切换，F1/F2/F3不作为默认键位。旧`preferences.theme`在caller迁移完成后拒绝，不保留alias、自动转换、silent fallback或平行reader。

- **Exit:** 新theme configuration仍能立即真实选择当前支持的dark/light theme；`app.help`实际打开帮助overlay，`app.session.tree`经过config preflight并可打开selector；仅发布上述已实现schema/API字段，每枚枚举/动作都有实际行为。普通无效用户TUI配置在terminal raw mode前被拒绝，给出可操作的field/value/config scope/path诊断、非零退出并保持terminal未改变，不泄漏内部Python traceback；unexpected/internal failures保留诊断，不压制异常、不fallback、不自动修复输入。只跑owner已有的configuration shape/preference/schema检查，不给未来字段留stub或no-op。

### P1 — 完整颜色/符号资源、严格resolve和package provenance

**Owner/files:** `src/voidcode/tui/theme.py`单resolver、`src/voidcode/tui/themes/*.json`及`LICENSE.oh-my-pi`资源、`runtime/config_models.py`/`runtime/config.py`/schema generator与owner产物（仅同一P1变化发布真实palette/symbol fields）；`pyproject.toml`打包配置。不新建palette-specific legal schema、notice framework、第二颜色模块或“lab”renderer。

**Cutover:** 同一P1 changeset直接引入100个上游实际palette JSON、保留精确名称/mode/颜色与repo+commit+source file mapping；只删除consumer未使用的token/结构，不改色值凑数。按 pinned root MIT license复用并随包交付原copyright/license text；若实施时实际发现specific exception notice，按其具体条款保留或调整真实有冲突的那项，不扩大为100项预先法律调查。另行增加原创VoidCode暗/浅/高对比families。同步发布final `theme.mode/dark/light/custom/symbols` fields与真实renderer：auto OSC11/`COLORFGBG`选择、strict slot/custom modes和DAG、terminal-default surfaces、unicode/ascii/nerd preset及known-key overrides；无palette identity/color placeholders。
**Exit:** 100个palette名各自对应其 pinned source JSON和原始可见配色，身份/mode准确可解析；原repo MIT `LICENSE`版权/许可随wheel/sdist数据交付；VoidCode原创family区分明确。custom variables/symbols真实影响全consumer渲染，坏引用/control bytes拒绝；truecolor/256/16/no-color真PTY可读。只处理实施时实际发现的specific exception notice，不设逐palette legal audit或假色替换关卡。

### P2 — terminal geometry、native cursor和单一视图布局

**Owner/files:** `src/voidcode/tui/term.py`(所有TTY control/pos/style/restore)、`region.py`(transcript commit与动态尺寸)、`composer.py`(input layout/cursor cell)、`app.py`(single frame scheduling/layout)、必要的`transcript.py`、`runtime/config_models.py`/`runtime/config.py`及schema generator（仅同一 changeset公开实际native-cursor field）。

**Cutover:** 一次性实现structured frame/transcript-layout隔离、single Terminal writer与native cursor owner。composer主输入和任意可编辑overlay都返回cursor cell/row/visibility/shape；默认`composer.cursor.shape=terminal`纯继承，不改变进入前终端外观；用户显式选bar/block/underline时，在同一实现变更里通过有界、scoped、Terminal-owned probe mode验证终端支持并可精确restore；必须在报告unsupported前恢复所有终端flags，且probe失败时不启动interactive UI；没有software fallback。`composer.cursor.shape/blink`字段只有在此consumer已落地的同一schema/API cutover才接受。自动wrap/sync输出/raw mode/keyboard协议/cursor生命周期同owner恢复。
**Exit:** typed frame完全取代partial `rows: list[str]` editor/cursor ownership；任一主composer或selector/history editable filter的cell正确，cursor-only diffs repaint；resize/scrollback/overlay/height collapse/focus rollback后仍在正确可见row/cell。80×24/40×12/40×8/宽窄/20×6覆盖geometry；20×6及以上approval controls全部可用，低于minimum safe-hold不丢状态且不拦截既有显式取消/deny/quit。验收覆盖active-run显式Esc cancellation及emergency quit后的完整terminal restoration；真实退出/异常恢复所有终端flags与原生cursor，不渲染假的光标。

### P3 — 全八种 composer 结构与原生编辑语义

**Owner/files:** 唯一`composer.py`editor state machine/frame-data registry、`app.py`布局与状态宿主、`keys.py`现有input protocol、`runtime/config_models.py`/`runtime/config.py`和schema generator（仅与八种实际renderer同一changeset发布`composer.style/gutter/padding`字段）；没有第二editor/widget/frontend。

**Cutover:** 一次性把固定`╰─ `、单band胶水布局换为八种`box/band/claude/pi/borderless/rule/field/rail`封闭frame definitions及统一状态/数据owner。八个style与`composer.style/gutter/padding` enum 同一变更并已实际渲染后才入schema；状态presentation、主题、形状、光标正交，不因切换style改编辑行为。主composer/session picker/session tree/prompt-history filters使用同一editing engine和Terminal-owned native cursor；去掉所有filter `▌` suffix；chevron/radio/selected highlight是selection glyph，不是caret。默认draft没有`Ask voidcode...` microtext。
**Exit:** 八个style都实际工作；所有editable filters和主composer shape/cell/style/visibility一致，无hidden renderer。focus切换、preview rollback、cursor-only diff、overlay resize/collapse后caret可见且行cell正确；纯selection overlays无text cursor。验证八种shape下原始 Unicode/whitespace/paste/commit语义合同，无config accepted but unrendered style。

### P4 — statusline产品profile、approval/viewer和可发现操作

**Owner/files:** `statusline.py`单registry/resolver、`app.py`单action metadata/dispatch/frame/data projection、`overlay.py`现有overlay/action/help models；`runtime/config_models.py`/`runtime/config.py`及schema/API现有owner generator仅同一changeset发布有实际consumer的profile/segment/picker/save fields与action；`events.py`只投影现有runtime事件；同步`src/voidcode/tui/README.md`。

**Cutover:** P4在同一changeset里完整引入Codex/Claude与minimal/compact/full/ascii/custom renderers、真实来源segments/options/固定overflow、透明statusline、action/help和`app.tui.appearance` picker；此时才发布statusline schema/IDs及picker Save action，key bindings由用户配置，不提供F-key defaults。单overlay中方向键预览mode/dark slot/light slot/profile/composer/cursor；Esc取消还原预览，Enter把选择仅应用至当前TUI未来/live未提交view；显式Save是独立动作，先选`user|workspace`再调现有config writer。`app.help`显示当前实际值与auto source/fallback。`app.py`只投影runtime数据，无真实数据就隐藏segment；detail viewer将caller/session/artifact IDs交给runtime现有public reads，authorization/lineage完全由runtime判定，TUI不复制权限逻辑。保留Esc cancel与streaming submit→runtime steering queue；approval/question固定decision rows、正文分页，history immutable不重画。

**Exit:** 每个可接受profile/segment/binding都已能真实渲染、配置、在picker/help发现；builtin defaults保留真实`mode` label，custom只展示显式选定字段且允许有效省略；approval mode在`app.help`输出可见（仅当用户配置该action），实际approval dialog始终展示真实target/reason/parameters与controls，runtime authority不受UI preference影响。F1/F2/F3无默认绑定。透明背景用terminal default，无固定endcap。40×8或其他高面板测试中目标/理由/decision/help均完整可见，低于20×6转safe-hold且pending state不变，除非用户显式cancel/deny/quit；验收覆盖active-run显式Esc cancellation及emergency quit/restoration。artifact detail返回runtime真实分页数据；仅有已授权内容展示，拒绝状态原样呈现。

### P5 — Fresh/resume brand、完整终端验收、文档/旧路径收尾
**Owner/files:** `app.py`fresh/resume/live identity；在既有theme/composer数据中使用一行原创VoidCode brand；`runtime/config_models.py`/`runtime/config.py`和schema generator只在同一P5 changeset引入brand consumer实际使用的`brand.enabled`；`docs/contracts/runtime-config.md`、`src/voidcode/tui/README.md`。OpenAPI/frontend只在配置经public API暴露时经现有owner generator更新；不建web UI。


**Cutover:** P5同一变化接入`brand.enabled` renderer/schema/config与doc；fresh interactive TTY无transcript时显示一行原创wordmark，窄屏完整显示，首次prompt后隐藏；resume/noninteractive/quiet不显示。clean cutover删除旧theme path和过期terminal/cursor comments，不保留旧config alias或版本特定的OMP cursor假设；保留append-only/history/runtime-owns-slash边界。

**Exit:** 最终包包括原始100 palette数据/精确repo-commit-file映射及完整上游MIT license notice、8个真实composer shape、完整profiles/native cursor/appearance picker、brand和action/help路径；fresh/resume/TTY/API exposure语义明确；最终冻结实现通过**完整 `mise run check`、无`--no-verify`的全仓库正常 pre-commit，以及完整 native TTY/CLI acceptance matrix**。下方记录的局部源码检查和PTY smoke不替代这些release gates。

## 不降低范围的整体验收矩阵

| Surface/边界 | 稳定用户可见合同 | 实施验证证据 |
|---|---|---|
| Theme目录 / custom palette | 100目录名可通过schema精确选择；upstream颜色/identity不改，VoidCode原创暗/浅/高对比系列单独命名；完整consumer tokens可组合自定义；错误config具体拒绝；所有资源随package安装 | 使用已有owner测试校验generated schema、config precedence/unknown rejection；逐项解析100个palette并比对来源JSON/mode；构建wheel/sdist确认100项数据及上游完整MIT版权/许可文本被打包；实PTY检查颜色关键surface |
| 色深/透明 | truecolor、256、ANSI16、no-color 可读；默认Status ANSI使用terminal default BG，不输出status rgb background/endcap；没有像素不实承诺 | 各能力真实PTY输出抓取SGR；记录concrete terminal+TERM+tmux设置截图/字符状态；非GPU visual QA检查真实terminal颜色叠加与foreground contrast |
| Layout组合 | codex/claude/minimal/compact/full/ascii/custom分别可选择；layout、custom segment/separators、theme、shape互不切换；宽/窄overflow保重要状态 | 80×24/40×12/窄宽真PTY captures与cell map；验证不存在重复state/status host或超宽行 |
| Composer/editor/overlay input | 八种框架；prompt无默认microtext；主编辑器和可编辑overlay filter均由单native硬件caret及用户选择shape；selection glyph非caret；Unicode、paste、多行、空白/Tab按原意传输 | 各形状actual cell rendering 80宽与minimum width；主composer/session picker/session tree/prompt-history filters统一验证native cursor position/style/visibility、焦点切换、preview rollback、cursor-only diff、overlay展开/收起和height resize后的行/cell；字节级比较draft与runtime request；挂起term恢复shell再检查，注明terminal型号和TERM |
| Running/steering/cancel | Streaming时输入可编辑；submit使用现有steer queue并显示`Steering queued(N)`；无session和runtime拒绝有告知；Esc cancel仍走runtime，approval/turn queue不新语义 | 离线真实CLI/TUI runtime scenario，验证申请和message/status文本以及runtime stored/effective state；做运行时治理专属持久化行为用现有 runtime tests，不做fake-provider UI通路 |
| Approval/question | auth targets/reasons/args/selection透明；builtin statusline默认保留真实mode，custom可依用户选择省略status fields；approval mode可由已配置的`app.help` action显示，实际approval dialog始终展示target/reason/parameters与controls，runtime authority不因UI偏好改变；F1/F2/F3没有默认绑定；20×6及以上decision/actions/help完整可见；低于20×6 safe-hold阻止普通编辑/submit及不可见actions，pending state不变，除非用户显式cancel/deny/quit | ask mode真实PTY尺寸80×24、40×12、40×8、20×6及低于minimum；验证safe-hold下普通编辑/submit被阻止、pending state保留，以及active-run显式Esc取消和emergency quit后的terminal restoration；safe offline/deterministic allow/deny检查不调用外部provider；问题使用现存真实生产方法或runtime test producer，不能伪造状态 |
| Long tool detail/history | collapse可知范围；immutable prefix不许重画；已有授权artifact通过read API真实page，落盘数据仍in runtime | 真实 read命令产生store-owned artifact，viewer翻页显示确实artifact行内容和next offset/sha边界；改无权caller /无artifact确认不暴露DB详情；终端escape/control payload原样不流出protocol |
| Action/help/config | `TuiCommand`, parse, dispatch, docs, schema一致；session tree能配置；`app.help`和`app.tui.appearance`按用户keymap配置工作；F1/F2/F3不默认绑定 | CLI preflight `ctrl+t -> app.session.tree`并PTY打开selector；为`app.help`/`app.tui.appearance`设置私有test keymap后验证help、picker preview/Esc rollback/Enter session apply/user-or-workspace Save；另验默认 keymap 不含 F1/F2/F3；普通无效用户TUI配置必须提供可操作的field/value/config scope/path诊断、非零退出且terminal未改变，不泄漏内部traceback；unexpected/internal failures保留诊断，不压制异常、不fallback、不自动修复输入；action review确认每个enum有dispatch/help |
| Fresh/resume/no pollution | brand只在fresh interactive无history显示；resume history正常；brand/prompt hint永不作为conversation/provider内容 | fresh/interact CLI和真实session resume PTY；runtime-received prompt/transcript不含brand、workspace banner或editor help；resume前后scrollback无多一条虚假用户消息 |
| TUI/CLI release | NO_COLOR/TERM=dumb/non-TTY仍能清晰运行/明确不可交互；80×24/wide/wrap; Ctrl+C/Esc结尾protocol/cursor cleanup; terminal native restore完全 | `docs/testing.md`要求的真实CLI/TUI人工门禁：隔离用户全局/workspace/state/config，offline真实runtime；PTY热键/clipboard, resize, overlays; evidence列实际终端型号、env、宽高、输入指令、截图/ANSI细胞结果；保留跨Emulator/tmux pixel vary局限 |

**验证约束：** `docs/testing.md`允许为稳定、可观察的编辑器/profile/input 与 CLI/TUI 行为添加小型回归；进程/PTY测试必须运行真实命令并使用隔离state。不得建立永久snapshot/mock UI框架，也不得用假terminal response或fake runtime UI通路替代真实CLI。native交互必须在隔离state下通过PTY/原生console验收。P0–P5实现必须完成完整 `mise run check`、普通全仓库pre-commit与下表完整验收矩阵；当前局部检查/烟测只记录已观察证据，不表示完整matrix已通过。

## 当前实施与验收状态

P0–P5 的用户可见实现与旧路径 clean cutover 已完成：typed config/action、主题/颜色资源、terminal geometry 与 caret、八种 composer、statusline profiles、approval/question/artifact/history、appearance/help、brand 与小终端 safe-hold 均由现有 consumer 实际使用。用户可见 `RuntimeMode` / `/plan` 与默认 F1/F2/F3 绑定已删除；request-scoped `read_only` 及作为 wait/approval/question 生命周期投影的 `PlanState` 保留。

- **完整本地 gates:** post-fix 正常 all-files pre-commit、supplemental pre-commit 均 exit 0；`mise run check` exit 0，1,910 passed / 2 skipped，pytest 150.18s、所有配置的 Python/frontend/schema/API tasks 共 151.13s。日志在 `/tmp/voidcode-postfreeze.dSNPl1/logs/precommit-all-final-prompt-fix.log`, `precommit-supplemental-final-prompt-fix.log`, `mise-check-final-prompt-fix.log`。新增 ZIP prompt-resource 回归包含在此 suite 中；独立 `tests/unit/agent/test_prompt_compose.py` 为 5 passed in 2.26s。
- **Linux native evidence:** 真实 80×24 Linux/tmux PTY 对 7 个 statusline profiles × 8 个 composer styles 做 empty/draft-visible 两轮，共 112 captures；每个 profile/style 都显示唯一状态行。原始 bracketed hostile paste、已授权工具输出的控制字符 escaping、title/termios 恢复记录在 `local://tui-current-matrix-control-smoke.md` 与 `/tmp/voidcode-status-matrix-42z67_tp/proof.json`。原始草稿/硬件 caret 对齐、resize、安全 hold、approval、artifact paging、scrollback 和 cancel 细节见 `local://tui-native-evidence-index.md`；默认 keymap F1/F2/F3 unbound 的实际 PTY 证据见 `local://tui-default-key-cutover-proof.md`。
- **Package resources:** VCS-aware wheel/sdist 均通过真实构建；各有唯一 archive members，104 个 palette/index/license/NOTICE 资源与 5 个 Web bundle 文件逐字节匹配。隔离 `python -I` 从 ZIP wheel 导入并 materialize 六个 builtin prompts；100 palettes × 42 roles × STANDARD/EIGHT_BIT/TRUECOLOR/NO_COLOR 完成 16,800 renders，144 个 `voidcode` module origins 全部来自 wheel。证据：`/tmp/voidcode-postfreeze.dSNPl1/logs/build-vcs-prompt-fix-final.log`, `package-integrity-prompt-fix-final.log`, `wheel-import-smoke-prompt-fix-final.log`。
- **Extracted-wheel Web consumer:** private harness 用 `.venv/bin/python -I -u` 启动真实 CLI `voidcode web`，`sys.prefix` 保持原项目 `.venv`，不依赖 editable fallback；实际输出 `Local server running at: http://127.0.0.1:47023`，HTTP `/` 为 200 且包含 `<!DOCTYPE html>`，两个 GET 均为 200。入口前与 CLI 运行期间检查的 270 个 `voidcode` module origins 全部在 wheel extraction 内。Harness 按设计发送 SIGTERM 后观察到 child return code `-15`、已 reap 且 stdout 到 EOF；这不是 CLI 自然退出码。原始结果：`/tmp/voidcode-postfreeze.dSNPl1/logs/web-launcher-smoke-final-prompt-fix2.log`、`.exit`；CLI 输出：`/tmp/voidcode-postfreeze.dSNPl1/launcher-smoke-final-d502gq2c/launcher-output.log`。
- **平台边界:** 可复核的完整应用 PTY 矩阵是 Linux/tmux；Windows Console 与 macOS 原生验收未运行。此前 Kitty 0.49.2 DSR/terminal lifecycle 协议证据见 `local://tui-terminal-lifecycle-proof.md`，但没有新的 Kitty application GUI 或像素/截图验收。本状态不宣称 Windows/macOS parity 或完整跨平台 native matrix 通过。
