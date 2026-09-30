# `voidcode.provider`

这里是 provider 能力层的核心目录，承载 provider/model 解析契约、注册中心、配置解析以及 fallback 语义逻辑。

## 负责什么

- Provider 和 Model 引用 Schema 定义
- 已解析（Resolved）的 Provider Config 模型与校验
- Provider Registry 注册中心
- 确定性的模型 Fallback 解析辅助逻辑
- Provider 错误分类与流式事件标准化

## 不负责什么

- Graph 执行编排（由 `voidcode.graph` 负责）
- Runtime 重试循环和持久化 Attempt 状态
- Session Metadata 持久化（由 `voidcode.runtime.storage` 负责）
- Runtime 事件路由与客户端交付

## Python 导入边界

`voidcode.provider` 和 `voidcode.tools` 的 package init 不加载实现、不再 re-export 类或 helper；从定义 owner 导入：

```python
from voidcode.core.transcript import AssembledContext, ContextSegment, ContextWindow, ToolResultView
from voidcode.provider.protocol import ProviderTurnRequest, ProviderTurnResult
from voidcode.provider.openai_native import OpenAIChatCompletionsProvider
from voidcode.tools.contracts import ToolCall, ToolDefinition, ToolResult
from voidcode.tools.read import ReadTool
```

中立 transcript 是本轮 model-facing view，不是 session history。`ToolResultView` 隔离原始结果及其嵌套 data；runtime 仍拥有 history/replay、redaction、context budget 和 policy。导入 provider/tool contracts 或运行现有 graph 步骤不需要 runtime、SQLite 或 UI；独立完整 turn engine 的 cutover 属于 P3。

## Provider 配置与优先级

VoidCode 遵循严格的优先级阶梯来确定最终生效的模型和供应商配置。

### 优先级阶梯

1. **会话覆盖 (Session Override)**: 仅在恢复（Resume）已存在的会话时有效，从 `SessionState.metadata["runtime_config"]` 加载。
2. **请求覆盖 (Request Override)**: 通过 CLI 标志（如 `--model`）或客户端 API 请求显式传入的覆盖。
3. **仓库本地配置**: 工作区根目录下的 `.voidcode.json` 文件。
4. **环境变量**: 系统环境变量（如 `VOIDCODE_MODEL`）。
5. **内置默认值**: 系统预设的底座。

### 配置 Schema

在 `.voidcode.json` 中，Provider 配置位于顶级 `providers` 字段下：

```json
{
  "model": "anthropic/claude-3-5-sonnet-latest",
  "providers": {
    "openai": {
      "api_key": "sk-...",
      "base_url": "https://api.openai.com/v1"
    },
    "anthropic": {
      "api_key": "sk-ant-...",
      "timeout_seconds": 30.0
    },
    "google": {
      "auth": {
        "method": "api_key",
        "api_key": "AIza..."
      },
      "project": "my-project-id"
    }
  }
}
```

### Provider 命名（id 与 label）

Provider 只有**一个机器标识**和一个**人类标签**，两者各有出处：

- **机器标识**：小写 vendor id（`minimax`）。它是 registry key、`providers.<id>` 配置键、
  `provider/model` 前缀、`<ID>_API_KEY` 环境变量前缀、模型 catalog 的 key、`/api/providers` 的
  `name`，以及所有内部查表使用的值。
- **人类标签**：`provider_label`（`MiniMax`，唯一定义在 `provider/naming.py`）。`/api/providers`
  的 `label`、`voidcode provider inspect` 的 `provider.label` 与 `voidcode doctor` 的 provider 行
  都读它；没有显式标签的 provider 退化为它的 canonical id。
- **输入大小写不敏感且会 trim**：`MiniMax/MiniMax-M2.5`、`MINIMAX/minimax-m2.5`、
  ` minimax /minimax-m2.5 ` 解析为同一个 provider、同一个 endpoint、同一份能力元数据；
  `providers.MiniMax` 与 `providers.minimax` 是同一个配置块；`providers.custom.<name>` 的 key
  同样按 canonical id 归一（因此 `custom.MiniMax` 会因与内置名冲突而报错）。
  被接受输入写回配置时（`config init --model`、web settings save）存的是 canonical id。
- **未声明的 id 会明确失败**：既不是内置 id、也没有在 `providers.custom.<name>` 声明的 provider
  不再静默复用 `providers.endpoint` 的配置，而是报错并列出全部 canonical id 与自定义 provider
  的声明方式。需要通用 OpenAI-compatible endpoint 时用内置 id `endpoint`（`endpoint/<model>`），
  或按下方「自定义 Provider」声明 `providers.custom.<name>`。
- **model id 不归一**：`provider/model` 的 model 段按原样发给上游（大小写保留，仅 trim），
  catalog/能力表/fallback 链比较按大小写不敏感匹配。catalog 里的 model id 一律小写
  （`minimax-m2.5`），vendor 自身的 id 可能是混合大小写（`MiniMax-M2.5`）：用 `model_map`
  做别名/大小写映射，例如 `{"m2.5": "MiniMax-M2.5"}`。不要依赖 provider 侧做大小写折叠。

## 认证方式与机密管理

### 推荐做法

- **环境变量优先**: 推荐通过环境变量提供 API Key，避免在 `.voidcode.json` 中提交明文机密。
- **无持久化机密**: 运行时在内存中持有机密，持久化会话快照时会剔除敏感字段。

### 各供应商认证

| 供应商 | 认证字段 (Config) | 默认环境变量 | 支持的 Method |
| :--- | :--- | :--- | :--- |
| **OpenAI** | `api_key` | `OPENAI_API_KEY` | - |
| **Anthropic** | `api_key` | `ANTHROPIC_API_KEY` | - |
| **Google** | `auth.api_key` | `GOOGLE_API_KEY` | `api_key`, `oauth`, `service_account` |
| **GitHub Copilot** | `auth.token` | `GITHUB_COPILOT_TOKEN` | `token`, `oauth` |
| **Endpoint** | `api_key` / `api_key_env_var` | `ENDPOINT_API_KEY` | `api_key`, `none` |
| **OpenCode Zen** | `api_key` / `api_key_env_var` | `OPENCODE_API_KEY` | `api_key` |
| **OpenRouter** | `api_key` / `api_key_env_var` | `OPENROUTER_API_KEY` | `api_key`, `none` |

### 一等 OpenAI-compatible Provider

