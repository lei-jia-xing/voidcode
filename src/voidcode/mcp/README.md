# `voidcode.mcp`

这里是 VoidCode 的 MCP 能力层，包含协议模型、配置 schema 和可观测性接口。

> **状态**: 根据 Issue #107/#213，MCP 的静态类型、配置模型和边界约束已经提取到此目录。
> runtime 实现保留在 `src/voidcode/runtime/mcp.py`，基于官方 Python MCP SDK 提供 stdio + remote-http (Streamable HTTP) 传输的 runtime-managed 集成。

## 定位

`voidcode.mcp` 承载 MCP 相关的协议模型、配置 schema、server 定义与 capability-layer primitives。

## 负责什么

- ✅ MCP 相关 schema 与配置模型 (`config.py`)
- ✅ MCP server / connection definition 的纯数据结构 (`types.py`)
- ✅ 不依赖 runtime session 状态的协议契约 (`contract.py`)
- ✅ 可观测性与诊断接口定义 (`observability.py`)

## 不负责什么

- ❌ 真实连接生命周期管理 (保留在 `runtime/mcp.py`)
- ❌ runtime 事件发射 (保留在 `runtime/mcp.py`)
- ❌ session 持久化与恢复语义
- ❌ 客户端直连 MCP 的执行路径

## 当前状态

### 当前约束

参见 `contract.py` 中的 `SUPPORTED_CAPABILITIES`:

- **Transport**: stdio + remote-http (Streamable HTTP)
- **Discovery**: deferred (懒加载)
- **Operations**: tools/list, tools/call
- **Lifecycle**: runtime-owned
- **Client foundation**: official Python MCP SDK
- **Security/Governance**: trusted-local-only, MCP tool annotations mapped into `McpToolSafety`
- **Observability**: runtime events plus diagnostics collector

### 不支持的功能

参见 `contract.py` 中的 `NOT_SUPPORTED`:

- 完整 bidirectional MCP
- MCP resources / prompts / sampling
- Untrusted MCP servers

### Builtin MCP 描述符说明

`builtin.py` 定义了以下内置 MCP 描述符:

| 名称 | 传输 | URL | 状态 |
|------|------|-----|------|
| `context7` | remote-http | `https://mcp.context7.com/mcp` | descriptor-only, config-gated |
| `websearch` | remote-http | `https://mcp.exa.ai/mcp` | descriptor-only, config-gated |
| `grep_app` | remote-http | `https://mcp.grep.app` | config-gated, 启用后可连接 |
| `playwright` | stdio | N/A | skill-scoped, session scope |

#### grep.app 端点说明

grep.app 提供三个不同的端点，用途各不相同:

- **Web 界面**: `https://grep.app` — 用于浏览器搜索的代码搜索网站
- **公共 API**: `https://grep.app/api/search` — 用于程序化搜索的 REST API
- **MCP 端点**: `https://mcp.grep.app` — MCP 协议端点（远程 HTTP 传输）

内置 `grep_app` 描述符现在指向官方 MCP 端点 `https://mcp.grep.app`:
- 需要在 `.voidcode.json` 中配置 `mcp.enabled: true` 才能启用
- 启用后 runtime 会自动连接到远程 MCP 端点
- 连接失败会作为 runtime MCP 诊断/状态输出，而不是模糊的工具错误

### 文件结构

```
src/voidcode/mcp/
├── __init__.py         # 公共导出
├── config.py           # 静态配置模型
├── types.py            # 静态类型定义
├── contract.py         # 当前边界约束
├── observability.py    # 诊断与观测性接口定义
└── README.md           # 本文件
```

## 边界关系

- `voidcode.runtime` 持有 lifecycle、event、session truth 和 capability governance
- `voidcode.mcp` 负责提供纯 contract / schema / interface 层
- `voidcode.tools/mcp.py` 桥接 MCP tool 到 runtime 的工具系统

Runtime 仍然拥有产品层策略：配置解析、workspace 绑定、事件发射、错误归一化、诊断采集和工具审批语义；SDK 负责 stdio 进程启动/关闭、initialize 握手和 JSON-RPC request/response 匹配。

## 相关 Issue

- [#107](https://github.com/lei-jia-xing/voidcode/issues/107): stabilize MCP runtime boundary and extract config/schema/types
- [#213](https://github.com/lei-jia-xing/voidcode/issues/213): production-harden MCP integration on the official Python SDK
