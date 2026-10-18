# 小内核、插件机制与自举能力实施计划

状态：**已进入实施**。P0–P2 完成并通过完整验收（**FULL P2 已验证**，不属于 P3）；P3–P6 未开始。

P2 实施记录（截至本文件最近一次更新，**完整 P2 已验收**）：

- 内核/宿主：`kernel/engine.py 8f1bdb60…`（当前核心；相对付费活体所用的 `95e72163…` 仅新增 SESSION 内部状态的 read-only/ask 分类，其余不变）、`kernel/host.py 4b4c8453…`、`kernel/storage/store.py 8947131f…`、`sdk/` 目录输入摘要 `b15172ef…`（经父级源码评审批准的冻结版本）。完整 Python 验收 **182 通过**（governance + vertical + unit kernel/plugins/SDK 共 150，加 Goal 27、kernel transport、kernel TUI），不重复计数。
- 官方 goal 插件：`src/voidcode/plugins/goal/goal_plugin.py 4ee8a848…`、manifest `voidcode-plugin.json 5556ecdf…`（`scripts/generate_plugin_identities.py --check` 干净）。27 个行为用例（19 单测 + 8 集成）在本冻结栈上通过，覆盖状态严格解析、窗口化计量、未知计量、预算拒绝/解除、3 轮 blocked 审计（含同一 native batch 只算一轮）、长目标单次传输与篡改检测、第二插件隔离、进程重启保持等。
- 真实付费活体（历史证据，保持原样）：在付费时冻结栈 `engine 95e72163…` 上对 `deepseek/deepseek-flash` 执行**空闲会话 `goal.set`**（无用户 prompt、无手工 drain），目标上下文自行驱动模型读 `17 25` → 写 `42` → 回读校验 → 显式 complete；goal `complete`、tokens 7331、elapsed 6.356s（真实测量）、`cost_usd` 保持 `None`，重启后状态一致且不产生额外工作。之后的引擎 hash 变更（SESSION 分类）不重复付费，只由通用非付费 loopback 用例覆盖。
- 启动验收（一次性脚本，已删除）：生产命令持久化 goal 后关闭；禁用 goal 插件时普通对话正常且请求中不含 goal 上下文；恢复原配置后用原 session 重新打开，目标状态完整、无意外 provider 工作、无残留 continuation。禁用态基座对话使用新 session —— 同一 session 的在线启停属于 P3 范围。
- 客户端证明（已完成，父级验收）：frontend `KernelApp 8ebf0d06…`、`Forma fe0d460…` 冻结；Generic 的 t9 真实全量 bundle 浏览器 `KERNEL_SMOKE_GOAL=1` 通过（1.3 分钟：冷启动恰好 1 个自动 session；同一 SID 的尾斜杠客户端切换；parked 原生审批页刷新后仍是同一 ID；原生 allow/deny；stored confirms；真实首次 `goal.edit` POST budget5000 status=ok；null POST status=ok；同一 goal ID / created_at / 精确 4503 文本）；全 goal PTY 绿（31 commits / 4618 paints，无 settled replay 与重复）；frontend 169 用例 + typecheck/eslint、transport+TUI 24、API/identity 检查全部通过。

本文覆盖小 runtime、机制插件化、热更新、自举闭环与核心升级，不只是新增 `/reload`。

**硬约束：不做任何旧架构向后兼容。** 可以重写 runtime 外壳、SDK、配置、插件声明、HTTP 合同和存储 schema；不保留兼容别名、双读、旧路由转发或双执行路径。复用经过验证的算法与不变量，不迁就旧对象结构。

新架构启用新的状态库，不导入旧 session/composition 数据，不自动转换旧配置。旧文件不自动删除，也不被新内核误读；删除历史数据仍需显式操作。文中“状态迁移/版本校验/降级”仅指**新架构内部**为安全热更新所需的版本转换，不是支持遗留格式。

## 1. 建议结论

采用 **小型可信内核 + Python 插件 SDK + 官方默认插件集 + 可替换的插件宿主**。

- 提取现有 `TurnEngine` 的纯循环，以及 SQLite 事实提交、权限/审批、执行所有权和恢复的正确机制；允许重新组织实现。`VoidCodeRuntime` 及其巨大接口不是必须保留的资产。
- Python 承担类似 Emacs Lisp 的扩展语言角色。复用现有 Python 代码、工具链和包机制，不嵌入 Lisp/JavaScript，也不发明工作流 DSL。
- 官方机制也必须使用公开插件 API。`goal`、规划模式、review、skills、提醒、cron 策略不应拥有第三方插件无法取得的私有入口。
- 区分三件事：资源重载、插件代码更换、内核进程升级。它们共享版本与恢复规则，但不能伪装成同一种 Python module reload。
- 对需要更新执行代码的插件，建议使用**工作区级 Python 插件宿主进程**；在安全边界替换宿主，内核和会话保留。不要用 `importlib.reload()` 修补仍在使用的对象图。
- 不以文件变短作为成功标准。真正的标准是：**新增一种产品机制，不需要修改内核、工具执行器、HTTP 专用路由或客户端命令分发分支。**

第一项交付应是贯通“命令—状态—事件—上下文—工具—界面”的真实插件切片，而不是再加一组独立的 `reload_skills/reload_agents/reload_hooks`。

### 1.1 直接切换的删除边界

- 删除旧 `VoidCodeRuntime` 巨型外壳和 `RuntimeSurface` 业务接口；不是改名为更大的 `PluginManager`。
- 删除产品专用的 `ToolContext` 字段、执行器按工具名字授予能力的分支、内核中的 goal/review/cron/skill 业务分支。
- 删除分散的旧插件/钩子加载路径及其配置模型，统一成一个 manifest、一套注册所有权、一套生命周期。保留有价值的处理函数，不保留旧公开协议。
- 删除产品命令的专用 HTTP routes 和 Web/TUI 硬编码分发；保留必要 UI 组件，通过新命令/视图协议调用。
- 删除旧配置字段、旧数据库解析、旧 metadata 形状与兼容 re-export；仓库内调用者、样例、测试、schema、文档同步改用新合同。
- 已有实用行为继续由默认插件提供；不为保存历史偶然行为、旧字段名称或旧快照测试增加分支。


## 2. 用户可见目标

### 2.1 在同一会话中扩展自己

1. 用户要求一个当前没有的能力。
2. agent 查询扩展 API、现有插件和授权范围。
3. agent 在工作区编写或修改插件及其行为验证。
4. 内核展示候选版本、能力变化和校验结果；需要的新权限由用户授权，不能通过修改配置自授。
5. agent 请求激活；当前工具批次完成并落盘后，运行时切换版本。
6. 会话 ID、历史、已完成工具结果保持不变；agent 自动继续，调用新能力并验证结果。
7. 候选版本加载失败，旧版本仍可用；已切换后的降级遵循状态兼容规则，不撤销历史事实。

### 2.2 产品机制像插件一样编写

例如实现 `/review-project`，插件只需要声明命令、读取授权范围内的 diff、启动受约束的子会话、接收结构化结果并返回视图。不应修改 `RuntimeHost`、SQLite 专用业务字段和 Web/TUI 的命令 switch。

### 2.3 修改内核本身

