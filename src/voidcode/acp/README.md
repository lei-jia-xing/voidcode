# `voidcode.acp`

这里是 ACP capability layer 的目标目录。

当前它的定位不是“把 `runtime/acp.py` 里的所有实现整体搬过来”，而是只承载那些已经被运行时路径证明稳定、又不依赖 runtime lifecycle ownership 的纯 contract / schema / model 定义。

## 负责什么

- 与 runtime 生命周期解耦的 ACP request / response envelope model
- 可复用的 ACP adapter-facing protocol contract
- 可复用的 ACP 配置 schema helper
- 不直接绑定 connect / disconnect / recovery 行为的纯状态模型

## 不负责什么

- connect / disconnect lifecycle 管理
- runtime 管理的 availability / status ownership
- session persistence / resume 语义
- runtime event emission / recovery flow
- runtime 对 ACP adapter 的装配与治理

## 与 `runtime` 的边界

[`src/voidcode/runtime/acp.py`](../runtime/acp.py) 当前仍然是 runtime-owned control plane。

它现在同时包含：

- `AcpConfigState`
- `AcpRequestEnvelope`
- `AcpResponseEnvelope`
- `AcpRuntimeEvent`
- `AcpAdapterState`
- `AcpAdapter` protocol
- `DisabledAcpAdapter`
- `ManagedAcpAdapter`
- `build_acp_adapter()`

其中有些部分看起来像 capability-layer primitive，但是否适合迁入 `src/voidcode/acp/`，必须先由现有 runtime 语义反推，而不是提前假设边界已经稳定。

## `#97` 当前要求

