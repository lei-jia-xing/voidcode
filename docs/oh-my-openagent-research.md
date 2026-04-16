# Oh My OpenAgent 调研笔记

本文记录对 [`code-yeongyu/oh-my-openagent`](https://github.com/code-yeongyu/oh-my-openagent) 的 agent、skill、hook、MCP 设计的调研结果，目标不是复述营销文案，而是整理出对 VoidCode 后续产品化、能力层设计与 multi-agent 演进真正有参考价值的结构信息。

本文基于公开仓库 `dev` 分支的源码与仓库内说明文件，重点参考：

- `src/agents/builtin-agents.ts`
- `src/agents/types.ts`
- `src/features/builtin-skills/AGENTS.md`
- `src/features/opencode-skill-loader/AGENTS.md`
- `src/features/skill-mcp-manager/AGENTS.md`
- `src/hooks/AGENTS.md`
- `src/mcp/AGENTS.md`

## 总体判断

Oh My OpenAgent 的核心不是“有很多 agent”，而是把 agent、skills、hooks、MCP 都做成了**可组合的 runtime surface**。它的整体结构更像一个控制面：

- agent 决定“谁来做事”；
- category 决定“这类任务默认路由到什么模型/执行者”；
- skill 决定“在这个任务上下文里注入哪些专门能力与规则”；
- hook 决定“系统在哪些生命周期节点上自动干预”；
- MCP 决定“外部能力如何以统一协议接入，并被 skill 或系统按需使用”。

也就是说，它的强项不只是单个 prompt，而是把 orchestration 和 extensibility 变成了结构化系统。

## 一、Agent 体系

根据 `src/agents/types.ts` 与 `src/agents/builtin-agents.ts`，Oh My OpenAgent 把 agent 组织成内建 agent 工厂集合，并通过 `createBuiltinAgents()` 统一构造。当前可见的内建 agent 名称包括：

- `sisyphus`
- `hephaestus`
- `oracle`
- `librarian`
- `explore`
- `multimodal-looker`
- `metis`
- `momus`
- `atlas`
- `sisyphus-junior`

它们不是平铺的“十个机器人”，而是分成不同职责层：

### 1. 主 orchestrator / primary agent

- **Sisyphus**：主编排者，负责理解用户意图、判断复杂度、决定是否委派、何时验证、何时停下。
- **Atlas**：更偏 todo / plan 执行 orchestration 的角色。
- **Hephaestus**：更偏 autonomous deep worker，强调独立推进复杂任务。

这些 agent 的共同点是，它们不是单纯“干某个小功能”，而是承担任务编排与执行主路径职责。

### 2. 专项 read-only / advisory subagent

- **Oracle**：高难架构/调试/判断咨询。
- **Librarian**：外部仓库、官方文档、示例检索。
- **Explore**：仓库内部搜索与结构理解。
- **Multimodal-Looker**：PDF/图片/图表等多模态分析。
- **Metis**：pre-planning consultant，负责在计划前发现需求歧义与隐藏风险。
- **Momus**：plan reviewer / critic，负责挑出计划中的阻塞点与不可执行之处。

这类 agent 的特点不是“更聪明”，而是**工具权限更窄、职责边界更清楚**。

### 3. Category executor

- **Sisyphus-Junior**：被 category 路由出来的 focused executor。

这个角色很关键，因为它把“主 orchestrator”与“具体执行者”拆开了。主 agent 不需要自己处理所有具体任务，而是可以把某一类任务分发给更匹配的执行者。

## 二、Agent mode 与工厂模式

`src/agents/types.ts` 里最关键的两个概念是：

- `AgentFactory`
- `AgentMode = "primary" | "subagent" | "all"`

这里的意思不是 UI 标签，而是**模型选择与上下文地位**：

- `primary`：主对话/主 UI 入口会选择的 agent；
- `subagent`：作为被委派角色出现，使用自己的模型/回退链；
- `all`：可以出现在两种上下文中。

这说明 OMOA 不是简单地“多个 prompt 文件”，而是已经把 agent 设计成了**工厂 + mode + metadata** 的结构化系统。

## 三、Category 路由

OMOA 的另一个关键设计不是 agent 本身，而是 **category routing**。仓库内公开信息显示其内建 category 至少包括：

- `visual-engineering`
- `ultrabrain`
- `deep`
- `artistry`
- `quick`
- `unspecified-high`
- `unspecified-low`
- `writing`

这些 category 不是 agent 名，而是**任务语义槽位**。系统先把任务归类，再把该类任务路由到合适的模型/执行者。这样做的价值很大：

- 用户不需要先懂模型差异；
- orchestration 层可以统一决定哪个执行者更适合；
- 后续改模型时不需要改任务语义层。

这和我们在 VoidCode 里思考的“runtime 作为控制面”高度一致。

## 四、Skills 系统

根据 `src/features/opencode-skill-loader/AGENTS.md`，OMOA 的 skill loader 是一个独立 feature，负责发现、解析、合并和加载 `SKILL.md`。

### 1. Skill 的载体

Skill 以 `SKILL.md` 作为核心载体，通常带有 YAML frontmatter，既描述 skill 本身，也可以携带 MCP 配置。

这意味着 skill 不是“硬编码在 agent 里的一个 prompt 片段”，而是：

- 有独立文件格式；
- 可以跨项目复用；
- 可以随 scope 覆盖；
- 可以绑定额外能力（如 skill-embedded MCP）。

### 2. Skill 的 discovery / merge

从仓库说明看，OMOA skill discovery 的优先级大致是：

1. project (`.opencode/skills/`)
2. opencode config (`~/.config/opencode/skills/`)
3. user / compatibility scope
4. built-in skills

它的重点不是“找到多少 skill”，而是**同名 skill 的优先级覆盖**。这让 project-local 能力可以覆盖 builtin，而不是被 builtin 限死。

### 3. Built-in skills

`src/features/builtin-skills/AGENTS.md` 明确列出了 8 个 built-in skills：

- `git-master`
- `playwright`
- `playwright-cli`
- `agent-browser`
- `dev-browser`
- `frontend-ui-ux`
- `review-work`
- `ai-slop-remover`

这些 skill 的定位都很明确：不是把所有 instruction 塞给主 agent，而是把一些高频、可复用、带明显工作流的能力抽成 skill。

## 五、Hooks 系统

`src/hooks/AGENTS.md` 是 OMOA 很值得研究的一部分。它不是只有少量 lifecycle 回调，而是已经形成了一个相当大的 hooks 面。

### 1. Hook 数量与分层

仓库说明里给出的总量是 **52 个 lifecycle hooks**，并且按 tier 组织。

核心层次可以概括为：

- Session hooks
- Tool guard hooks
- Transform hooks
- Continuation hooks
- Skill hooks

这件事很重要，因为它说明 OMOA 的系统行为不只由 agent prompt 决定，而是大量依赖**事件驱动的系统干预**。

### 2. Hook 的职责类型

从仓库说明里能看到几类典型 hook：

- session 生命周期处理
- tool 执行前后 guard
- context / compaction / todo continuation
- keyword mode detector
- rules injector
- read/write/JSON error recovery
- background orchestration

其中很多 hook 并不是“小优化”，而是决定系统产品感的关键机制，比如：

- `todo-continuation-enforcer`
- `atlas`
- `ralph-loop`
- `keyword-detector`
- `rules-injector`

这说明 OMOA 的 orchestration 不只靠 task tool，而是把大量系统行为沉到了 hook 层。

### 3. Hook 的价值

对我们最值得借鉴的地方不是“也做 52 个 hook”，而是这个思路：

> 把 agent 行为里那些稳定、重复、可系统化的干预，尽量从 prompt 里下沉为 event-driven hook。

这能显著降低 prompt 膨胀，也让系统策略更可观察、可配置、可替换。

## 六、MCP 体系

`src/mcp/AGENTS.md` 和 `src/features/skill-mcp-manager/AGENTS.md` 共同说明了 OMOA 的 MCP 不是单一入口，而是**三层结构**。

### 1. Tier 1：built-in MCP

内建的 remote MCP 主要包括：

- `websearch`
- `context7`
- `grep_app`

这些是全局基础能力，属于系统预置层。

### 2. Tier 2：兼容外部配置来源

OMOA 还兼容 `.mcp.json` 这类外部 MCP 配置来源。这一层的意义在于：

- 让已有 MCP 生态可以接进来；
- 让系统不是只支持“自己发明的配置格式”。

### 3. Tier 3：skill-embedded MCP

这层最值得注意。`skill-mcp-manager` 管理的是**绑定在 skill 上的 MCP client lifecycle**。

它的核心能力包括：

- 每个 session 隔离；
- 首次使用时 lazy create；
- 支持 stdio 与 HTTP transport；
- session 删除时清理；
- 空闲超时自动 cleanup；
- 支持 OAuth / token 处理；
- skill 级 client key 组织。

这其实就是把 MCP 从“全局配置的工具清单”升级成了“任务上下文相关的能力注入”。

## 六点五、OpenCode / OMOA 对异步 agent 的关键能力面

这次继续往下看时，一个更重要的结论变得很清楚：

> 如果要支持“主 agent 异步调用另一个 agent，并在完成后被可靠通知回来”，真正的前提不是先多几个 hook，而是先拥有一套可恢复的 background task / child-session substrate。

从 OpenCode 核心能力看，至少已经能看到两个重要基础：

- session 支持 `parentID`，说明系统原生理解 parent / child session 关系；
- 存在 `prompt_async` 这类 fire-and-forget 异步 prompt 入口，说明异步驱动不是完全依赖 prompt hack。

而 OMOA 在此基础上又补了几层运行时能力：

- `BackgroundManager` 维护后台任务对象与状态机；
- 后台任务具有稳定 task id 与 `pending/running/completed/error/cancelled/interrupt` 等状态；
- `background_output` 提供结果读取面；
- `background_cancel` 提供取消面；
- `background-notification hook` 把完成通知注入主会话；
- `session.idle` 等事件被用于判断后台会话是否真正完成。

这说明 OMOA 的异步 subagent 体验不是单个 hook 造出来的，而是多个 runtime surface 叠加的结果：

1. 子会话 / 子任务的创建与 parent 关联；
2. 后台任务状态跟踪；
3. 会话生命周期事件；
4. 完成后的通知注入；
5. 结果读取与 transcript 回收；
6. 取消、并发限制与错误状态处理。

因此，hook 在这条链路里的位置更准确地说是：

- **hook 负责把任务状态变化“送回主 agent”**；
- 但 **task model / session model / result model 才是异步 agent 的底座**。

如果缺少这些底座，只给系统加一个“任务完成时通知 leader”的 hook，最终也只会得到一个 best-effort notification，而不是可靠的 async agent orchestration。

## 七、系统是如何连起来的

把 OMOA 的几个 subsystem 放在一起看，它的结构可以概括成这样：

1. **agent** 负责决策与执行分工；
2. **category** 负责按任务语义路由执行者；
3. **skill** 负责在任务上下文里注入专门 workflow / instruction；
4. **hook** 负责在生命周期节点上做系统干预；
5. **MCP** 负责把外部能力接到 skill 或系统中；
6. **runtime / event system** 则把这些东西粘起来。

也就是说，OMOA 的强点不只是“有很多 agent 名字”，而是把这些组件做成了**分层、组合、事件驱动**的控制面。

## 八、对 VoidCode 的启发

这份调研最值得我们吸收的不是具体 agent 名称，而是下面几件事。

### 1. Agent 不该只是 prompt 人设，而应是 runtime surface

OMOA 里 agent 已经有：

- factory
- mode
- metadata
- category 路由
- tool restriction

这对 VoidCode 的启发是：如果我们未来真的做多 agent / 协同合作，agent 必须是 runtime 能识别和治理的结构，而不是 prompt 层的“扮演某某专家”。

### 2. Skill 应该是可覆盖、可绑定能力的独立载体

我们已经有 `skills/` 边界，但未来如果要真正把 skill 做成系统能力，最值得参考的是：

- 独立文件载体；
- project > user > builtin 的覆盖顺序；
- skill 既能注入 instruction，也能绑定 MCP。

这会比“把一堆模板 prompt 散在仓库里”可维护得多。

### 3. Hook 是产品化控制面的关键

OMOA 的很多“产品感”其实来自 hook，而不是来自 agent 本身。对 VoidCode 来说，这提示我们：

- 运行时事件如果足够稳定；
- hook surface 如果设计得好；
- 那很多“智能行为”都可以从 agent prompt 里下沉出来。

这和我们正在做的 runtime-owned scheduler / event contracts 是同一方向。

### 4. MCP 最值得学的是 lifecycle 管理，而不是接更多工具

OMOA 的 skill-MCP manager 之所以重要，不是因为接了更多服务，而是因为它把：

- session 隔离
- lazy loading
- cleanup
- auth
- transport

都做成了 runtime 关心的生命周期问题。这正是 VoidCode 后续做 MCP / ACP / LSP 时都需要面对的核心问题。

### 5. ACP 对我们仍然是关键差距

从 OMOA 这套结构反看 VoidCode，会更明显地看到：

- 我们已经有 runtime、event、tool、skills、MCP/LSP/ACP 边界；
- 但 ACP 仍然没有从 config-stub 真正长成 agent control plane。

换句话说，如果未来想把“agent 之间的协同合作”做成 kill feature，光有 agent prompt 远远不够，还需要 ACP 或等价机制来承担：

- agent-to-agent transport
- capability ownership
- lifecycle governance
- event topology
- permission / approval boundary

这也是为什么 ACP 迟迟不落地会显得格外阻塞。

不过这次对照 OpenCode / OMOA 之后，也需要把问题再说得更准确一些：

- ACP 很重要，但它不是唯一阻塞点；
- 真正先卡住 VoidCode agent 设计的，往往是更基础的 runtime substrate；
- 例如 background task truth、child-session 关系、session lifecycle 事件、completion notification、result retrieval、cancellation 与 resume semantics。

换句话说，哪怕 ACP 明天增强了，如果这些 runtime capability 仍然缺失，我们也依然很难把 async leader/worker 流程写成一个可信的产品能力。

## 九、结论

Oh My OpenAgent 的设计最值得研究的地方不是“它有哪些炫酷 agent”，而是它已经把 agent、skill、hook、MCP 组织成了一个**可扩展的 agent harness**。它真正厉害的地方在于：

- agent 是结构化角色，而不是 prompt 别名；
- skill 是可加载、可覆盖、可绑定外部能力的载体；
- hook 是事件驱动的系统控制面；
- MCP 被纳入生命周期管理，而不是仅仅当作外部工具入口。

对 VoidCode 而言，这份调研最直接的价值有两点：

1. 帮助我们更清楚地区分“当前主路径产品化”与“未来多 agent 差异化”之间的边界；
2. 为后续 ACP、skill system、hook surface、capability lifecycle 的设计提供一个可对照的成熟样本。

再进一步说，对 VoidCode 当前最直接的提醒是：

- `agent/` 文档层可以先定义角色；
- 但只要 runtime 还没有 first-class background task / notification / child-session substrate，
- 就不应该把 `leader` 异步调用 `worker` / `explore` / `researcher` 写成近在眼前的能力。

否则文档很容易把“想要的协作语义”误写成“已经具备的 runtime 能力”。
