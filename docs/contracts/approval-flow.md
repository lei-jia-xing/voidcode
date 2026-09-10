# 审批流契约

来源 Issue：#15

## 目的

定义围绕具有写入能力或高风险操作的 MVP 受控执行契约及审批决策。

## 状态

当前运行时已经实现完整的受控审批流：`allow` / `deny` / `ask` 三种模式都已可用，未决审批会被持久化，并且 approval resume 现在拥有运行时内部的持久化 checkpoint anchor。

## 当前代码锚点

- `src/voidcode/runtime/service.py` 中的权限解析与事件发射是运行时实现锚点
- 当前运行时在 `src/voidcode/runtime/service.py` 中发出 `runtime.permission_resolved`
- 当前 payload 包含：
  - `tool`
  - `decision`

## MVP 决策词汇表

- `allow`：继续执行
- `deny`：不执行该工具调用，将拒绝作为工具级错误反馈给模型，会话继续处于
  `running` 状态；模型可以重新规划或解释约束
- `ask`：暂停执行，直到记录显式的客户端或操作员决策

### 拒绝语义

权限拒绝是执行约束，而非致命运行时错误。当用户或策略拒绝某个工具调用时：

- 该拒绝应作为工具结果/错误负载返回给模型，关联到原始工具调用
- 会话应保持 `running`，除非运行时确实无法继续
- 拒绝应通过运行时事件可观察（`runtime.approval_resolved`），但不应自动触发
  `runtime.failed`
- 终端失败应保留给运行时错误、取消、无效会话状态或模型无法继续的情况


## 命令执行授权

`shell_exec` 与后台进程启动统一通过 runtime 的执行能力授权，默认 `ask`；显式 `allow` 允许任意命令，显式 `deny` 与只读模式禁止执行。运行时不再解析 shell 字符串推断危险操作或外部文件访问。结构化文件工具的路径权限不受此变更影响。

## 审批请求契约

审批请求必须至少能表示以下内容：

- `request_id`
- `session_id`
- `sequence`
- `tool`
- `reason` 或风险上下文
- 建议的参数（arguments）或目标摘要
- 当前策略上下文

这应作为一个运行时事件发出，而不是作为客户端专有的 UI 状态。

### 计划的审批请求形状

MVP 契约应至少支持一个如下形状的 `runtime.approval_requested` 运行时事件：

```json
{
  "event_type": "runtime.approval_requested",
  "source": "runtime",
  "session_id": "session-123",
  "sequence": 4,
  "payload": {
    "request_id": "approval-1",
    "tool": "write",
    "decision": "ask",
    "arguments": {
      "path": "README.md"
    },
    "target_summary": "write README.md",
    "reason": "write-capable tool invocation",
    "policy": {
      "mode": "ask"
    }
  }
}
```

信封（envelope）字段意图：

- `event_type`：待处理审批请求的 `runtime.approval_requested`
- `source`：`runtime`，因为审批由运行时拥有
- `session_id`：所属会话
- `sequence`：事件流中的排序标记

Payload 字段意图：

- `request_id`：后续处理和重放的稳定标识符
- `tool`：等待审批的工具名称
- `decision`：待处理审批请求的 `ask`
- `arguments`：建议的工具参数或脱敏后的等价内容
- `target_summary`：面向客户端的人类可读目标摘要
- `reason`：为什么需要审批
- `policy`：与决策相关的策略上下文

## 审批处理（Resolution）契约

审批处理结果必须能够记录：

- `session_id`
- 正在处理的请求
- `decision`：`allow` / `deny`
- 可选的操作员说明（note）
- 足以用于恢复/重放的时间戳或排序标记

### 计划的审批处理形状

MVP 契约应支持一个至少包含以下内容的处理运行时事件：

```json
{
  "event_type": "runtime.approval_resolved",
  "source": "runtime",
  "session_id": "session-123",
  "sequence": 5,
  "payload": {
    "request_id": "approval-1",
    "decision": "allow",
    "note": "approved from tui"
  }
}
```

信封字段意图：

- `event_type`：针对已处理审批决策的 `runtime.approval_resolved`
- `source`：`runtime`，因为处理由运行时拥有
- `session_id`：所属会话
- `sequence`：足以用于重放和恢复的排序标记

Payload 字段意图：

- `request_id`：将处理结果与原始审批请求关联
- `decision`：最终决策，`allow` 或 `deny`
- `note`：可选的操作员或客户端说明

### 客户端向运行时的决策提交

客户端应将审批决策作为运行时拥有的操作（runtime-owned action）返回给运行时，而不是直接执行工具。

最小的客户端提交形状应为：

```json
{
  "request_id": "approval-1",
  "decision": "allow",
  "note": "approved from web"
}
```

运行时负责验证：

