# 运行时配置契约

来源 Issue：#16

## 目的

定义使 MVP 运行时具备可配置性所需的最小配置界面，同时确保系统是受控的而非过于宽泛。

## 配置数据边界的单一来源

### 用户级配置不在发布 schema 内（已接受的限制）

用户级 `~/.config/voidcode/config.json`（`UserConfigPayload`）不属于随包发布的 `schema/voidcode.config.schema.json`，因此它没有对应的 artifact，也没有 loader-vs-artifact 语料行；这是**已接受的限制而非遗漏**。用户级文件当前接受的键为 `$schema`、`approval_mode`、`model`、`tui`、`web`、`providers`、`hooks`；其中 `approval_mode` / `model` 是该链中**最不具体**的一层（机器级基线，见下），其余键的语义各自独立。它的 loader 行为由 `tests/unit/runtime/test_config_loader_parity.py`（`$schema` 容忍、`web` 容忍、providers 解析先于模型）与 `tests/unit/runtime/test_runtime_config.py` 中的 XDG/用户配置用例固定。若将来需要用户级 schema，应单独决策并单独生成 artifact。


配置输入的**形状**只在一个模块中定义：`src/voidcode/runtime/config_models.py`（payload 模型 + 共享的取值校验器）。仓库本地 `.voidcode.json`、用户级 `config.json`、环境变量、请求 metadata 覆盖与持久化的 `runtime_config` 快照都通过这些模型校验与类型化。

维护者须知：**模型拥有形状，不拥有策略。** 配置来源包括环境变量、用户级文件、仓库本地文件、请求 metadata 覆盖与持久化的会话快照，但**哪个来源覆盖哪个**不属于本模块：`approval_mode` / `model` 的唯一有序链见下文「推荐优先级」（其余字段各自的合并/覆盖规则由各自的 owner 定义，例如 hooks 为拼接语义）。此外还有合并规则、默认值与“未设置”的解析、哪些值属于 recovery-critical 必须写入会话快照，以及 provider / policy 语义，全部留在原有 owner：

- `src/voidcode/runtime/config.py`：加载顺序、优先级、合并、默认应用与文件读写
- `src/voidcode/runtime/config_materializer.py`：持久化快照的序列化/解析边界
- `src/voidcode/runtime/policy.py`：policy 语义与错误消息
- `src/voidcode/provider/config.py`：provider payload 边界模型与解析

因此，如果需要新增“在多个来源之间选择”或“解释某个值含义”的规则，它不属于 `config_models.py`。

随包发布的编辑器 schema `schema/voidcode.config.schema.json` 由上述模型生成，不再是手写产物：

```bash
uv run python scripts/generate_config_schema.py          # 重新生成
uv run python scripts/generate_config_schema.py --check  # 校验是否过期（mise run schema:check）
```

给模型新增字段后必须重新生成该 artifact；`mise run check` 会通过 `schema:check` 与 `tests/unit/runtime/test_config_schema.py` 阻断漂移。

## 状态

当前运行时从仓库本地的 `.voidcode.json` 中加载以下已实现领域的配置：

- `approval_mode`
- `model`
- `execution_engine`
- `tool_timeout_seconds`
- `reasoning_effort`
- `hooks`
- `tools`
- `skills`
- `context_window`
- `lsp`
- `mcp`
- `provider_fallback`
- `providers`
- `agent`
- `policy`（Runtime Harness Policy v1 schema-bounded 配置面；只能选择或收窄 runtime-owned policy，不得作为无界 metadata 使用）
- `tui`

目前 hooks/config 的 MVP 收敛目标已经锁定：

- hooks 继续保持 runtime-owned，但当前已不再只限 `pre_tool` / `post_tool`，而是同时包含 `session_start`、`session_end`、`session_idle`、`background_task_registered`、`background_task_started`、`background_task_progress`、`background_task_completed`、`background_task_failed`、`background_task_cancelled`、`background_task_notification_enqueued`、`background_task_result_read`、`delegated_result_available`、`turn_progress` 与 `stuck_detected` 等已解析的 lifecycle hook phases
- 显式 / 仓库本地 / 环境 / 用户 / 默认值这条完整优先级链当前适用于 `approval_mode` 与 `model`；`execution_engine` 和 `reasoning_effort` 仍为显式 / 仓库本地 / 环境 / 默认值（用户级文件不接受这两个键）
- 单一可见检查面为 CLI：`voidcode config show --workspace <path> [--session <id>]`
- 当前 schema-backed 配置 UX 包含 `voidcode config schema` 与 `voidcode config init`
- 恢复会话的配置覆盖仍存放在 `SessionState.metadata["runtime_config"]`，并继续覆盖新的 runtime 默认值

## MVP 配置领域

MVP 配置界面应仅覆盖以下区域：

- 工作区根目录（workspace root）
- 模型/供应商选择
- 审批模式
- 钩子（hook）的启用/默认值
- 工具发现/供应商默认值
- 技能发现默认值
- LSP 的扩展基础设施开关，以及 ACP 的 runtime-managed 内部 capability 基线
- 恢复（resume）所需的客户端可见会话设置

## 计划的最小配置形状

MVP 契约应能够表示一个至少包含以下内容的运行时配置对象：

```json
{
  "workspace": "/workspace/project",
  "model": "opencode-zen/gpt-5.4",
  "approval_mode": "yolo",
  "execution_engine": "provider",
  "hooks": {
    "enabled": true,
    "pre_tool": [["python", "scripts/pre_tool.py"]],
    "post_tool": [["python", "scripts/post_tool.py"]],
    "on_session_start": [["python", "scripts/session_start.py"]],
    "on_session_end": [["python", "scripts/session_end.py"]],
    "on_session_idle": [["python", "scripts/session_idle.py"]],
    "on_background_task_registered": [["python", "scripts/background_task_registered.py"]],
    "on_background_task_started": [["python", "scripts/background_task_started.py"]],
    "on_background_task_progress": [["python", "scripts/background_task_progress.py"]],
    "on_background_task_completed": [["python", "scripts/background_task_completed.py"]],
    "on_background_task_failed": [["python", "scripts/background_task_failed.py"]],
    "on_background_task_cancelled": [["python", "scripts/background_task_cancelled.py"]],
    "on_background_task_notification_enqueued": [["python", "scripts/background_task_notification.py"]],
    "on_background_task_result_read": [["python", "scripts/background_task_result_read.py"]],
    "on_delegated_result_available": [["python", "scripts/delegated_result.py"]],
    "on_turn_progress": [["python", "scripts/turn_progress.py"]],
    "on_stuck_detected": [["python", "scripts/stuck_detected.py"]]
  },
  "tools": {
    "builtin": {
      "enabled": true
    },
    "local": {
      "enabled": false,
      "path": ".voidcode/tools"
    }
  },
  "skills": {
    "enabled": true,
    "paths": [".voidcode/skills"]
  },
  "agent": {
    "preset": "leader",
    "model": "opencode-zen/gpt-5.4",
    "execution_engine": "provider"
  },
  "lsp": {
    "enabled": false,
    "servers": {}
  }
}
```

字段意图：