以下 provider 默认都复用官方 OpenAI SDK（`openai` package）的 chat-completions 路径（`opencode-go` / `opencode-zen` 例外：它们按模型选择 wire，见下文 OpenCode 说明）；配置支持 `api_key`、`api_key_env_var`、`base_url`、`ssl_verify`、`timeout_seconds` 和 `model_map`。

| Provider | 配置 Key | 默认 Base URL | 默认环境变量 |
| :--- | :--- | :--- | :--- |
| **Z.AI** | `zai` | `https://api.z.ai/api/paas/v4` | `ZAI_API_KEY` |
| **智谱 AI** | `zhipuai` | `https://open.bigmodel.cn/api/paas/v4` | `ZHIPU_API_KEY`（无值时回退 `ZAI_API_KEY`） |
| **OpenRouter** | `openrouter` | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |
| **MiniMax** | `minimax` | `https://api.minimax.io` | `MINIMAX_API_KEY` |
| **Moonshot** | `moonshot` | `https://api.moonshot.ai` | `MOONSHOT_API_KEY`（无值时回退 `KIMI_API_KEY`） |
| **OpenCode Go** | `opencode-go` | `https://opencode.ai/zen/go` | `OPENCODE_API_KEY` |
| **Qwen** (通义千问) | `qwen` | `https://dashscope.aliyuncs.com/compatible-mode` | `DASHSCOPE_API_KEY` |
| **Groq** | `groq` | `https://api.groq.com/openai/v1` | `GROQ_API_KEY` |
| **Together** | `together` | `https://api.together.ai/v1` | `TOGETHER_API_KEY` |
| **Fireworks AI** | `fireworks` | `https://api.fireworks.ai/inference/v1` | `FIREWORKS_API_KEY` |
| **Mistral** | `mistral` | `https://api.mistral.ai/v1` | `MISTRAL_API_KEY` |
| **ai&** | `aiand` | `https://api.aiand.com/v1` | `AIAND_API_KEY` |
| **Alibaba Token Plan** | `alibaba-token-plan` | `https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1` | `ALIBABA_TOKEN_PLAN_API_KEY` |
| **Baseten** | `baseten` | `https://inference.baseten.co/v1` | `BASETEN_API_KEY` |
| **ClinePass** | `cline-pass` | `https://api.cline.bot/api/v1` | `CLINE_API_KEY` |
| **CoreWeave** | `coreweave` | `https://api.inference.wandb.ai/v1` | `COREWEAVE_API_KEY`（无值时回退 `WANDB_API_KEY`） |
| **GMI Cloud** | `gmi-cloud` | `https://api.gmi-serving.com/v1` | `GMI_API_KEY`（无值时回退 `GMICLOUD_API_KEY`） |
| **Hugging Face** | `huggingface` | `https://router.huggingface.co/v1` | `HUGGINGFACE_HUB_TOKEN`（无值时回退 `HF_TOKEN`） |
| **Kilo Gateway** | `kilo` | `https://api.kilo.ai/api/gateway` | `KILO_API_KEY` |
| **NovitaAI** | `novita` | `https://api.novita.ai/openai/v1` | `NOVITA_API_KEY` |
| **Nvidia** | `nvidia` | `https://integrate.api.nvidia.com/v1` | `NVIDIA_API_KEY` |
| **Venice AI** | `venice` | `https://api.venice.ai/api/v1` | `VENICE_API_KEY` |
| **Wafer** | `wafer-serverless` | `https://pass.wafer.ai/v1` | `WAFER_SERVERLESS_API_KEY`（无值时回退 `WAFER_API_KEY`） |
| **Xiaomi** | `xiaomi` | `https://api.xiaomimimo.com/v1` | `XIAOMI_API_KEY` |
| **Xiaomi Token Plan (Europe)** | `xiaomi-token-plan-ams` | `https://token-plan-ams.xiaomimimo.com/v1` | `XIAOMI_TOKEN_PLAN_AMS_API_KEY`（无值时回退 `XIAOMI_API_KEY`） |
| **Xiaomi Token Plan (China)** | `xiaomi-token-plan-cn` | `https://token-plan-cn.xiaomimimo.com/v1` | `XIAOMI_TOKEN_PLAN_CN_API_KEY`（无值时回退 `XIAOMI_API_KEY`） |
| **Xiaomi Token Plan (Singapore)** | `xiaomi-token-plan-sgp` | `https://token-plan-sgp.xiaomimimo.com/v1` | `XIAOMI_TOKEN_PLAN_SGP_API_KEY`（无值时回退 `XIAOMI_API_KEY`） |
| **ZenMux** | `zenmux` | `https://zenmux.ai/api/v1` | `ZENMUX_API_KEY` |
| **OpenCode Zen** | `opencode-zen` | `https://opencode.ai/zen/v1` | `OPENCODE_API_KEY` |

同样走 OpenAI chat-completions 路径的还有 `openai`（`https://api.openai.com/v1`）、
`github-copilot`（`https://api.individual.githubcopilot.com`，Copilot 凭据不会发往 OpenAI）、
`endpoint`（未配置时为本地网关 `http://127.0.0.1:4000/v1`）、`deepseek`（`https://api.deepseek.com`）
与 `xai`（`https://api.x.ai`）。

### 一等 Anthropic-wire Provider

以下 provider 的 wire 是 Anthropic Messages（`provider/anthropic_native.py`）；vendor 默认 host 与凭据环境变量来自
`provider/provider_config.py` 的 `_DEFAULT_ANTHROPIC_WIRE_BASE_URLS`，配置支持 `api_key`、`base_url`、
`version`、`beta_headers`、`cache_retention`、`timeout_seconds` 和 `transient_retry`。

| Provider | 配置 Key | 默认 Base URL | 默认环境变量 |
| :--- | :--- | :--- | :--- |
| **Anthropic** | `anthropic` | `https://api.anthropic.com` | `ANTHROPIC_API_KEY` |
| **Kimi For Coding** | `kimi-code` | `https://api.kimi.com/coding` | `KIMI_CODING_API_KEY` |
| **MiniMax CN** | `minimax-cn` | `https://api.minimaxi.com/anthropic` | `MINIMAX_CN_API_KEY` |

`kimi-code` / `minimax-cn` 与 OpenAI-wire 的 `moonshot` / `minimax` 是不同的 host，因此凭据环境变量刻意不共用：
`MOONSHOT_API_KEY`（回退 `KIMI_API_KEY`）只发往 `api.moonshot.ai`，`MINIMAX_API_KEY` 只发往 `api.minimax.io`，一个变量不会把一个 host 的凭据发到另一个 host。
（Kimi Code 的 OAuth 订阅登录 VoidCode 不支持：`kimi-code` 只接受 API key。）

