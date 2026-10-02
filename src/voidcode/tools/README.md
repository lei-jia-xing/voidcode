# `voidcode.tools`

这里是 VoidCode 的工具能力层。

## 定位

`voidcode.tools` 承载内置工具实现与工具契约，供 runtime 统一注册、暴露和治理。它的目标是提供稳定的能力表面，而不是自己决定运行时策略。

## 负责什么

- 工具契约与参数/返回形状
- 内置工具的具体实现
- 与特定能力相关的最小适配层（例如 LSP tool adapter）

## 不负责什么

- 会话持久化与恢复
- 审批决策
- 客户端交互逻辑
- 独立的 capability lifecycle 管理

## 边界关系

产品执行路径由 runtime 注册并治理工具；core turn engine 只向 host 请求执行，不管理产品能力生命周期，客户端也不绕过 runtime。工具契约面向 runtime 和有明确 host 的独立调用，而不是直接面向 UI。

## 显式调用边界

每个工具接收 `invoke(call, *, context=ToolContext(...))`。`ToolContext`
位于 `voidcode.core.tool_context`，只携带真实 invocation facts 和实际需要的
中立资源接口；文件工具可用只有真实 `workspace` 的非持久化 host 调用。
session URI、task/process 等能力必须提供真实 session identity 与对应资源，
缺少能力会报错，不使用空 session ID 或隐藏 binder 代替。

`ToolDefinition.effects` 描述 `read/write/execute/network/spawn/session`
行为；它不是 permission grant。runtime 根据实际 operation class、路径、
规则和模式决策，目录的 `read_only` 仅是共享 read-tier predicate 的派生观察值。
没有 effects 声明时共享 classifier fail closed：不进入 read-tier，默认 replay
为 `never`，实际未知调用按 `execute` 受 mode/rules 治理。

task/process 的真实算法与生命周期位于 `runtime/execution/delegation` 和
`runtime/execution/process`。工具只获得当前 approved call、caller、workspace
和 cancellation 绑定的窄 callback；native、resume、inner invoke 共用
runtime canonical executor。runtime 的 execution-ownership lease 保持独立。

`read` 的 `content` 是显示摘要；实际已限界文本位于 `data.raw_content`，
文件和 archive 文本也有 `data.lines`。`raw_content` 只在真实文本正文存在时提供，
空文本可为 `""`；image/PDF 与未解码的二进制 archive member 没有该字段。
确定性最终输出与 continuity preview 共用中立结构化正文选择器，不解析显示文本；
已 clipped/pruned 的 `ToolResultView` 继续使用投影后的 `content`。


## 当前状态

这里已经是相对稳定的能力层。后续如果引入更多 capability package，它们通常应通过 runtime 和 tools 的既有边界接入系统。