- `workspace`：引导（bootstrap）字段，用于在发现仓库本地配置之前确定运行时工作区根目录，随后重用于工具执行和持久化
- `model`：OpenCode `provider/model` 格式的供应商/模型标识符
- `approval_mode`：由运行时治理的工具所使用的最小审批模式（`ask` / `write` / `yolo`）：决定按工具 tier 自动放行 `read` / `write` / `execute` 中哪些调用，其余进入审批。默认 `yolo`（全部放行）。tier 语义、未知工具默认 `execute` 与解析优先级见 `docs/contracts/approval-flow.md`。
- `execution_engine`：当前接受 `deterministic` 或 `provider`。`provider` 是产品默认主路径；`deterministic` 保留为显式 test/dev/no-key harness（参考/debug）。
- `hooks`：运行时拥有的最小钩子配置对象，覆盖 pre/post tool 与当前已解析的 lifecycle hook phases
- `tools`：内置工具启用、仓库本地自定义 tool manifest 发现，以及 provider 可见工具收窄的最小配置；所有 tool 都通过 runtime registry / allowlist / permission 路径治理
- `skills`：技能发现启用的最小配置，以及额外的技能搜索路径
- `context_window`：provider-backed 路径的上下文窗口与 tool-result retention 配置
- `lsp`：当前 runtime-managed 语言服务器（Language-server）能力的最小配置容器
- `mcp`：当前已解析的 runtime-managed MCP 配置容器
- `provider_fallback`：provider fallback 链的配置入口
- `providers`：provider 级配置对象；当未提供仓库本地 `providers` block 时，runtime 也会从标准 provider 凭据环境变量构造最小 provider 配置（例如 `OPENCODE_API_KEY`）
- `agent`：agent preset 的 runtime 消费入口。当前顶层 active run 仅使用 builtin `leader`（默认或显式选择）；runtime-owned delegation path 上的 child run 可执行 builtin child preset（包括只读 plan subagent `product`）或本地自定义 `mode: subagent` manifest。`product` 不能作为 top-level active agent。
- `policy`：Runtime Harness Policy v1 配置入口，只能提供 schema-bounded narrowing/default intent；runtime hard denials、persisted snapshot、agent manifest 与 request/session 边界仍按固定优先级收口。
- `agents`：按 agent preset 配置 model / fallback defaults。这里是“已发现 preset 的配置覆盖/别名入口”，不是 manifest 定义入口；内置 preset key 与已发现本地 manifest key 可省略 `preset`，其他 alias key 必须显式声明 `preset`。
- `reasoning_effort`：可选的 runtime-owned reasoning-effort hint（例如 `low` / `medium` / `high`）。能力判定**只**以 model 自身的 catalog metadata（`supports_reasoning_effort` / `supported_effort_levels` / `default_reasoning_effort`）为准，没有 provider 级判定：model metadata 显式 `supports_reasoning_effort=false` 时 runtime 在请求早期 fail-fast，显式 `true` 时通过；metadata 缺省的 model 一律按 best-effort 透传，并在 readiness 中标记为 `status="forwarded_unverified"`（`reason="model_capability_unknown"`），不静默当作已支持。这条判定对所有 provider（含多上游网关 `opencode-go`）一致：model metadata 是唯一权威，provider 名不再参与能力结论。`supported_effort_levels` 同时决定 clamp：转发前会向下对齐到该 model 真实支持的档位。`off` 是「关闭推理」的请求状态而不是 ladder 档位：它仍是 CLI / config / frontend 接受的输入，转发形态由 `provider/thinking_rules.json` 中该 provider/model 行的 `disable_mode` 数据解析（实现见 `src/voidcode/provider/reasoning_effort.py::disabled_reasoning_kwargs`）。

### Execution engine 生命周期决策

当前生命周期约定为：

- `provider`：产品默认主路径；执行前必须配置 `model = "provider/model"`（或等价环境变量），否则 runtime 会在 run preflight 阶段返回清晰的 provider/model 配置错误。
- `deterministic`：保留为显式支持的 test/dev/no-key harness，并继续承担 graph harness 与确定性回归测试。
- `voidcode config init` 默认不写入 `execution_engine`，避免 repo-local config 锁死 provider/no-model 状态；若显式写入 `execution_engine = "provider"`，应同时写入 `model`。
- 会话 replay/resume 必须读取持久化 runtime config 元数据，不得被新默认值覆盖。

## 当前实现的仓库本地形状

当前的 `.voidcode.json` 解析器接受以下仓库本地形状：

