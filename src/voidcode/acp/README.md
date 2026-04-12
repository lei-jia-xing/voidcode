# `voidcode.acp`

这里是 ACP 能力层的预期目录。当 ACP 的语义足够稳定、能够独立站住时，这里可以承载 ACP 请求/响应契约、配置 schema 和可复用的 adapter-facing 模型。

## 负责什么

- 不与 runtime 强绑定的 ACP envelope 和状态模型定义
- 可复用的 ACP 配置 schema 辅助逻辑
- 稳定的 ACP 集成协议契约

## 不负责什么

- 连接生命周期管理
- runtime 管理的可用性状态
- session 持久化与 resume 行为
- runtime 事件发射与恢复流程

## 与 runtime 的边界

`src/voidcode/runtime/acp.py` 仍然是 runtime 管理的控制面。只有当 ACP 的纯契约与 schema 能够干净地从 runtime 生命周期 ownership 中分离出来时，才适合把更多实现迁入这个包。

## 当前状态

这个目录目前只是未来能力层的占位。现有实现仍然位于 `src/voidcode/runtime/acp.py`。
