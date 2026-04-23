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
| **LiteLLM** | `api_key` / `api_key_env_var` | `LITELLM_API_KEY` / `LITELLM_PROXY_API_KEY` | `api_key`, `none` |

### 中国 AI Provider（一等支持）

以下 Provider 使用简化的 `SimplifiedProviderConfig`，支持最小配置：`api_key` + 可选 `base_url`。

| Provider | 配置 Key | 默认 Base URL | 默认环境变量 |
| :--- | :--- | :--- | :--- |
| **GLM** (智谱 AI) | `glm` | `https://open.bigmodel.cn/api/paas/v4` | `GLM_API_KEY` |
| **MiniMax** | `minimax` | `https://api.minimax.io` | `MINIMAX_API_KEY` |
| **Kimi** (Moonshot AI) | `kimi` | `https://api.moonshot.ai` | `KIMI_API_KEY` |
| **OpenCode Go** | `opencode-go` | `https://opencode.ai/zen/go` | `OPENCODE_API_KEY` |
| **Qwen** (通义千问) | `qwen` | `https://dashscope.aliyuncs.com/compatible-mode` | `DASHSCOPE_API_KEY` |

#### 模型发现策略

| Provider | Discovery 方式 | 说明 |
| :--- | :--- | :--- |
| **GLM** | `/v4/models` endpoint | OpenAI-compatible，自动发现 |
| **MiniMax** | `model_map` fallback | 默认不启用 discovery，使用内置 model_map |
| **Kimi** | `/v1/models` endpoint | OpenAI-compatible，自动发现 |
| **OpenCode Go** | `model_map` fallback | 无公开 discovery endpoint，使用内置 model_map |
| **Qwen** | `/v1/models` endpoint | DashScope compatible-mode，自动发现 |

所有 Provider 都内置了稳定的 `model_map` fallback 列表，用户无需手动配置即可使用。

内置默认模型映射：

| Provider | 可用模型别名 |
| :--- | :--- |
| **GLM** | `glm-4-flash`, `glm-4-plus`, `glm-4`, `glm-5`, `glm-5-turbo` |
| **MiniMax** | `minimax-m2.7`, `minimax-m2.5`, `minimax-m2.1`, `minimax-m2` |
| **Kimi** | `kimi-k2.6`, `kimi-k2.5`, `kimi-k2`, `kimi-k2-turbo`, `kimi-k2-thinking` |
| **OpenCode Go** | `glm-5`, `glm-5.1`, `kimi-k2.5`, `kimi-k2.6`, `mimo-v2-omni`, `mimo-v2-pro`, `mimo-v2.5`, `mimo-v2.5-pro`, `minimax-m2.5`, `minimax-m2.7`, `qwen3.5-plus`, `qwen3.6-plus` |
| **Qwen** | `qwen-plus`, `qwen-max`, `qwen-flash`, `qwen3.5-plus`, `qwen3.5-flash`, `qwq-plus` |

配置示例（最小）：

```json
{
  "providers": {
    "glm": {},
    "kimi": {},
    "qwen": {}
  },
  "model": "glm/glm-4-flash"
}
```

OpenCode Go 的用户可见模型引用始终保持 `opencode-go/<model-id>`，例如
`opencode-go/glm-5.1`。后端调用 `https://opencode.ai/zen/go/v1` 时只把下游模型名交给
LiteLLM，并按 OpenCode Go 模型族选择兼容 SDK 适配器：

- GLM / Kimi / MiMo：OpenAI-compatible chat completions
- MiniMax M2.5 / M2.7：Anthropic-compatible messages
- Qwen3.5 Plus / Qwen3.6 Plus：Alibaba-compatible chat completions（LiteLLM 中走
  DashScope/OpenAI-compatible 适配器）

runtime 仍统一注入工具 schema、审批与会话状态。

配置示例（完整）：

```json
{
  "providers": {
    "glm": {
      "api_key": "your-glm-api-key",
      "base_url": "https://open.bigmodel.cn/api/paas/v4",
      "model_map": { "glm4": "glm-4-flash" }
    },
    "minimax": {
      "api_key_env_var": "MINIMAX_API_KEY",
      "timeout_seconds": 60.0
    },
    "kimi": {
      "api_key": "your-kimi-api-key",
      "base_url": "https://api.moonshot.ai/v1"
    }
  },
  "model": "minimax/minimax-m2.7"
}
```

这些 Provider 不允许在 `providers.custom` 下定义（会与内置名称冲突）。

### 自定义 Provider（生产可用路径）

- 使用 `providers.custom.<provider_name>` 定义任意自定义 provider（名称必须不包含 `/`）。
- `providers.custom.<provider_name>` 不能与内置 provider 名称冲突（`openai` / `anthropic` / `google` / `copilot` / `litellm` / `opencode`）。
- 每个自定义 provider 都复用 LiteLLM 后端调用路径，支持与 `providers.litellm` 相同的字段：
  - `api_key` / `api_key_env_var`
  - `base_url`
  - `auth_scheme` + `auth_header`
  - `model_map`