- `approval_mode`：`ask`、`write`、`yolo` 之一（审批模式，按 tier 决定自动放行阈值；默认 `yolo`）。此前的 `allow`/`deny`/`ask` 取值**已移除且不再接受**：它们是**决策**词表，不是 mode；per-tool 的 `allow`/`deny` 意图请用 `permission.rules`、`permission.external_directory_*` 表达。契约前向演进，未知或旧值直接 fail-fast，不做兼容映射
- `permission.external_directory_read`：对象，key 为路径 pattern（支持 absolute / `~` / glob），value 为 `allow|deny|ask`
- `permission.external_directory_write`：对象，key 为路径 pattern（支持 absolute / `~` / glob），value 为 `allow|deny|ask`
- `permission.rules`：有序数组，每条规则可包含 `tool`、`path`、`command` 与必填 `decision`，用于 runtime-owned 的工具/路径/命令 pattern 权限匹配
- `model`：字符串
- `reasoning_effort`：字符串枚举，仅接受 `off` / `minimal` / `low` / `medium` / `high` / `xhigh` / `max`；`"none"` 不被接受（实现见 `src/voidcode/provider/reasoning_effort.py`）
- `reasoning_effort = "off"` 表示“不要推理”，其转发形态按 provider/model 解析：已知的二元关闭形态（Z.AI / ZhipuAI 与 DeepSeek 的 `extra_body.thinking.type = "disabled"`）保持不变；其余 provider/model 发送该 model `supported_effort_levels` 中**最低**的一级；model 没有任何受支持档位时不发送该参数。runtime 不会向不接受 `"none"` 的 model 发送字面量 `"none"`（实现见 `src/voidcode/provider/reasoning_effort.py::explicit_off_kwargs`）
- `hooks.enabled`：布尔值；默认 `true`
- `hooks.pre_tool`：命令数组的数组，每个命令在 workspace cwd 中执行
- `hooks.post_tool`：命令数组的数组，每个命令在 workspace cwd 中执行
- hook 命令必须是 argv 数组，不是 shell 字符串；runtime 不隐式启动 `sh` / `cmd.exe`。需要 shell 功能时必须显式写出解释器，例如 `["python", "script.py"]` 或平台专属的 `["bash", "-lc", "..."]`。
- `hooks.on_session_start`：命令数组的数组
- `hooks.on_session_end`：命令数组的数组
- `hooks.on_session_idle`：命令数组的数组
- `hooks.on_background_task_registered`：命令数组的数组
- `hooks.on_background_task_started`：命令数组的数组
- `hooks.on_background_task_progress`：命令数组的数组
- `hooks.on_background_task_completed`：命令数组的数组
- `hooks.on_background_task_failed`：命令数组的数组
- `hooks.on_background_task_cancelled`：命令数组的数组
- `hooks.on_background_task_notification_enqueued`：命令数组的数组
- `hooks.on_background_task_result_read`：命令数组的数组
- `hooks.on_delegated_result_available`：命令数组的数组
- `hooks.on_turn_progress`：命令数组的数组
- `hooks.on_stuck_detected`：命令数组的数组
- `hooks.formatter_presets`：对象，用于覆盖或扩展 formatter preset
- `tools.builtin.enabled`：布尔值
- `tools.local.enabled`：布尔值；显式为 `true` 时 runtime 才会发现仓库本地自定义 tool manifest
- `tools.local.path`：workspace-relative 目录，默认 `.voidcode/tools`，包含 `*.json` tool manifest
- `tools.allowlist`：字符串数组，用于 active agent tool boundary 的硬边界
- `tools.default`：字符串数组，用于 active agent 默认可见工具集合，只能在 allowlist 内进一步收窄
- `skills.enabled`：布尔值
- `skills.paths`：字符串数组
- `context_window.default_tool_result_chars`：单个 tool result provider payload 的明确字符 hard cap；默认 `6000`，`null` 表示不截断单个结果
- `context_window.per_tool_result_chars`：对象，按 tool name 覆盖单个结果字符 hard cap
- `context_window.compaction.enabled`：布尔值；默认 `true`
- `context_window.compaction.threshold_tokens`：正整数；显式阈值（估算 tokens），缺省时按下文推导
- `context_window.compaction.reserve_tokens`：正整数；缺省时按下文推导
- `context_window.compaction.keep_recent_tool_tokens`：非负整数，默认 `20000`
- `context_window.compaction.summary_enabled`：布尔值，默认 `false`（关闭）。开启后，压缩摘要由一次模型调用生成并替代确定性的 `Runtime context projection` 投影文本；关闭时行为与今天完全一致（不产生额外 provider 调用与开销）。该调用失败、被取消或返回空内容时**保证回退**到确定性投影，压缩本身绝不因摘要失败而中断。
- provider context 使用**有界裁剪**（本轮契约）：判定数字 = **`max(实测锚点, 本轮全量本地估算)`**（对齐 omp `compactionContextTokens`）。锚点取最近一次 provider 上报的 usage（`provider_usage.latest`），口径为 `input_tokens + cache_write_tokens + output_tokens`（`input_tokens` 已是**含 cacheRead 的 prompt 总量**，三条 wire 一致，再加 `cache_read_tokens` 会把 cache 命中数两遍；本轮 output 会成为下一轮历史）；全量本地估算 = 本次调用 provider 实际看到的 payload：文本部分（当前 prompt + 全部保留工具结果的 provider 视图）走**该模型的真实 tokenizer**（`voidcode.provider.tokenizer.count_tokens`，见下条），其余按字节规模（`_provider_payload_bytes` 的 plan 段）按 UTF-8 字节 / 4 估算。锚与估算是对**同一个量**（本轮 payload 规模）的两种测量——只能取 max，**不能相加**（相加会把 payload 数两遍，判定数≈2×实际）。voidcode 的 usage 桶就是 provider 自己的数字，没有单独计入计费的 orchestration 桶，因此不做扣减。usage 缺失或全零时锚点不可用，退化为纯估算。
- `estimated_delta_tokens` = 本轮全量本地估算超出实测锚的差额（`max(0, 估算 − 锚)`；锚胜出时为 0）；`measured_anchor_tokens` 语义不变（最近一次 provider usage 的总量）；`usage_tokens_estimated` = 本地估算这一侧胜出（锚缺失或差额 > 0），锚胜出时为 false。纯估算时 `measured_anchor_tokens` 为 null。
- 本地计数走 **omp 同款 tokenizer 阶梯**（`src/voidcode/provider/tokenizer.py`）：模型目录条目带 `tokenizer`（omp `Encoding` 名，如 `O200kBase` / `deepseek-v3`）时用**该编码的真实词表**计数；没有该字段时回退到 **UTF-8 字节 / 4（向上取整）**，即 `(bytes + 3) >> 2`，与上游 pi-agent-core 默认一致。不按字符数估算——中文/多字节载荷按字符会低估约 3 倍。
- **口径是混合的，不是全量精确**：真实 tokenizer 只覆盖**文本**部分——当前 prompt 与全部保留工具结果的 provider 视图；`_provider_payload_bytes` 那部分（plan/instruction 段 + replay 会话）只有**字节规模**、没有文本可分词，仍按 UTF-8 字节 / 4 计（`count_payload_bytes`）。裁剪循环与判定用**同一把尺**（工具结果文本走 `count_tokens`，占位文本也对占位内容分词），不再出现"判定用真词表、省下的量用字节"的混算。
- 目录里 4 个 Claude 编码（`claude-v3`/`claude-v47`/`claude-v5`/`claude-v5-sonnet`，共 64 条模型）走 **`claude_tokenizer` 引擎**（`src/voidcode/provider/claude_tokenizer/`）：词表容器 `CTOK\x02` 已解析（467 条字节前缀 OOV 表 + 前向编码的 tiling 词表），算法是 oh-my-pi 所移植的 `sanderland/ctok`（MIT，pin `df3b59b`）——NFC/引号折叠 → 标记流（词首/词尾/大写标记、单一空格折叠为 `⟨eow⟩⟨bow⟩` 接缝）→ 最小代价分词 → 族消息帧。四者共用两套词表（v3，v4.7×{V47,V5,V5Sonnet} 仅帧不同）。对原生 `countTokens` **零不一致**：6000 段真实代码块 + 109 条对抗串 + 241 KB 中英日混排，四个编码全 0；不近似、也不映射到 cl100k。
- 词表随包发布（`provider/tokenizer_data/` 下的 `*.utok1.bz2` 与 Claude 的 `ctok_v3.bin.bz2`/`ctok_v4_7.bin.bz2`，bz2 压缩容器，共 3.90 MB），**惰性加载**：不 import 词表就不读盘，构造编码**全程离线**（直接喂 `mergeable_ranks`，不走会联网下载的 `tiktoken.get_encoding`）。重新生成见 `scripts/extract_tokenizer_data.py`，逐文件的 `source`/`license` 记录在 `tokenizer_data/manifest.json`。
- **再分发口径（已拍板）**：`Cl100kBase`/`O200kBase` 与 tiktoken 自家词表逐 rank 字节相同（MIT，上游 OpenAI）；`Glm5`/`Qwen3`/`KimiK2`/`DeepSeekV3` 提取自 oh-my-pi 的原生插件（第三方 vendor 词表，许可未随之声明），**由项目所有者决定照常随包分发**——真实 token 计数优先；4 个 Claude 词表则出自 `sanderland/ctok`（**MIT**，pin `df3b59b`），随 oh-my-pi 的容器格式转写，是许可最清晰的一例。归属与来源两段声明见仓库根 `NOTICE`，逐文件的 `source`/`license` 见 `tokenizer_data/manifest.json`。若日后要更干净的来源，可改从各 vendor 自己发布的 `tokenizer.json` 取（UTOK1 容器就是 rank==index 的原始 token 字节，直接放入无需改格式）。
- 对原生实现的覆盖：`O200kBase`/`Cl100kBase`/`Glm5`/`KimiK2`/`DeepSeekV3` 与原生实现**逐字节一致**——12 组语料共 114558 条输入上 **0 不一致**（6000+4000+9000 真实代码块与本仓/系统源码、2×混排 241 KB+54 KB、12000 条 emoji/ZWJ 密集串、72591 条逐码点模糊、3064 条定向构造、269 条空白/数字判别、6824 条随机模糊、109+699 条对抗串）；4 个 Claude 编码亦**完全一致**（6000 真实代码块 + 109 对抗串 + 241 KB 混排，0 不一致）。`Qwen3`（NFC 归一化）曾经**不精确**：其标点串类漏掉了上游的 `\p{M}` 排除（上游是 ` ?[^\s\p{L}\p{M}\p{N}]+`），于是紧跟在标点后、或在 ZWJ 语境里的组合符／变体选择符被并入标点片而非独立成片，最小复现是 ZWJ+U+2764+VS16（原生 4，旧实现 3，方向偏低）；该残差在真实代码块上约 0.05%（4000 段 1 条、9000 段 6 条），在 emoji/ZWJ 密集文本上高达 9.3%（12000 条 1117 条）；修复后（`_PAT_QWEN3` 补上 `\p{M}` 排除）在上述同一批 114558 条输入上同样 **0 不一致**。`DeepSeekV3` 曾经**不精确且方向混合**，而方向才是要紧的地方：真实代码块/源码共 19000 段中 11 段不一致（0.06%，6000 段 4 条、4000 段 6 条、9000 段 1 条），其中 7 条偏高、4 条偏低；对抗 Unicode 上偏差更大——72591+269 条模糊/判别串 **322 条不一致，其中 317 条偏低**；全 12 组语料合计 925 条不一致，**396 条偏低、529 条偏高**，两个方向都不占绝对多数。方向混合正说明旧注释里那句"总是向上取整、方向安全"是错的：偏低会推迟压缩触发，恰是预算护栏最不能接受的一侧。根因是它的分割器本就是**三段链式 `Split(Isolated)`**（上游 `deepseek-ai/DeepSeek-V3` 的 `tokenizer.json` 与 omp 的手写端口 `crates/pi-natives/src/utok/scan/deepseek.rs` 完全一致）：`\p{N}{1,3}` → Han/假名连段 → 主模式；而旧实现把它压成了一个正则。现在 `_PAT_DEEPSEEK` 是这三段的精确转写——第三段的类按 CJK 做集合交收窄、空档码点单独成段（`Isolated` 保留它们、tiktoken 会丢弃未匹配文本）、空白规则在 `ws_end` 触及一/二段边界时保留整段（`"  一"` → 2 而非 3）——在**同一批 114558 条输入上 0 不一致**，另有 341399 条新模糊输入与 4448256 条逐码点探针（每个 Unicode 标量 × 4 种上下文）同样 0。
- 所有数字都是 inexact 估算或实测锚点，**必须在 payload 中标明来源**（来源由 `measured_anchor_tokens` / `estimated_delta_tokens` 是否存在体现；纯估算时见 `usage_tokens_estimated`），不得把纯估算当作权威用量。
- 判定数字**超过**阈值（`>`，对齐 omp `compaction.ts:338`；恰好等于阈值不触发）时，从**最旧的 tool 结果**开始把 content 替换为有界占位文本，直到剩余 tool 内容回到 `keep_recent_tool_tokens` 之内（tool 结果同时计入 content 与 `data` 载荷）
- 裁剪只替换 tool result 的 content：`assistant(tool_call)` / `tool` 消息与 pairing 永不删除，system/instruction 段永不被裁剪
- 保护集（永不裁剪，对应 `window.py` 的 `_PRUNE_PROTECTED_TOOL_NAMES` / `_PRUNE_PROTECTED_PATH_PREFIXES` / `_PRUNE_PROTECTED_PATH_PARTS`）：工具名 `todo`、`skill`；`read` 的 `voidcode://rule/<name>` 路径；`.voidcode/rules` 下的结果；被保护内容不计入节省额度。artifact **故意不保护**——裁剪正是把模型指向 `voidcode://artifact/<id>`
- 无效裁剪下限（均为命名常量，不是配置键）：单条结果估算低于 `min_prune_tokens` 不裁；本次总节省低于 `min_savings_tokens` 则整轮不裁（避免无效抖动，此时只报告 overage，不改写视图）。**例外（紧急恢复）**：`min_savings_tokens` 只约束**常规**裁剪；`context_limit` 的一次性本地裁剪重试（`fit_payload`）把它视为 `0`——该请求已确定超窗，只要单条裁剪跨过 `min_prune_tokens` 就必须重试同一次调用，不得因「省得不够多」报 `context_limit_recovery=unavailable` 而直接进入升级/失败。
- 被裁掉且带 `data["artifact"]` 的结果会产出 `runtime_context_artifact_reference` 段（模型可 `read(path="voidcode://artifact/<id>")` 取回完整输出）；无 artifact 的结果只留占位
- `compacted` 仅在**实际发生缩减**时为 true；`RuntimeContextWindow` 的 `original` / `retained` / `dropped` / `truncated` 计数与 `runtime.context_compacted` payload 都是真实计数
- catalog 未描述该 model（`context_window` 无法解析）时**不裁剪**，并以明确的 `compaction_reason`（`compaction_unsized:...`）暴露该边界，不静默返回无界视图
- `context_limit` 恢复：provider 报 `context_limit` 时**不再由解析层硬判终态**（`retryable=None`，与 `rate_limit` 同属 runtime-owned lane），由 run loop 按顺序处理：
  1. **一次性本地裁剪重试**：用 `context_window.compaction.*` 的 knob 重新组装 provider view，恢复目标比常规触发更狠——整个 view 要落回 `catalog window − reserve`（等价于临时把 tool 预算压到「阈值 − 指令与 prompt 的估算」）；若实际发生缩减，则重试**同一次** provider 调用一次。每个 turn 最多一次（run-local 状态，不写入会话元数据、不影响 resume）。
  2. **升级窗口（窗口感知）**：本地裁剪不可行或重试后仍 `context_limit` 时，走**既有** provider fallback 机制（同一 target 链解析、`runtime.provider_fallback` 事件与决策形状）切换目标后重试，但候选按**有效窗口**排序：用 catalog 的 `max_input_tokens`（缺失且不可由 `context_window - max_output_tokens` 推导时为未知，不参与比较）与失败模型自己的窗口比较，只挑**严格更大**的候选；同窗或更小的候选不会因「链上在前」而被选。无严格更大候选时保持既有链顺序，并在 `runtime.provider_context_recovery` 的 `promotion_reason`（`larger_window` / `no_larger_candidate`）与 `window_tokens_before`/`window_tokens_after` 里如实记录。该偏好仅作用于 `context_limit` lane，其它 provider 错误的 fallback 顺序不变。本 turn 同样最多升级一次。
  3. **可恢复失败**：两者都不可行时以 `runtime.failed` 结束，payload 带 `provider_error_kind=context_limit`、`resumable=true`、`context_limit_recovery`（`prune` / `unavailable`）与可执行 `guidance`（缩减上下文或配置更大窗口的模型后 `voidcode sessions resume`）。该失败**保持可 resume**（`provider_failure_retryable` checkpoint 的 kind 判定已包含 `context_limit`），失败态不会被 seal 成不可恢复。
