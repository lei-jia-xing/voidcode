# TODO

本文件集中跨模块取舍与导航；具体待办、现状证据和验收条件放在所属模块。新增项不代表功能已实现或已经实测。

- [Core](../src/voidcode/core/TODO.md)：可选 provider-turn cap 与显式共享 root-action grant 已实现；adaptive model-headroom 已实现（per-dispatch 自适应收缩）；输出投影剩余事项及真实生产化边界继续按专属 TODO 跟踪。
- [SDK](../src/voidcode/sdk/TODO.md)：`BackgroundContext.wait_tool` 的后台 action 结果等待已实现并经 Core/外部插件验证；普通前台 callback 的 suspend/恢复与完整 `eval` 产品仍属 P1。adaptive headroom 已实现，结果 schema 等表达力缺口仍开放；typed child budget outcome 与 owned first-settled child wait/atomic delivery 已实现，含精确事件来源、重开恢复与相邻 `join_child` polling gap 已修复（事件驱动）；typed child output/history capture errors 及 review/task/batch/wait/agent:// 透传与 workflow 持久化亦已实现，snapshot 当前-run 诊断为显式剩余缺口，见 SDK TODO。
- [插件](../src/voidcode/plugins/TODO.md)：仅记录已验证的具体整改与仍未收敛的图片/UI、生产化、URI、Agent、进程和检索任务，不宣称全插件审计。
- [TUI](../src/voidcode/tui/TODO.md)：独立记录图片渲染与实时进度/交互边界；viewer/API 不替代每条路径的真实 PTY 验收。
- [Web UI](../frontend/src/core/TODO.md)：artifact 查看/复制/导出与真实浏览器验收。
- [ast_grep](../src/voidcode/plugins/ast_grep/TODO.md)：只读查询边界。
- [命令交互验收](./command-ux.md)：逐命令客户端验收清单。

## 取舍

要吸收的不是一批同名工具，而是 **可组合的工具调用、统一可寻址的资源、可持续交互的 Agent，以及消费这些契约的 TUI**。只补皮肤、增加 shell 包装器或堆插件数量，不能补上这条链路。

当前已有 typed tool body、`OutputBounds`、内容寻址 artifact、可注册 `ResourceReader`、工具进度回调、子 Agent 跟进/完成通知、动态 workflow、命令表单，以及两端长输出分页；这些不是从零重建的待办。已完成项保持勾选，不把“图片输入已通”混同于“终端可以显示工具返回的图片”。

已核实的关键断点：普通 SDK `invoke_tool` 仍只排队；后台 action 可用 `BackgroundContext.wait_tool` 安全等待完整、request-correlated 结果，但这不是普通前台 callback 的 suspend API。`ResourceReaderServices` 已提供 caller-scoped child/process read projections；Subagents `agent://` 与 Exec `proc://` 复用注册、artifact 分页和 owner/session 边界。通用资源能力发现/枚举/搜索仍开放；`ToolOutput` 组合输出（有序 parts + 有界声明式视图 + 两端渲染）已实现，见 SDK TODO。Core 已删除隐式 32-turn 默认和普通图片文件 50 KiB 拒绝，root-action grant 已持久共享并通过重启/并行 child 验收；`ArtifactPage` 已贯通 Core、SDK 本地/RPC。active-model 自适应投影 headroom 已实现，仍开放结果 schema。Typed child budget 与 owned first-settled wait/atomic result delivery 已由精确 durable facts 贯通；native Core proof 覆盖三个 gated child、52,000 个 UTF-8 字节、不可变输出 URI/read/reread、去重与 reopen。等待事务只在有效 tool result 提交时保存精确 source event receipt；读快照不消费。Typed child output/history capture errors 已贯通 SDK outcome、review/task/batch/wait/agent:// 与 workflow 持久化，preview 独立于 artifact 发射；单 run `join_child` 已改为事件驱动、无 polling；accepted-yield、shared batch context、peer/process waits、snapshot 当前-run 诊断与真实 UI 交付仍开放。

## 优先级

| 优先级 | 落地点 | 归属 |
|---|---|---|
| P0 | 自适应模型投影 headroom（已实现）、资源完整读取与真实 UI/生产任务验收 | Core、插件、TUI/Web |
| P1 | 前台 suspend/完整 `eval` 工具桥、URI 跨工具复用、Agent 持续交互、富结果与结构化问答、后台执行收尾 | SDK/Core、插件、TUI/Web |
| P2 | 多格式读取、语义检索、结构化模型查询、语义重构闭环 | 插件、SDK |

