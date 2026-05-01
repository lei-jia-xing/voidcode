# `voidcode.hook`

这里是 VoidCode 的 hook 能力层。

## 定位

`voidcode.hook` 承载 hook 配置、hook preset catalog 与执行器逻辑，为 runtime 提供一致的 lifecycle / tool hook 扩展点。

## 负责什么

- hook 配置模型
- builtin hook preset catalog，用于校验 agent preset hook refs
- hook 执行器与执行协议
- 与格式化 preset 相关的 hook 支撑逻辑
- 当前 runtime-owned `pre_tool` / `post_tool` 执行面
- 当前已落地的 session/background-task lifecycle hook phases 配置面

## 不负责什么

- session 生命周期管理
- 客户端事件协议设计
- tool/provider/skill 的具体业务语义
- background task orchestration 与 leader notification
- agent role 执行、delegation routing 或 multi-agent orchestration

## 边界关系

runtime 负责决定何时执行 hook、如何把 hook 纳入审批/恢复语义；`voidcode.hook` 负责提供可复用的 hook primitives、配置模型、agent hook preset catalog 与执行能力。

需要区分两类 hook 概念：

- **hook preset**：`src/voidcode/hook/presets.py` 中的 builtin guidance / guard / continuation catalog，供 `AgentManifest.preset_hook_refs` 与 runtime `agent.hook_refs` 校验使用；
- **runtime lifecycle hook**：`RuntimeHooksConfig` 中的 `session_start`、`pre_tool`、`background_task_completed` 等 command execution surface。

hook preset 表达的是 agent 角色 intent，不自动执行 shell command，也不替代 runtime lifecycle hook surface。

当前需要特别注意的是：本层已经具备 `session_start`、`session_end`、`session_idle`、`background_task_completed`、`background_task_failed`、`background_task_cancelled` 与 `delegated_result_available` 这些 richer lifecycle hook phases 的**配置边界**；但它们并不自动等价于完整的 async agent substrate。对于未来 async agent 设计，hook 仍然只能是通知与干预面，不能替代 background task / session lifecycle substrate。

## 当前状态

hook 已经是相对独立的能力层，是后续 capability-layer 文档化的参考样板之一。agent hook preset contract 见 [`docs/contracts/agent-hook-presets.md`](../../../docs/contracts/agent-hook-presets.md)。