- 裁剪本身仍是可判定函数（给定预算 + 结果顺序 → 确定结果），恢复只在其上叠加「一次性」与「升级」两条有界策略。
- compaction 的窗口由随包 catalog 提供：`max_input_tokens`（无上游 `limit.input` 时由 `context_window - max_output_tokens` 派生）优先，否则 `context_window`；调用方显式提供的 `context_window` / `threshold_tokens` / `reserve_tokens` 仍然优先于它，catalog 未描述的 model 则让 compaction 保持 unsized
- 阈值 = `window − reserve`，其中 `reserve = max(floor(0.15 × window), 16384)`（对齐上游；显式 `context_window.compaction.reserve_tokens` 最高优先）。锚点不可用（无 provider usage）时仍按同一阈值做纯估算判定，并在 `usage_tokens_estimated` 里标为纯估算
- token usage 只来自 provider response/terminal stream，并保留 `None`（未报告）与 `0`（观测到零）的区别；它是后验 turn usage，不是下一轮 transcript 余额
- `provider_usage.latest.cost_usd` / `provider_usage.cumulative.cost_usd`：USD 金额，由该 turn 自己的 usage 与 catalog 的扁平 `cost_per_*` 费率（加可选的 long-context 政策 tier）在 graph 中计算一次，写入 `latest` 并累加到 `cumulative`；token 桶保持整数、金额是 float，持久化的 usage 不会被重新计价。前端在 composer 的上下文行显示 `cumulative.cost_usd`（`$1.50 spent`），未定价的 model 整个省略而不是显示 `$0.00`
- `lsp.enabled`：布尔值；默认 `true`（未声明即开启，显式 `false` 关闭；未配置 `servers` 时不启动任何 server 进程）
- `lsp.servers`：对象

对于内置 LSP server，推荐的用户配置路径是直接使用内置 server 名作为 key，例如：

```json
{
  "lsp": {
    "enabled": true,
    "servers": {
      "pyright": {},
      "gopls": {},
      "clangd": {}
    }
  }
}
```

