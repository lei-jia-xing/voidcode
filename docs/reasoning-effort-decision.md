# 关于 reasoning effort 抽象的决策

## 文档状态

**状态：accepted（issue #325）**

本文档记录当前阶段的架构决策：**VoidCode 把 reasoning effort 作为 runtime-owned 的可选 hint 引入**，并保留 provider adapter 层的 best-effort 映射 / fail-fast 拒绝语义。

这一决策替代了早期的 “暂不引入” 草案；早期草案保留在 issue #325 的讨论中作为历史背景。

## 问题

在 OpenCode / oh-my-opencode 一类系统中，常见会提供一层 reasoning effort / thinking budget 抽象，用于控制模型在正式输出前花多少内部推理预算。典型目的包括：

- 为复杂任务提供更高的推理预算
- 为简单任务降低延迟与成本
- 在多模型、多 provider 场景中提供一个较统一的“思考强度”旋钮

问题在于：**VoidCode 当前是否有必要引入这样一层抽象？**

## 当前现实

issue #325 落地后，仓库的 reasoning effort 表面已经成为 runtime-owned 一等可选 hint：

- `RuntimeConfig.reasoning_effort: str | None` 与 `EffectiveRuntimeConfig.reasoning_effort` 已存在并参与 effective config 解析
- `voidcode run --reasoning-effort <level>` 与 `VOIDCODE_REASONING_EFFORT` 环境变量都进入同一条优先级链
- `.voidcode.json` 接受 `"reasoning_effort"` 顶层字段，并由 `voidcode config schema` 暴露 JSON Schema
- `SessionState.metadata["runtime_config"]["reasoning_effort"]` 在 run 时被持久化，并在 resume 时优先于新的 runtime 默认值
- runtime 在请求处理早期对“当前 model metadata 明确 `supports_reasoning_effort=False`”做 fail-fast 校验
- provider 适配器（GLM thinking、direct provider `reasoning_effort` kwarg、opencode-go 显式 ignore）继续负责实际映射；`reasoning` stream channel 仍只用于可观测性输出，不属于配置面

## 决策

引入 reasoning effort 作为 **runtime-owned 可选 hint**：

- 它是 optional string，不强制语义统一；具体含义由 provider adapter 翻译
- 优先级（高 → 低）：persisted session override > request metadata override > 显式 CLI / 客户端 override > `.voidcode.json` > `VOIDCODE_REASONING_EFFORT` > 默认 `None`
- 仅在 `supports_reasoning_effort` 显式为 `False` 时 fail-fast；当前 model metadata 未知时按 best-effort 透传，并在 provider 适配器层尽量降级而不是伪装成功
- runtime 持有 hint，provider adapter 负责映射；不强制每个 provider 都精确实现相同语义

## 为什么现在引入

### 1. 底层链路已经存在，只是产品配置面缺失

实施前 `RuntimeRequest.metadata["reasoning_effort"]`、`ProviderTurnRequest.reasoning_effort`、`ProviderGraph` 透传与 `LiteLLMProviderBackend` 的 GLM/direct/opencode-go 分支都已经存在；同时 `ProviderModelMetadata.supports_reasoning_effort` / `default_reasoning_effort` 也已暴露。继续把它当作隐式 metadata 字段会让能力存在但 CLI 用户用不上，并导致 docs 与代码现状脱节。

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

### 2. 它是可选 hint，不是强保证

字段形状是 `reasoning_effort: str | None`，表示 **runtime-level hint**，而非对 provider 行为的强一致保证。

provider adapter 仍可以：

- 映射（`openai`、`anthropic`、`google`、`gemini`、`vertex_ai`、`litellm`、`grok` 直接透传 `reasoning_effort` kwarg；`glm` / GLM-5/Z1 模型映射为 `extra_body.thinking.type=enabled|disabled`）
- 显式 ignore（`opencode-go` 当前对 effort 不做映射，并在 `model_catalog.py` 中通过 `supports_reasoning_effort=False` 暴露这一点；runtime 在请求阶段就 fail-fast）
- 静默 best-effort（custom provider 或 metadata 未知时透传，不强制一致）

### 3. 它只作用于 provider-backed 路径

`execution_engine = "deterministic"` 不消费 reasoning effort；只有 provider-backed single-agent 路径会把这层 hint 透传给 provider。

### 4. provider 映射继续留在 provider 层

runtime 持有 hint；真正翻译成 provider 请求参数的责任仍在 `voidcode.provider.litellm_backend` 等适配器，避免 runtime 层被 provider-specific 细节污染。

## Capability-aware validation

当前 pre-MVP 阶段采用 fail-fast 策略：

- 如果 `ProviderModelMetadata.supports_reasoning_effort` 显式为 `False`，runtime 在 `_runtime_config_for_request()` 中拒绝请求，错误信息提示 “remove the reasoning_effort hint or pick a reasoning-effort capable model”
- 如果 metadata 未知（`None`），runtime 不阻塞，按 best-effort 透传；diagnostics 仍可通过 `voidcode provider inspect <provider>` 查询 model 的 `supports_reasoning_effort` / `default_reasoning_effort`
- 容许 fallback chain 中存在能力差异，因为 fail-fast 只针对当前 active target

未来如果用户反馈表明 fail-fast 过于严格，可以切换为 warn + ignore；该决策可以在不破坏 schema 的前提下迭代。

## 非目标

本文档不主张：

- 把 provider-specific budget 字段直接透出给所有客户端（仍由 provider adapter 翻译）
- 把 `reasoning` stream channel 误当成配置能力（它仍是观测面）
- 在 deterministic execution 路径上消费 reasoning effort
- 在 delegated/multi-agent 拓扑中独立扩张 reasoning effort 语义（child run 通过 binding scope 继承 parent 的 reasoning_effort，不引入新拓扑字段）

## 结论

reasoning effort 现在已经是 VoidCode 的 runtime-owned optional hint，覆盖 CLI、`.voidcode.json`、环境变量、HTTP metadata 与 session 持久化；provider 映射继续由适配器负责，runtime 在能力 mismatch 时 fail-fast，避免用户以为 effort 生效但实际被 provider 忽略。
