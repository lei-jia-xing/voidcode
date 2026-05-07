# `voidcode.context`

这里是 VoidCode 的上下文能力层。

## 定位

`voidcode.context` 承载与 provider context 组装相关的纯能力逻辑，例如目录级 README/规则发现与 context injection helper。

## 负责什么

- 目录级上下文文件发现
- provider context injection 所需的纯 helper / provider 逻辑
- 与上下文注入相关的有界截断与 metadata 生成

## 不负责什么

- session 生命周期与持久化
- 审批与权限决策
- runtime event 路由
- provider 调用与 fallback

## 边界关系

`runtime/` 负责决定何时组装 context、如何持久化 metadata、以及何时把这些能力接入 execution path。
`voidcode.context` 负责提供可复用、可测试的上下文能力实现。