只有在需要自定义 server 名、复用内置 preset 或声明完全自定义 server 时，才需要提供 `command` 或显式 `preset` 字段。
- `mcp.enabled`：布尔值；默认 `true`（未声明即开启，显式 `false` 关闭）
- `mcp.servers`：对象；未声明时（默认开启状态下）自动装载内置远程 MCP descriptors：`context7`、`websearch`、`grep_app`（均为 remote-http，不 spawn 本地进程；skill-scoped 的 `playwright` 不参与默认装载）
- `fallback_models`：顶层配置中的 provider fallback 入口；它以当前顶层 `model` 作为 preferred model，并指定有序 fallback chain
- `provider_fallback`：runtime 内部解析后的 fallback chain 对象，不是 `.voidcode.json` 的独立公共字段
- provider 凭据环境变量可用于 first-run discovery：设置 `VOIDCODE_MODEL=opencode-go/<model>` 与 `OPENCODE_API_KEY` 时，即使 `.voidcode.json` 没有 `providers.opencode-go` block，runtime 也会构造最小 OpenCode Go provider 配置。该 discovery 也覆盖现有标准变量：`OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`GOOGLE_API_KEY`、`GITHUB_COPILOT_TOKEN`、`ENDPOINT_API_KEY`、`OPENROUTER_API_KEY`、`DEEPSEEK_API_KEY`、`ZAI_API_KEY`、`ZHIPU_API_KEY`、`XAI_API_KEY`、`MINIMAX_API_KEY`、`MOONSHOT_API_KEY`、`KIMI_API_KEY`、`DASHSCOPE_API_KEY`、`GROQ_API_KEY`、`TOGETHER_API_KEY`、`FIREWORKS_API_KEY` 与 `MISTRAL_API_KEY`，以及 W6 新增 vendor 的 `AIAND_API_KEY`、`ALIBABA_TOKEN_PLAN_API_KEY`、`BASETEN_API_KEY`、`CLINE_API_KEY`、`COREWEAVE_API_KEY`（回退 `WANDB_API_KEY`）、`GMI_API_KEY`（回退 `GMICLOUD_API_KEY`）、`HUGGINGFACE_HUB_TOKEN`（回退 `HF_TOKEN`）、`KILO_API_KEY`、`NOVITA_API_KEY`、`NVIDIA_API_KEY`、`VENICE_API_KEY`、`WAFER_SERVERLESS_API_KEY`（回退 `WAFER_API_KEY`）、`XIAOMI_API_KEY`、`XIAOMI_TOKEN_PLAN_AMS_API_KEY` / `XIAOMI_TOKEN_PLAN_CN_API_KEY` / `XIAOMI_TOKEN_PLAN_SGP_API_KEY`（均回退 `XIAOMI_API_KEY`）与 `ZENMUX_API_KEY`；完整且权威的映射是 `provider/provider_table.json` 的 `env_vars`。这些值只进入运行时配置对象；`config show` 与 persisted runtime metadata 不会输出 s…
- provider 命名只有一套语义：机器标识是小写 vendor id（`minimax`，用于 registry key、`providers.<id>`、`provider/model` 前缀、`<ID>_API_KEY`、catalog key 与 `/api/providers` 的 `name`），人类标签由 `provider/naming.py` 单一来源给出（`MiniMax`，用于 `/api/providers` 的 `label`、`provider inspect` 与 `doctor` 的 provider 行）。配置与 CLI/HTTP 输入（`provider/model`、`providers.<id>`、web settings save、`providers.custom.<name>`）按 trim + 小写归一后再匹配，写回配置时使用 canonical id。既非内置 id、也未在 `providers.custom.<name>` 声明的 provider id 会明确报错并列出全部 canonical id 与声明方式，不再静默回退到 `providers.endpoint`；需要通用 OpenAI-compatible 网关时使用内置 `endpoint` id 或声明自定义 provider。`provider/model` 的 model 段保持原样发往上游，catalog/能力表/fallback 链比较忽略大小写，vendor 的大小写差异用 `model_map` 表达。
- provider endpoint 解析是 per-provider 的：`providers.<name>.base_url` 优先，其次才是该 provider 自身的默认 host；某个 provider 的配置缺失不会让请求回落到另一个 vendor 的 endpoint（包括 `api.openai.com`）。配置 block 完全缺失时按同一默认 host 解析；模型列表从解析出的 `base_url` 推导，没有独立的 discovery URL 配置项。`providers.google` 使用 `service_account` auth 时不产生 discovery 可用的凭据，因此该模式下 discovery 记为禁用。
- Anthropic-wire（`providers.anthropic` 及其它 Anthropic Messages 兼容 vendor）的 `cache_retention` 默认 `short`（与 omp 上游一致）：请求默认带 5 分钟 ephemeral prompt-cache breakpoint；显式 `none` 关闭缓存写入，`long` 选用 1 小时 TTL。请求级 `cache_retention`（`ProviderTurnRequest.cache_retention`）优先于 provider 配置。
- 既没有配置 `base_url`、自身也没有默认 host 的 provider 会以 `not_configured` provider error 失败（不可重试、允许 fallback），而非静默借用其它 host。
- 一次 usage/limit（HTTP 429 或 402，kind `rate_limit`）turn 不进 provider 内联重试 lane：runtime 为该运行武装了 rate-limit lane（后台任务）时延后重试（按 `retry_after` 等待，`ProviderTerminalDecision(kind="background_rate_limit_retry")`），否则交给 fallback 链的下一个 provider；`decide_provider_error_policy` 对 `rate_limit` 有独立分支，永远不会返回 `ProviderTransientRetryDecision`（`provider/README.md` → 「错误边界」）。
- `voidcode provider inspect <provider>` 的 payload 包含 `endpoint.base_url`（wire 实际使用、已按 transport 规则归一化的基础 URL）与 `endpoint.source`（`config` / `provider_default` / `endpoint_default`）。
- `providers.opencode-zen` / `providers.opencode-go` 是「一个 host、多种 wire」的网关：实际 wire 由模型决定，不由 provider 名决定；解析链为 catalog 行的 `api` → `provider/api_routes.json` 的 pin → provider table 的 wire（`api_routes.json` 按 provider 分组、声明序 first-match-wins，matcher 为 `exact` / `prefix` / `substring` / `token` / `glob`，可带 `strip_prefix`）。默认路由是 OpenAI-compatible chat-completions（`opencode-go` 的默认 base URL 归一化为 `https://opencode.ai/zen/go/v1`，Zen 为 `https://opencode.ai/zen/v1`）；解析 wire 为 `anthropic-messages` 的模型走 Anthropic Messages（host root，凭据以 `x-api-key` 发送）；解析 wire 为 `google-generative-ai` 的模型走 Google wire（`<base>/models/<model>:generateContent`，凭据以 `x-goog-api-key` 发送）；`opencode-go/minimax-m3` 解析为 chat-completions（此前误判为 Anthropic）。wire 为 OpenAI Responses API 的模型 VoidCode 未实现，会以 `unsupported_feature`（不可重试、允许 fallback）失败，不会降级到 chat-completions。没有 routing 条目的模型使用该 provider 的默认路由；`model_map` 别名先解析再选 wire，别名指向被路由的模型时走该模型的 wire（并把解析后的模型名发给上游）；`providers.<name>.base_url` 对默认路由与 Zen 的 Google 路由仍然优先。
- 两个 OpenCode 网关按会话路由：每条 wire 的每个请求都必须带 `x-opencode-session: <conversation id>`（缺失时网关返回 HTTP 400 `MissingSessionID`）与 `x-opencode-client`；runtime 在请求时从 runtime session id 解析 `x-opencode-session`（wire client 跨 turn 复用），请求没有 session id 时该 header 不发送。
- `providers.google` 的 auth 同时决定 endpoint surface：`api_key` / `oauth` 沿用原语义（只有 auth 未提供凭据时才回退到 `GOOGLE_API_KEY`）；`service_account` 会把 `auth.service_account_json_path` 指向的文件加载为 cloud-platform ADC 凭据并选择 Vertex AI surface（不会静默替换成 `GOOGLE_API_KEY`；文件缺失或无法解析时产生 `missing_auth`，不可重试、允许 fallback）。未配置 auth block 且没有 API key 的纯 ADC 用户，只要提供 `project` 和/或 `region` 也会选择 Vertex；`api_key` + `project`（无 `region`）保持原有 Gemini API endpoint。`providers.google.base_url` 是完整的 endpoint root（含版本段），SDK 不会再追加自己的 `v1beta` / `v1beta1`，并在设置自定义 base URL 时跳过自身的 ADC project 解析。
- `agent.preset`：agent preset id。可解析 builtin `leader`、`worker`、`advisor`、`explore`、`researcher`、`product`，以及本地发现的 markdown manifest id（见下方“本地 markdown agent manifest”）。
- `agent.prompt_profile`：字符串；省略时从内置 manifest 回填
- `agent.model`：字符串；对 active agent 覆盖顶层 `model`
- `agent.execution_engine`：`deterministic` 或 `provider`；`leader` 省略时从内置 manifest 回填为 `provider`
- `agent.tools`：与顶层 `tools` 相同的配置 shape；当前作为 active agent tool boundary 解析、序列化和持久化
- `agent.tools.allowlist`：字符串数组；与 manifest allowlist 一起收窄 active agent 可见/可调用工具集合
- `agent.tools.default`：字符串数组；在 allowlist 允许范围内进一步收窄默认暴露工具
- `agent.skills`：与顶层 `skills` 相同的配置 shape；对 active agent 覆盖本次运行使用的 runtime-managed skill discovery / application policy
- `agent.fallback_models`：agent-scoped shorthand；必须同时配置 `agent.model`，runtime 会把 `agent.model` 作为内部 `provider_fallback.preferred_model`，并把该数组作为 fallback chain；这是 agent 配置中唯一的 fallback 配置入口
- `agents.<preset>`：按 preset 配置 delegated child / primary agent defaults；builtin key 与已发现本地 manifest key 可省略 `preset`，alias key 必须显式声明 `preset`。
- `agents.<preset>.fallback_models`：与 `agent.fallback_models` 相同的 shorthand；delegation path 会把选中 preset 的 fallback chain 持久化到 child session metadata。
- `tui.keymap`：对象，值只允许 TUI 暴露的 namespaced action：`app.session.new`、`app.session.resume`、`app.tools.expand`、`app.display.reset`（强制整帧重绘 live 区域）、`app.history.search`（从 composer 的 prompt history 里挑一条回填 draft）。未知 action 在启动前直接报错。
- `tui.preferences.theme.name`：字符串，可选。runtime 只携带/合并该偏好，不做任何调色板名校验、也不提供内置调色板列表；TUI 用自己的调色板注册表解析，未知或缺失的名字回落到 `theme.mode` 对应的默认调色板。
- `tui.preferences.theme.mode`：`auto`、`light`、`dark` 之一；缺省为 `auto`
- `reminders.enabled`：布尔值；默认 `true`。关闭后 runtime 不再通过 per-call reminder 通道注入任何提醒（也不写 reminder 计数器）
- `reminders.todo.max_per_cycle`：正整数；默认 `3`。一个 cycle（一次 run，即 `runtime_state.run_id`）内最多注入多少条 todo 完成提醒

### external directory permission 语义

- `permission.external_directory_read` 与 `permission.external_directory_write` 是 runtime-owned 的外部目录权限面。
- workspace 内路径保持现有治理：`read` tier 在每个模式下都自动放行；`write`/`execute` tier 由 `approval_mode` 与 tier 的矩阵决定是否进入审批（`yolo` 除外）。
- workspace 外路径使用 external permission 决策：
  - read-like tool calls 使用 `external_directory_read`
  - write-like tool calls 使用 `external_directory_write`
- 当前默认值：
  - `external_directory_read = {"*": "allow"}`
  - `external_directory_write = {"*": "allow"}`
- rule matching 使用按顺序匹配（first-match-wins）；路径在匹配前会做 canonicalization。

### reminder 语义（`reminders`）

- reminder 是 runtime-owned 的 **per-call** 注入通道：提醒文本作为 provider context 的尾部 segment 只对本次 provider 调用可见（`hook/percall.py` 的 `PerCallMessage(per_call=True)`），既不写入 SQLite transcript，也不进入 per-call cache hash；可持久化的只有 cycle 计数器（`SessionState.metadata["runtime_state"]["reminders"]`）与一条 `runtime.reminder_injected` 事件（见 `docs/contracts/runtime-events.md`）。
- mid-run nudge 无独立开关：它随 `reminders.enabled` 启用，阈值（累计 12 次变更类工具调用）与每 cycle 上限（2 条）是命名常量（对齐上游），不提供配置键。
- 第一个 reminder 类型是 todo 完成提醒：terminal assistant 回合结束时若仍有 `pending` / `in_progress` todo，runtime 注入一条 `<system-reminder>` 尾巴并继续同一 run（而不是结束回合）；提醒文本列出未完成的 phase/task 与 `(Reminder k/max)`。
- 抑制条件：`reminders.enabled = false`、execution engine 不是 `provider`（deterministic graph 没有可注入的 provider 调用）、todo 全部完成、上一条 reminder 之后的回合没有产生新的 tool result（仍在等待 agent 行动）、本 cycle 已达 `reminders.todo.max_per_cycle`、assistant 已停在等待用户回答（`plan_state.status` 为 `waiting_question` / `waiting_approval`）、父会话仍有会重新唤醒 loop 的 background task，或当前是被委派的 child session（必须通过 `yield` 终止）。
- cycle 由 run 标识：每次 `run` / `resume` 都是新的 cycle，`attempts` 随之复位；持久化的 `runtime_state.reminders` 因此不携带跨 run 的累计语义，也不参与 context checkpoint 的完整性校验（见 `context/continuity.py`）。
- 该配置是仓库本地（`.voidcode.json`）配置面；像 `background_task` 一样，它随会话的 `runtime_config` 快照持久化，缺省时以当前进程解析出的默认值补齐。