### Endpoint 解析规则

- 每个 provider 只解析到自己的 endpoint：先取 `providers.<name>.base_url`，没有则取该 provider 自身的默认 host。
  **不会**回落到其它 vendor 的 host——某个 vendor 的配置缺失时，请求也不会被发到 `api.openai.com`。
  （例外：OpenCode 网关的 Anthropic 路由固定使用网关 host root，见下文 OpenCode 说明。）
- 模型发现没有独立配置项：provider 的模型列表一律从它自己解析出的 `base_url` 推导（`<base_url>` + 该 wire 的路径），
  所以 `base_url` 指向哪，探测就发往哪。配置 block 完全缺失（`.voidcode.json` 里没有该 block，也没有对应凭据
  环境变量）时，同样按该 provider 的默认 host 解析，并从该 host 推导模型列表。
- 只有既没有配置 `base_url`、自身也没有默认 host 的 provider 才会失败：runtime 产生
  `not_configured`（不可重试、允许 fallback），并提示设置 `providers.<name>.base_url` 与凭据。
- `google` 的 `service_account` auth 不产生 discovery 可用的凭据，因此该模式下 discovery 记为禁用，
  不会发起未鉴权探测。
- `providers.google` 的 auth 同时决定 endpoint surface：`api_key` / `oauth` 沿用原语义（只有 auth 未提供
  凭据时才回退到 `GOOGLE_API_KEY`）；`service_account` 会把 `auth.service_account_json_path` 指向的文件
  加载为 cloud-platform ADC 凭据并选择 Vertex AI surface（不会静默替换成 `GOOGLE_API_KEY`；文件缺失或
  无法解析时产生 `missing_auth`，不可重试、允许 fallback）。未配置 auth block 且没有 API key 的纯 ADC
  用户，只要提供 `project` 和/或 `region` 也会选择 Vertex；`api_key` + `project`（无 `region`）保持原有
  Gemini API endpoint。
- `google` 的默认 host 是 `https://generativelanguage.googleapis.com`（`api_key` / `oauth` 的 Gemini API surface；`service_account` 选择 Vertex AI，host 由 SDK 从 ADC project/region 解析），wire 为 `google-generative-ai`。
- `providers.google.base_url` 同样生效：它是完整的 endpoint root（含版本段），SDK 不会再追加自己的
  `v1beta` / `v1beta1`；设置了自定义 base URL 时 SDK 也会跳过自身的 ADC project 解析。
- 未配置时 `endpoint` provider 仍按文档化的本地网关默认值解析（`endpoint_default`），这是唯一的
  「没有配置也能调用」的 provider。
- `voidcode provider inspect <provider>` 的输出包含 `endpoint` 字段，说明解析结果与来源：
  - `endpoint.base_url`：wire 实际使用的基础 URL（与 transport 的归一化规则一致；`null` 表示配置里没有可解析的 base URL）
  - `endpoint.source`：`config`（来自 `providers.<name>.base_url`）、`provider_default`（provider 自身默认 host）、
    `endpoint_default`（`endpoint` provider 未配置时的本地网关默认值）

#### 模型发现策略

| **Z.AI** | `/v4/models` endpoint | OpenAI-compatible，自动发现 |
| **智谱 AI** | `/v4/models` endpoint | OpenAI-compatible，自动发现 |
| **OpenRouter** | `/api/v1/models` endpoint | 自动发现真实模型 ID；模型引用保留 provider/model 中的全部 slash，也包含 API 返回的 `:free` 模型 |
| **MiniMax** | `/v1/models` endpoint | 从 `https://api.minimax.io` 推导（`/v1/models`），OpenAI-compatible 自动发现。catalog 中的 `minimax` 条目由 models.dev 的 `minimax` 来源键生成 |
| **Moonshot** | `/v1/models` endpoint | OpenAI-compatible，自动发现 |
| **OpenCode Zen** | `/zen/v1/models` endpoint | OpenAI-compatible，自动发现；模型引用为 `opencode-zen/<model-id>` |
| **OpenCode Go** | `/zen/go/v1/models` endpoint | 从 `https://opencode.ai/zen/go` 推导，OpenAI-compatible 自动发现（该网关的列表无需鉴权） |
| **Qwen** | `/v1/models` endpoint | DashScope compatible-mode，自动发现 |
| **Groq** | `/v1/models` endpoint | OpenAI-compatible，自动发现 |
| **Together** | `/v1/models` endpoint | OpenAI-compatible，自动发现 |
| **Fireworks AI** | `/inference/v1/models` endpoint | 从 `https://api.fireworks.ai/inference/v1` 推导，OpenAI-compatible 自动发现 |
| **Mistral** | `/v1/models` endpoint | OpenAI-compatible，自动发现 |
| **ai&** | `/v1/models` endpoint | OpenAI-compatible，自动发现 |
| **Alibaba Token Plan** | `/compatible-mode/v1/models` endpoint | OpenAI-compatible，自动发现 |
| **Baseten** | `/v1/models` endpoint | OpenAI-compatible，自动发现 |
| **ClinePass** | `/api/v1/models` endpoint | OpenAI-compatible，自动发现 |
| **CoreWeave** | `/v1/models` endpoint | OpenAI-compatible，自动发现（从本机探测时该 host 返回地区限制 403） |
| **GMI Cloud** | `/v1/models` endpoint | OpenAI-compatible，自动发现 |
| **Hugging Face** | `/v1/models` endpoint | OpenAI-compatible，自动发现 |
| **Kilo Gateway** | `/api/gateway/v1/models` endpoint | OpenAI-compatible，自动发现 |
| **NovitaAI** | `/openai/v1/models` endpoint | OpenAI-compatible，自动发现 |
| **Nvidia** | `/v1/models` endpoint | OpenAI-compatible，自动发现 |
| **Venice AI** | `/api/v1/models` endpoint | OpenAI-compatible，自动发现 |
| **Wafer** | `/v1/models` endpoint | OpenAI-compatible，自动发现 |
| **Xiaomi** | `/v1/models` endpoint | OpenAI-compatible，自动发现 |
| **Xiaomi Token Plan (Europe / China / Singapore)** | `/v1/models` endpoint | OpenAI-compatible，自动发现 |
| **ZenMux** | `/api/v1/models` endpoint | OpenAI-compatible，自动发现；catalog 行按上游 `provider.npm` 逐模型选择 wire（含 anthropic-messages） |
| **Kimi For Coding** | `/coding/v1/models` endpoint | Anthropic-wire；从 `https://api.kimi.com/coding` 推导列表，凭据以 `Authorization: Bearer` 发送 |
| **MiniMax CN** | `/anthropic/v1/models` endpoint | Anthropic-wire；从 `https://api.minimaxi.com/anthropic` 推导列表，凭据以 `X-Api-Key` 发送；catalog 中的 `minimax-cn` 条目由 models.dev 的 `minimax-cn` 来源键生成 |

