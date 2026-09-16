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
| **Copilot** | `auth.token` | `GITHUB_COPILOT_TOKEN` | `token`, `oauth` |
| **Endpoint** | `api_key` / `api_key_env_var` | `ENDPOINT_API_KEY` | `api_key`, `none` |
| **OpenCode Zen** | `api_key` / `api_key_env_var` | `OPENCODE_API_KEY` | `api_key` |
| **OpenRouter** | `api_key` / `api_key_env_var` | `OPENROUTER_API_KEY` | `api_key`, `none` |

### 一等 OpenAI-compatible Provider

以下 provider 默认都复用官方 OpenAI SDK（`openai` package）的 chat-completions 路径（`opencode-go` 例外：它按模型选择 wire，见下文 OpenCode 说明）；配置支持 `api_key`、`api_key_env_var`、`base_url`、`discovery_base_url`、`ssl_verify`、`timeout_seconds` 和 `model_map`。

| Provider | 配置 Key | 默认 Base URL | 默认环境变量 |
| :--- | :--- | :--- | :--- |
| **Z.AI** | `zai` | `https://api.z.ai/api/paas/v4` | `ZAI_API_KEY` |
| **智谱 AI** | `zhipuai` | `https://open.bigmodel.cn/api/paas/v4` | `ZHIPU_API_KEY`（无值时回退 `ZAI_API_KEY`） |
| **OpenRouter** | `openrouter` | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |
| **MiniMax** | `minimax` | `https://api.minimax.io` | `MINIMAX_API_KEY` |
| **Kimi** (Moonshot AI) | `kimi` | `https://api.moonshot.ai` | `KIMI_API_KEY` |
| **OpenCode Go** | `opencode-go` | `https://opencode.ai/zen/go` | `OPENCODE_API_KEY` |
| **Qwen** (通义千问) | `qwen` | `https://dashscope.aliyuncs.com/compatible-mode` | `DASHSCOPE_API_KEY` |
| **Groq** | `groq` | `https://api.groq.com/openai/v1` | `GROQ_API_KEY` |
| **Together** | `together` | `https://api.together.ai/v1` | `TOGETHER_API_KEY` |
| **Fireworks AI** | `fireworks` | `https://api.fireworks.ai/inference/v1` | `FIREWORKS_API_KEY` |
| **Mistral** | `mistral` | `https://api.mistral.ai/v1` | `MISTRAL_API_KEY` |

同样走 OpenAI chat-completions 路径的还有 `openai`（`https://api.openai.com/v1`）、
`copilot`（`https://api.individual.githubcopilot.com`，Copilot 凭据不会发往 OpenAI）、
`endpoint`（未配置时为本地网关 `http://127.0.0.1:4000/v1`）、`deepseek`（`https://api.deepseek.com`）
与 `grok`（`https://api.x.ai`）。

### Endpoint 解析规则

- 每个 provider 只解析到自己的 endpoint：先取 `providers.<name>.base_url`，没有则取该 provider 自身的默认 host。
  **不会**回落到其它 vendor 的 host——某个 vendor 的配置缺失时，请求也不会被发到 `api.openai.com`。
  （例外：OpenCode 网关的 Anthropic 路由固定使用网关 host root，见下文 OpenCode 说明。）
- 配置 block 完全缺失（`.voidcode.json` 里没有该 block，也没有对应凭据环境变量）时，同样按该 provider 的默认 host
  解析，同时关闭远端 discovery：不会对没人配置过的 provider 发起未鉴权的模型列表请求。
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
- `providers.google.base_url` 同样生效：它是完整的 endpoint root（含版本段），SDK 不会再追加自己的
  `v1beta` / `v1beta1`；设置了自定义 base URL 时 SDK 也会跳过自身的 ADC project 解析。
- 未配置时 `endpoint` provider 仍按文档化的本地网关默认值解析（`endpoint_default`），这是唯一的
  「没有配置也能调用」的 provider。
- `voidcode provider inspect <provider>` 的输出包含 `endpoint` 字段，说明解析结果与来源：
  - `endpoint.base_url`：wire 实际使用的基础 URL（与 transport 的归一化规则一致；`null` 表示配置里没有可解析的 base URL）
  - `endpoint.source`：`config`（来自 `providers.<name>.base_url`）、`provider_default`（provider 自身默认 host）、
    `endpoint_default`（`endpoint` provider 未配置时的本地网关默认值）
  - `endpoint.discovery_base_url`：模型发现使用的基址；`""` 表示禁用远端发现，`null` 表示未声明、由 provider 自行决定

#### 模型发现策略