[`#97`](https://github.com/lei-jia-xing/voidcode/issues/97) 的要求不是直接迁移 ACP，而是两阶段推进：

1. 先验证哪些 ACP contract 已经稳定
2. 只有验证成立后，再把稳定的那部分迁入 `src/voidcode/acp/`

这条 issue 当前关注的验证问题是：

- `AcpRequestEnvelope` / `AcpResponseEnvelope` 是否已经覆盖 runtime 真正需要的语义
- `AcpAdapter` protocol 是否已经稳定表达 runtime 依赖的 adapter-facing contract
- `AcpConfigState` / `AcpAdapterState` 里哪些字段是纯模型，哪些仍强依赖 runtime lifecycle ownership
- 现有 ACP 测试与调用路径是否足以证明这些边界稳定

## 当前清单

基于当前代码状态，可以先把对象分成三类。

### 已经较稳定、优先验证后可抽取

- `AcpRequestEnvelope`
  - 目前只是 `request_type` + `payload`
  - 作为 adapter-facing request contract，相对纯净
- `AcpResponseEnvelope`
  - 目前只是 `status` + `payload` + `error`
  - 作为 adapter-facing response contract，相对纯净
- `AcpAdapter` protocol
  - 是这次 issue 最值得验证的一层
  - 但要先确认其中哪些方法属于纯 adapter-facing contract，哪些其实夹带了 runtime lifecycle 语义

### 可抽取潜力存在，但需要更保守判断

- `AcpConfigState`
  - `configured_enabled` 很像纯配置派生结果
  - 但它当前直接依赖 `RuntimeAcpConfig`
  - 如果迁移，最好避免 capability layer 继续直接反向依赖 runtime config
- `AcpAdapterState`
  - 其中一部分字段像纯状态模型
  - 但 `mode` / `configured` / `available` / `status` / `last_error` 已明显带有 runtime ownership 色彩
  - 很可能需要拆成“纯状态片段”和“runtime-owned 聚合状态”两层，而不是整类搬迁

### 当前不应抽取，继续留在 `runtime`

- `AcpRuntimeEvent`
  - 事件类型和 payload 直接绑定 runtime event flow
- `DisabledAcpAdapter`
- `ManagedAcpAdapter`
- `build_acp_adapter()`
  - 这些都直接属于 runtime-managed control-plane 行为
- `connect()` / `disconnect()` / `fail()` 相关 lifecycle 实现
  - 当前明显是 runtime ownership，不适合能力层提前接管

## 当前判断

按现在仓库的实现强度，`#97` 更像一个“先写清楚边界，再小步抽取”的任务，而不是一次性重构。

比较稳妥的判断是：

- `AcpRequestEnvelope` / `AcpResponseEnvelope` 大概率已经接近稳定
- `AcpAdapter` protocol 需要先判断方法集合是否过于 runtime-owned
- `AcpConfigState` / `AcpAdapterState` 不适合整类直接迁出
- adapter 实现与 lifecycle 逻辑当前不应离开 `runtime/acp.py`

## 如何完成 `#97`

推荐按下面顺序推进。

### 第一步：先写出验证结论

先在代码和测试基础上形成一份明确结论，回答：

- 哪些 envelope / contract 已稳定
- 哪些 state/model 仍耦合 runtime lifecycle
- 为什么某些对象现在不能抽

如果没有这一步，后面的迁移很容易变成“结构先行”。

### 第二步：最小化抽取稳定 primitive

如果第一步验证成立，优先只抽这些相对稳定的对象：

- `AcpRequestEnvelope`
- `AcpResponseEnvelope`
- 经过收缩后的 adapter-facing protocol contract
- 必要的 schema / helper

优先策略应该是：

- 小范围迁移
- runtime 继续消费新位置的定义
- 行为保持不变

### 第三步：保留 runtime-owned 聚合与 lifecycle

以下内容应继续保留在 `runtime/acp.py`：

- adapter 管理与装配
- disabled / managed 行为分支
- runtime event emission
- connect / disconnect / fail lifecycle
- availability / recovery / status ownership

### 第四步：用测试证明边界没有被破坏

完成 `#97` 时，至少应确保：

- 现有 ACP 行为测试语义保持一致
- runtime 仍然通过同样的边界消费 ACP contract
- 新的 `src/voidcode/acp/` 不只是占位，而是真正承载已验证稳定的定义

## 不应把 `#97` 做成什么

这条 issue 不应演变成：

- 现在就把 ACP 全量迁入 `src/voidcode/acp/`
- 提前设计未来网络/传输层扩展
- 在 runtime 语义未稳定前发明过大的 capability-layer API 面
- 为了“目录整洁”而提前固化尚未验证的边界

## 当前建议

如果现在开始实现，最稳的做法是：

1. 先审查 `runtime/acp.py` 与测试，写出“稳定 / 不稳定”结论
2. 只抽取最小稳定 contract 集合
3. 让 `runtime/acp.py` 改为消费 `voidcode.acp` 的稳定定义
4. 保持 runtime lifecycle 行为完全不变

## 当前落地状态

当前目录已经开始承载最小稳定 contract 集合：

- `AcpRequestEnvelope`
- `AcpResponseEnvelope`
- `AcpRequestHandler`

这些定义现在位于：

- [contracts.py](./contracts.py)
- [__init__.py](./__init__.py)

`runtime/acp.py` 继续保留 runtime-owned lifecycle、state 聚合和 adapter 实现，并消费这里暴露出的稳定 contract。

一句话说，`#97` 的完成标准不是“ACP 被搬走了”，而是：

`src/voidcode/acp/` 承载了已经被证明稳定的 ACP contract/schema 层，而 `runtime/acp.py` 继续持有 runtime-managed control-plane 行为。
## 2026-04-17 Validation Update

The current validation result for `#97` is:

- Stable and now extracted into `src/voidcode/acp/`:
  `AcpConfigState`, `AcpRequestEnvelope`, `AcpResponseEnvelope`, `AcpRequestHandler`
- `AcpConfigState` has been reduced to a capability-layer model plus a runtime-agnostic helper:
  `configured_enabled` and `from_enabled()`
- Still runtime-owned:
  `AcpAdapterState`, `AcpRuntimeEvent`, `DisabledAcpAdapter`, `ManagedAcpAdapter`,
  `build_acp_adapter()`, and the connect/disconnect/fail lifecycle
- Validation coverage:
  `tests/unit/acp/test_acp.py`,
  `tests/unit/runtime/test_acp.py`,
  `tests/unit/runtime/test_runtime_service_extensions.py`

This means `src/voidcode/acp/` is no longer just a placeholder directory. It now
holds the ACP contract/schema layer that has been validated against real runtime
usage, while `runtime/acp.py` continues to own the runtime control-plane behavior.