没有模型列表的 provider 由代码计算，而不是配置开关：`github-copilot` 没有公开列表，`google` 的 `service_account` auth
（或完全没有凭据）不产生列表请求可发送的凭据；这两者报告 `disabled`。

W6 新增的 17 个 provider（`aiand` / `alibaba-token-plan` / `baseten` / `cline-pass` / `coreweave` /
`gmi-cloud` / `huggingface` / `kilo` / `novita` / `nvidia` / `venice` / `wafer-serverless` / `xiaomi` /
`xiaomi-token-plan-{ams,cn,sgp}` / `zenmux`）的启用依据是**无凭据探测自己的列表路由**：对该 provider 的
`<base_url>` 推导出的 `/models` 发一次不带任何凭据的 GET，`200` + JSON listing 或 `401`/`403` + API 自己的
错误体即视为 host 与路径存在并说该 wire 的协议（每个 provider 的实测状态与 URL 记在
`provider/provider_table.json` 的 `notes` 里）；catalog 行由该 vendor 的 models.dev key 生成。凭据验证过的
refresh 冒烟本轮没有做（环境里没有这些 vendor 的 key），因此它们只按"host 可用 + catalog 有行"启用。
候选清单、逐项探测结果与被排除项的理由见 `.omo/plans/w6-additions-draft.json`。

OpenRouter 不硬编码易变的免费模型 slug；请使用 `/api/v1/models` 刷新得到的模型 ID，例如
`openrouter/anthropic/claude-3.7-sonnet` 或 API 当前返回的 `openrouter/<provider>/<model>:free`。

模型列表一律来自 provider 自己的列表路由（`<base_url>` + wire 路径），不再需要 `discovery_base_url`；
没有公开列表的 provider 报告 `disabled`，可用 `model_map` 显式给出模型。

配置示例（最小）：

```json
{
  "providers": {
    "openrouter": {},
    "zai": {}
  },
  "model": "openrouter/anthropic/claude-3.7-sonnet"
}
```

OpenCode Go 与 OpenCode Zen 都是「一个 host、多种 wire」的网关：用户可见的模型引用始终是
`opencode-go/<model-id>` / `opencode-zen/<model-id>`，实际 wire 由模型本身决定，而不是由 provider 名决定
（与 OMP 的 per-model catalog 对齐）。wire 是模型的数据，不是 adapter 里的常量：解析链为
catalog 行的 `api`（生成期已按 OMP 优先级定好，runtime 原样消费）→ `provider/api_routes.json`
的 pin → provider table 里该 provider 自己的 wire。adapter 只把解析出的 wire 映射到本进程
实现的调用路径；解析不出实现路径的 wire 一律 typed 失败，不会被猜测或降级。

`api_routes.json` 镜像 OMP 的 `api-routes`：按 provider 分组、声明序 first-match-wins，matcher 为
`exact` / `prefix` / `substring` / `token` / `glob`（可带 `strip_prefix`，命中后先剥掉前缀再发给上游）；
没有命中 row 的 provider 回退到它自己的 wire。模型生成器与 runtime dispatch 读同一份数据，
因此出厂的 catalog 与运行时的判定不会分叉。

- **OpenCode Go**（默认 base URL `https://opencode.ai/zen/go`，wire 归一化为 `https://opencode.ai/zen/go/v1`）：
  - 默认（其余全部模型）：OpenAI-compatible chat-completions，`https://opencode.ai/zen/go/v1/chat/completions`
  - `minimax-m3`：chat-completions（与 OMP `behavior.kdl:209` 对齐；此前被误判为 Anthropic）
  - 解析 wire 为 `anthropic-messages` 的模型（例如 `qwen3.8-flash`）：Anthropic Messages，
    `https://opencode.ai/zen/go/v1/messages`（route 持有 host root `https://opencode.ai/zen/go`，
    `/v1/messages` 由 Anthropic SDK 追加），凭据以 `x-api-key` 发送
  - 2026-09 观察：上游网关对 `minimax-m2.7` 的每一种请求形状都返回 HTTP 500，属网关侧故障；
    该问题关闭前 `minimax-m2.7` 保持在默认 chat-completions 路由（上游恢复后此行可直接删除）。
  - 解析 wire 为 `openai-responses` 的模型（例如 `gpt-5.6-luna`）：VoidCode 未实现该 wire，会以
    `unsupported_feature`（不可重试、允许 fallback）失败，而不是静默降级到 chat-completions
- **OpenCode Zen**（默认 base URL `https://opencode.ai/zen/v1`）：
  - 默认（其余全部模型）：chat-completions，`https://opencode.ai/zen/v1/chat/completions`
  - 解析 wire 为 `anthropic-messages` 的模型（`claude-*` 系列与部分 `qwen3.x-*`）：Anthropic Messages，
    `https://opencode.ai/zen/v1/messages`（route 持有 host root `https://opencode.ai/zen`）
  - 解析 wire 为 `google-generative-ai` 的 `gemini-*`：Google generative-ai wire，
    `https://opencode.ai/zen/v1/models/<model>:generateContent`，凭据以 `x-goog-api-key` 发送
  - 解析 wire 为 `openai-responses` 的 `gpt-*` / `grok-*` / `muse-spark-*`：同样以
    `unsupported_feature` 失败
- catalog 没有对应行、也没有 `api_routes.json` pin 的模型（例如 OMP 未收录的 `*-free`）回退到该 provider
  的默认 wire；`model_map` 别名先解析再选 wire，别名指向被路由的模型时走该模型的 wire（并把解析后的
  模型名发给上游）；`providers.<name>.base_url` 对默认路由与 Zen 的 Google 路由仍然优先，
  Anthropic 路由固定使用网关 host root。

catalog 的每个 model 行由生成器写入固定字段（当前 18 个路径），runtime 直接消费其中的 wire 判定；本波新增 4 个：

