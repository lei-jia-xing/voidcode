# `voidcode.lsp`

这里是 LSP 能力层的预期目录，用来承载 LSP 的预配置、语言支持定义、配置 schema 和注册中心逻辑。

## 负责什么

- 语言到 LSP server 的映射关系
- 默认 LSP preset 和可复用的 server 定义
- 纯粹的配置归一化与校验辅助逻辑
- 不依赖 runtime session 状态的 LSP 能力契约

## 不负责什么

- 进程生命周期与 stdio 管理
- 从 runtime 入口发起的请求路由
- runtime 事件发射
- session 持久化或 resume 状态

## 与 runtime 的边界

`src/voidcode/runtime/lsp.py` 仍然是 runtime 集成层。它应当依赖 `voidcode.lsp` 中可复用的定义与 schema，同时继续持有 runtime 管理的生命周期、事件和 session 生效真相。

## 当前状态

这个目录目前是规划中的能力层。现有实现仍然位于 `src/voidcode/runtime/lsp.py` 与 `src/voidcode/tools/lsp.py`。
