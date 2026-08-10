# VoidCode 与 Oh My Pi 对比及产品化优先级

本文记录 VoidCode 与 Oh My Pi（OMP）的阶段性对比，并将结论转化为可执行的产品化优先级。它不是功能追平清单，也不改变 VoidCode 既有的 runtime-centric 边界。

## 调研基线

- VoidCode：当前仓库实现，以及 [`current-state.md`](./current-state.md)、[`roadmap.md`](./roadmap.md)、[`context-tool-design-backlog.md`](./context-tool-design-backlog.md) 和 [`deliberate-omissions.md`](./deliberate-omissions.md)。
- Oh My Pi：[`can1357/oh-my-pi`](https://github.com/can1357/oh-my-pi)，调研快照为 2026-08-06 的 commit `3a8591a8af5b6d200088d12ca75a5517cb064fa8`。
- OMP 的工具成功率、token 降幅、provider/tool 数量等数字来自其项目自述，不视为独立 benchmark 结论。

## 核心判断

VoidCode 的主要优势是运行时治理：持久化、恢复、审批、事件、策略快照、父子会话，以及 CLI/Web 共享 runtime truth 都已经具备较清晰的契约。

OMP 的主要优势是模型执行真实任务时的成功率与产品体验：编辑协议、结构化读取、LSP、终端交互、模型路由、上下文压缩、子任务隔离和工具呈现形成了更完整的闭环。

因此，VoidCode 当前最重要的方向不是继续扩张能力面，而是：

> 把现有可靠 runtime 转化为一个模型容易使用、用户开箱可用、真实改码成功率可度量的产品。

## 能力对比

| 维度 | VoidCode | Oh My Pi | 当前判断 |
|---|---|---|---|
| 核心架构 | Python runtime-centric，治理边界清晰 | TypeScript/Bun 加 Rust native core，产品与工具高度集成 | VoidCode 更规整，OMP 更成熟 |
| 真实改码工具 | read/edit/apply_patch/multi_edit/grep/AST 等已具备 | Hashline、结构化 read、AST preview、冲突 URI、Git URI | OMP 明显领先 |
| 工具成功率 | 缺少统一 benchmark；部分 schema 和输出契约仍待统一 | 围绕不同模型优化，并公布编辑成功率数据 | VoidCode 当前最大差距 |
| LSP | runtime-managed 基线，以只读能力及编辑后诊断为主 | rename、code action 和 write lifecycle 深度接入 | OMP 明显领先 |
| 调试 | 无完整 DAP 产品面 | 可驱动 lldb、dlv、debugpy 等 DAP adapter | OMP 领先，但不是近期 P0 |
| Shell/执行 | shell、interactive shell 和后台任务已有 | 持久 shell、PTY、内建 coreutils、跨平台 native | OMP 体验更完整 |
| Provider | 多个直连 provider、LiteLLM 和 fallback | 大量 provider、OAuth/订阅计划、本地模型和角色路由 | OMP 明显领先 |
| 首次配置 | 有 doctor/readiness，但仍有手动配置成本 | install/setup/login/model picker 一体化 | OMP 领先 |
| 会话持久化 | SQLite、事件真相、checkpoint、resume 和 bundle | JSONL session tree、fork、branch、resume 和 export/share | 各有优势；VoidCode 治理契约更强 |
| 上下文压缩 | deterministic continuity 和截断，完整产品语义未收口 | 模型摘要、branch summary、native compaction、checkpoint/rewind | OMP 明显领先 |
| 长期记忆 | workspace 文件或扩展点，不属于 runtime core | retain/recall/reflect/learn，多种后端 | 战略不同，不需要立即追平 |
| 子代理 | 固定角色、background task、parent/child session、通知/重试/取消 | 并行 fan-out、隔离 workspace、typed result、agent URI 和通信 | VoidCode 有治理骨架，OMP 产品化更强 |
| 权限与恢复 | 策略快照、审批连续性和 replay 语义较强 | 完整交互审批并支持 ACP | VoidCode 的现有优势 |
| 扩展体系 | skills/hooks/local tools/MCP/ACP 分层存在 | Extension API 可注册工具、命令、UI、provider，并有 marketplace | OMP 显著领先 |
| MCP | runtime/session scoped，config-gated | 更完整的 lifecycle 和工具挂载体验 | OMP 产品化领先 |
| 客户端 | CLI 完整，Web 已走真实 runtime，TUI 较早期 | 强 TUI、one-shot、RPC、SDK、ACP 和协作 Web | Web 不是核心差异化；应优先保证共享 runtime 下的任务闭环与客户端一致性 |
| Web/协作 | 本地 Web 支持 session、review、审批和 child session | 加密 relay、浏览器旁观/协作和二维码 | 产品目标不同 |
| 浏览器/桌面 | web fetch/search | 浏览器、Electron 和桌面控制 | OMP 领先，但应后置 |
| 跨平台/性能 | Python 加外部工具，维护成本较低 | 大量 Rust 内建能力，Windows 无需 WSL | OMP 更强，但复制成本极高 |
| 测试与治理 | 契约测试、fake provider/MCP、严格 replay 较突出 | 大型成熟项目，强调工具 benchmark | VoidCode 的工程基础应继续保留 |

## 优先事项

### P0：建立真实任务成功率工程体系

建立固定、可重复运行的 agent coding 任务集，至少覆盖：

- 精确修改已有函数；
- 跨文件重命名；
- 修复测试失败；
- 修改后运行 formatter、typecheck 和 test；
- 大文件定位与局部读取；
- stale edit、并发修改和错误工具参数恢复；
- 中途审批、context compaction 和 session resume。

每次运行至少记录：

- 任务成功率和最终 diff 正确性；
- 工具调用及重试次数；
- 输入/输出 token 和 provider cache 命中；
- 总耗时和首个有效修改耗时；
- 是否需要人工纠偏；
- compaction/resume 后是否继续保持任务主线。

没有这套基准，就无法判断 prompt、tool schema、LSP 或 compaction 修改是否真正提高了产品能力。

### P0：重做 read/edit 工具协议

围绕现有 [`context-tool-design-backlog.md`](./context-tool-design-backlog.md) 收口以下问题：

1. 将路径字段统一到 `path`，为 `read_file.filePath` 设计兼容迁移。
2. read 同时提供结构化原始行、稳定定位信息和简短的人类展示内容。
3. 统一 `content` 为短摘要，机器可消费 payload 放入 `data`。
4. 为所有输入 schema 补齐 required、enum、约束和字段级描述。
5. 编辑调用携带内容锚点、revision 或 hash，发现 stale edit 时拒绝落盘并返回可恢复信息。
6. 大文件默认返回结构摘要、相关片段和稳定 continuation，不无界倾倒内容。

不要求直接复制 OMP Hashline，但必须解决带展示前缀文本被错误复制、字符串匹配失败、旧内容误改和无效重试等核心问题。

### P0：让 LSP 进入每次写入闭环

优先交付：

1. rename 和 workspace edit；
2. definitions、references 和 symbols；
3. code action；
4. edit/write 后自动收集新增 diagnostics；
5. diagnostics 进入当次工具结果，使模型可以立即修复；
6. rename/file move 遵守 `willRenameFiles` 等 LSP 生命周期。

这比 DAP、浏览器或增加 provider 数量更直接地提高真实代码修改成功率。

### P0：完成开箱可用的首任务流程

将 `voidcode doctor` 从诊断报告推进到可执行的修复路径：

- 交互式 provider 登录或 API key 检测；
- 模型选择和可用性验证；
- 自动选择合理默认模型，避免 Web 默认绑死单一模型；
- 自动探测 workspace、git、formatter 和 LSP；
- 第一个任务前只暴露唯一、明确的阻塞原因和下一步；
- CLI 与 Web 使用相同的 readiness/config contract。

目标是新用户安装后五分钟内可以完成一次真实代码修改。

### P1：产品化上下文和长会话连续性

在保留 append-only session truth 的前提下改进 context projection：

- compaction 生成结构化 handoff，包括目标、已完成事项、未解决问题、修改文件、关键诊断和下一步；
- 支持 branch/fork，或至少提供稳定的 checkpoint/rewind 用户语义；
- 对 compaction 前后的任务继续能力建立 benchmark；
- 将 provider cache identity 真正接入请求，并报告 cache hit；
- 调整 prompt assembly，使稳定段位于动态边界之前；
- 缓存 git/environment observation，只在 workspace 发生有效变化时刷新。

应重新评估 [`deliberate-omissions.md`](./deliberate-omissions.md) 中“永不使用 model-assisted distillation”的绝对结论。模型辅助摘要可以是受治理、可选的 context projection 策略，不应改写持久化事件真相。

### P1：完成受限子代理产品闭环

不扩展为任意拓扑，继续基于现有固定 child preset：

- worker 使用隔离 worktree/workspace，或显式只读模式；
- task 支持 JSON Schema 类型化结果；
- parent 直接读取结构化 findings，不解析自由文本；
- Web 显示实时状态、耗时、成本、摘要和 child transcript；
- 定义明确的合并和冲突处理语义；
- 用 benchmark 判断何时并行优于单 agent。

VoidCode 已经具备 lineage、通知、重试和取消，下一步重点是可靠产出，而不是增加 agent 角色数量。

### P1：完善 CLI 与 Web 的客户端闭环

近期不以客户端形态作为主要差异化，也不同时追求 OMP 级 TUI 和全面成熟的 Web。沿用 CLI + Web 主路径，把投入集中在真实任务闭环和共享 runtime 能力的可靠呈现：

- 为常用工具提供专用卡片，避免通用 JSON 成为主要表现形式；
- edit diff 可审查，并支持明确的接受/拒绝流程；
- approval/question 在时间线上原位处理；
- child session 可以从侧栏钻取；
- token、成本、provider fallback 和 context pressure 可见；
- 提升大仓库 review tree、搜索和文件预览体验；
- 断线重连后 running/waiting 状态保持准确。

CLI 继续承担自动化、诊断和恢复入口，Web 提供审查、审批和长任务可视化体验；两者共享相同的运行时契约与产品能力，不将 Web 本身视为核心护城河。TUI 继续降权。

### P2：统一扩展开发模型

skills、hooks、local tools、MCP 和 commands 已经存在，但用户心智较分散。未来应提供稳定的扩展 SDK/manifest，并明确：

- 可注册的能力；
- 生命周期和故障隔离；
- 权限和父级策略继承；
- 客户端渲染接口；
- session/replay 持久化内容；
- 本地开发、测试和打包流程。

先实现本地可开发、可测试和可分发，再考虑 marketplace。

## 暂缓事项

以下事项当前不应进入产品化主线：

- 复制 OMP 的大规模 Rust native core；
- 以 provider 数量为目标扩张到 60+ provider；
- 完整 DAP 工具面；
- 桌面控制、Slack 或通用浏览器自动化；
- TTS 和图片生成；
- 云端协作 relay；
- 插件 marketplace；
- 任意 agent topology 或 peer-to-peer agent bus；
- 将长期 memory 工具直接加入 runtime core。

它们会显著扩大维护面，但不能优先解决第一次真实改码能否成功的问题。

## 推荐实施顺序

### 第一阶段：建立测量基础

- 真实任务 benchmark；
- read/edit 协议；
- stale edit 防护；
- 基础成功率和成本报表。

### 第二阶段：提高改码闭环质量

- LSP 写入闭环；
- formatter/typecheck/test 自动反馈；
- 首次配置和 readiness 修复路径。

### 第三阶段：提高长任务和客户端闭环

- 结构化 compaction；
- provider cache；
- Web 专用工具卡片和 diff/approval 工作流。

### 第四阶段：产品化受限并行执行

- 隔离 worker；
- typed result；
- child session 产品体验；
- 并行任务收益 benchmark。

只有前三阶段的成功率数据稳定后，再决定是否投入 DAP、浏览器、长期 memory 或 extension marketplace。

## 目标状态

VoidCode 不以拥有和 OMP 同样多的功能为目标。近期目标是：

> 在少数核心改码任务上取得可测量、可重复的高成功率，同时保留更严格的 runtime 治理、恢复语义和跨客户端一致性。
