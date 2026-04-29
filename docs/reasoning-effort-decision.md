# Reasoning Effort 产品化决策

## 文档状态

**状态：accepted**

本文档记录 VoidCode 将 reasoning effort 作为 runtime-owned optional hint 产品化的决策。

## 问题

VoidCode 对 reasoning effort 的支持处于"底层链路已有、产品配置面未完成"的中间状态：

- `RuntimeRequest.metadata["reasoning_effort"]` 已经是稳定允许字段
- `ProviderTurnRequest.reasoning_effort` 已经进入 provider request contract
- `ProviderGraph` 会把 request metadata 里的 `reasoning_effort` 传给 provider
- `LiteLLMProviderBackend` 已经会把 effort 映射/透传到部分 provider
- provider model metadata 已经暴露 `supports_reasoning_effort` 和 `default_reasoning_effort`

但用户主路径还没有完整支持：

- `voidcode run` 没有 `--reasoning-effort` 参数
- `.voidcode.json` / runtime config 没有一等 `reasoning_effort` 字段
- 环境变量没有对应入口（如 `VOIDCODE_REASONING_EFFORT`）
- session resume / persisted runtime config 语义没有明确这个字段应该如何保存与覆盖
- HTTP/API 虽可通过 metadata 传入，但缺少更高层的 config/schema 约束和 capability-aware validation

## 决策

**将 reasoning effort 产品化为 runtime-owned optional hint。**

这不是一个"是否引入"的问题，而是"如何将已有能力产品化"的问题。底层链路已经存在，现在需要将其提升到用户可配置的主路径上。

## 设计原则

### 1. Runtime-owned

这层能力应当由 runtime 拥有，而不是由 client、graph 或 prompt 约定拥有。它会进入配置优先级、恢复语义和 provider 调度语义，因此必须由 runtime 控制。

### 2. Optional hint，非强保证

`reasoning_effort: str | None` 表示 runtime-level hint，而不是对 provider 行为的强一致保证。

Provider adapter 应当可以：
- 映射（如 OpenAI/Grok 的 `reasoning_effort`）
- 转换（如 GLM 的 `extra_body.thinking.type`）
- 忽略（如明确不支持的 provider）

### 3. 配置优先级

```
request override > persisted session override > repo config > env > default
```

- **Request override**: `voidcode run --reasoning-effort high` 或 HTTP API metadata
- **Persisted session override**: session resume 时保留当时生效的 effort
- **Repo config**: `.voidcode.json` 中的 `reasoning_effort` 字段
- **Environment**: `VOIDCODE_REASONING_EFFORT` 环境变量
- **Default**: 无（不设置时 provider 使用自身默认行为）

### 4. Capability-aware validation

如果当前 model metadata 明确 `supports_reasoning_effort=false`：

- **Pre-MVP 阶段**: fail-fast，避免用户以为 effort 生效但实际被 provider 忽略
- 对未知 metadata/custom provider，允许 best-effort pass-through，但在 diagnostics 中标明 unknown support

### 5. 作用范围

首个作用范围应当仅限：
- provider-backed
- single-agent

deterministic execution 不应受这层能力影响。

## 实现计划

### 1. Runtime config schema

在 `RuntimeConfig` / `.voidcode.json` 中加入可选字段：

```python
class RuntimeConfig:
    # ... 现有字段 ...
    reasoning_effort: str | None = None
```

同步更新 `RuntimeConfigOverrides` 和 `config_schema.py` 中的 JSON schema。

### 2. CLI 支持

`voidcode run` 增加 `--reasoning-effort <level>` 参数。

```bash
voidcode run "complex task" --reasoning-effort high
```

`voidcode doctor` / provider readiness 可提示当前模型是否支持 effort。

### 3. 环境变量支持

增加 `VOIDCODE_REASONING_EFFORT` 环境变量支持。空字符串视为 unset。

### 4. Provider adapter contract

保留 runtime-level hint，provider adapter 负责映射：

- **OpenAI/Grok 等**: `reasoning_effort` 参数
- **GLM**: `extra_body.thinking.type` 或 provider-compatible 参数
- **不支持的 provider**: 明确 ignore 或报错，不要静默伪装成功
- **opencode-go**: 当前明确忽略 effort，需要在 metadata/readiness 中如实暴露

### 5. Session persistence

Session resume 时保留当时生效的 effort，避免恢复后行为漂移。将 `reasoning_effort` 纳入 session metadata 的 runtime config 快照中。

### 6. HTTP/API surface

继续允许 metadata override，但也支持 runtime config 层字段。在 transport payload / schema 文档中明确 request metadata 和 runtime config 的关系。

## 错误处理的代码指导

### CLI 参数验证

```python
# src/voidcode/cli.py
if getattr(args, "reasoning_effort", None) is not None:
    effort = cast(str, args.reasoning_effort)
    if effort not in {"low", "medium", "high", "xhigh"}:
        raise SystemExit(
            f"error: invalid reasoning_effort {effort!r}; "
            "must be one of: low, medium, high, xhigh"
        )
    metadata["reasoning_effort"] = effort
```

### Capability-aware validation

```python
# src/voidcode/runtime/service.py (或 config.py)
def _validate_reasoning_effort_for_model(
    effort: str | None,
    model_metadata: ProviderModelMetadata | None,
) -> str | None:
    """验证 reasoning_effort 与模型能力是否匹配。

    返回生效的 effort（可能为 None），或在不匹配时 fail-fast。
    """
    if effort is None:
        return None

    if model_metadata is None:
        # 未知模型，允许 best-effort pass-through
        return effort

    supports = model_metadata.supports_reasoning_effort
    if supports is False:
        raise RuntimeRequestError(
            f"model does not support reasoning_effort, but {effort!r} was requested; "
            f"either remove --reasoning-effort or switch to a supported model"
        )

    # supports is True or None (unknown) → 允许
    return effort
```

### 环境变量解析

```python
# src/voidcode/runtime/config.py
REASONING_EFFORT_ENV_VAR = "VOIDCODE_REASONING_EFFORT"

def _parse_reasoning_effort_from_env(env: Mapping[str, str] | None = None) -> str | None:
    """从环境变量解析 reasoning_effort。空字符串视为 unset。"""
    environment: Mapping[str, str] = os.environ if env is None else env
    value = environment.get(REASONING_EFFORT_ENV_VAR)
    if value is None or value.strip() == "":
        return None
    return value.strip()
```

## 参考文档

- Runtime contracts: `src/voidcode/runtime/contracts.py` - `RuntimeRequestMetadata.reasoning_effort`
- Provider protocol: `src/voidcode/provider/protocol.py` - `ProviderTurnRequest.reasoning_effort`
- LiteLLM backend: `src/voidcode/provider/litellm_backend.py` - `_completion_kwargs_for_request()`
- Model catalog: `src/voidcode/provider/model_catalog.py` - `supports_reasoning_effort`
- Config schema: `src/voidcode/runtime/config_schema.py`
- Runtime config: `src/voidcode/runtime/config.py` - `RuntimeConfig`

## 测试计划

增加以下测试：

1. **CLI flag 测试**: `--reasoning-effort` 参数解析和验证
2. **Config/env 优先级测试**: 验证 request > session > repo config > env > default 的优先级
3. **Resume persistence 测试**: session resume 时 effort 的正确恢复
4. **Unsupported model validation 测试**: 对不支持的模型 fail-fast
5. **Provider kwargs mapping 测试**: 验证各 provider 的 effort 参数映射
6. **Model metadata exposure 测试**: 验证 `supports_reasoning_effort` 和 `default_reasoning_effort` 正确暴露