- `api`：该模型的 wire。生成期按 OMP 优先级解析——（1）`api_routes.json` 的 pin、（2）上游 `provider.npm`
  （`@ai-sdk/openai`→`openai-responses`、`@ai-sdk/anthropic`→`anthropic-messages`、
  `@ai-sdk/google`→`google-generative-ai`）、（3）provider table 的 wire——每个条目都会写出该字段。
  该字段是否真的决定 dispatch，由 `provider_table.json` 的 `wire_source` 决定：`model`（默认，两条
  OpenCode 网关及所有其它 provider）表示 runtime 按行内 `api` 选 wire；`provider`（目前只有
  `github-copilot`）表示该行的 `api` 仍是上游事实（`claude-*` 为 `anthropic-messages`，
  `gpt-5*` / `grok-4.5` / `4.6` / `oswe*` / `mai-*` 为 `openai-responses`），但 dispatch 固定走
  provider table 自己的 wire，因为 OMP 的 copilot Anthropic 路由需要凭据信封、`Authorization: Bearer`、
  Copilot 身份 header 与协商出的 integration id（pi `anthropic.ts:2017-2041,3423`），没有真实
  Copilot token 无法验证。
- `display_name`（上游 `name`）、`modalities_output`（上游 `modalities.output[]`）、
  `max_input_tokens`（上游 `limit.input`）。

#### Thinking 规则与输出上限（`thinking_rules.json`）

请求构造中的 thinking / reasoning 语义同样来自 checked-in 数据，不写在 adapter 里：
`provider/thinking_rules.json` 由 `provider/thinking_rules.py` 的
`thinking_rule_for(provider_id, model_id)` 读取，且它是唯一读取者，因此没有任何 adapter 硬编码
mode、budget、disable 拼写、effort 映射或 max-tokens 字段名。

- 行选择：每个 provider 的第一条不带 `match` 的行是该 provider 的默认值，随后按声明序取第一条命中的
  model-scoped 行覆盖它。matcher 为 `exact` / `prefix` / `substring` / `token` / `glob`，与
  `api_routes.json` 共用 `model_match.py` 的同一套语义（`exact` / `prefix` / `substring` 比较原始
  id，`token` / `glob` 比较小写化后的 id）。
- 行字段：`mode`（`effort` / `binary` / `budget` / `google-level`，**必须是该 model 实际 dispatch 的
  wire 上 adapter 真正会发出的旋钮**）、`budgets`、`disable_mode`、`effort_map`、`max_tokens_field`、
  `requires_effort`、`reasoning_content_field`、
  `requires_reasoning_content_for_tool_calls`、`sends_output_cap_by_default` 与 `source`。
  `mode` 与 wire 的一致性由 `tests/unit/provider/test_thinking_rule_invariants.py` 对全量 catalog
  model 断言：`effort` / `binary` 只能落在 OpenAI-compatible wire，`budget` 只能落在 Anthropic wire
  （且必须带 budget 表）或 Google wire，`google-level` 只能落在 Google wire。
- `budgets` 要么写顶层 `budget_tables` 里的具名表，要么行内直接写映射：两份 OMP 表的六个数字
  （`anthropic-thinking` = OMP `ANTHROPIC_THINKING`、`google-thinking` = OMP `GOOGLE_THINKING`）
  只在文件顶部出现一次，读表的行只写表名。
- `disable_mode` 只保留 OMP 中真正会被我们这 21 个 provider 选中的拼写：`lowest-effort`、
  `none-effort`、`openrouter-enabled-false`、`zai-thinking-disabled`、`qwen-enable-thinking-false`；
  OMP 另外 5 种（`omit` / `cline-enabled-false` / `venice-disable-thinking` / `qwen-template-false` /
  `chat-template-thinking-false`）在我们的 id 集合里没有生产者，已删除而不是留成死分支。
  `max_tokens_field` 是 `max_tokens` / `max_completion_tokens` 之一，按 OMP 的
  `useMaxTokens` 谓词（`resolve.ts:404-411,497`）逐 provider 写定：`max_tokens` 用于 `deepseek` /
  `fireworks` / `mistral` / `moonshot` / `zai` / `zhipuai` / `opencode-go`，其余（含 `openai` /
  `qwen` / `xai` / `groq` / `together` / `minimax` / `opencode-zen` / `github-copilot` /
  `openrouter`）为 `max_completion_tokens`；`anthropic` / `google` / `minimax-cn` / `kimi-code`
  不走该字段（各自的 wire 有固定名或没有该字段），`endpoint` 无 OMP 对应项，保持默认。
- `reasoning_content_field` + `requires_reasoning_content_for_tool_calls` 取代了原先按 provider 名 /
  `deepseek-` 前缀判断「tool-call 回放必须带 reasoning content」的启发式：只有 `deepseek`、
  `opencode-zen`、`opencode-go` 要求（`zhipuai` / `kimi-code` 只声明字段名），其余为 none。
- ladder 就是 OMP 的 6 个成员（`minimal` / `low` / `medium` / `high` / `xhigh` / `max`）；
  `off` **不是** ladder 成员，而是「关闭推理」的请求状态（即 OMP 的 `undefined`）：它仍是 CLI /
  config / frontend 接受的用户输入，请求形态由该行的 `disable_mode` 解析（`requires_effort` 的
  model 退化为最低受支持档位），不会被当作 ladder 档位 clamp 或映射。
- 输出上限：**默认不发**（OMP 只在调用方显式给 `maxTokens` 时才发；voidcode 目前没有这个配置旋钮，
  因此请求级默认值不存在）。唯一的例外是 kimi 家族（`moonshot`，`alwaysSendMaxTokens = facts.is("kimi")`），
  它按该行 `max_tokens_field` 发送 `max_output_tokens`（未知则 64000）。Anthropic wire 的
  `max_tokens` 是 API 必填，取该 model 自己的 `max_output_tokens`（未知则 64000），且
  `ensureMaxTokensForThinking` 的 ceiling 就是该 model 自己的上限——thinking 只会把偏小的 cap 抬到
  `budget + 4000`，**绝不会**把 cap 压小（`64000` 只是未知 model 的兜底，不是已知更大 model 的上限）。
  Google wire 仍用 `max_output_tokens`。
- reasoning 能力只来自 catalog 的 `supports_reasoning` / `supports_reasoning_effort`：provider 级
  allowlist / denylist 与 `glm-5` / `glm-z1` 前缀启发式都已删除。catalog 没有描述的 model 属于
  「未知」，按 best-effort 透传并记为 unverified，而不是被拒绝。