### pattern-based permission rules 语义

- `permission.rules` 是 runtime 在工具执行前评估的通用 permission rule 面；客户端、agent、command 与 custom tool 不能绕过它。
- 每条规则的形状为：

```json
{
  "tool": "write",
  "path": ".github/**",
  "decision": "ask"
}
```

- 字段语义：
  - `tool`：工具名 glob；省略时等同 `*`。
  - `path`：workspace-relative 或 canonical path glob；对文件系统工具与 `shell_exec` 中已识别的显式输出路径生效。
  - `command`：`shell_exec` command 字符串 glob，例如 `pytest*`、`mise run test` 或 `rm -rf *`。
  - `decision`：必填，值为 `allow`、`ask` 或 `deny`。
- 匹配语义是确定性的 first-match-wins；只有第一条匹配的 pattern rule 生效。
- `permission.rules` 不能扩大 hard boundary：agent/tool allowlist 仍先收窄可见与可调用工具；外部目录规则仍先保护 workspace 外路径，且 pattern rule 只能把 external decision 收紧（例如 `allow -> ask/deny` 或 `ask -> deny`），不能把 external `deny` 降级为 `allow`。
- workspace 内只读工具仍默认允许；如果需要让某个只读工具进入审批或拒绝路径，可用 `permission.rules` 显式 `ask` 或 `deny`。

这些规则不替代稳定 runtime `mode` / `read_only` 策略。`analyze`、`plan` 与显式 `read_only=true` 会先形成 effective read-only context：mutating tools 会被 registry policy 隐藏/拒绝；`shell_exec` 仍可见，但每条命令还要经过 centralized shell classifier，package-manager、mutating 与 destructive command 在 read-only context 中会被拒绝。CLI / frontend / graph 只能传递请求意图，不应复制这层 enforcement。

常见策略示例：

```json
{
  "permission": {
    "rules": [
      {"tool": "read", "path": "src/**", "decision": "allow"},
      {"tool": "grep", "path": "src/**", "decision": "allow"},
      {"tool": "glob", "path": "src/**", "decision": "allow"},
      {"tool": "write", "path": ".github/**", "decision": "ask"},
      {"tool": "edit", "path": ".github/**", "decision": "ask"},
      {"tool": "shell_exec", "command": "pytest*", "decision": "allow"},
      {"tool": "shell_exec", "command": "mise run test", "decision": "allow"},
      {"tool": "shell_exec", "command": "rm -rf *", "decision": "deny"}
    ],
    "external_directory_write": {"*": "ask"}
  }
}
```

所有扩展领域字段都是可选的。省略时，它们在领域级别解析为 `None`，并且数组字段在提供的领域对象内部默认回退为空元组。

### 仓库本地自定义 Tools

`tools.local` 是一个 opt-in 的本地优先扩展点，用于把仓库内声明的命令包装成 typed tool。它不是 marketplace、不是客户端执行路径，也不是 workspace-scoped MCP；runtime 负责发现 manifest、注册 tool、执行命令、注入 session context，并继续使用现有 `tools.allowlist` / `agent.tools.allowlist` / permission 默认策略治理可见性和调用。

当 `tools.local.enabled=true` 时，runtime 会读取 `tools.local.path`（默认 `.voidcode/tools`）下的 `*.json` manifest。最小 manifest 形状：

```json
{
  "name": "local/echo",
  "description": "Echo JSON arguments",
  "input_schema": {
    "type": "object",
    "properties": {
      "path": {"type": "string"},
      "message": {"type": "string"}
    }
  },
  "command": ["python", "${manifest_dir}/echo.py"],
  "read_only": true,
  "path_argument_keys": ["path"]
}
```

字段语义：

- `read_only` 直接进入 `ToolDefinition`，影响 runtime 的默认 permission policy（只读工具默认 allow，非只读工具默认 ask）；但 local custom tool 仍按 command execution 治理，不会仅凭声明绕过 approval、read-only mode 或 replay 约束。
- `path_argument_keys` 是可选的字符串数组，列出 `ToolCall.arguments` 中应作为路径候选的字段名。runtime 将这些值传入统一的 permission context，用于 external-directory policy；它不会改变 local custom tool 的 `execute` operation class。

执行语义：

- `name` 必须稳定且不能与内置、MCP 或其他 runtime tool 重名；重名会 fail-fast，而不是覆盖。
- `description` 与 `input_schema` 直接进入 `ToolDefinition`，供 provider/tool boundary 使用。
- `command` 由 runtime 在 workspace cwd 中执行；它必须是 argv 数组，不是 shell 字符串，runtime 不隐式启动 `sh` / `cmd.exe`。tool arguments 仅以 JSON 写入 stdin，不会通过环境变量暴露完整参数 payload。
- runtime 环境变量只携带有界执行上下文，例如 `VOIDCODE_WORKSPACE`、`VOIDCODE_TOOL_NAME`、`VOIDCODE_TOOL_CALL_ID`（如有）、`VOIDCODE_SESSION_ID`、`VOIDCODE_PARENT_SESSION_ID`（如有）和 `VOIDCODE_DELEGATION_DEPTH`，tool 作者不应从客户端获得这些上下文字段。

内置 `shell_exec` 的 command string 不是 hook/local-tool argv contract 的例外绕行通道。runtime 会在执行前解析 workspace、timeout 与 command classification；interactive/TUI command 在非交互路径 fail-fast，read-only context 拒绝 package-manager/mutating/destructive command，project package manager 的 non-interactive env injection 只把 key name 进入结果 metadata，不持久化 env value。

## Runtime Harness Policy 配置字段

Runtime Harness Policy v1 的仓库本地配置面必须保持 schema-bounded。字段为 `policy`，它只能声明可验证的、可序列化的默认策略意图，例如：

```json
{
  "policy": {
    "enabled": true,
    "version": "v1",
    "tool_policy": {
      "default": "runtime_default"
    },
    "delegation_policy": {
      "default": "runtime_default",
      "deny": ["product"]
    },
    "hook_policy": {
      "allowed_event_scopes": ["session_start", "pre_tool", "post_tool", "delegated_result_available"]
    },
    "prompt_activation": {
      "enabled": true
    }
  }
}
```

配置只能选择、收窄或记录 policy intent；不能授予 hard-denied tool、delegation target、hook authority、MCP server、approval 或 product delegation。未知 policy key、未知 hook event scope、无效 tool policy shape、`metadata` 这类无界 escape hatch、以及任何试图把 `product` 加入 delegated child allowlist 的配置都必须 fail fast。

当 fresh run materialize policy 后，`RuntimePolicySnapshot` 会进入 session truth，并通过 `runtime.request_received.payload.runtime_policy` 暴露有界、脱敏的 debug projection。resume/replay 必须读取完整的已持久化 v1 snapshot；snapshot 缺失、版本不匹配或 shape 不完整都会立即失败。未知 policy key、unsupported policy version、无界 diagnostics 入口和 product delegation allow 同样是 fail-fast 配置错误。

## Agent preset runtime consumption 边界

当前 runtime 已经能够解析内置 agent preset，但会区分“顶层 active agent”与“delegated child agent”两条执行边界：

- 顶层 active run 的唯一 builtin preset 是 `leader`；`product` 不能作为 top-level active agent
- runtime-owned delegation path 上的 child run 可执行 builtin `advisor`、`explore`、`researcher`、`worker`、`product`，以及本地自定义 `mode: subagent` manifest

这个限制是有意的：`product` 是 delegated read-only plan child，只有在 delegation path（例如带 `parent_session_id` 且通过 subagent routing 校验的请求）中才会进入真实执行路径。

### 本地 markdown agent manifest

MVP 支持 true local manifest，而不是 marketplace / plugin distribution。发现路径为：

- project scope：`<workspace>/.voidcode/agents/*.md`
- user scope：Linux/macOS 使用 `$XDG_CONFIG_HOME/voidcode/agents/*.md`，未设置 `XDG_CONFIG_HOME` 时为 `~/.config/voidcode/agents/*.md`；Windows 使用 `%APPDATA%\voidcode\agents`，缺失时回退到 `%LOCALAPPDATA%\voidcode\agents`。

