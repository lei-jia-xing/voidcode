# 审批流契约

来源 Issue：#15

## 目的

定义围绕具有写入能力或高风险操作的 MVP 受控执行契约及审批决策。

## 状态

当前运行时已经实现完整的受控审批流：`allow` / `deny` / `ask` 三种模式都已可用，未决审批会被持久化，并且 approval resume 现在拥有运行时内部的持久化 checkpoint anchor。

## 当前代码锚点

- `docs/architecture.md` 中将权限责任分配给运行时
- 当前运行时在 `src/voidcode/runtime/service.py` 中发出 `runtime.permission_resolved`
- 当前 payload 包含：
  - `tool`
  - `decision`

## MVP 决策词汇表

- `allow`：继续执行
- `deny`：不继续执行
- `ask`：暂停执行，直到记录显式的客户端或操作员决策

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
    "tool": "write_file",
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
- 执行是根据记录的决策恢复还是终止

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

计划的 MVP 行为：
- 运行时可以在 `ask` 时暂停
- 客户端可以根据运行时状态处理审批
- 持久化会话可以重放审批历史并正确恢复

## 持久化与恢复预期

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