VoidCode 未实现的 wire（目前只有 OpenAI Responses API）一律以 `unsupported_feature`（不可重试、
允许 fallback）失败，不会被降级到 chat-completions 等其它 wire。Zen 的三条 wire 目前只对 mocked HTTP
transport 验证过：当前 Zen 账号对任何 Zen 请求都返回 HTTP 401 `CreditsError: Insufficient request balance`
（per-conversation header 已被接受，属账号余额问题而非 `MissingSessionID`）；用有余额的 key 复验时，
按每条 wire 各跑一个 Zen 模型（`glm-5.1` chat、`claude-opus-5` Anthropic、`gemini-3-flash` Google），
期望 HTTP 200 且带 `x-opencode-session`。

两个网关都按会话路由：**每条 wire 的每个请求**都必须带 `x-opencode-session: <conversation id>`
（缺失时网关返回 HTTP 400 `MissingSessionID`）与 `x-opencode-client`。VoidCode 在请求时从 runtime
session id 解析 `x-opencode-session`（wire client 跨 turn 复用，因此该值不能在构造时固定）；请求没有
session id 时该 header 不发送，`x-opencode-client` 仍然发送。

runtime 仍统一注入工具 schema、审批与会话状态。

OpenCode Zen 与 OpenCode Go 是不同 provider：Zen 使用 `opencode-zen/<model-id>`，默认从
`https://opencode.ai/zen/v1/models` 自动发现模型；Go 使用 `opencode-go/<model-id>`，同样从
`https://opencode.ai/zen/go/v1/models` 自动发现（该列表无需鉴权）。

配置示例（完整）：

```json
{
  "providers": {
    "openrouter": {
      "api_key_env_var": "OPENROUTER_API_KEY",
      "model_map": { "sonnet": "anthropic/claude-3.7-sonnet" }
    },
    "zai": {
      "api_key_env_var": "ZAI_API_KEY",
      "base_url": "https://api.z.ai/api/paas/v4"
    },
    "minimax": {
      "api_key_env_var": "MINIMAX_API_KEY",
      "timeout_seconds": 60.0
    }
  },
  "model": "openrouter/sonnet"
}
```
这些 provider 不允许在 `providers.custom` 下定义（会与内置名称冲突）。

### 自定义 Provider（生产可用路径）

- 使用 `providers.custom.<provider_name>` 定义任意自定义 provider（名称必须不包含 `/`）。
- `providers.custom.<provider_name>` 不能与内置 provider 名称冲突（包括 `openai` / `anthropic` / `google` / `github-copilot` / `endpoint` / `opencode-zen` / `openrouter` / `opencode-go` / `deepseek` / `zai` / `zhipuai` / `xai` / `minimax` / `minimax-cn` / `moonshot` / `kimi-code` / `qwen` / `groq` / `together` / `fireworks` / `mistral` / `aiand` / `alibaba-token-plan` / `baseten` / `cline-pass` / `coreweave` / `gmi-cloud` / `huggingface` / `kilo` / `novita` / `nvidia` / `venice` / `wafer-serverless` / `xiaomi` / `xiaomi-token-plan-ams` / `xiaomi-token-plan-cn` / `xiaomi-token-plan-sgp` / `zenmux`）。
- 每个自定义 provider 都复用 OpenAI-compatible 后端调用路径，支持：
  - `api_key` / `api_key_env_var`
  - `base_url`
  - `ssl_verify`（可选；仅在确需连接自签名或私有 CA HTTPS 端点时显式设为 `false`）
  - `auth_scheme` + `auth_header`
  - `model_map`
- 在模型配置中使用 `model: "<provider_name>/<model_alias_or_raw_model>"` 即可路由到对应自定义 provider。
- **强烈建议**为自定义 provider 配置 `model_map`：
  - 配置 `model_map` 时，可直接使用稳定别名（例如 `llama-local/coder`），并映射到真实后端模型 ID。
  - 未配置 `model_map` 时，模型名会原样以 provider/model 形式发送给后端，通常建议显式映射到真实 downstream 模型 ID。
- Provider Auth 接口现在同样支持 `providers.custom.<provider_name>`：
  - 自定义 provider 会复用 endpoint auth 语义（`api_key` / `none`）
  - 对应 `provider auth methods` / `authorize` 时可直接传入 custom provider 名称

示例：

```json
{
  "providers": {
    "custom": {
      "llama-local": {
        "base_url": "http://localhost:11434/v1",
        "auth_scheme": "none",
        "model_map": {
          "coder": "ollama/qwen2.5-coder:latest"
        }
      }
    }
  },
  "model": "llama-local/coder"
}
```

### OpenAI-compatible 后端与证书校验

- 对具名 OpenAI-compatible 后端，使用 `providers.custom.<name>`；例如本地代理或内部模型网关。
- `ssl_verify: false` 是 HTTPS 证书校验问题的显式逃生口；优先使用 `http://` 本地代理 `base_url` 或修复 CA 信任链，只有在受控环境中才禁用校验。
- 对内置具体 provider（例如 `openrouter`、`opencode-go`），也可以在对应 provider block 上设置 `ssl_verify: false`。
- 当前该字段作用于实际模型调用路径；模型列表刷新仍走独立 discovery HTTP 路径，不应依赖它绕过 discovery 证书校验。

### 可用模型列表动态刷新（端点/API 驱动）

- Runtime 现在支持按 provider 动态刷新可用模型列表：
  - `voidcode provider models <provider>`：读取当前缓存
  - `voidcode provider models <provider> --refresh`：主动请求 provider `/v1/models` 刷新
  - `voidcode provider inspect <provider>`：输出 provider 状态与解析后的 `endpoint`（`base_url` / `source`）
- provider-specific 端点策略（与 opencode 的 provider 分层思路对齐）：
  - `openai` / 自定义 OpenAI-compatible：`<base>/v1/models`
  - Anthropic-wire（`anthropic` / `kimi-code` / `minimax-cn`）：`<base>/v1/models`，并带 `anthropic-version` 与该 vendor 的凭据 header（默认 `x-api-key`；`kimi-code` 用 `Authorization: Bearer`，`minimax-cn` 用 `X-Api-Key`）
  - `google`：`<base>/v1beta/models?key=...`（读取 `models[].name`）
- 刷新结果会融合三类来源并去重：
  1. `model_map` 的别名键（便于用户直接选别名）
  2. provider 端点返回的真实模型 ID
  3. `model_map` 的映射目标值（便于调试真实路由）