| **Z.AI** | `/v4/models` endpoint | OpenAI-compatible，自动发现 |
| **智谱 AI** | `/v4/models` endpoint | OpenAI-compatible，自动发现 |
| **OpenRouter** | `/api/v1/models` endpoint | 自动发现真实模型 ID；模型引用保留 provider/model 中的全部 slash，也包含 API 返回的 `:free` 模型 |
| **MiniMax** | 无公开 discovery endpoint | 默认禁用远端发现；配置 `discovery_base_url` 或 `model_map` 后可用 |
| **Kimi** | `/v1/models` endpoint | OpenAI-compatible，自动发现 |
| **OpenCode Zen** | `/zen/v1/models` endpoint | OpenAI-compatible，自动发现；模型引用为 `opencode/<model-id>` |
| **OpenCode Go** | 无公开 discovery endpoint | 默认禁用远端发现；配置 `discovery_base_url` 或 `model_map` 后可用 |
| **Qwen** | `/v1/models` endpoint | DashScope compatible-mode，自动发现 |
| **Groq** | `/v1/models` endpoint | OpenAI-compatible，自动发现 |
| **Together** | `/v1/models` endpoint | OpenAI-compatible，自动发现 |
| **Fireworks AI** | 无通用 `/v1/models` | 默认禁用远端发现；可通过 `discovery_base_url` 显式启用 |
| **Mistral** | `/v1/models` endpoint | OpenAI-compatible，自动发现 |

OpenRouter 不硬编码易变的免费模型 slug；请使用 `/api/v1/models` 刷新得到的模型 ID，例如
`openrouter/anthropic/claude-3.7-sonnet` 或 API 当前返回的 `openrouter/<provider>/<model>:free`。

VoidCode 不内置任何静态模型清单（避免长期维护两份列表）：模型列表一律来自 provider 的 discovery endpoint；没有公开 discovery 的 provider 需要在配置里显式给出 `discovery_base_url` 或 `model_map`。

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
`opencode-go/<model-id>` / `opencode/<model-id>`，实际 wire 由模型本身决定，而不是由 provider 名决定
（与 OMP 的 per-model catalog 对齐）。routing 表以 `provider/opencode_go.py` / `provider/opencode.py`
中的常量为准：

- **OpenCode Go**（默认 base URL `https://opencode.ai/zen/go`，wire 归一化为 `https://opencode.ai/zen/go/v1`）：
  - 默认（其余全部模型）：OpenAI-compatible chat-completions，`https://opencode.ai/zen/go/v1/chat/completions`
  - `minimax-m3`：Anthropic Messages，`https://opencode.ai/zen/go/v1/messages`（route 持有 host root
    `https://opencode.ai/zen/go`，`/v1/messages` 由 Anthropic SDK 追加），凭据以 `x-api-key` 发送
  - 2026-09 观察：上游网关对 `minimax-m2.7` 的每一种请求形状都返回 HTTP 500，属网关侧故障；
    该问题关闭前 `minimax-m2.7` 保持在默认 chat-completions 路由（上游恢复后此行可直接删除）。
  - 上游为 OpenAI Responses API 的模型（`gpt-5.6-luna`）：VoidCode 未实现该 wire，会以
    `unsupported_feature`（不可重试、允许 fallback）失败，而不是静默降级到 chat-completions
- **OpenCode Zen**（默认 base URL `https://opencode.ai/zen/v1`）：
  - 默认（其余全部模型，含 `minimax-m2.5` / `minimax-m3`）：chat-completions，
    `https://opencode.ai/zen/v1/chat/completions`
  - 11 个 `claude-*` 与 `qwen3.5-plus` / `qwen3.6-plus`（共 13 个）：Anthropic Messages，
    `https://opencode.ai/zen/v1/messages`（route 持有 host root `https://opencode.ai/zen`）
  - 6 个 `gemini-*`：Google generative-ai wire，`https://opencode.ai/zen/v1/models/<model>:generateContent`，
    凭据以 `x-goog-api-key` 发送
  - 23 个 `gpt-*` / `grok-*` / `muse-spark-*`：上游为 OpenAI Responses API，同样以
    `unsupported_feature` 失败
- 没有 routing 条目的模型（例如 OMP 未收录的 `*-free`）走该 provider 的默认 chat-completions 路由；
  `model_map` 别名先解析再选 wire，别名指向被路由的模型时走该模型的 wire（并把解析后的模型名发给上游）；
  `providers.<name>.base_url` 对默认路由与 Zen 的 Google 路由仍然优先，Anthropic 路由固定使用网关 host root。

