# Runtime Lifecycle Hooks Contract

来源 Issue：#170

## 目的

定义当前已经在配置层声明的 richer lifecycle hook phases，何时进入 runtime-owned execution path，以及它们如何与 session / background task / delegated result truth 对齐。

这份契约的重点不是“支持更多 hook 名称”，而是：

> 哪些 lifecycle phases 一旦被声明为 runtime hook surface，就必须具有清晰、可执行、可恢复的运行时语义。

## 状态

当前仓库已经具备：

- `pre_tool` / `post_tool` 的 runtime-owned 执行面
- richer lifecycle hook phases 的配置边界：
  - `session_start`
  - `session_end`
  - `session_idle`
  - `background_task_registered`
  - `background_task_started`
  - `background_task_progress`
  - `background_task_completed`
  - `background_task_failed`
  - `background_task_cancelled`
  - `background_task_notification_enqueued`
  - `background_task_result_read`
  - `delegated_result_available`

但这些 richer phases 当前仍不等于已经进入真实 runtime execution。

## 范围

这份契约只覆盖：

- richer lifecycle hook phases 的 runtime 触发点
- hook 与 runtime truth 的边界
- hook failure 的基本治理方式
- run / resume / replay 下的可接受语义

## 非目标

这份契约**不**定义：

- TUI 特定行为
- client-side hook execution
- message transform hooks
- multi-agent orchestration
- async agent substrate
- hook DSL 扩张

## 当前代码锚点

- `src/voidcode/hook/config.py`
- `src/voidcode/hook/README.md`
- `src/voidcode/runtime/service.py`
- `src/voidcode/runtime/storage.py`
- `src/voidcode/runtime/task.py`

## 核心原则

### Principle 1：Trigger 来自 runtime truth

所有 richer lifecycle hook phases 都必须由 runtime 根据真实 session / background task 状态触发。

它们不能由：

- 客户端猜测触发
- hook 自身链式触发
- graph prompt 文本暗示触发

### Principle 2：Hook 是通知与干预面，不是 authority

hook 可以：

- 观察 runtime lifecycle
- 作为失败门槛中止当前动作
- 对外发出副作用

hook 不能：

- 重写 runtime truth
- 接管 session / task 状态机
- 替代 background task substrate

### Principle 3：Replay 不重放 hook side effects

replay 的职责是重放存量事件 truth，而不是重新执行 hook 命令。

因此 replay 只应展示 hook 已发生过的事件，不应再次执行 richer lifecycle hooks。

## Hook Surface 定义

### `session_start`

触发点：

- fresh run 进入真实 execution path 之前

语义：

- 表示本次运行已进入 active runtime session 执行阶段
- 只在当前运行开始时触发一次
- `resume` 重新进入 execution path 时**不重复触发**；resume 只对应新的 runtime-owned state transition

### `session_end`

触发点：

- 当前运行进入 terminal state 之后

语义：

- 表示本次运行已经完成 terminal outcome（成功或失败）
- 应晚于最终 output / failure truth 形成
- hook failure 只作为已完成 truth 上的附加失败信息，不得把已完成的 session 改写为新的 `runtime.failed`

### `session_idle`

触发点：

- 当前运行不再 active，但会话并未进入 terminal completion，而是进入等待外部动作的 idle/waiting 状态时

语义：

- 这是 runtime-owned waiting/idle 信号
- 不能由客户端的“UI 空闲”替代

### `background_task_registered`

触发点：

- background task 已写入 runtime 持久化状态、可被读取或取消之后。

### `background_task_started`

触发点：

- background task 从 queued 进入 running，child session id 已确定之后。

### `background_task_progress`

触发点：

- running background task 产生 runtime-visible child event 时。

语义：

- progress hook 不得回退 terminal task truth；如果 task 已 terminal，late progress 必须成为 no-op。

### `background_task_completed`

触发点：

- background task 状态机进入 completed

语义：

- 只在 runtime 确认 background task terminal completion 后触发

