# `voidcode.mcp`

这里是 VoidCode 的 MCP 能力层，包含协议模型、配置 schema 和可观测性接口。

> **状态**: 根据 Issue #107/#213，MCP 的静态类型、配置模型和边界约束已经提取到此目录。
> runtime 实现仍保留在 `src/voidcode/runtime/mcp.py`，并基于官方 Python MCP SDK 提供受限、stdio-only、runtime-managed 的预发布集成。

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

- **Transport**: stdio-only
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
- Remote / non-stdio transports
- Untrusted MCP servers

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