两个网关都按会话路由：**每条 wire 的每个请求**都必须带 `x-opencode-session: <conversation id>`
（缺失时网关返回 HTTP 400 `MissingSessionID`）与 `x-opencode-client`。VoidCode 在请求时从 runtime
session id 解析 `x-opencode-session`（wire client 跨 turn 复用，因此该值不能在构造时固定）；请求没有
session id 时该 header 不发送，`x-opencode-client` 仍然发送。

runtime 仍统一注入工具 schema、审批与会话状态。

OpenCode Zen 与 OpenCode Go 是不同 provider：Zen 使用 `opencode/<model-id>`，默认从
`https://opencode.ai/zen/v1/models` 自动发现模型；Go 使用 `opencode-go/<model-id>`，网关没有公开的
模型列表端点，需要 `model_map` 或显式 `discovery_base_url`。

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
- `providers.custom.<provider_name>` 不能与内置 provider 名称冲突（包括 `openai` / `anthropic` / `google` / `copilot` / `endpoint` / `opencode` / `openrouter` / `opencode-go` / `deepseek` / `zai` / `zhipuai` / `grok` / `minimax` / `kimi` / `qwen` / `groq` / `together` / `fireworks` / `mistral`）。
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
  - `voidcode provider inspect <provider>`：输出 provider 状态与解析后的 `endpoint`（`base_url` / `source` / `discovery_base_url`）
- provider-specific 端点策略（与 opencode 的 provider 分层思路对齐）：
  - `openai` / 自定义 OpenAI-compatible：`<base>/v1/models`
  - `anthropic`：`<base>/v1/models`，并带 `anthropic-version` + `x-api-key`
  - `google`：`<base>/v1beta/models?key=...`（读取 `models[].name`）
- 刷新结果会融合三类来源并去重：
  1. `model_map` 的别名键（便于用户直接选别名）
  2. provider 端点返回的真实模型 ID
  3. `model_map` 的映射目标值（便于调试真实路由）
- 对于 OpenAI 默认支持 endpoint 探测；自定义 provider 若配置了 `base_url` 也会走同样的 OpenAI-compatible `/v1/models` 探测路径。

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
- `done_reason`: 上游终态原因；成功终态必须是 `stop`、`tool_calls`、`function_call`、`length` 或 `content_filter`。缺失或未知终态不会被视为成功。

### 错误边界

- `ProviderExecutionError.message` 是 provider 原始错误的已脱敏可读文本，不应包含 SDK wrapper 前缀。
- `details` 保存有界、递归脱敏后的诊断信息；认证 token、cookie、header secret 不得进入持久化事件或 trace。
- `retryable`、`fallback_allowed` 和 `retry_after` 是 runtime 恢复策略的输入；显式 `False` 会覆盖按错误种类推导出的默认策略。
- `retry_after` 会被限制在 3600 秒以内，并覆盖同一次 retry 的指数退避延迟（仍受 runtime 最大延迟限制）。

### 取消、超时与不完整 tool call

- **显式取消**: 通过 `ProviderAbortSignal` 触发。一旦取消，流将立即产生 `error_kind: cancelled` 事件并终止。
- **分片超时**: transport 在相邻分片之间执行配置的 timeout 检查，并报告 `transient_failure`。
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
  - `default_endpoint`：未声明 provider 时回退到 `providers.endpoint` 配置
- fallback chain 不允许重复 target；即使绕过原始 config parser，`resolution.py` / `snapshot.py`
  仍会在 provider 模块内拒绝重复链路。
- model discovery 会显式区分：
  - `configured_endpoint`：使用 `discovery_base_url`
  - `configured_base_url`：从 `base_url` 推导探测端点
  - `disabled`：`discovery_base_url` 被显式置空，表示禁用远端发现
  - `unavailable`：provider 本身没有可用 discovery endpoint
- provider error 解析会同时给出 `kind` 与恢复语义（`retryable` / `fallback_allowed`），减少调用侧对启发式字符串的重复判断。

## 官方 SDK 边界

- `openai_native.py`：OpenAI Chat Completions adapter（`openai` package）。所有
  OpenAI-compatible provider 与 `endpoint` provider 都走这里。
- `anthropic_native.py`：Anthropic Messages adapter（`anthropic` package）。
- `google_native.py`：Gemini adapter（`google-genai` package）。
- `provider_config.py`：把各 provider 的配置对象规范化成 `ProviderEndpointConfig`（只含端点、
  鉴权、超时与用户自定义 `model_map`，不含内置模型清单）。
- `endpoint.py`：`endpoint` provider 的 adapter 外壳，用于消费 `providers.endpoint` 配置。
