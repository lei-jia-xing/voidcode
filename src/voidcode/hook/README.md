# `voidcode.hook`

这里是 VoidCode 的 hook 能力层。

## 定位

`voidcode.hook` 承载 hook 配置与执行器逻辑，为 runtime 提供一致的 tool pre/post 扩展点。

## 负责什么

- hook 配置模型
- hook 执行器与执行协议
- 与格式化 preset 相关的 hook 支撑逻辑
- 当前 runtime-owned `pre_tool` / `post_tool` 执行面

## 不负责什么

- session 生命周期管理
- 客户端事件协议设计
- tool/provider/skill 的具体业务语义
- background task orchestration 与 leader notification

## 边界关系

runtime 负责决定何时执行 hook、如何把 hook 纳入审批/恢复语义；`voidcode.hook` 负责提供可复用的 hook primitives 与执行能力。

当前需要特别注意的是：本层还**没有** session-start/session-end、session-idle、message-transform、background completion notification 这类 richer lifecycle hook phases。对于未来 async agent 设计，hook 只能是通知与干预面，不能替代 background task / session lifecycle substrate。

## 当前状态

hook 已经是相对独立的能力层，是后续 capability-layer 文档化的参考样板之一。