- 在模型配置中使用 `model: "<provider_name>/<model_alias_or_raw_model>"` 即可路由到对应自定义 provider。
- **强烈建议**为自定义 provider 配置 `model_map`：
  - 配置 `model_map` 时，可直接使用稳定别名（例如 `llama-local/coder`），并映射到真实后端模型 ID。
  - 未配置 `model_map` 时，模型名会原样以 provider/model 形式发送给后端，通常建议显式映射到真实 downstream 模型 ID。
- Provider Auth 接口现在同样支持 `providers.custom.<provider_name>`：
  - 自定义 provider 会复用 LiteLLM auth 语义（`api_key` / `none`）
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

### LiteLLM 开箱即用

- 默认基地址支持 `LITELLM_BASE_URL` / `LITELLM_PROXY_URL`，未配置时回退到 `http://127.0.0.1:4000`。
- 通过 `model: "litellm/<model>"` 可直接走 LiteLLM OpenAI-Compatible `/v1/chat/completions`。
- 支持 `providers.litellm.auth_scheme`（`bearer` / `token` / `none`）和 `auth_header` 自定义认证头。
- 支持 `providers.litellm.model_map` 做别名映射（例如将 `gpt-4o` 映射到 `openrouter/openai/gpt-4o`）。

### `providers.litellm` vs `providers.custom`

- `providers.litellm`：用于配置默认 LiteLLM 兼容后端（provider 名称固定为 `litellm`）。
- `providers.custom`：用于定义任意自定义 provider 名称（如 `llama-local`、`openrouter-team-a`），每个条目都复用 LiteLLM-compatible 配置结构。

### 可用模型列表动态刷新（端点/API 驱动）

- Runtime 现在支持按 provider 动态刷新可用模型列表：
  - `voidcode provider models <provider>`：读取当前缓存
  - `voidcode provider models <provider> --refresh`：主动请求 provider `/v1/models` 刷新
- provider-specific 端点策略（与 opencode 的 provider 分层思路对齐）：
  - `openai` / `litellm` / 自定义 OpenAI-compatible：`<base>/v1/models`
  - `anthropic`：`<base>/v1/models`，并带 `anthropic-version` + `x-api-key`
  - `google`：`<base>/v1beta/models?key=...`（读取 `models[].name`）
- 刷新结果会融合三类来源并去重：
  1. `model_map` 的别名键（便于用户直接选别名）
  2. provider 端点返回的真实模型 ID
  3. `model_map` 的映射目标值（便于调试真实路由）
- 对于 OpenAI/LiteLLM 默认支持 endpoint 探测；自定义 provider 若配置了 `base_url` 也会走同样的 OpenAI-compatible `/v1/models` 探测路径。

## 流式传输 (Streaming)

VoidCode 的 Provider 抽象层输出标准化的流式事件包。

### 事件包 (Event Envelope)

事件通过 `ProviderStreamEvent` 结构表示，包含以下字段：

- `kind`: 事件类型 (`delta`, `content`, `error`, `done`)。
- `channel`: 数据通道 (`text`, `tool`, `reasoning`, `error`)。
- `text`: 流片段文本（仅 `delta` / `content`）。
- `error`: 错误描述（仅 `error`）。
- `error_kind`: 错误分类（`rate_limit`, `context_limit`, `invalid_model`, `transient_failure`, `cancelled`）。
- `done_reason`: 完成原因（`completed`, `cancelled`, `error`）。

### 取消与超时行为

- **显式取消**: 通过 `SingleAgentAbortSignal` 触发。一旦取消，流将立即产生 `error_kind: cancelled` 事件并终止。
- **分片超时**: 如果两个流分片（chunk）之间的时间间隔超过配置的超时阈值，将抛出 `transient_failure` 错误。

## 故障排除 (Troubleshooting)

| 错误种类 (`error_kind`) | 常见原因 | 建议对策 |
| :--- | :--- | :--- |
| `invalid_model` | API Key 缺失/无效、模型名称拼写错误、无权限访问 | 检查环境变量和 `.voidcode.json`；确认供应商控制台权限。 |
| `rate_limit` | 触发供应商频率限制或配额不足。 | 稍后重试或联系供应商增加额度。 |
| `context_limit` | 对话历史过长或单次 Prompt 超过模型窗口。 | 减少工作区上下文注入或切换到更大窗口的模型。 |
| `transient_failure` | 供应商服务中断、网络波动、超时。 | 检查网络连接；VoidCode 运行时会自动尝试 Fallback。 |
| `cancelled` | 用户手动中止任务或客户端断开。 | 无需动作，按预期停止。 |

## 代码结构

- `protocol.py`: 定义 Provider 契约与流式事件模型。
- `config.py`: 处理各供应商的配置解析与校验。
- `errors.py`: 负责从原始供应商响应中提取并分类错误。
- `registry.py`: 维护已实现的 Provider 实例。
- `resolution.py`: 负责将原始请求解析为具体的 Provider 配置。
- `snapshot.py`: 提供安全的快照导出逻辑，确保不泄露机密。
