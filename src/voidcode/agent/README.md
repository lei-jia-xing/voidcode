# `voidcode.agent`

这里是预定义 agent 与 agent preset/configuration 的规划边界。

## 定位

`voidcode.agent` 用于承载未来 multi-agent 支持所需的预定义 agent 定义，例如 agent profile、prompt/profile 组合，以及对 hook、skill、MCP、tool、provider 等能力的声明式绑定。

## 负责什么

- 预定义 agent manifest / preset
- prompt / profile 定义
- hook 绑定
- skill 绑定
- MCP server/profile 绑定
- tool allowlist / default tool set
- provider / model preference metadata

## 不负责什么

- session 持久化与恢复
- 审批 / 权限决策
- runtime 事件发射
- transport / client 行为
- 工具直接执行
- provider invocation loop

## 边界关系

- `voidcode.runtime` 继续拥有 session、approval、permission、persistence、events、transport 与 capability lifecycle truth
- `voidcode.graph` 继续拥有 loop / step orchestration
- `voidcode.agent` 负责预定义 agent profile 与组合层
- `hook/`、`skills/`、`mcp/`、`tools/`、`provider/` 继续提供可复用能力，由 runtime 集成、由 agent 定义按需声明

## 当前状态

这是一个规划中的能力边界。当前仓库尚未在此目录中实现真实的 multi-agent execution 语义；这里描述的是未来方向，而不是已交付功能。