- 对于 OpenAI 默认支持 endpoint 探测；自定义 provider 若配置了 `base_url` 也会走同样的 OpenAI-compatible `/v1/models` 探测路径。

## 定价与上下文预算（`pricing_rules.json`）

- **定价**：`provider/pricing_rules.json` 由唯一读取者 `provider/pricing_rules.py` 的
  `usage_cost_usd(*, provider_id, model_id, usage, metadata)` 消费。一个 turn 先用 catalog 的扁平
  `cost_per_*` 费率计价；仅当 prompt input（`uncached + cache_read + cache_write`）**严格大于**该行的
  `threshold` 时才换成该行的长上下文费率——除非该行为 `inclusive: true`，此时等于阈值也算（OMP 语义）。
  `openai` 的 5 条行阈值 272000 且 strict，各自带绝对 `rates`；`xai` 的 `grok-4.3` / `4.5` / `4.6` /
  `grok-build-0.1` / `grok-4.20*` 阈值 200000 且 inclusive，行内写 `multiplier: 2.0`，即以该模型自己的
  基础费率 ×2 作为 tier 费率。行选择为声明序 first-match-wins，matcher 与 `api_routes.json` /
  `thinking_rules.json` 共用 `model_match.py`。
- `pricing_rules.json` 只收录 OMP 亲证的 `long-context-cost` 行；mirror 的 `context_over_200k` /
  `tiers[]` **永不读取**（OMP 的 models.dev row 类型没有 tier 字段），因此没有对应行的 model 不会被
  顺手套上 tier。
- 成本落在既有的 `provider_usage` 记录上：graph 在该 turn 用它自己的 usage 计算一次，写入
  `latest.cost_usd` 并累加到 `cumulative.cost_usd`；token 桶保持整数，金额是 float，持久化的 usage
  不会被重新计价。
- **compaction 预算**：随包 catalog 的 `max_input_tokens`（无上游 `limit.input` 时由
  `context_window - max_output_tokens` 派生）优先、否则 `context_window`，经
  `stream_prep._context_budget_for` 传给 `prepare_provider_context(context_window=...)` 作为 compaction
  窗口；调用方显式传入的 `context_window` / `threshold_*` / `reserve_tokens` 仍然优先，catalog 未描述的
  model 则让 compaction 保持 unsized（与之前一致）。runtime 拥有预算，catalog 只提供数字。
- **图片**：今天没有任何图片进入 provider 请求——read 工具把附件作为 tool-result **data** 返回
  （`tools/read.py:301-316`），`ContextSegment.content` 只有 `str | None`，adapter 只发文本，
  因此没有可丢弃的 image part，也无需 vision gate。

- 定价读取的是**随包 catalog**（`static_catalog_metadata`），不是 discovery 合并后的 metadata：与
  compaction 预算同一个理由——价格不该随一次网关列表刷新而变动。因此 `provider_graph._priced_usage`
  明确传入 shipped 行；一个 discovery 刷新后缺少定价的条目不会把成本静默变成 0。
- 「未定价」与「免费」是两种结果：`usage_cost_usd` 在 shipped catalog 没有该 model 时返回 `None`
  （而不是 `0.0`），只有 catalog 真的把四个费率都写成 0 的 model 才是免费（返回 `0.0`）。surface
  输出 `None` 时显示为未知/省略，而不是 `$0.00`；`effectiveness.py` 的 `provider_usage.cost_usd`
  与前端 `providerUsage.ts::providerCostUsd` 都遵循这一点。
- 只报告 prompt 总量的 provider（没有 uncached/cached 拆分）也会被正确计价：`uncached` 由
  `input_tokens - cache_read - cache_write` 推导（下限 0）；Google 的 `_usage` 同样从
  `prompt_token_count - cached_content_token_count` 填出 `uncached_input_tokens`。

## 流式传输

VoidCode 的 Provider 抽象层输出标准化的流式事件包。执行路径直接使用各家的官方 SDK：

- `openai`：`openai` package（OpenAI 及所有 OpenAI-compatible provider）
- `anthropic`：`anthropic` package（Messages API）
- `google`：`google-genai` package

SDK 负责 HTTP、SSE 解码与异常类型；adapter 只把 SDK 事件投影成 `ProviderStreamEvent`，
不重新解释 provider 语义。`max_retries=0`：retry/fallback 由 runtime 拥有，SDK 不做隐式重试。

### 事件包 (Event Envelope)

事件通过 `ProviderStreamEvent` 结构表示，包含以下字段：

- `kind`: 事件类型 (`delta`, `content`, `error`, `done`).
- `channel`: 数据通道 (`text`, `tool`, `reasoning`, `error`).
- `text`: 流片段文本（仅 `delta` / `content`）。
- `error`: 已脱敏的错误描述（仅 `error`）。
- `error_kind`: 错误分类（包括 `missing_auth`, `invalid_model`, `not_configured`, `rate_limit`, `context_limit`, `transient_failure`, `unsupported_feature`, `stream_tool_feedback_shape`, `cancelled`）。
- `done_reason`: 上游终态原因。成功终态为 `stop`、`tool_calls`、`function_call`、`length`、`content_filter`；`unknown` 表示上游未给出可识别的原因，仍被视为已完成（等价于 `stop`）的终态，不会作为面向用户的失败。`error` / `cancelled` 仍是失败终态。
- `finish_reason_reported`: 仅当上游确实携带了可用的 finish reason 值（键存在且非 null/空）时为 `true`。`done_reason` 为 `unknown` 且该值为 `false` 时属于"无原因即结束"，运行时以 `warning` 记录（可能是被截断的流），并在持久化的 `graph.response_ready` 中写入 `finish_reason` / `finish_reason_reported` 供 `sessions debug` 之类检查；可识别但未映射的 token 仍为 `true`，只记录 `debug`。

### 错误边界

