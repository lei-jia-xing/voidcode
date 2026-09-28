# `voidcode.lsp`

这里是 LSP 能力层，承载 LSP 的预配置、语言支持定义、配置 schema 和注册中心逻辑。它是 runtime 依赖的活代码，不是占位目录。

## 负责什么

- 语言到 LSP server 的映射关系（`presets.py` 的 builtin server presets）
- 默认 LSP preset 和可复用的 server 定义
- 配置归一化与校验辅助逻辑（`registry.py`）
- workspace root 探测（`roots.py::discover_workspace_root`）
- 不依赖 runtime session 状态的 LSP 能力契约（`contracts.py`）

## 模块

- `contracts.py`：`LspServerPreset`、`LspServerConfigOverride`、`ResolvedLspServerConfig`
- `presets.py`：builtin server presets 与 `get_builtin_lsp_server_preset` / `has_builtin_lsp_server_preset`
- `registry.py`：`resolve_lsp_server_config` / `resolve_lsp_server_configs` / `match_lsp_servers_for_path` / `derive_workspace_lsp_defaults`
- `roots.py`：`discover_workspace_root`

`__init__.py` 重新导出上述符号；`runtime/config.py`、`runtime/lsp.py`（导入 `resolved/roots/registry` 定义）与 `doctor/checker.py` / `doctor/doctor.py` 都直接从 `voidcode.lsp` 导入这些定义。与此同时，`runtime/coordinators/` 从 `voidcode.runtime.lsp`（runtime 集成层）导入 `LspManager`。

## 不负责什么

- 进程生命周期与 stdio 管理
- 从 runtime 入口发起的请求路由
- runtime 事件发射
- session 持久化或 resume 状态

## 与 runtime 的边界

`src/voidcode/runtime/lsp.py` 是 runtime 集成层。它导入 `voidcode.lsp` 中可复用的定义与 schema（`presets` / `registry` / `roots` / `contracts`），同时继续持有 runtime 管理的生命周期、事件和 session 生效真相。工具面入口位于 `src/voidcode/tools/lsp.py`。
