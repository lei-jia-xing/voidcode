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

### LiteLLM 开箱即用

- 默认基地址支持 `LITELLM_BASE_URL` / `LITELLM_PROXY_URL`，未配置时回退到 `http://127.0.0.1:4000`。
- 通过 `model: "litellm/<model>"` 可直接走 LiteLLM OpenAI-Compatible `/v1/chat/completions`。
- 支持 `providers.litellm.auth_scheme`（`bearer` / `token` / `none`）和 `auth_header` 自定义认证头。
- 支持 `providers.litellm.model_map` 做别名映射（例如将 `gpt-4o` 映射到 `openrouter/openai/gpt-4o`）。

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