agent 可以修改内核源码并执行验证，但生效方式是：构建候选版本 → 检查存储/协议兼容性 → 保存继续执行意图 → 受控重启 → 恢复原会话。**不承诺正在执行的 Python 栈无缝变成新代码。**

## 3. 参考系统：借鉴什么，不照搬什么

### 3.1 Pi

调研固定到上游提交 [`c268154ca1171c2f9eefe75d3d7372a68aada2d5`](https://github.com/earendil-works/pi/commit/c268154ca1171c2f9eefe75d3d7372a68aada2d5)。旧 `badlogic/pi-mono` 地址目前指向 `earendil-works/pi`；不要混用旧 npm 命名下的文档和当前源码。

- [Extension API](https://github.com/earendil-works/pi/blob/c268154ca1171c2f9eefe75d3d7372a68aada2d5/packages/coding-agent/docs/extensions.md) 提供工具、命令、生命周期、消息、provider、状态和 UI 扩展。关键是可组合能力，不是一个特殊“自举算法”。
- [`AgentSession.reload()`](https://github.com/earendil-works/pi/blob/c268154ca1171c2f9eefe75d3d7372a68aada2d5/packages/coding-agent/src/core/agent-session.ts#L3690-L3729) 先触发旧扩展 shutdown、使旧 runner 失效，再重载资源、重建工具与扩展 runner，并触发新 session_start。不能据此声称加载失败会完整事务回滚。
- [`DefaultResourceLoader.reload()`](https://github.com/earendil-works/pi/blob/c268154ca1171c2f9eefe75d3d7372a68aada2d5/packages/coding-agent/src/core/resource-loader.ts#L513-L638) 清扩展缓存，重新解析 packages、extensions、skills、prompts、themes；[`loader.ts`](https://github.com/earendil-works/pi/blob/c268154ca1171c2f9eefe75d3d7372a68aada2d5/packages/coding-agent/src/core/extensions/loader.ts#L559-L589) 使用 `jiti` 且 `moduleCache: false`。这是其 JS/TS 加载策略，不能直接映射为 Python `reload`。
- `ctx.reload()` 是 command context 操作；普通工具和生命周期回调不能随意调用，以免死锁或使用失效上下文。[reload 示例](https://github.com/earendil-works/pi/blob/c268154ca1171c2f9eefe75d3d7372a68aada2d5/packages/coding-agent/examples/extensions/reload-runtime.ts) 展示 agent 请求重载的意图。VoidCode 应采用结构化控制意图，不依赖把 `/reload` 字符串塞进用户消息。
- 当前同仓的 [Chord reload 规范](https://github.com/earendil-works/pi/blob/c268154ca1171c2f9eefe75d3d7372a68aada2d5/packages/chord/docs/spec.md#7-reload) 明确了 prepare、gate、commit、retire、状态版本迁移和旧调用版本归属。这与 coding-agent 的资源 reload 是不同层，不应混为一谈。借鉴边界与不变量，不移植整套分布式对象框架。
- Pi 扩展运行于同进程、拥有宿主 OS 权限；不能拿它的扩展便利性替代 VoidCode 的权限治理。

### 3.2 Emacs

- [`provide/require`](https://www.gnu.org/software/emacs/manual/html_node/elisp/Named-Features.html)：明确能力身份、依赖和按需加载。对应插件 ID、依赖、公开能力注册。
- [Hooks](https://www.gnu.org/software/emacs/manual/html_node/elisp/Hooks.html)：可组合扩展点。手册也区分通知型 hook 与返回值有语义的 hook；VoidCode 同样需要区分观察、变换和控制请求。
- [`unload-feature`](https://www.gnu.org/software/emacs/manual/html_node/elisp/Unloading.html)：追踪定义来源、删除 hook、取消 timer、执行自定义清理，并默认拒绝卸载仍被依赖的 feature。对应插件所有权、依赖闭包、可撤销注册和资源释放。
- [Advice](https://www.gnu.org/software/emacs/manual/html_node/elisp/Advising-Functions.html) 能包装任意函数，但这不是 VoidCode 的默认扩展合同。随意 monkey-patch 权限、落盘和恢复路径，会破坏可维护性与审计。

借鉴的是“基础原语稳定，上层机制可编程”，不是“所有内部符号都是公共 API”。

## 4. VoidCode 已有基础与实测缺口

### 4.1 已有基础，不重复造轮子

| 能力 | 当前实现与证据 | 计划中的用途 |
|---|---|---|
| 小型中立 turn engine | `src/voidcode/core/engine.py:195-321`：`TurnHost` 与 `TurnEngine`；文件实测 321 行 | 保留循环、工具批次身份与安全边界 |
| 包式组件准入 | `runtime/composition.py:39-48,514-616`：provider/tool/agent/context/typed-input/before-agent-start；文件/依赖哈希、prepare、persist-before-activate | 演化为唯一插件准入与生命周期所有者 |
| 工具版本标识 | `runtime/tools/materializer.py:19-49,94-115`：provenance、`generation` | 扩展现有版本身份，不另建竞争版本系统 |
| 持久消息与继续执行 | `runtime/service.py:5223-5314`：steering/follow-up；`runtime/execution/` 和 `resume.py` | 自请求激活后的受控继续执行 |
| 权限和恢复 | `docs/contracts/runtime-execution.md:82-109`：已完成前缀、never-replay、冻结权限 | 内核不变量，不迁给插件 |
| 现有客户端 | `command/native.py`、HTTP/SSE、Web/TUI | 改为数据驱动目录和通用 action，不另造 UI |
| 生命周期清理 | `runtime/service.py:1087-1129`：调度器/任务、子进程、协议适配器的清理顺序 | 插件资源所有权与宿主切换 |

### 4.2 真正的结构性问题

1. **小 engine 外面仍是大控制面。** 实测 `runtime/service.py` 7010 行、`runtime/run_loop.py` 4728 行。行数只是线索；关键是 goal、review、cron、skills 等产品知识直接参与执行路径。
2. **能力按工具名字授予。** `runtime/execution/tool_executor.py:278-324` 根据 `read/skill/task/cron_*` 等名字注入能力。新工具想使用已有原语，仍可能需要修改执行器。
3. **core 的调用上下文含业务字段。** `core/tool_context.py:305-323` 包含 todo、goal、skill、cron 专属字段，无法维持稳定通用边界。
4. **命令只是可列举，不是可扩展。** `command/native.py:40-227` 是闭集；Web `App.tsx:1023` 起、TUI `app.py`、HTTP handlers 都有按名称处理的业务分支。当前文档明确删除自定义宏，但新的插件目标需要有意修改“所有 slash 均闭集”的合同；不等于恢复旧 Markdown 宏实现。
5. **状态写入以功能为单位开后门。** goal、mode 等各有 metadata 字段和专用存储方法；缺少受限、带版本、可原子更新的插件状态原语。
6. **注册模型分散。** argv hooks、typed-input、before-agent-start、context transforms、composition phases 各有部分生命周期。需要统一身份、准入、注册/撤销、诊断；不同回调语义仍保持强类型，不能压成万能字典事件总线。
7. **冻结与刷新粒度不一致。** `CompositionOwner.refresh()` 拒绝包/资源身份变化；`storage/sessions.py:428-431,1233-1235` 禁止改变 session 的 composition 引用或重写 canonical body；本地工具却按 run 重新发现。
8. **现有 reload 不通用。** `service.py:5005-5072` 仅原子刷新 provider/model，并明确不重载治理、agent、hook 等协作者。

### 4.3 本次执行的 smoke

使用临时工作区、临时 SQLite 和隔离的 HOME/XDG 路径；未写用户配置或修改生产代码。运行了实际 CLI、实际 `VoidCodeRuntime` 和真实子进程工具。双轮 runtime 场景使用确定性 `TurnProducer`，没有调用外部模型。

| 场景 | 实际输出/结果 | 说明 |
|---|---|---|
| 同一会话先运行，再新增本地工具 manifest，再运行 | provider 工具列表从 `[probe]` 变为 `[added_probe, probe]`；两轮 completed；tool generation 变化、composition_ref 不变 | 已有“下一次用户 run 发现工具”，不等于当前 run 内自举激活 |
| 同一个 `LocalCustomTool` 实例，修改被执行脚本 | 输出 `version-a` → `version-b`，source fingerprint 不变 | 现有指纹只覆盖 manifest，不覆盖脚本内容；不能据此保证执行版本/回滚 |
| 显式启用 skills 后，runtime 启动再新增 beta | lifetime 目录 `[alpha]`；逐 run 新目录 `[alpha,beta]`；执行 lookup 报 `unknown skill: beta` | 必须统一发现、展示、provider 暴露和执行使用的快照 |
| 已准入组件声明中的资源文件改变 | 原版本 refresh 成功；修改后报 `frozen implementation/resource identity changed: voidcode` | 不能绕过现有冻结校验实现更新 |
| 新 runtime 重开同一个临时数据库 | 会话 replay 状态 completed | 当前持久会话基础可复用；没有验证跨内核版本升级 |

另运行 `voidcode --help`、`voidcode config --help`，均无通用 reload 命令。未运行 Pi 自身、真实在线 provider、自举升级或 UI 热更新；这些仍是后续实施验收，不能把源码阅读当运行证明。

## 5. 目标边界：什么留在内核

目标目录直接划分为 `kernel/`、`sdk/`、`plugin_host/`、`plugins/`，另由应用 bootstrap 选择默认插件。`kernel/` 收纳从现有 `core/`、`runtime/` 提取的纯引擎和可信机制；旧目录不是永久兼容外壳。先建立可运行的纵向切片，再迁移其余机制；切换完成后删除旧路径。

```text
Web / TUI / CLI
  │  通用命令、视图、会话、事件流
  ▼
可信 Runtime：权限 / 会话 / 事件 / 执行 / 恢复 / 激活
  ├── 纯 TurnEngine（提取已有算法，不保留业务上下文字段）
  ├── SQLite 与 artifacts
  └── 窄插件协议与能力句柄
          ▼
     Python 插件宿主（每工作区一个活动版本）
       ├── 官方默认插件：goal、plan、review、skills、cron…
       └── 工作区 / 已安装第三方插件
```

建议代码归属：

| 新区域 | 责任 |
|---|---|
| `src/voidcode/kernel/` | turn、invocation、policy、session、storage、tasks/resources、activation/recovery；不知道产品插件名字 |
| `src/voidcode/sdk/` | 中立类型、插件作者 API、能力句柄与消息合同；不依赖 kernel 实现 |
| `src/voidcode/plugin_host/` | Python 加载、注册、RPC 收发、生命周期、退出；不成为第二个权限/数据库 owner |
| `src/voidcode/plugins/` | 官方默认机制及协议/工具插件；和外部插件用同一 SDK |
| 应用 bootstrap 与现有客户端 | 选择默认插件、连接内核、渲染通用视图；不承载业务执行规则 |


### 5.1 内核原语

以下是能力合同，不是要求新增同名类。优先从已有 owner 提取实现。

| 原语 | 内核必须保证 | 插件可使用但不能改变 |
|---|---|---|
| Session / Run / Task | 身份、父子关系、取消、预算、执行所有权、checkpoint | 创建受约束子任务、等待、取消、读取允许的会话视图 |
| Governed invocation | 参数验证、授权、审批、effect、调用 ID、结果落盘、输出限制 | 调用模型/工具；嵌套调用仍走同一治理路径 |
| Facts / State | append-only 事件；状态与对应事件的原子提交；CAS | 自己 namespace 的 JSON 状态与事件；不能伪造核心事件 |
| Capability registry | owner、声明、版本、冲突检测、授予与撤销 | 注册工具、provider、命令、上下文贡献和生命周期处理器 |
| Context / Messages | 不修改历史事实、受限视图、模型输入上限、真实工具结果配对 | 贡献 prompt section、选择/摘要上下文、steering、请求 continuation |
| Resource lifetime | scope、lease、取消、进程/订阅/计时器归属与释放 | 使用宿主管理的资源；不能留下无人负责的后台线程 |
| Clock / Wakeup | 持久到期任务、原子 claim、取消、恢复、不重复领取 | cron 插件计算下一次时间、重叠/过期策略；内核不理解 cron 语法 |
| Activation / Recovery | 版本切换、兼容性、审批绑定、旧前缀恢复、核心升级交接 | 请求激活/降级；不得在 callback 中直接替换自己 |

**治理机制留在内核，产品策略可以插件化。** 例如 plan 插件请求一个限制性权限 overlay；内核负责 overlay 只能收紧、退出只能撤销自己拥有的限制、不能解除用户冻结的 read-only。

### 5.2 应迁出的机制

| 机制 | 插件拥有 | 留在内核 |
|---|---|---|
| goal / todo / reminders | 数据语义、工具、命令、提示、阻塞/预算提醒策略 | 状态事务、usage facts、预算硬上限、继续执行入口 |
| plan / review | 模式说明、prompt、review 目标与流程、结果呈现 | 限制性 overlay、只读执行、子任务所有权 |
| skills / rules / agent profiles | 发现规则、选择、指令正文、展示 | 资源身份、读取边界、当前能力快照 |
| compaction / memory | 摘要算法、保留策略、检索与提示 | 历史不变、上下文硬上限、checkpoint 与结果完整性 |
| cron / loop | 表达式解析、7 天过期等产品默认值、错过触发策略 | 通用持久唤醒、CAS claim、任务执行与取消 |
| subagent 工作流 | agent 路由、模板、fan-out 编排、结果聚合策略 | spawn/join/cancel、深度/预算、lease、恢复 |
| MCP / LSP / provider adapters | 协议适配、连接配置、目录和工具声明 | 凭证/授权交付、执行上下文、取消、输出/事件规范 |
| init / info / export 等命令 | 用户动作解释、格式化和视图 | 受控文件写入、transcript 查询、核心生命周期操作 |

核心 reload/recovery/permission 管理保留救援入口，不能被普通插件覆盖。即使所有产品插件都加载失败，仍应能查看错误、停用版本、恢复已知可用版本、读取/导出历史。

## 6. 插件合同：少量强类型入口，不暴露整个 runtime

### 6.1 一个 SDK、一套准入

- 定义唯一的新插件 manifest（建议 `voidcode-plugin.json`）：ID、SDK 协议版本、entrypoint、依赖、状态 schema/version、请求能力及实现资源身份。复用现有内容哈希、来源验证和 persist-before-activate 机制，不兼容旧 `voidcode-components.json` 或旧 component slot API。
- 工作区开发插件与已安装包走相同准入规则；本地开发目录建议 `.voidcode/plugins/<id>/`。
- 不再引入独立 command.json、hook.json 或竞争 registry。现有本地 manifest 工具与 argv hooks 的执行实现可迁为普通插件，但旧 manifest/config 入口直接删除，不加兼容适配层。
- 注册与使用分离：加载时提交声明，执行时取得经内核授予的能力句柄。不能传 `VoidCodeRuntime`、SQLite connection、可变 SessionState 或整个 `RuntimeSurface` 给插件。
- 不再通过工具名字注入能力；按插件声明、调用上下文和内核授权交集授予。同名工具不能通过伪装 `read` 或 `task` 获得额外权限。

### 6.2 公开扩展点

最初只覆盖实际迁移需要的形状：tool、provider、command、context contribution、生命周期 observer/transform/control request。

- **Observer**：接收不可变事实，不能修改已持久化事件。失败报告到对应插件，不破坏已完成事实。
- **Transform**：返回一个经 schema 验证的变换结果。顺序固定；禁止无序抢占或任意修改内核对象。
- **Control request**：请求 enqueue、受约束子任务、暂停或激活；由内核在安全边界执行。
- 输入变换若改变待执行工具/参数，必须重新做最终 schema、权限和审批匹配，不能在已批准之后改成另一项操作。
- 工具失败成为该调用的失败结果；关键上下文/准入失败中止本次操作。权限约束不能依赖可 fail-open 的插件回调。
- 每个注册、订阅和资源带 owner 与 generation；卸载时由宿主集中撤销，不靠插件作者记住所有全局列表。

统一的是身份和生命周期，不是把所有接口改成 `handle(event: dict)`。

### 6.3 状态与配置

- durable state 由内核按 `plugin_id + scope(session/workspace) + schema_version + revision` 管理；仅 JSON，较大数据通过现有 artifact 引用。
- 插件更新状态需带 expected revision；状态变更与 namespaced event 同事务提交。业务功能不再各新增一个 SQLite writer 或 metadata 顶层字段。
- 插件只能写自己的 namespace；不能写核心 policy、active revision、审批结果、工具完成事实或其他插件状态。
- 状态迁移是明确的旧版本 → 新版本纯变换，基于安全边界处最终状态生成候选副本；迁移失败不覆盖旧状态。
- 停用保留 durable state；卸载删除数据是独立显式操作。内存对象、socket、task handle 不作为持久状态保存。
- 配置增加插件命名空间，但不是任意 `extra=allow` 洞：内核验证外层，已准入插件 schema 验证自己的部分；错误指向 plugin ID、字段与文件，不静默忽略或替换成默认值。
- 不转换存量旧配置。仓库内调用者与样例直接改成新 schema，用户按新配置重新启用所需插件；删除旧字段读取、fallback 与双重优先级。

### 6.4 命令和 UI

- `NATIVE_COMMANDS` 收缩为不可覆盖的内核命令；产品命令进入已激活插件目录。
- 复用现有目录模型，加入 owner、参数 schema、busy/effect 要求、可用客户端能力和帮助来源。
- HTTP 提供通用 command/action invocation；CLI、Web、TUI 使用同一 registry，服务端执行相同验证。插件 action 不能因为来自 slash command 就获得“真实用户显式操作”的身份。
- Web/TUI 提供有限的数据视图：notice、Markdown、字段表、选择/确认、表单、diff、status。优先复用已有组件；不加载插件提供的任意远程 JS。
- 新插件使用这些视图时无需修改 `App.tsx` 或 TUI 分发器。客户端无对应交互能力时明确返回 unsupported，不能偷偷批准或假装展示成功。
- 未知命令、名称冲突和插件未激活都显式报错。产品命令迁移后删除旧专用分支、路由与过时文档。

## 7. 执行代码热更新：建议使用独立插件宿主

### 7.1 选择与代价

| 方案 | 结论 |
|---|---|
| Python `importlib.reload()` | 不采用。旧实例/外部引用不会自动更新，删除的全局名可能残留，而且非线程安全；见 [Python 3.14 文档](https://docs.python.org/3.14/library/importlib.html#importlib.reload) |
| 自定义版本化 import namespace | 不作为默认方案。需要管理传递依赖、绝对 import、native modules、全局缓存和清理；容易变成自研模块系统 |
| 每个插件一个进程 | 暂不采用。过多 IPC、依赖重复与跨插件调用，不是当前必要条件 |
| 每个工作区一个活动插件宿主；准备时启动候选宿主 | **推荐**。新解释器自然隔离旧模块缓存；统一重建/释放插件执行环境；内核继续持有会话和事实 |
| 内核本身升级 | 独立受控重启，不混入插件热更新 |

这比受信任同进程插件增加一道 IPC，代价真实存在。采用它的原因是本次明确要求执行代码热更新与可维护性，不是声称现有对象可以直接搬到另一个进程。

### 7.2 实施约束

- 宿主接口只传不可变数据、操作 ID、能力句柄、声明与结果；**不把现有 `RuntimeHost`/`ToolContext` 整体 RPC 化**。
- 用窄、本地、版本化的 JSON 消息协议；复用现有 JSON 值/模型/stream 规范。无需分布式对象、跨机器路由或完整 Chord 实现。
- 工具进度和 provider stream 支持取消、EOF、bounded queue 和输出限制；不为每个 observer 重复传完整历史。实际 IPC 开销必须测量，不能预先声称零开销。
- 插件调用原语时走独立控制收发路径；内核等待插件 callback 时不能持有 SQLite 事务锁或阻塞唯一 RPC 消费者，避免反向调用死锁。
- 宿主崩溃时停止新调用；在途有副作用操作按现有 pending intent/never-replay 规则处理，不宣称 exactly-once。
- 第一版只保留一个活动 generation，不支持新旧代码长期并发。稳定依赖只校验 API 兼容性；可更新代码与声明的资源来自不可变的候选 artifact。
- 这是故障/生命周期隔离，不是安全沙箱。任意 Python 代码仍可能访问同一 OS 用户的数据；默认只加载明确受信任代码。项目插件必须先授权，不能先 import validator/factory 再询问信任。真正不可信代码需要 OS sandbox，不能用 SDK 约定替代。

## 8. 合法的版本切换，而不是破坏 frozen composition

### 8.1 新存储模型

现状是每个 owner 的 metadata 保存一份 canonical composition，并禁止换 ref。新内核不兼容这份布局，直接使用支持能力版本的新 schema：

1. 在**新内核唯一的 SQLite 状态库**中保存不可变的能力 revision。用一个 `revision_id` 标识已准入的插件 artifact、配置快照、资源、工具声明及生命周期计划；不为了兼容继续搬运旧 binding/plan 多份嵌套 metadata。
2. 每次激活追加控制事实：旧 ref、新 ref、请求来源、能力差异、状态迁移版本、验证结果引用。
3. session 的当前 ref 成为受控投影，只能通过专用激活事务更新；普通 metadata merge 继续禁止改 ref。
4. 同一事务写入新版本记录、状态迁移结果、当前 ref、checkpoint 与 continuation。通过 CAS 检查旧 ref 和安全边界版本。
5. 每个 run/tool intent/checkpoint 记录所属版本；phase records 按版本归属，不能拿旧 callback 完成记录冒充新版本已执行。
6. 不迁入旧架构的 canonical body 或事件布局。新架构创建的会话在后续热更新中保持同一 session ID，历史事件不改写，不用自动 fork 偷换“原会话继续”的目标。

每个 revision 是一份可重建执行环境的不可变记录；当前 ref 是事实派生的投影。没有旧存储双读，也没有与内核并存的插件数据库。

### 8.2 激活流程

```text
编写源码
  → 检查 manifest / 信任 / 能力 diff / 依赖 / 行为验证
  → 固定候选 artifact，启动候选宿主并取得声明
  → 请求安全边界，停止接收新的受影响工作
  → 当前工具批次完成、结果落盘；确认无未决审批/问题
  → 读取最终插件状态、生成并验证迁移副本
  → SQLite 原子提交版本切换与继续执行意图
  → 发布新 registry，撤销旧句柄，释放旧宿主
  → 下一次 provider 请求看到新 schema/context，继续原会话
```

- agent 的 reload 工具只提交控制意图并返回结果；不能在自己的执行栈里同步等待“重载自己”。整个旧工具批次完成后再切换。
- 首版采用工作区级安全屏障。其他会话或后台任务仍在运行、存在审批/question 等待时，报告 busy/阻塞者，不隐式取消、不偷偷换其工具实现。
- 暂停新的 scheduler claim；现有任务完成或经明确取消后再激活。复用现有 shutdown/ownership 机制，不另造任务监督器。
- registry 的展示、工具文档、模型 schema、实际 dispatch 必须来自同一激活快照，消除 skills 当前的双目录问题。
- 本地工具必须绑定声明的实现文件/依赖身份；仅 hash manifest 不够。受管理扩展从激活后的不可变路径执行；不要把整个业务工作区一起冻结。
- 对任意外部 binary、远端 MCP 服务，不能仅凭本地 manifest 宣称实现可回滚；其生命周期/版本由适配器显式管理，否则仅保证本地声明版本。
- 文件 watcher 不是激活器：若以后加入，只标记 dirty 或请求校验，仍走上述事务，不在每次保存时自动执行新代码。

### 8.3 回退边界

- 提交前失败：丢弃候选宿主和候选状态；旧 active ref 不变。
- 提交后崩溃：根据 SQLite 中已提交的目标版本恢复，不凭内存指针猜测成功或失败。
- 新版本已经写入状态后：只有旧版本能读取新 schema，或存在经过验证的降级迁移时，才能回退代码。不能恢复一份旧快照而丢掉期间的新事实。
- 网络/文件等外部副作用不可随数据库事务回滚。加载/注册阶段禁止把业务副作用藏进 factory；它们必须成为独立的受治理 invocation。
- 卸载先检查依赖闭包；同名注册冲突失败，不采用隐式“最后加载者覆盖”。

## 9. 自举闭环与核心升级

### 9.1 自举需要的不只是 loader

- **可发现性**：插件 SDK 参考、最小真实示例、当前 API 版本、已激活能力/版本、诊断与资源来源。复用现有 read/tool catalog 能力，让 agent 能读到准确合同。
- **可验证性**：候选插件可在隔离临时工作区/会话中运行行为检查，不污染当前状态；检查失败直接反馈错误，不伪造激活成功。
- **可激活性**：agent 与用户控制入口调用相同的 activation service，身份和权限保持区分。
- **可继续性**：持久控制记录携带 request ID、目标版本、原 session/run、继续执行意图和消费状态。重启/重连不能重复领取 continuation，也不能重复执行已完成工具。
- **可退出性**：失败时停留在可诊断状态，保留旧 artifact 和恢复入口；没有无限自动修复/重载循环。

现有文本 steering/follow-up 队列可复用，但必须补结构化控制意图与原子消费语义；不能简单注入一个字符串伪装用户指令，也不能绕过 sealed-session 校验。

### 9.2 内核升级

- 使用候选安装目录/环境，而不是边运行边覆盖当前内核文件。
- 独立 launcher/现有进程管理器负责停止接单、保存 checkpoint、按既有顺序 drain、启动候选版本与健康检查；`RuntimeBackgroundTaskSupervisor` 不是内核进程 supervisor。
- 新版本先检查 DB schema、SDK/宿主协议、已记录的能力与状态版本，再接收会话。历史回放与重新执行旧代码是不同能力：不能因为历史属于旧版本就伪称已在新版本执行过。
- 存储迁移和回退必须共同设计。不兼容 schema 升级不能宣称“换回旧二进制即可回退”。失败时保留数据，给出可执行恢复步骤。
- Web/TUI 通过持久事件游标重连并恢复 UI，不重发用户 prompt 或 replay 有副作用调用；升级的退出/重连状态需要真实端到端演练。

## 10. 分阶段实施

采用新内核纵向切片，而不是在旧 runtime 上连续打补丁。设计阶段明确边界；每个实现阶段必须交付可运行行为。开发中可暂时保留尚未迁出的代码，但发布切换只保留新路径，不提供 legacy 模式。

### P0 — 定义新合同与删除清单

**工作**：固定 kernel/SDK/host/plugin 依赖方向；确定新 manifest、状态库、事件/调用/命令协议、信任模型和安全边界。为每个现有模块标记“提取机制 / 迁为插件 / 删除”，不设计旧数据转换器。

**来源**：`core/engine.py`、`runtime/composition.py`、`runtime/storage/`、`runtime/execution/` 的正确机制与本次行为证据；旧 `RuntimeSurface` 不作为新 API 蓝本。

**验收**：每次调用的 actor、session/run、revision、effect、取消归属与恢复行为明确；新内核能够拒绝误用旧配置/旧数据库，且不修改旧文件。这是设计门禁，不把接口声明当已实现功能。

### P1 — 新内核最小可运行纵向切片

**工作**：实现 `kernel/ + sdk/ + plugin_host/`，从一开始使用工作区级宿主和新版本存储，不先做注定废弃的进程内公开 ABI。提取 turn loop、受治理 invocation、事件/checkpoint、权限与取消；迁入一个真实 provider 和 read/write 基础工具，完成真实会话。

**位置**：新区域；现有 engine、tool executor、SQLite 事务和 subprocess/output 实现是提取来源，不继续向 7010 行 service 添加职责。

**验收**：真实模型读写临时工作区，审批/read-only 正常；内核重启恢复事件与已完成前缀；未知副作用不自动重放；杀掉插件宿主不破坏已落盘事实。不是只有 hello world 或返回固定字符串的框架。

### P2 — 通用三端入口 + goal 完整插件

**工作**：补齐插件状态事务、context/usage/continuation、命令目录、通用 action route 与 Web/TUI 视图。迁移 **goal 的完整纵向切片**：命令、tools、状态、usage accounting、prompt、确认/状态呈现，不保留 `thread_goal` 特权通道。

**位置**：SDK、kernel 的窄原语、官方 goal 插件和通用客户端；迁出 `runtime/goal.py`、`tools/goal.py` 及 service/run_loop/storage/UI 的业务 wiring。

**验收**：第三方 `/hello-state` 与官方 goal 通过相同 API；新增第二个插件不改内核/客户端；goal 停用后基础对话仍可用，重新启用恢复新架构保存的状态。跨 namespace 写入、保留命令覆盖、伪造 actor 被拒绝；反向调用和取消不死锁。

### P3 — 同会话热更新与自请求继续执行

**依赖**：P1 的 revision 存储与宿主，P2 的公开能力合同；不依赖旧 composition 存储迁移。

**工作**：不可变 artifact、候选宿主、新架构内的状态迁移、安全屏障、CAS 激活、旧句柄撤销、结构化 reload 请求和 durable continuation。所有 catalog/schema/context/dispatch 使用同一激活快照。

**位置**：`kernel/activation`、`kernel/storage`、`kernel/recovery`、`plugin_host/` 及统一资源 registry；从旧代码提取必要验证，但不提供旧 reload API 的转发入口。

**验收**：同 session 将插件 v1 改成 v2，agent 请求激活并调用 v2；语法/依赖/状态迁移失败保留 v1；工具批次不混版；审批等待拒绝切换；提交各边界杀进程后恢复唯一已提交版本，不重复副作用。

**证据**：`docs/contracts/hot-activation.md` 固定准入/快照/屏障/CAS/续跑/信任合同；`tests/unit/kernel/test_activation.py`（同 session v1→v2 真实工具与状态迁移、整批不混版、自动续跑调用 v2、语法/依赖/迁移失败保留 v1、审批等待阻塞、路径信任绑定、SDK pin、禁用→普通对话→重新启用迁移暂停态、重启只恢复已持久化版本、expected_revision 与 catalog/views 快照 revision）、`tests/unit/kernel/test_host_lifecycle.py`（纯迁移/停用变换与 arity 校验）、`tests/integration/test_kernel_activation.py`（SIGKILL 于提交前/提交后：唯一已提交版本、不重复迁移、fresh kernel 自动接管欠下的续跑并执行新工具；候选宿主迁移期崩溃只否决候选）。

### P4 — 迁完产品机制，完成唯一入口切换

1. todo/reminders：状态、usage/tool facts、context 与 continuation。
2. plan、skills/rules/agent profiles：限制性 overlay、资源/context 槽位；内核不认识产品模式名字。
3. review、subagent 工作流、btw：子任务与受限模型查询；保留安全、预算、真实结果约束，不绑定旧 payload。
4. cron/loop：表达式、过期/重叠策略移至插件；内核只有持久 wakeup/claim。
5. compaction/memory 策略、剩余 provider/MCP/LSP 适配、信息面板：用新 SDK 接入。

**位置**：`plugins/` 与通用客户端。删除旧 `runtime/service.py`/`RuntimeSurface` 外壳、旧分散注册系统、产品专用存储方法/路由/配置和失效 re-export；更新项目架构指引、包入口与全部仓库调用者。

**验收**：CLI/Web/TUI 只启动新内核；没有 legacy 开关；旧配置/数据库明确拒绝而不擦除；默认机制可独立启停；无 kernel→具体插件 import、按名字特判或业务专用调用上下文字段。测试以新消费者合同为准，删除旧结构/措辞/偶然行为测试，不重新固定它们。

### P5 — 新内核自身的受控升级

**工作**：候选安装、SDK/状态版本预检、launcher 交接、调度/任务 drain、健康检查、Web/TUI 事件游标重连。只处理新架构内部明确支持的版本转换；不支持旧 runtime 升级迁入，也不承诺任意历史版本兼容。

**位置**：新 kernel recovery/storage、launcher/server 边界、客户端 stream/reconnect。

**验收**：修改真实内核行为后受控重启，同 session 继续；已完成副作用只发生一次；候选启动失败仍可恢复；不支持的 schema 转换在停机前拒绝，不损坏当前数据。

### P6 — 完整自举与维护性验收

**工作**：真实模型执行“读 SDK → 写插件 → 行为验证 → 请求激活 → 自动继续 → 使用新工具”，再完成一次插件状态升级和一次核心升级。补插件作者合同、可运行示例、诊断指南和变更记录。

**验收**：下面的行为矩阵与依赖门禁通过；新机制无需修改 kernel 和客户端分发。旧 runtime/API/config/schema 的兼容代码不存在。

依赖主线：`P0 → P1 → P2 → P3`；随后完成 P4/P5，P6 验证组合。不需要等待整个旧工程迁完才第一次验证新架构，也不把开发期过渡路径变成产品中的永久双轨。

## 11. 可维护性与行为验收

### 可维护性

- 内核只依赖中立合同；bootstrap 可以选择默认插件，但 engine/governance/storage 不 import 具体插件。
- 官方插件不能访问 `_runtime` 私有字段或 SQLite；公开 SDK 之外的依赖由静态依赖检查阻止。
- 新插件的命令、配置、状态、工具与 UI 通过声明接入；无需新增 `if plugin_name == ...`。
- 不把 7010 行 service 原样搬进 `PluginManager`；注册、执行、状态、激活各有已有或明确的单一 owner。
- 只为实际迁出的机制增加 API；每个新增原语要能指出真实调用者和责任边界，不预建插件市场、分布式总线和泛型工作流系统。
- 小内核不等于删掉恢复/安全代码。以职责、依赖方向和扩展改动面衡量，行数作为趋势而非拍脑袋预算。
- 无旧 API 别名、旧配置 fallback、旧状态双读、legacy runtime 开关；依赖和发布检查必须验证旧路径确实删除。

### 必须覆盖的消费者行为

1. 新插件可发现、可调用、可卸载，三端行为一致。
2. 注册名冲突、未知 schema 字段、SDK 不兼容、依赖环都显式失败。
3. 状态跨 reload/进程重启保留；迁移失败不污染旧值；跨 namespace 写入被拒绝。
4. registry/catalog/schema/dispatch 始终指向相同 generation。
5. 当前批次保持原版本；新批次只用新版本；旧句柄失效可诊断。
6. 提交前/后崩溃都不会重复消费 continuation 或重放 `never` 副作用。
7. read-only、plan overlay、子任务约束不被新插件、改配置或命令身份绕过。
8. 停用插件后不残留计时器、后台任务、订阅和可调用命令；重复 reload 不持续增加存活资源。
9. 插件宿主失效、反向调用和取消可终止，不能挂死内核或丢失已落盘事件。
10. 候选预检失败保留旧版本；新状态不兼容时拒绝不安全降级。
11. 默认机制抽离前后，对话、review、cron、skills、审批/恢复的用户行为不退化。
12. 实际 Web/TUI 表面和真实模型自举闭环均演练；单元测试不是这些场景的替代品。

永久回归测试关注上述边界与状态转换，沿用现有测试框架。UI/自举/升级需真实 smoke；不要写只检查复制字段、注册行数或源代码字符串的测试。

## 12. 明确不采用的捷径

- 不嵌入 Elisp，仅为形式上像 Emacs；Python 已是可用扩展语言。
- 不为旧架构做兼容层、自动配置迁移或数据库导入；新架构的热更新状态转换另按明确版本合同执行。
- 不用 `importlib.reload`、全局 monkey patch 或清空 `sys.modules` 实现运行中升级。
- 不删除 frozen identity、审批匹配或 never-replay 校验来让 reload 看似成功。
- 不把 skills/prompt 模板当执行插件系统；它们只是机制的一种资源。
- 不把“新开会话才能看到修改”命名为同会话自举，也不自动 fork 掩盖缺失的版本转换。
- 不实现多代插件并行、任意前端代码注入、跨机器插件宿主或自动插件市场；它们不是当前目标的前置条件。
- 不让 agent 自行提升信任/权限；不把进程隔离宣传为沙箱。

**建议开工顺序：新内核 + SDK + 插件宿主的真实对话纵切 → goal 与通用三端入口 → 同会话版本化激活 → 全量机制迁出并删除旧 runtime → 新内核升级与自举验收。**

---

## P0 实施合同与切换清单

### 依赖方向

- `sdk/`：中立调用、消息、结果、manifest/config 合同；只依赖标准库和已安装的校验库。不复制 agent、goal、todo、skills、MCP/LSP 等产品模型。
- `kernel/`：SDK、存储与治理原语；不 import 具体插件或旧 runtime。模型适配器也由插件宿主运行。
- `plugin_host/`：只通过 SDK 和有界本地协议接受内核调用；不 import kernel，不直接读写状态库。回调执行不能阻塞接收取消/关闭的控制循环。
- `plugins/`：官方机制和第三方机制使用相同 SDK。宿主进程不是操作系统沙箱；只加载已由用户/应用信任的代码。
- bootstrap 负责选择默认插件；客户端依赖中立目录/事件/投影。开发期未迁出的旧路径继续服务旧入口，P4 一次性切换并删除，不提供兼容转发。

### 调用合同

- 每次工具调用显式携带 `workspace/session_id/run_id/invocation_id/revision_id/actor`；身份由内核创建，不从插件 arguments 提取。
- 一个原生模型响应中的所有工具构成一个批次；相同批次不能混用 revision。历史恢复只能恢复真实已完成前缀。
- 完整 JSON Schema 验证、effect/read-only、审批都在内核执行。改写 arguments 后必须重新校验；插件声明不能自行提权。
- 执行前持久化 intent，完成后原子写入结果和 checkpoint。崩溃留下的 `never` intent 不自动重放，也不伪造成功结果。
- SDK 不识别工具名字。副作用与 replay 依据声明，不保留 `ast_grep` 等特判。

### 提取与删除归属

| 现有区域 | 新归属与边界 |
| --- | --- |
| `core/engine.py`、`turns.py`、`transcript.py` | 提取批次顺序、身份、真实前缀、消息配对不变量；不原样复制整个旧 host API |
| `runtime/execution/`、`permissions/` | kernel 的 schema/effect/审批/取消/intent 门控；delegation/process 产品工具迁插件 |
| `runtime/storage/`、`fact_*`、`events.py` | 提取事务与 append-only 不变量；新库，不复用产品专用 mixin 或旧 composition metadata |
| `runtime/background/` | kernel 保留所有权/取消/持久 claim；路由、cron 语法、提醒和聚合策略迁插件 |
| `runtime/context/` | kernel 保留上下文边界和历史完整性；rules/skills/prompt/compaction 策略迁插件 |
| `runtime/{goal,todos,reminders,review,user_shell,effectiveness}` | 普通官方插件，不进入 SDK 或 kernel 特权状态 |
| `tools/`、`provider/`、`agent/`、`skills/`、`hook/`、`formatter/`、`mcp/`、`lsp/` | 工具/适配器/资源/策略迁普通插件；只提取实际调用需要的中立值类型 |
| `cli/`、`tui/`、`frontend/`、HTTP/ACP transport、doctor | 逐步对接新中立 API，P4 切换全部产品入口 |
| `runtime/service.py`、`run_loop.py`、`RuntimeSurface`、`composition.py` | 提取正确算法后删除旧外壳，不改名搬运、不包新 facade |
| 旧配置/schema、产品路由、tool-name 授权、re-export | 切换时删除；不做旧数据导入、旧配置 fallback、别名或 legacy 开关 |

先前逐文件侦察是提取线索，不是整模块搬迁授权；某文件包含安全机制不意味着其中的产品策略都属于 kernel。

### 新输入与存储

- `voidcode-plugin.json`：schema/SDK 协议 v1；声明入口、实现/资源身份、tools/providers/commands、所需能力、依赖和状态版本。拒绝未知字段、无效 effect、危险 safe-replay、重复身份和目录逃逸。
- 工作区 `voidcode.json`：schema v1；`model/policy/providers/plugins` 四段。这里只定义输入合同；实际加载与旧文件拒绝由 P1 bootstrap 交付，不把解析器说成已运行的 loader。
- 新库 `kernel-v1.sqlite3`：`kernel_identity`、`revisions`、`sessions`、`session_events`、`checkpoints`。SQLite triggers 禁止更新/删除 revision 和事件；event/session/checkpoint 单事务提交。
- P1 checkpoint 只保存批次/intent/审批等事件引用，历史消息由事件重建。CAS 使用 sequence 与 run identity，阻止旧执行者迟到写入。
- 插件状态、队列、后台任务、wakeup 表随 P2/P4 的实际 owner 交付，不预建空表。
- 旧数据库必须在任何 schema/pragma 写入前被拒绝；不靠文件名判断格式，也不改写旧文件。

### 阶段提交

每个阶段完成真实 smoke、相关回归与仓库门禁后立即提交。P0 只交付合同与可执行约束；P1 必须交付真实 provider + 工作区宿主 + read/write + 审批/取消/恢复，不能用进程内 builtin 代替。

## P1 实施记录（已交付行为）

### 实际路径

- 开发入口：`uv run python -m voidcode.kernel run/resume/show`（`src/voidcode/kernel/__main__.py`），`--workspace/--state-dir/--trust-plugin`。生产入口切换在 P4，P1 不动旧 CLI。
- 治理引擎：`src/voidcode/kernel/engine.py`（不可变批次、intent 先行、结果+checkpoint 原子提交、CAS sequence/run/revision、审批精确绑定、read-only 覆盖 yolo、串行 workspace 锁）。
- 准入与装配：`src/voidcode/kernel/activation.py`（artifact 内容寻址+哈希校验）、`src/voidcode/kernel/bootstrap.py`（新 `voidcode.json`、拒绝 legacy、state 必须在 workspace 外、`{NAME}_API_KEY` 解析、显式 base_url、revision pin）。
- 持久化：`src/voidcode/kernel/storage/{store.py,schema.sql}`（5 表 + append-only triggers、RLock 线程安全、preflight 只读校验拒绝外部/legacy/空库且字节不变）。
- 插件宿主：`src/voidcode/kernel/host.py` + `src/voidcode/plugin_host/`（独立进程、1MiB 有界 JSONL、无自动重启、取消/超时/kill 真实 `execution_stopped`）；SDK 线编解码 `src/voidcode/sdk/codec.py`，注册结构 `src/voidcode/sdk/plugin.py`。
- 内置插件：`src/voidcode/plugins/files/`（read 有界 UTF-8 单遍哈希+50KiB/1000 行截断、write stale-hash + 原子替换；schema 拒绝未知参数）与 `src/voidcode/plugins/openai_chat/`（官方 SDK、max_retries=0、严格 finish/args 校验、usage None/0 保留、无密钥落盘/日志）。
- 身份生成器：`scripts/generate_plugin_identities.py`（格式化后运行；`mise run plugins:identities:check` 与 pre-commit `plugin-identities` 卡点；禁止手改 files 数组）。

### CLI/配置示例

```json
{
  "schema_version": 1,
  "model": {"model": "deepseek/deepseek-flash"},
  "policy": {"approval_mode": "ask"},
  "providers": {"deepseek": {"base_url": "https://api.deepseek.com", "wire": "openai-chat"}}
}
```

```bash
uv run python -m voidcode.kernel --workspace ./ws --state-dir ./state run "读 input 写 result" --session-id s1
uv run python -m voidcode.kernel --workspace ./ws --state-dir ./state resume s1 --approval-request-id <id> --decision allow
uv run python -m voidcode.kernel --workspace ./ws --state-dir ./state show s1 --events
```

密钥从显式 providers 配置或 `{NAME}_API_KEY` 环境变量解析，不进 revision/checkpoint/日志。外部插件源码需 `--trust-plugin <id>` 显式信任，工作区配置不能自授。

### 无 legacy

- 只读新工作区 `voidcode.json`；新配置缺失但存在 legacy `.voidcode.json` 时明确拒绝，不解析、不转换。
- 只认新库 `kernel-v1.sqlite3`（magic `voidcode-kernel-v1`）；已存在外部/legacy/空文件在任何 pragma/schema 写入前拒绝，字节不变。
- 无兼容 shim、无旧 import、无迁移器、无 legacy 开关。

### 已知边界与 P2 下一步

- 已验证：审批 allow/deny 跨重启、错误 request_id 拒绝、revision 漂移拒绝、跨批次 native call ID 复用（批次内重复 ID 拒绝）、read 前缀不重跑、RO+yolo 无 intent、外部未信任拒绝、kernel SIGKILL/SIGINT 中断不重放 mutation 且 tail 不执行、安全读恢复复用 invocation_id、kill 插件宿主后 intent 保持 pending 且 resume 不重放、未知参数拼写拒绝无副作用；真实 DeepSeek 活体检（read→write→read，跨进程审批，terminal 落盘）。
- 未做（不在 P1 声称范围内）：无热更新（P3）、无 OS 沙箱（宿主只是受信 Python 进程）、无流式 provider、无插件状态/命令目录/通用 action（P2）、无生产入口切换（P4）。串行 workspace 执行是当前上限。

## P2 实施记录（已交付行为）

**状态：完整 P2 已验收**（通用三端入口 + goal 完整插件）；热更新/激活属于 P3，尚未开始。

### 实际路径

- 内核窄原语：`src/voidcode/kernel/engine.py`（单事务 checkpoint 引用绑定、root action budget、owner+id 取消作用域、可恢复停靠工作的显式取消、共享 fresh-work 准入、上下文失败durable failed、外部 mutation 效果分类）、`src/voidcode/kernel/host.py`（跨进程租约、按 owner 过滤的 fact 通知、确定性 keyed continuation 身份）、`src/voidcode/kernel/storage/{store.py,schema.sql}`（root_budgets/continuations.root_id/observer_receipts/session_budgets，只读 preflight 校验 root 列）、`src/voidcode/kernel/{bootstrap,activation}.py`（新 `voidcode.json`、拒绝 legacy、内容寻址 artifact、revision pin）。
- 通用客户端：`src/voidcode/kernel/transport/{http,models,server}.py`（命令目录、会话/运行/恢复/确认/视图/continuation/usage/events 路由 + OpenAPI）、`src/voidcode/kernel/__main__.py`（CLI）、`src/voidcode/kernel/tui/`（TUI 客户端与视图）、`frontend/src/kernel/**` + `frontend/src/lib/kernel/**`（KernelApp、8 种声明式视图、生成 API 类型）。
- SDK 与宿主：`src/voidcode/sdk/{manifest,plugin,services,tools,commands,hooks,views,codec}.py`、`src/voidcode/plugin_host/server.py`（独立进程、按调用租约、能力准入、反向 RPC 服务）。
- 官方插件：`src/voidcode/plugins/files/`（read/write 基础工具）与 `src/voidcode/plugins/goal/`（goal 完整纵向切片：命令、tools、状态、usage accounting、prompt、确认/状态呈现，无 `thread_goal` 特权通道）。
- 证据：`tests/integration/test_kernel_governance.py`、`test_kernel_vertical.py`、`test_kernel_transport.py`、`test_goal_plugin.py`、`tests/unit/kernel/*`、`tests/unit/plugins/{test_files,test_goal_plugin}.py`、`tests/unit/test_kernel_tui.py`（常驻客户端边界用例）。一次性浏览器/PTY smoke 夹具与截图是临时产物，已完成证明后移出仓库，保留在 `/tmp/voidcode-kernel-evidence/`（含 `harness/kernel-smoke.spec.ts`、`smoke_pty_tui.py`、`smoke_plugin.py`），不在仓库中。

### 验证

- 完整 Python 验收 **182 通过**（不重复计数）：governance + vertical + unit kernel/plugins/SDK 150，Goal 27（19 单测 + 8 集成），kernel transport，kernel TUI；命令见 `docs/CHANGELOG.md` 的 Verification 段。
- 付费活体保持原记录：`deepseek/deepseek-flash` 空闲 `goal.set`（engine `95e72163…`，tokens 7331、elapsed 6.356s、cost `None`、重启无额外工作）；当前核心 `8f1bdb60…` 只新增 SESSION read-only/ask 分类，由通用非付费 loopback 用例覆盖，不重复付费。
- 客户端证明：见本文件头部记录（真实全量 bundle 浏览器 `KERNEL_SMOKE_GOAL=1` 1.3 分钟、全 goal PTY 31 commits/4618 paints、frontend 169 + typecheck/eslint、transport+TUI 24、API/identity 检查）。
- 边界证据覆盖：审批/确认跨重启、SIGKILL 后 native batch 前缀不重放不重复计费、审批问询不被绕过、owner 取消仅限自身、两个 owner 部分回执只补投缺失方、有限 root 预算跨 fan-out/drain/重启、未知计量与 elapsed 均 fail closed、上下文失败 durable failed 且不产生 provider 计费。

### 无 legacy

- 内核只读新 `voidcode.json`，存在 legacy `.voidcode.json` 时明确拒绝且不写盘；只认 `kernel-v1.sqlite3`（magic `voidcode-kernel-v1`），外部/legacy/空文件在任何 pragma/schema 写入前只读校验拒绝、字节不变。
- 无兼容 shim、无双执行路径、无旧 payload/schema 适配；默认插件选择仍在 `kernel/bootstrap.py`，P4 迁出到应用入口。

### 已知边界与下一步

- P2 不包含：同会话热更新与自请求继续执行（P3，HotActivation 进行中）、生产唯一入口切换/ACP/doctor/legacy 位置探测（P4）、内核自身受控升级（P5）、完整自举验收（P6）。
- 单 workspace 串行执行仍是当前上限。