### `background_task_failed`

触发点：

- background task 状态机进入 failed

### `background_task_cancelled`

触发点：

- background task 状态机进入 cancelled

### `background_task_notification_enqueued`

触发点：

- parent-visible background/delegation notification event 已持久化到 parent session 后。

语义：

- 它必须晚于 terminal/waiting truth 持久化，不能早于 `background_output(task_id)` 可读取相同 task truth。

### `background_task_result_read`

触发点：

- `load_background_task_result` / `background_output` 读取 runtime-owned delegated result view 时。

### `delegated_result_available`

触发点：

- delegated/background result 已经作为 runtime truth 可被 leader-facing retrieval 消费时

语义：

- 它不是“child session 有输出”这么宽泛
- 它表示 leader-facing 结果可见性已经成立

## Failure 语义

### Pre-terminal lifecycle hooks

对于 `session_start` 等发生在执行过程中的 hooks：

- runtime 可以像 `pre_tool` 一样，把 hook failure 视为当前运行失败门槛
- failure 必须通过 runtime-owned failure path 对外可见

### Post-terminal / notification-like hooks

对于 `session_end`、`background_task_completed`、`delegated_result_available` 这类更接近通知面的 hooks：

- hook failure 不得回滚已经成立的 runtime truth
- runtime 可以记录失败事件或错误信息
- 但不能因为 post-truth hook failure 否认 session/task 已完成这一事实

当前实现约束：

- `session_start`、`session_idle` failure 仍可作为当前运行失败门槛
- `session_end`、`background_task_completed`、`background_task_failed`、`background_task_cancelled`、`delegated_result_available` failure 为 post-truth / notification-like failure，只记录 hook 错误，不改写 truth

## Resume / Replay 规则

### Resume

resume 重新进入 execution path 时，只允许触发与本次 resumed execution 新成立的 runtime truth 对应的 hooks。

也就是说：

- 不能因为 resume 而重复触发已经在此前运行中成立的 `session_start` / `background_task_completed`
- 只能为新的 runtime-owned state transition 触发新的 hook execution

当前已落地的 resume 语义：

- approval/question resume 不会重复执行 `session_start`
- resumed execution 若再次进入 waiting，可触发新的 `session_idle`
- resumed execution 若进入 terminal，可触发新的 `session_end`

### Replay

replay 不重新执行 hook，只重放历史事件。

## 与 Background Task Substrate 的边界

这份契约必须保持一个硬边界：

- richer lifecycle hooks 可以订阅 background task / delegated result truth
- richer lifecycle hooks 不能替代 background task truth 本身

也就是说，实现这些 hooks 不等于已经实现 async agent substrate。

## 与 Event Contract 的关系

如果 richer lifecycle hooks 进入真实执行面，则其事件化语义必须继续通过 runtime-owned event vocabulary 进入统一序列，而不是由客户端本地制造伪事件。

## 验收检查点

实现满足这份契约时，至少应能验证：

1. richer lifecycle hook phases 具有清晰、唯一的 runtime trigger
2. post-truth hooks 失败不会回滚已成立的 runtime truth
3. resume 不会重复触发旧的 lifecycle transition hooks
4. replay 不会重新执行 hook side effects
5. richer lifecycle hooks 仍然不被误解释为 async agent substrate 本身

## Payload Contract

lifecycle hook 的 payload 通过环境变量注入，当前约定如下：

- `VOIDCODE_HOOK_SURFACE`：当前 surface 名称
- `VOIDCODE_SESSION_ID`：触发该 hook 的 session id
- `VOIDCODE_HOOK_PAYLOAD_JSON`：完整、权威、无损的 payload JSON
- `VOIDCODE_*` convenience env：把 payload key 做大写与非字母数字转 `_` 后得到的兼容变量

其中 `VOIDCODE_HOOK_PAYLOAD_JSON` 是权威 contract；逐字段 `VOIDCODE_*` 变量仅作为兼容与易用镜像，可能出现 lossy key transform。
