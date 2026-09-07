# `voidcode.runtime`

这里是 VoidCode 的运行时控制面，拥有执行治理、权限、持久化、恢复与生命周期的最终控制权，也是当前产品级执行边界。

## 定位

`voidcode.runtime` 负责把执行、配置、权限、事件、持久化与恢复收口到同一个 runtime truth 中。CLI、HTTP、Web 以及后续 TUI 都应通过这里暴露的契约消费运行状态，而不是各自维护执行路径。

## 负责什么

- run / stream / resume 等运行时入口
- 会话持久化、恢复与本地状态真相
- 运行时配置解析与优先级收口
- 权限决策、审批与 hooks 执行
- capability 生命周期与 runtime 集成
- 事件发射与客户端共享的执行观测面

## 不负责什么

- graph 内部的编排细节
- tools 自己的具体业务实现
- 客户端 UI 布局与交互逻辑
- 独立 capability layer 的纯 schema / registry / preset 数据定义

## 边界关系

- `voidcode.graph` 负责执行编排，不负责产品治理。
- `voidcode.tools`、`voidcode.hook` 以及 capability-layer 目录都通过 runtime 进入真实执行路径。
- 客户端只能消费 runtime contracts、events 和 session state，不能绕过 runtime 直接执行工具。

## 当前状态

这是当前最成熟、最稳定的系统边界。后续 provider、skills、LSP、ACP、MCP 等能力演进，都应优先服从 runtime 控制面的所有权。