每个 markdown 文件必须以 YAML frontmatter 开头（共享实现见 `src/voidcode/frontmatter.py`，使用标准 YAML 语义：隐式类型、引号、flow sequence/mapping、`|`/`>` block scalar；重复 key、非字符串 key、非法 YAML 与超过 64 KiB 的 frontmatter 都会 fail-fast），随后正文作为 prompt material：

```markdown
---
name: Review Helper
description: Read-only reviewer for focused code quality checks.
mode: subagent
tool_allowlist: [read, glob, grep]
skill_refs: [code-review]
preset_hook_refs: [role_reminder]
prompt_append: |
  Always include severity and exact file paths in findings.
---
You are a focused reviewer. Stay within the runtime-provided tools and report risks clearly.
```

必需字段：`name`、`description`、`mode`（`primary` 或 `subagent`）。支持字段：`id`、`name`、`description`、`mode`、`model`、`fallback_models`、`tool_allowlist`、`skill_refs`、`preset_hook_refs`、`mcp_binding`、`prompt_append`。字段类型仍是 manifest 校验职责：字符串字段必须是非空字符串（YAML 隐式类型如未加引号的 `yes`、日期会被拒绝，不会被静默 `str()`），`fallback_models` / `tool_allowlist` / `skill_refs` / `preset_hook_refs` 必须是字符串数组，`mcp_binding` 必须是只含 `profile` / `servers` 的对象。正文是 primary prompt；`prompt_append` 会作为附加本地 guidance 单独持久化，并在 provider context 中追加一次。未声明 `id` 时，runtime 使用 `name` 的 lowercase-kebab 形式作为稳定 id；显式 / 派生 id 必须匹配现有 agents key 风格 `^[a-z][a-z0-9_-]*$`。

发现优先级：同一 custom id 下 project scope 覆盖 user scope；同一 scope 内重复 id 会 fail-fast；custom manifest 不允许使用 builtin id（例如 `leader` 或 `worker`）替换 builtin preset。错误会包含具体文件路径，便于修复。

frontmatter 是标准 YAML：未加引号的标量中 ` #` 会开始注释（`description: Fix bug #42` 实际读作 `Fix bug`），需要保留字面文本时必须加引号（`description: "Fix bug #42"`）；值中间出现 `: ` 或以 YAML 指示符（`&`、`*`、`!`、`%`、`@`、反引号）开头时同样要加引号。声明了 frontmatter 的 manifest 必须同时提供非空的正文 prompt。

本地 manifest 只声明 prompt 和默认 capability intent。它不会绕过 runtime tool allowlist、approval、MCP lifecycle、hook execution、skill loading 或 delegated child session contract。正文 prompt 与可选 `prompt_append` 仅作为 runtime-owned 的 `runtime_internal` persisted structure 写入 session metadata（不属于 public runtime config 或 provider-visible contract），用于 resume / replay 固定历史 session 的解释上下文；manifest 文件后续变更不会静默改变历史 session。

`.voidcode.json` 的 `agent` / `agents.<key>` 也可声明 `prompt` 与 `prompt_append`：`prompt` 明确替换/定义 profile text，`prompt_append` 在 resolved base prompt 后追加本地 guidance。resolved prompt materialization 只保存在 runtime_internal persisted structure 中，渲染时只追加一次。

`voidcode run --agent <id>` 不再使用 argparse 静态 choices；它会在加载 runtime config 与本地 manifest 后验证 `<id>` 是否为可顶层执行的 builtin/custom primary agent。

`voidcode agents list --workspace <path> [--json]` 会列出 builtin 与本地 custom primary agents，并在 custom agents 上显示 `source_scope` / `source_path`。

`leader` 顶层 preset 与 `product` delegated child preset 当前进入 runtime truth 的字段是：

- `preset`
- `prompt_profile`：注入 provider turn 的 agent profile system message
- `model`：覆盖本次运行的 resolved provider model
- `execution_engine`：`leader` 与 `product` 默认进入 runtime-managed `provider` 路径
- `tools`：收窄 provider 可见工具与实际 tool lookup / invocation 边界；manifest allowlist、`agent.tools.allowlist`、`agent.tools.default` 按交集生效，`agent.tools.builtin.enabled=false` 只移除内置工具名集合，仍保留已通过 allowlist 的 runtime-managed MCP / 注入工具
- `skills`：覆盖本次运行的 skill registry discovery 与 applied skill payload / prompt context；active manifest 的 `skill_refs` 会作为默认 skill selection 进入 application，并与 request metadata `skills` 去重合并
- `provider_fallback`：runtime 内部解析后的 fallback model chain
- `fallback_models`：配置与 session metadata 中的 fallback chain 字段；仅在同一 agent 配置了 `model` 时作为 shorthand 生效

这些字段影响当前 runtime-managed provider 主路径；其中 resolved prompt materialization 仅作为 runtime_internal persisted structure 持久化，不是 public runtime config 或 provider-visible metadata，以保证 resume / replay 不被新的 runtime 默认值污染。

以下字段当前仍只作为声明层 metadata 保留，不代表 runtime 已经实现相应能力语义：

- 把 `worker` / `advisor` / `explore` / `researcher` 作为任意顶层 active preset 的 config intent：runtime 仍不会把它们当作普通顶层会话直接执行。

如果运行时在顶层 active run 中收到非 top-level-selectable preset，例如：

```json
{"agent": {"preset": "worker"}}
```

则 runtime 会拒绝执行，而不是把 child role 悄悄映射到普通顶层 deterministic/provider 路径。这保持了 `agent/` declaration layer 与 `runtime/` execution truth 的边界；这些 preset 只能通过 runtime-owned delegation path 进入真实 child execution。

## TUI 偏好优先级与持久化语义

TUI 偏好与其他多数领域不同，拥有一条单独的双层优先级链：

1. workspace override（仓库本地 `.voidcode.json`）
2. global default（`~/.config/voidcode/config.json`）
3. built-in defaults

其中第一阶段已实现的 built-in defaults 为：

- `tui.preferences.theme.mode` -> `auto`

runtime 不提供 `theme.name` 的内置默认值，也不持有调色板注册表；名字缺省时由 TUI 按 mode 默认调色板解析。

### 重要语义

- workspace override 仍然是“局部覆盖”，不是完整快照。
- 但当前 TUI 产品默认不会把普通偏好修改写回 workspace。
- 普通 theme / theme mode 修改默认写回 global default。
- workspace 中未覆盖的字段继续继承 global default；workspace override 只用于显式的项目级覆盖语义。

### 当前已实现的全局配置路径

用户级全局 TUI 默认配置路径为：

`~/.config/voidcode/config.json`

workspace 本地覆盖路径保持为：

`<workspace>/.voidcode.json`

## 关于 LSP 和 ACP 基础设施状态的说明

在当前切片中，`acp` 已进入最小的 runtime-managed transport/lifecycle 路径；`lsp` 也已经拥有最小 runtime-managed 基线，但两者都仍保持严格收敛的 MVP 范围。

- 它们的存在是为了让运行时消费稳定的类型化配置，并为更强的 capability 管理保留边界。
- `acp` 作为 runtime 内部 capability 继续存在，但不再属于 repo-local `.voidcode.json` 的用户配置领域；它的运行结果通过 session metadata 中的 `runtime_state` 暴露，而不是进入用户主配置快照 `runtime_config`。
- `acp` 当前只支持 runtime-owned `memory` transport。启用后，运行时会在 run / approval-resume 启动阶段执行 connect + handshake，并在该次运行结束时 disconnect。
- ACP contract 现在已经是 delegation-aware：request / response / event envelope 会携带 `parent_session_id` 与 `AcpDelegatedExecution`，runtime 也会发出 `runtime.acp_delegated_lifecycle` 来对齐 delegated child lifecycle observability。
- 如果 `acp` startup / handshake 失败，运行时会将 ACP 状态标记为 `failed`，发出 `runtime.acp_failed`，并使本次运行通过已有失败路径结束；不会静默降级为 disconnected 继续执行。
- `lsp` 已支持最小的 runtime-managed server 启动与只读工具访问，并且 `lsp.servers` 已可消费内置 preset、extension/language 映射、root markers 与默认 command/preset override merge。
- 对用户来说，内置 LSP server 的规范配置面是 `lsp.servers.{builtin_name}: {}`；显式 `preset` 仅用于自定义 server 名复用内置 preset，而不是主配置入口。
- 当 repo-local `.voidcode.json` 未显式提供 `lsp` 配置时，运行时现在会为高置信度 workspace 自动推导最小默认值：当前覆盖 Python (`pyright`)、TypeScript/JavaScript (`tsserver`)、Go (`gopls`)、Rust (`rust-analyzer`)、C/C++ (`clangd`)、Java (`jdtls`)、Lua (`lua_ls`)、Zig (`zls`) 与 C# (`csharp-ls`)，并且只有在对应语言服务器可执行文件存在时才会启用。
- 显式的 repo-local `lsp` 配置（包括 `enabled: false`）继续具有最高优先级；自动推导默认值不会覆盖用户已声明的 server 列表或关闭语义。