后台 action 的 SDK 工具等待协议已实现；完整 `eval` 的普通前台 suspend/恢复仍属 P1。混合图文结果与完整问答分别依赖 SDK 的组合输出、用户问答契约。按模块待办中的依赖落地，不先造一个覆盖所有场景的平台。

## 实施约束

- 保留 plugin-only Core、SDK 依赖方向、owner/lineage/revision 隔离、可撤销 lease、显式审批和 append-only 事件；产品机制不下沉为 Core 内置工具。
- 优先用已有 body、资源页、进度、子会话与 background action 契约；公开契约不够则先补 SDK TODO，不导入 peer 插件、不解析私有 `core.*` 事件、不维护第二套能力或权限注册表。
- 不照搬 undo/fork/历史重写、OMP 的双存储体系，或把所有 scheme 都做成可写文件。任意脚本执行是执行能力，工具桥和审批不是 OS 沙箱。
- 以完整任务验收：发现能力 → 调用 → 等待/交互 → 查看完整结果 → 跟进/取消 → 断线/重启后的真实状态；UI 要在真实客户端观察，缺依赖、未知状态和不能恢复必须显式呈现。

## 对照证据口径

历史条目的 OMP 源码基线固定到 [v18.12.6（commit `45c35e40c46166dc726ccc52f5d27f40f2449f99`）](https://github.com/can1357/oh-my-pi/tree/45c35e40c46166dc726ccc52f5d27f40f2449f99)；当时隔离运行实际打开过 `/model` 选择器，其他对照仅为源码核对。

Agent `agent://` 对照固定到 OMP v18.20.2 源码 commit [`7c49c11aa5bbaf3f418406583096168ad5b8c6dc`](https://github.com/can1357/oh-my-pi/tree/7c49c11aa5bbaf3f418406583096168ad5b8c6dc)，并用当前官方 runtime 文档核对 `agent://` 最新 published output 与 `history://` 跨轮 transcript 的区别；不宣称本轮实跑 OMP。其他本轮新增条目核对 VoidCode 当前源码/契约与当前会话可读取的 OMP 官方运行时文档，不宣称这些新文档对应旧版 v18.12.6，也不宣称本轮实跑 OMP 图像终端或 `eval` 场景。

## 不以添加 MAX 代替设计

| 限制的性质 | 决策 |
|---|---|
| 真实协议、解码、内存或外部输入风险边界 | 只在负责该边界的一层保留有依据的保护；说明单位、来源与触发行为 |
| 用户/宿主明确授予的费用、token、时间或轮数预算 | 明确授权与继承；不能偷换成所有任务默认都有的寿命 |
| UI 预览、折叠、分页与模型上下文投影 | 属于消费策略；原件、资源身份及完整读取能力不变，按视窗/请求/上下文选择 |
| 无依据的业务 MAX、重复预览、预览截断原始标识 | 删除或改成按需读取；不是把常量改大，也不是每个数字都加配置项 |

本轮只抽查并逐条记录 shell/web/child 输出、monitor、AST、goal 及 Core 投影的行为与证据；不宣称审完所有插件或删除所有 MAX。已有完整 artifact、UTF-8 游标分页和 UI 展开是应复用的能力，不能为了删常量而删掉资源保护。

OMP 自己也有 request budget 与 preview 阈值；它们不自动构成 VoidCode 的合理默认值。新待办中的“有界”必须对应实际消费者或资源风险，不能成为继续机械添加 `MAX_*` 的理由。

项目自己写下的帧大小、preview 或输入常量，不能反过来作为“这是协议硬限制”的证据；保留限制需要可核查的来源或资源风险，默认值也需要独立理由。

## Hook 与 staged-tree 隔离观察

首次 Core/SDK 阶段提交时，repo-wide pre-commit 会自动暂存 tracked unstaged diff，但 Ruff/ty 仍扫描工作树中的 untracked 新模块。实际失败是 `artifact_viewer.py` 已导入 `KittyImagePlacement`，而未暂存的 `region.py` 尚无该纯 frame placement 数据契约。通过把经审阅的 `region.py` 作为完整 foundation 文件提交，并修复有限格式问题后，所有原配置 hooks 均通过；未绕过或弱化检查。

这是 staged-tree/worktree 隔离脆弱点，不是运行时或 SDK 缺陷。后续可研究 exact-staged-tree/isolation，但必须保留完整 Ruff、ty、API 检查；不得靠抑制导入或降低 hooks 规避依赖。
