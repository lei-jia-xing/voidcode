# `voidcode.acp`

这里是 VoidCode 当前已经落地的 ACP capability layer。

它的职责不是接管 runtime 里的 ACP lifecycle，而是承载那些已经被运行时路径验证过、并且可以脱离 runtime ownership 复用的 **contract / schema / helper**。

换句话说，`voidcode.acp` 现在已经不是一个纯占位目录，但它也不是“ACP 已经被完整迁出 runtime”的信号。

## 当前定位

当前仓库里，ACP 的合理分层是：

- `src/voidcode/acp/`：稳定的 capability-layer contract / schema
- `src/voidcode/runtime/acp.py`：runtime-owned lifecycle / adapter management / event ownership

这和仓库对 `runtime` 作为系统控制面的整体判断一致：

- runtime 持有连接、状态、事件、恢复与治理真相
- capability layer 只承载已经稳定、可复用、且不依赖 runtime 生命周期所有权的定义

## 当前已经承载什么

当前 `voidcode.acp` 已经承载的稳定定义包括：

- `AcpConfigState`
- `AcpDelegatedExecution`
- `AcpRequestEnvelope`
- `AcpResponseEnvelope`
- `AcpEventEnvelope`
- `AcpRequestHandler`
- `AcpEventPublisher`

这些定义位于：

- [`contracts.py`](./contracts.py)
- [`__init__.py`](./__init__.py)

它们当前的语义是：

- `AcpConfigState`
  - 只保留 capability-layer 需要的最小配置派生结果
  - 当前字段只有 `configured_enabled`
  - 提供 `from_enabled()` 作为 runtime-agnostic helper
- `AcpRequestEnvelope`
  - adapter-facing request envelope
  - 当前包含 `request_type`、request/session/parent correlation，以及 delegation-aware payload
- `AcpResponseEnvelope`
  - adapter-facing response envelope
  - 当前包含 `status`、request/session/parent correlation、delegation-aware payload 与 `error`
- `AcpEventEnvelope`
  - adapter-facing event envelope
  - 当前支持 `parent_session_id` 与 `delegation`
- `AcpRequestHandler`
  - 最小 adapter-facing protocol contract
  - 当前只要求 `request(envelope)`
- `AcpEventPublisher`
  - 最小 adapter-facing event publish contract
- `AcpDelegatedExecution`
  - capability-layer 的 delegation identity/correlation shape
  - 与 runtime-owned delegated lifecycle truth 对齐，但不夺走 lifecycle ownership

## 不负责什么

`voidcode.acp` 当前**不**负责：

- connect / disconnect / fail lifecycle
- runtime-owned availability / status ownership
- session persistence / resume semantics
- runtime event emission
- adapter 装配与治理
- recovery / startup / handshake flow
- agent-to-agent messaging semantics
- multi-agent routing plane
- delegated execution lifecycle ownership

这些能力仍然必须留在 `runtime/`，而不是提前抽成 capability-layer API。

## 与 `runtime/acp.py` 的边界

[`src/voidcode/runtime/acp.py`](../runtime/acp.py) 当前仍然是 runtime-owned ACP control plane。

它继续持有：

- `_MemoryAcpTransport`
- `AcpRuntimeEvent`
- `AcpAdapterState`
- `AcpAdapter`
- `DisabledAcpAdapter`
- `ManagedAcpAdapter`
- `build_acp_adapter()`
- connect / disconnect / fail / drain-events lifecycle

当前 runtime 里的 ACP 行为是：

- 仅支持 runtime-owned `memory` transport
- 在 run / approval-resume 启动阶段 connect + handshake
- 在 finalized run / approval-resume 结束路径上 disconnect；某些 waiting / approval-blocked 路径也会断开 ACP 并持久化运行态，但不会在当次响应里发出 `runtime.acp_disconnected`
- 通过 `runtime.acp_connected` / `runtime.acp_failed` 发出事件，并在正常 finalized disconnect 路径上发出 `runtime.acp_disconnected`
- 在 delegated child lifecycle 过程中发出 `runtime.acp_delegated_lifecycle`
- 将运行态写入 `session.metadata["runtime_state"]["acp"]`

因此，ACP 现在应被理解为：

> 一个已经进入 runtime-managed transport / lifecycle 路径、并且已经具备 delegation-aware contract / event envelope 的受管能力边界。

## 它现在不是什么

当前 ACP 还不是：

- agent-to-agent messaging bus
- multi-agent routing layer
- supervisor / worker handoff transport
- 当前可直接产品化的 agent control plane

这点非常重要。对 VoidCode 当前阶段而言，ACP 仍然是**边界预留**，不是已经成熟的协作底座。

即使未来想做 async leader / worker 协作，仅有 ACP 也不够；更靠前的前提通常仍然是：

- background task truth
- parent / child session linkage
- leader notification path
- result retrieval / transcript recovery
- approval / resume correctness across delegated work

只有这些 runtime capability 先成立，ACP 才更像是协作控制面的放大器，而不是空中楼阁。

## 为什么没有继续大规模迁移

`#97` 的核心不是“把 ACP 全搬走”，而是先验证哪些定义已经稳定，再做小范围抽取。

当前验证结论是：

- 已稳定并适合留在 capability layer：
  - `AcpConfigState`
  - `AcpRequestEnvelope`
  - `AcpResponseEnvelope`
  - `AcpRequestHandler`
- 仍然 runtime-owned：
  - `AcpAdapterState`
  - `AcpRuntimeEvent`
  - `DisabledAcpAdapter`
  - `ManagedAcpAdapter`
  - `build_acp_adapter()`
  - connect / disconnect / fail lifecycle

这意味着当前仓库已经完成了一轮 boundary-first 的最小落地，但还没有进入“更宽 ACP capability surface”的阶段。

## 验证覆盖

当前 ACP 边界由以下测试共同覆盖：

- `tests/unit/acp/test_acp.py`
- `tests/unit/runtime/test_acp.py`
- `tests/unit/runtime/test_runtime_service_extensions.py`

这些测试的意义不是证明 ACP 已经是成熟控制面，而是证明：

- capability-layer contracts 已经能被 runtime 稳定消费
- runtime-owned lifecycle 语义仍保持不变
- boundary 抽取没有破坏现有 run / resume / event 行为

## 与 agent 层的关系

ACP 和 agent 不是同一个维度。

- `voidcode.agent` 负责声明角色 preset / capability intent
- `voidcode.runtime` 负责执行治理与 capability lifecycle truth
- `voidcode.acp` 负责稳定的 ACP contract / schema 边界

因此，当前不应把 “已有 `voidcode.acp` 目录” 误读成 “多 agent 只差一点 transport”。

更准确的说法是：

> ACP 已经有了 delegation-aware contract 层与 runtime-managed lifecycle 基线，并参与当前 delegated execution observability；但距离真正的 agent coordination control plane 仍然很远。

## 相关文档

- [`docs/architecture.md`](../../../docs/architecture.md)
- [`docs/current-state.md`](../../../docs/current-state.md)
- [`docs/roadmap.md`](../../../docs/roadmap.md)
- [`docs/agent-architecture.md`](../../../docs/agent-architecture.md)
- [`docs/contracts/runtime-config.md`](../../../docs/contracts/runtime-config.md)
- [`docs/contracts/runtime-events.md`](../../../docs/contracts/runtime-events.md)
