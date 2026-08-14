# 关于 reasoning effort 抽象的决策

## 文档状态

**状态：accepted（issue #325）**

本文档记录当前阶段的架构决策：**VoidCode 把 reasoning effort 作为 runtime-owned 的规范枚举（canonical enum）hint 引入**，在 runtime 边界做严格校验，并在 provider 层完成 `normalize → clamp → map` 的确定性映射；对不支持该能力的 provider 保持 fail-fast 拒绝。

这一决策替代了早期的 “暂不引入” 草案；早期草案保留在 issue #325 的讨论中作为历史背景。

## 问题

在 OpenCode / oh-my-opencode 一类系统中，常见会提供一层 reasoning effort / thinking budget 抽象，用于控制模型在正式输出前花多少内部推理预算。典型目的包括：

- 为复杂任务提供更高的推理预算
- 为简单任务降低延迟与成本
- 在多模型、多 provider 场景中提供一个较统一的“思考强度”旋钮

问题在于：**VoidCode 当前是否有必要引入这样一层抽象？**

## 当前现实

issue #325 落地后，仓库的 reasoning effort 表面已经成为 runtime-owned 一等规范枚举：

- 规范枚举梯子 `off | minimal | low | medium | high | xhigh | max`，在 4 个边界（`config.py`、`contracts.py`、`config_materializer.py`、JSON Schema）做严格校验；非法值（包括旧的自由字符串 `"none"`）一律拒绝，不做向后兼容
- 流水线为 `normalize → clamp → map`，实现在 `provider/reasoning_effort.py`，并由 `provider/litellm_backend.py:_completion_kwargs_for_request` 接线
- `off` 是哨兵值（sentinel），永远不会被 clamp；`clamp_effort_to_supported` 在 `ProviderModelMetadata.supported_effort_levels` 被设置时把请求档位吸附到最近的支持档位（默认 `None` 表示不 clamp）
- `voidcode run --reasoning-effort <level>` 与 `VOIDCODE_REASONING_EFFORT` 环境变量都进入同一条优先级链
- `.voidcode.json` 接受 `"reasoning_effort"` 顶层字段，并由 `voidcode config schema` 暴露 JSON Schema
- `SessionState.metadata["runtime_config"]["reasoning_effort"]` 在 run 时被持久化，并在 resume 时优先于新的 runtime 默认值
- runtime 在请求处理早期对“当前 model metadata 明确 `supports_reasoning_effort=False`”做 fail-fast 校验；opencode-go 的禁用由 `model_catalog.py` 通过 `supports_reasoning_effort=False` 在运行时强制（fail-fast），而不是适配器层的显式 ignore
- `reasoning` stream channel 仍只用于可观测性输出，不属于配置面

## 决策

引入 reasoning effort 作为 **runtime-owned 规范枚举 hint**：

- 枚举梯子固定为 `off | minimal | low | medium | high | xhigh | max`，语义由 canonical enum 统一定义，不再依赖 provider adapter 的自由翻译；runtime 在 4 个配置边界严格校验，非法值直接拒绝
- 优先级（高 → 低）：persisted session override > request metadata override > 显式 CLI / 客户端 override > `.voidcode.json` > `VOIDCODE_REASONING_EFFORT` > 默认 `None`
- 流水线：`normalize`（把用户输入规约到 canonical enum）→ `clamp`（当 provider 声明 `supported_effort_levels` 时吸附到最近支持档位；`off` 是哨兵，永不 clamp）→ `map`（按 provider 方言翻译成具体请求参数）
- 仅在 `supports_reasoning_effort` 显式为 `False` 时 fail-fast；metadata 未知（`None`）时不 clamp，按 canonical 枚举直接透传
- runtime 持有 hint，provider adapter 负责映射；每个 provider 的映射方式由映射表显式定义，不接受自由透传

## 为什么现在引入

### 1. 底层链路已经存在，只是产品配置面缺失

实施前 `RuntimeRequest.metadata["reasoning_effort"]`、`ProviderTurnRequest.reasoning_effort`、`ProviderGraph` 透传与 `LiteLLMProviderBackend` 的 GLM/direct/provider 分支都已经存在；同时 `ProviderModelMetadata.supports_reasoning_effort` / `default_reasoning_effort` / `supported_effort_levels` 也已暴露。继续把它当作隐式 metadata 字段会让能力存在但 CLI 用户用不上，并导致 docs 与代码现状脱节。

### 2. 同一模型不同思考预算的需求已经出现

现代 reasoning-capable 模型（GPT-5/o-series、Claude 4、Gemini 2.5/3、GLM-5/Z1、Grok reasoning 等）都暴露了显式的 reasoning effort 控制；不同档位的 latency/cost/quality 取舍是真实需求，而不是“换个模型”就能表达的差异。

### 3. 把它锁进 runtime-owned 边界，避免 CLI/API/UI 重复发明入口

CLI、HTTP、未来的 TUI 都共用 `voidcode.runtime` 控制面；把 reasoning effort 作为 runtime-owned 一等字段，比让每个 client 各自维护私有 metadata 更可控。

## 边界与不变量

实现遵循以下约束：

### 1. 它是 runtime-owned

这层能力由 runtime 拥有，而不是由：

- client 拥有
- graph 拥有
- prompt 约定拥有

它进入配置优先级、恢复语义与 provider 调度语义，因此必须由 runtime 控制。

### 2. 它是规范枚举，不是自由字符串

