# Agent Hook Preset Contract

## 状态

当前仓库已经引入 builtin agent hook preset catalog。它为 `AgentManifest.preset_hook_refs` 与 runtime config 中的 `agent.hook_refs` 提供类型化、可校验的引用集合。

这份契约定义的是 **agent preset intent**：一个 agent 角色默认希望携带哪些 guidance / guard / continuation 关注点。它不是 lifecycle hook execution contract；真实 hook 执行时机仍由 runtime 与 [`runtime-lifecycle-hooks.md`](./runtime-lifecycle-hooks.md) 管理。

## 目的

在引入 hook preset catalog 之前，`agent.hook_refs` 曾借用 formatter preset 名称做校验，这会把“agent role guidance intent”和“formatter/hook command config”混在一起。

现在的目标是把两者拆开：

- `src/voidcode/hook/presets.py` 负责声明 builtin hook preset catalog；
- `src/voidcode/agent/builtin.py` 负责校验 builtin agent manifest 的 `preset_hook_refs`；
- `src/voidcode/runtime/config.py` 负责校验用户/runtime config 中的 `agent.hook_refs`；
- runtime 仍然拥有是否、何时、如何 materialize 或执行这些 preset 的最终控制权。

## 当前 builtin presets

当前 builtin hook preset refs 是：

| Ref | Kind | 语义 |
|-----|------|------|
| `role_reminder` | `guidance` | 提醒 active agent 遵守当前角色边界、工具范围与输出责任。 |
| `delegation_guard` | `guard` | 约束 delegation 必须通过 runtime-owned task routing、child preset 与治理路径。 |
| `background_output_quality_guidance` | `guidance` | 引导 agent 有界读取 background output，并先总结再行动。 |
| `delegated_retry_guidance` | `guard` | 引导 failed/cancelled/interrupted delegated task 的 retry 必须显式、leader-owned，并通过 runtime-owned `background_retry` 执行。 |
| `delegated_task_timing_guidance` | `guidance` | 提醒 agent 把 delegated background task 当作异步引用处理，只在真的需要时检查状态或阻塞等待。 |
| `todo_continuation_guidance` | `continuation` | 引导多步任务保持 todo 状态，并用剩余 todo 继续下一步。 |

## 输入来源

runtime 可以从两个位置看到 agent hook preset refs：

1. builtin agent manifest 的 `preset_hook_refs`
2. runtime config / request metadata 解析后的 `RuntimeAgentConfig.hook_refs`

这些 refs 必须能被 builtin hook preset catalog 解析。未知 ref 应 fail fast，而不是被静默当作 formatter preset、shell command、lifecycle hook surface 或自由文本。

## 与 runtime lifecycle hooks 的区别

`agent hook preset` 与 `runtime lifecycle hook` 是两个不同层次：

- agent hook preset：声明某个 agent role 希望携带的 guidance / guard / continuation intent；
- runtime lifecycle hook：在具体 runtime phase 上执行用户配置的 command，并发出 runtime event。

因此：

- `role_reminder` 不是一个 `session_start` command；
- `delegation_guard` 不是一个 agent-to-agent bus；
- `todo_continuation_guidance` 不是自动 continuation loop；
- hook preset catalog 不能绕过 session、approval、permission、tool registry 或 background task truth。

## 合并与 materialization 规则

当前实现只负责 catalog 与 validation。后续 materialization 必须遵守以下规则：

1. builtin manifest refs 与 runtime config refs 都只能引用 catalog 中存在的 preset；
2. runtime 可以把 resolved preset snapshot 持久化到 session metadata 或 provider context，但不能让 agent 层决定执行时机；
3. materialized guidance 只能收窄或提醒角色行为，不能扩大 tool allowlist、permission 或 delegation budget；
4. replay 应展示历史 truth，不能用新的 hook preset catalog 重新解释旧 session；
5. 如果未来支持用户自定义 hook preset，必须先定义独立 schema 与 precedence，不能复用 formatter preset 命名空间。

## 非目标

这份契约不定义：

- 任意拓扑 multi-agent orchestration；
- agent-to-agent messaging；
- lifecycle hook command execution；
- formatter preset 配置；
- skill loading 语义；
- MCP / ACP lifecycle；
- 自动 continuation loop。

## 验收检查点

实现满足这份契约时，至少应能验证：

1. builtin hook preset catalog 暴露稳定 ref 集合；
2. builtin agent manifest 的 `preset_hook_refs` 引用未知 preset 时 fail fast；
3. runtime config 的 `agent.hook_refs` 引用未知 preset 时 fail fast；
4. `agent.hook_refs` 不再依赖 `hooks.formatter_presets`；
5. hook preset API 可从 `voidcode.hook` 导入。

## 相关代码

- `src/voidcode/hook/presets.py`
- `src/voidcode/hook/__init__.py`
- `src/voidcode/agent/builtin.py`
- `src/voidcode/runtime/config.py`
- `tests/unit/hook/test_presets.py`
- `tests/unit/agent/test_builtin.py`
- `tests/unit/runtime/test_runtime_config.py`
