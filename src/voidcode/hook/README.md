# `voidcode.hook`

这里是 VoidCode 的 hook 能力层。

## 定位

`voidcode.hook` 承载 hook 配置、hook preset catalog 与执行器逻辑，为 runtime 提供一致的 lifecycle / tool hook 扩展点。

## 负责什么

- hook 配置模型
- builtin hook preset catalog，用于校验 agent preset hook refs
- hook 执行器与执行协议
- 当前 runtime-owned `pre_tool` / `post_tool` 执行面
- 当前已落地的 session/background-task lifecycle hook phases 配置面

## 不负责什么

- session 生命周期管理
- 客户端事件协议设计
- tool/provider/skill/formatter 的具体业务语义
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

## Surface catalog

17 个 runtime hook surface 的配置字段、foreground/background phase 与事件名称由
`src/voidcode/hook/surfaces.py` 的 `HOOK_SURFACE_DESCRIPTORS` 统一描述。`RuntimeHooksConfig`
仍保留现有 `pre_tool`、`on_session_start`、`on_background_task_*` 等外部字段，catalog
只负责内部查找，不改变配置形状、argv 执行、失败策略或事件顺序。`plan.py` 与
`executor.py` 也从同一 catalog 读取 phase/event metadata，避免新增 surface 时重复维护
多份映射。

## Declarative execution plan (v2)

`materialize_hook_plan()` resolves explicit `RuntimeHooksConfig` surfaces into a runtime-owned, frozen `ResolvedHookPlan` with a `plan_hash`. Each binding carries stable identity, event, command, deterministic contiguous `order`, scope, phase, failure policy, timeout, and payload schema. Removed declaration fields are not part of the dataclass, payload, parser, or canonical hash.

Agent hook preset refs remain guidance-only plan metadata. The persisted v2 snapshot is self-contained; restoration rejects older schema versions rather than reinterpreting them through the current catalog.

The existing hook executor consumes the resolved plan; this contract does not add dynamic handlers, middleware, agent messaging, memory, or a new URI namespace.

plan 不引入第二套 executor：现有 `run_tool_hooks` / `run_lifecycle_hooks` 可接受 resolved plan，并继续承担 foreground failure 与 background post-truth observer 语义。当前不支持自定义 preset command、动态 handler 注册、agent bus、middleware chain、memory 或新的 URI namespace；command stdout、payload body 与 secrets 不是 plan snapshot 内容。