字段形状是 canonical enum（`off | minimal | low | medium | high | xhigh | max`），表示 **runtime-level hint**，而非对 provider 行为的强一致保证。严格校验发生在 4 个边界：`config.py`、`contracts.py`、`config_materializer.py`、JSON Schema；旧的自由字符串形式（如 `"none"`）不再被接受，也不做向后兼容转换。

provider 层通过 `normalize → clamp → map` 流水线处理：

- `normalize`：把 CLI / 文件 / 环境变量 / metadata 中的输入规约到 canonical enum
- `clamp`：`clamp_effort_to_supported` 在 `ProviderModelMetadata.supported_effort_levels` 非空时把档位吸附到最近支持档位；`off` 是哨兵，永不参与 clamp；默认 `None` 表示不 clamp
- `map`：把 canonical 档位翻译成各 provider 方言（见下方映射表）

各 provider 的映射方式：

| Provider / 模型 | 映射方式 | 备注 |
| --- | --- | --- |
| GLM（`glm-5` / `glm-z1`） | `extra_body.thinking.type = enabled \| disabled` | 二元映射（`off` → `disabled`，其余档位 → `enabled`）；LiteLLM 的 zai 方言不支持 `reasoning_effort`，因此 GLM 走 thinking type 而不是 kwarg |
| OpenAI / compatible | `reasoning_effort` kwarg | `off` → `"none"`，`max` → `"xhigh"`，中间档位按枚举名透传 |
| Anthropic | `reasoning_effort` kwarg | `off` → `"none"`，`max` → `"max"`，中间档位按枚举名透传 |
| Google（gemini / vertex_ai） | `reasoning_effort` kwarg | `off` → `"none"`，`max` → `"high"`（Google 最高档位即 `"high"`），中间档位按枚举名透传 |
| Grok / 其他 | `reasoning_effort` kwarg | 采用 openai/compat 语义：`off` → `"none"`，`max` → `"xhigh"` |
| opencode-go | 运行时 fail-fast | `model_catalog.py` 中 `supports_reasoning_effort=False`，在请求阶段 fail-fast；不是适配器层的显式 ignore |

### 3. 它只作用于 provider-backed 路径

`execution_engine = "deterministic"` 不消费 reasoning effort；只有 provider-backed single-agent 路径会把这层 hint 透传给 provider。

### 4. provider 映射继续留在 provider 层

runtime 持有 hint，执行 `normalize` 与 `clamp`；真正翻译成 provider 请求参数的 `map` 责任仍在 `voidcode.provider.litellm_backend` 等适配器，避免 runtime 层被 provider-specific 细节污染。

## Capability-aware validation

当前采用 fail-fast + 严格校验策略：

- canonical enum 在 4 个边界严格校验：`config.py`、`contracts.py`、`config_materializer.py`、JSON Schema；非法值（包括旧的 `"none"` 等自由字符串）一律拒绝
- 如果 `ProviderModelMetadata.supports_reasoning_effort` 显式为 `False`，runtime 在 `_runtime_config_for_request()` 中拒绝请求，错误信息提示 “remove the reasoning_effort hint or pick a reasoning-effort capable model”
- 如果 metadata 未知（`None`），runtime 不阻塞、不 clamp，按 canonical 枚举透传；diagnostics 仍可通过 `voidcode provider inspect <provider>` 查询 model 的 `supports_reasoning_effort` / `default_reasoning_effort` / `supported_effort_levels`
- 当 `supported_effort_levels` 被设置时，`clamp_effort_to_supported` 把越界档位吸附到最近支持档位（`off` 哨兵除外），避免向不支持某档位的 provider 发送无效值
- 容许 fallback chain 中存在能力差异，因为 fail-fast 只针对当前 active target

未来如果用户反馈表明 fail-fast 过于严格，可以切换为 warn + ignore；该决策可以在不破坏 schema 的前提下迭代。但 canonical enum 的严格校验与“不做向后兼容”在当前阶段是有意选择，不随该迭代回退。

## 非目标

本文档不主张：

- 把 provider-specific budget 字段直接透出给所有客户端（仍由 provider adapter 通过映射表翻译）
- 为旧的自由字符串 effort 值（如 `"none"`）提供向后兼容；枚举范围之外的值一律拒绝
- 把 `reasoning` stream channel 误当成配置能力（它仍是观测面）
- 在 deterministic execution 路径上消费 reasoning effort
- 在 delegated/multi-agent 拓扑中独立扩张 reasoning effort 语义（child run 通过 binding scope 继承 parent 的 reasoning_effort，不引入新拓扑字段）
- 解除 DeepSeek / Qwen / Kimi / MiniMax 的 `supports_reasoning_effort=False`：这些模型保持运行时 fail-fast，这是有意的产品决策

## 结论

reasoning effort 现在已经是 VoidCode 的 runtime-owned canonical enum hint（`off | minimal | low | medium | high | xhigh | max`），在 `config.py`、`contracts.py`、`config_materializer.py`、JSON Schema 四处严格校验，覆盖 CLI、`.voidcode.json`、环境变量、HTTP metadata 与 session 持久化；provider 侧通过 `normalize → clamp → map` 流水线做确定性映射，`off` 作为永不 clamp 的哨兵；GLM 走 `extra_body.thinking.type`，其余 provider 走 `reasoning_effort` kwarg；对不支持该能力或未解锁的模型（含 opencode-go、DeepSeek、Qwen、Kimi、MiniMax）在运行时 fail-fast，避免用户以为 effort 生效但实际被 provider 忽略。