- 请求是否仍然存在
- 请求是否属于当前活跃会话
- 请求是否已被处理过
- 执行是根据记录的决策恢复、生成工具级拒绝反馈，还是因真正的运行时错误终止

`POST /api/sessions/{id}/approval` 提交决策并继续执行，返回下一次暂停或执行结束时的会话快照；它不是仅记录决策的接口，客户端无需再调用 `/resume`。后续出现不同 `request_id` 的审批属于新的暂停，不能重复使用已处理的审批请求。

同步审批恢复与流式审批恢复都必须注册活跃运行并传递中断信号，在结束或异常退出时释放注册。question 回答后的同步与流式继续执行遵守相同生命周期。HTTP 审批恢复与 question 回答在工作线程执行，不能阻塞事件循环上的状态查询、事件跟随或取消请求；HTTP 请求取消也不能提前关闭仍由工作线程使用的 runtime。

## MVP 不变量

- 审批状态属于运行时，而非客户端
- 写入/风险工具执行不得绕过审批契约
- `ask` 需要一个可恢复的暂停状态
- 客户端必须能够区分待处理审批与已处理审批

## 当前 vs 计划行为

当前已实现行为：
- 只读工具仍通过 `runtime.permission_resolved` 直接继续执行
- 写入/高风险工具在 `ask` 时会进入可持久化的等待状态
- `allow` / `deny` / `ask` 的恢复路径都由运行时负责
- approval resume 可以优先使用运行时内部 checkpoint anchor 恢复，而不是只依赖重新扫描历史事件
- `deny` 决策会将权限拒绝作为工具级错误反馈给模型，并通过
  `runtime.tool_completed` 公开 `status="error"` 结果；会话不会仅因为拒绝而自动进入
  `failed`

计划的 MVP 行为：
- 运行时可以在 `ask` 时暂停
- 客户端可以根据运行时状态处理审批
- 持久化会话可以重放审批历史并正确恢复

### TODO：用 OS 级 syscall 访问控制替代 shell 推断

当前已移除基于命令字符串的危险操作识别与文件路径推断。前台 shell 与后台命令统一按任意执行能力授权，默认 `ask`，显式 `allow` 不再附加危险命令黑名单；显式 command 匹配规则仍可用于授权，但不是文件隔离保证。后续由 runtime 管理的 OS 级 syscall 拦截与访问控制实现执行隔离；该方案尚未实现，当前没有工作区 sandbox。

- 统一覆盖前台 shell、后台进程及其子进程；不能通过更换执行工具绕过同一权限边界。
- 区分 syscall 观察与强制执行：仅记录调用（如 `strace`）不足以阻止访问，必须在有副作用的操作发生前实施授权或拒绝。
- 文件访问约束应基于 OS 实际解析的对象，处理符号链接、目录文件描述符、路径替换竞态与子进程继承；不能重新退化为命令字符串或 syscall 路径参数的简单匹配。具体拦截机制及平台支持需另行验证。
- 保留执行能力的 `ask` / `allow` / `deny`、只读模式、审批参数绑定，以及中断、超时和进程清理。无隔离能力时，`allow` 表示允许任意命令执行，不应向用户暗示工作区沙箱。
- 结构化文件工具继续使用明确的路径权限检查；shell 超时和非交互环境设置不属于待删除的权限推断逻辑。

## 持久化与恢复预期

SQLite schema 15 将每条 delivery 去重记录绑定到事件序号。中断恢复截断事件尾部时，在同一事务中只回收对应尾部的 delivery claims，保留前缀去重记录。旧 schema 不兼容、不自动迁移或重建；版本不匹配时明确报错并保留原库。

持久化的会话状态必须能够保存：

- 未处理的审批请求
- 已处理的审批历史
- 与每个 `request_id` 关联的最终决策
- 足够的排序信息，以便按顺序重放审批历史

恢复行为必须支持两种情况：

- 未处理的审批请求：会话在等待状态下恢复，客户端仍可对该挂起请求采取行动
- 已处理的审批请求：会话重放将决策显示为历史事件流的一部分

对于运行时内部实现，未处理审批还可以拥有一个持久化 checkpoint anchor，用于在进程重启后恢复继续执行所需的最小状态。该 checkpoint 不是客户端提交 shape 的一部分，也不会替代客户端可见的事件历史重放。

## 相关客户端

- CLI 可以以文本形式显示审批事件
- TUI 应支持直接的审批交互
- Web 客户端应从运行时事件和持久化状态中渲染审批状态

## 非目标

- 多用户审批工作流
- 基于角色的策略系统
- post-MVP 的高级审批策略矩阵

## 验收检查点

- 具有写入能力的请求在执行前可以表示为待处理审批
- 恢复的会话能够准确保留未处理或已处理的审批状态
- 客户端无需自定义逻辑即可解释运行时状态