## 工作区的引导规则

`workspace` 的解析不遵循与普通运行时配置字段相同的优先级阶梯。

它必须首先被确定，以便运行时发现该工作区下的任何仓库本地配置。在 MVP 中：

1. 显式的运行时/引导输入选择工作区根目录
2. 随后在该工作区内发现仓库本地配置
3. 普通运行时配置优先级适用于非引导字段，如 `model`、`approval_mode` 和 `hooks`

对于 harness/runtime 这条配置链，`approval_mode`、`model`、`execution_engine` 和 `reasoning_effort` 的固定优先级与运行时 policy 一致，按下列顺序解释：

1. Hardcoded safety denylist and workspace/session invariants.
2. Runtime request mode and `read_only` policy.
3. Parent session/delegated-task inherited constraints.
4. Workspace/project runtime config.
5. Agent manifest/tool allowlist.
6. CLI flags that map into runtime request fields.
7. Default tool registry/provider capabilities.

这里的“CLI flags”指会被 runtime 归一化进 request fields 的输入，而不是绕过 runtime 的直接开关。恢复会话时，`SessionState.metadata["runtime_config"]` 仍然是 session-level 的 inherited constraint，属于第 3 层，不会被新的默认值静默覆盖。

其余领域仍保持浅层仓库本地配置语义，不在此轨道中获得这条完整优先级引擎。

对于 fresh run，`RuntimeRequest.metadata["reasoning_effort"]` 可以作为窄范围的请求级覆盖；`execution_engine` 同样会进入显式 / 仓库本地 / 环境 / 默认值解析，并在会话恢复时优先采用持久化的会话配置。未知 metadata 会因 request schema 拒绝，不能静默忽略。

`reasoning_effort` 的 capability-aware 校验由 runtime 在请求处理早期完成，判定顺序固定为：先取解析后的 `provider/model` 的 model metadata `supports_reasoning_effort`（显式 `False` → 抛 `RuntimeRequestError`；显式 `True` → 通过），metadata 缺省即视为未知（没有 provider 级 allowlist / denylist 判定），此时按 best-effort 透传，并在 provider readiness 中报告 `capability_source="unknown"` 与 `status="forwarded_unverified"`。readiness payload 不再声明具体的 provider 参数名：不同 adapter 使用 `extra_body.thinking.type`、`extra_body.reasoning_effort`、`thinking`、`thinking_config` 等不同字段，凭 provider 名猜测会给出错误信息。

## 当前代码锚点

- `VoidCodeRuntime(workspace=...)`
- `RuntimeRequest(prompt, session_id, metadata)`
- `SessionState.metadata`
- SQLite 存储的会话持久化元数据

## 推荐优先级

`load_runtime_config()` 使用以下唯一解析顺序（`approval_mode` / `model`）：

1. 显式参数和 request-level 覆盖
2. 仓库本地配置文件（`<workspace>/.voidcode.json`）
3. 环境变量（`VOIDCODE_APPROVAL_MODE` / `VOIDCODE_MODEL`）
4. 用户级配置文件（`~/.config/voidcode/config.json`）
5. 内置默认值

用户级文件是机器级基线：它比项目文件（更具体）和环境变量（更显式）都弱，因此在这两者存在时被覆盖。环境变量这一层仍严格按契约解析——设置为非法值时直接抛错，不会静默落到用户级基线；空字符串的处理与既有行为一致（`model` 为空直接抛错）。

对于恢复的会话，持久化在 `SessionState.metadata["runtime_config"]` 中的 `approval_mode` / `model` 就是会话覆盖，并且优先级高于新的 CLI / 客户端覆盖。

持久化 runtime config 使用单一当前 shape。`approval_mode`、`permission`、`execution_engine`、`tool_timeout_seconds` 和 `fallback_models` 均为必填字段；缺失字段、无效类型或未知字段直接失败。resume/replay 不迁移、不丢弃未知字段，也不从当前进程默认值补齐。


## 计划的会话覆盖形状

会话作用域的覆盖应能与仓库默认值分开表示。锁定的 MVP 形状为：

```json
{
  "runtime_config": {
    "model": "opencode-zen/gpt-5.4-pro",
    "approval_mode": "yolo",
    "execution_engine": "provider",
    "reasoning_effort": "high"
  }
}
```

这有意设计得很窄：在 MVP 中，`approval_mode`、`model`、`execution_engine` 与 `reasoning_effort` 是恢复关键字段，并在会话启动后转化为持久化会话配置。

## 会话持久化设置

关键的恢复设置应随会话一起持久化，至少包括：

- 工作区（现有持久化字段）
- 审批模式
- 与确定性恢复行为相关的已选模型/供应商
- execution engine
- execution engine 的 step budget

## 当前代码映射

代码库中当前的具体存储/映射点包括：

- `VoidCodeRuntime(workspace=...)` 提供活跃的工作区根目录
- `RuntimeRequest.metadata` 是经过 `validate_runtime_request_metadata` 校验的、schema-bounded 请求作用域容器；未知字段、错误类型和无效嵌套 shape 会 fail-fast
- `SessionState.metadata` 在内存中存储运行时/会话元数据
- SQLite 会话存储将 `SessionState.metadata` 作为持久化会话 payload 的一部分进行保存
- SQLite 会话存储还将 `workspace` 持久化为 `sessions.workspace` 中的一等公民列，并将其用于会话列出和查找

锁定的 CLI 检查路径为：

```bash
voidcode config show --workspace <path> [--session <id>]
voidcode config schema
voidcode config init --workspace <path> [--force] [--print] [--with-examples]
```

`config show` 成功输出必须是 JSON，且当前至少包含：

- `workspace`
- `session_id`
- `approval_mode`
- `execution_engine`
- `model`
- `fallback_models`（provider fallback 链；未配置时为空数组）
- `reasoning_effort`（仅当配置或会话覆盖该字段时出现）
- `agent`
- `agents`
- `categories`
- `resolved_provider`
- `provider_readiness`
- `context_budget`
- `mcp`

`config schema` 成功输出必须是 JSON Schema 文档，用于描述仓库本地 `.voidcode.json` 的当前公共形状。schema 的稳定 `$id` 为：

```text
https://raw.githubusercontent.com/lei-jia-xing/voidcode/master/schema/voidcode.config.schema.json
```

`config init` 生成不含 secrets 的 starter workspace 配置。默认写入 `<workspace>/.voidcode.json`，并在文件已存在时失败；`--force` 显式覆盖，`--print` 只输出 JSON 而不写入文件，`--with-examples` 会加入最小 `tools` / `skills` 示例块。生成配置默认包含 `$schema` 与 `approval_mode: "yolo"`，不会生成 `providers` 或任何 `api_key` / token 字段。

失败契约锁定为：

- invalid workspace → 非零退出码，stderr 文本错误，无 JSON
- nonexistent session → 非零退出码，stderr 文本错误，无 JSON
- workspace/session mismatch → 非零退出码，stderr 文本错误，无 JSON

## 不变量

- 用户无需编辑代码即可更改运行时行为
- 优先级必须是确定性的
- 持久化会话必须携带足够的配置，以便进行有意义的重放或恢复
- MVP 配置界面必须专注于运行时驱动的确定性执行路径

## 当前限制

- hooks 在此轨道中已包含 `pre_tool` / `post_tool` 以及 `session_start`、`session_end`、`session_idle`、`background_task_completed`、`background_task_failed`、`background_task_cancelled`、`delegated_result_available`、`turn_progress`、`stuck_detected` 这些 lifecycle hook phases；但仍不包含 render/message-transform 一类更宽的展示层阶段
- hooks 不得改变工具参数或结果，只能观察与失败中止
- 除 `approval_mode` / `model` / `execution_engine` / `reasoning_effort` 外，其余扩展领域继续保持浅层仓库本地配置
- 仅 `approval_mode` / `model` / `execution_engine` / `reasoning_effort` 在此轨道中具备恢复关键的优先级行为
- 当前 request metadata 已是受限的 runtime schema：顶层字段必须属于稳定 allowlist（内部字段仅由 runtime 使用），`command` / `delegation` 等嵌套对象也会拒绝未知字段或无效类型；入口统一 fail-fast，详见 `src/voidcode/runtime/contracts.py::validate_runtime_request_metadata`。

## 非目标

- 高级的多智能体配置
- 特定于供应商的机密管理详情
- 完整的策略 DSL
- 丰富的 OpenCode 风格 hooks 框架
- HTTP config inspection endpoint

## 验收检查点

- 存在一份配置文档，供后续实现直接遵循
- 持久化会话契约显式指出了哪些设置在恢复后依然有效
- 配置优先级已被记录，并被 TUI/Web 实现工作所复用
- 配置文档包含仓库/运行时默认值和会话级覆盖的最小具体形状