- `ProviderExecutionError.message` 是 provider 原始错误的已脱敏可读文本，不应包含 SDK wrapper 前缀。
- `details` 保存有界、递归脱敏后的诊断信息；认证 token、cookie、header secret 不得进入持久化事件或 trace。
- `retryable`、`fallback_allowed` 和 `retry_after` 是 runtime 恢复策略的输入；显式 `False` 会覆盖按错误种类推导出的默认策略。
- **4xx 终态由状态码决定，不由消息决定**：非 408/429 的 4xx 一律 `retryable=False`，即使消息没匹配到任何 marker（fallback 仍允许，链上的另一个 provider 可能能服务）。402 与 429 都是 usage/limit（OMP `isUsageLimitStatus`），归为 `rate_limit`，永远不会被当作 `transient_failure`。
- **usage/limit 不进 provider 内联重试 lane**：解析层对一个 `rate_limit` 答案不下 `retryable` 判决（`None`），`PROVIDER_TRANSIENT_RETRYABLE_KINDS` 也不含它，`decide_provider_error_policy` 里有独立分支——因此一个 429/402 **不可能**变成 `ProviderTransientRetryDecision`。它只有两条出路：runtime 为该次运行武装了 rate-limit lane（后台任务）时，产生 `ProviderTerminalDecision(kind="background_rate_limit_retry")`，延后重试并按 `retry_after` 等待；否则交给 fallback 链上的下一个 provider。402 因为同时是"非 408/429 的 4xx"而带显式 `retryable=False`，两条路都走 fallback（不延后）。OMP 把这两类交给凭据轮换，voidcode 没有轮换，所以落点是 runtime 自己已有的这条 lane，不新增机制。
- **`retry_after` 多来源取最大**：`retry-after-ms` / `retry-after` / `x-ratelimit-reset-ms` / `x-ratelimit-reset` 四个 header 与消息文本里的时序提示（`Please retry in 12s`、`reset after 1h2m3s`、`Resets in 2hr 15min`、`"retryDelay": "2500ms"`、`retry-after-ms=7200000` 等）在同一最大值里竞争；`x-ratelimit-reset*` 的数字大到像绝对 epoch 时按目标时间解释（OMP `parseResetHeader`）。解析出的 `0` 表示立即重试，不会被当作"没有提示"丢弃；例外是 **header** 形式的 `retry-after-ms`，它是正数毫秒增量，`<= 0` 直接忽略（OMP `parseRetryAfterMsHeader`），而 body 里的 `retry-after-ms=0` 仍是立即重试。带时区的 `reset at` 时间戳直接换算，无时区的按 UTC 读取且只在没有任何相对提示时生效（voidcode 不携带 per-provider 时区数据）。
- `retry_after` 最终被限制在 3600 秒以内，并覆盖同一次 retry 的指数退避延迟（仍受 runtime 最大延迟限制）；OMP 在超过自己的 `maxDelayMs` 时改为快速失败，voidcode 保留这个上限。
- **带内错误（HTTP 200 + error body）不识别：有意分歧**。OMP 会从 200 响应体里推断错误（`error/body-error.ts`）并按它可以推断出的 kind 重试；voidcode 只对非 2xx 响应与 stream 上的 `error` 事件分类，一个状态码为 200 的带内 `rate_limit` code 因此不会得到 OMP 那套推断结果。这条在 `.omo/plans/error-retry-rules-draft-notes.md` §2 的 `should_not_copy` 里，属于**有意不复制**（该特性的触发形态需要独立设计），不是缺口。

### 取消、超时与不完整 tool call

- **显式取消**: 通过 `ProviderAbortSignal` 触发。一旦取消，流将立即产生 `error_kind: cancelled` 事件并终止。
- **分片超时**: transport 在相邻分片之间执行配置的 timeout 检查（`providers.<id>.timeout_seconds`，默认 300s），并报告 `transient_failure` / `provider stream chunk timeout exceeded`。
- **首事件看门狗**: 首个分片单独计时（`DEFAULT_STREAM_FIRST_EVENT_TIMEOUT_SECONDS = 300`），报告 `transient_failure` / `provider stream first-event timeout exceeded`。它不会低于分片超时：实际等待是 `max(first_event, timeout_seconds)`，因为首个 token 合法地可能比分片间隔更久（OMP `getOpenAIStreamFirstEventTimeoutMs` 的 `max(base, idleTimeoutMs)`）——两者默认都是 300s，配置更长的 `timeout_seconds` 会同时放宽首 token 的等待。
- **abort 归因**: 等待超时时若 caller 已经取消，报 `cancelled`（不可重试、不允许 fallback）而不是 timeout —— caller 意图优先。
- **不完整 tool call**: stream 结束时仍未形成完整 JSON object 的 tool call 会报告 `stream_tool_feedback_shape`，不会静默丢弃。

## 代码结构

- `protocol.py`: 定义 Provider 契约与流式事件模型。
- `config.py`: 处理各供应商的配置解析与校验。
- `errors.py`: 负责从原始供应商响应中提取并分类错误。
- `registry.py`: 维护已实现的 Provider 实例。
- `resolution.py`: 负责将原始请求解析为具体的 Provider 配置。
- `snapshot.py`: 提供安全的快照导出逻辑，确保不泄露机密。

## 模块内约束

- `ResolvedProviderModel` 会显式记录 provider resolution 来源：
  - `builtin`：内置 provider adapter
  - `custom`：`providers.custom.<name>` 显式配置的 OpenAI-compatible endpoint
  - 未声明的 provider id 不再产生 resolution：`provider/model` 的 provider 段必须是内置 id
    或 `providers.custom.<name>`，否则抛 `UnknownProviderIdError`（见上「Provider 命名」）。
    `OpenAIEndpointProvider` 只服务两类已声明的目标：内置 `endpoint` id 与
    `providers.custom.<name>`。
- fallback chain 不允许重复 target；即使绕过原始 config parser，`resolution.py` / `snapshot.py`
  仍会在 provider 模块内拒绝重复链路。
- model discovery 会显式区分：
  - `configured_base_url`：从 provider 自己解析出的 `base_url` 推导探测端点
  - `disabled`：provider 本身没有模型列表（`github-copilot`；`google` 在无凭据时）
  - `unavailable`：provider 解析不出 `base_url`，没有可探测的端点
- provider error 解析会同时给出 `kind` 与恢复语义（`retryable` / `fallback_allowed`），减少调用侧对启发式字符串的重复判断。

## 官方 SDK 边界

- `openai_native.py`：OpenAI Chat Completions adapter（`openai` package）。所有
  OpenAI-compatible provider 与 `endpoint` provider 都走这里。
- `anthropic_native.py`：Anthropic Messages adapter（`anthropic` package）。
- `google_native.py`：Gemini adapter（`google-genai` package）。
- `provider_config.py`：把各 provider 的配置对象规范化成 `ProviderEndpointConfig`（只含端点、
  鉴权、超时与用户自定义 `model_map`，不含内置模型清单）。
- `endpoint.py`：`endpoint` provider 的 adapter 外壳，用于消费 `providers.endpoint` 配置。
