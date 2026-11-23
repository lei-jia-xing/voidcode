# Plugins TODO

## 插件生产化与能力收敛

- [ ] **为官方插件建立真实任务的生产化门槛，先覆盖 files、exec、mcp、lsp、subagents、workflows。**
- [ ] **让正常截图/图片读取真正可用，贯通工具结果和客户端显示。**
- [ ] **删除没有边界依据的 PREVIEW/MAX，以及同一数据链路的重复截断。**
- [ ] **提供面向模型的持久 Python `eval`，而不只是 shell 的另一层包装。**
- [ ] **让 URI 成为跨工具的资源身份，而不只是一条 `read` 特例。**
- [ ] **把现有子 Agent 生命周期做成持续交互、可寻址结果的闭环。**
  - `subagents` 提供可供客户端渲染的子任务状态投影：运行/审批/排队/失败、最近活动、已用时间，以及进入 monitor 的入口。
- [ ] **提供工作区路径补全查询。**
  - files 能力经 `workspace_queries` 回答路径前缀/模糊匹配，支持递归与忽略规则；插件禁用即不可用，客户端不再直接读本地文件系统。
- [ ] **统一有限命令、后台任务与长期服务的使用和收尾体验。**
- [ ] **增加模型可调用的结构化问答，而不滥用确认门。**
- [ ] **扩展统一读取入口，覆盖常见工作材料而不是一律退回 shell。**
- [ ] **把 LSP 语义重构、安全编辑和诊断连成一次可靠操作。**
  - 需要产品先决定只读 managed-service 策略：`lsp` 目前保持 `READ` + `EXECUTE`，因为每个查询都会惰性启动受监管的语言服务器进程；`plan`/只读模式因此不可用，`ask` 和 `write` 模式每次调用都要审批。仅改 `ToolEffect` 会让运行时把租约标为只读，从而使所有 LSP 调用在 `spawn_service_process` 处失败。若要让只读 LSP 可用，需要新增一个窄的“只读受监管服务”核心能力（只读租约可启动/读写给定时进程、强制只读沙箱、仍按 generation 收尾），而不是在插件内改效果声明。
- [ ] **削平四大巨石插件（`workflows`、`subagents`、`goal`、`todo`）的重复实现与历史包袱。**

## P0/P1：安全、正确性与能力缺口

- [ ] **修复 `compaction` 默认配置下自动压缩失效。**
  - `compaction_observer` 向 `resolve_compaction_budget` 传入 `context_window=None`，未显式配置 `threshold_tokens` 时 `trigger_tokens` 恒为 `0`，自动压缩永远不触发（依赖 `sdk/TODO.md` 暴露模型 `context_window`）。
- [ ] **为 `web_fetch` 补齐 SSRF 防护，消除 `web_search` 静默降级。**
  - `plugins/web/web_fetch.py` 的 `_validated_url` 未校验回环地址、私有网段、云元数据（`169.254.169.254`）与重定向目标内网；`plugins/web/web_search.py` 对 DeepSeek 静默截断 `query[:128]`，且无任何 API Key 时默认外发至第三方 Jina 接口。
- [ ] **完善 `files` 的忽略规则、`write` 限制与 Diff 回传。**
  - `GitIgnoreMatcher`（`capabilities/files/gitignore.py`）仅读根目录 `.gitignore`（忽略嵌套 `.gitignore` 与 `.git/info/exclude`），且 `glob`/`grep` 默认 `respect_gitignore=False`。
  - `WriteTool`（`capabilities/files/write_tool.py`）硬编码 `MAX_WRITE_BYTES = 64 KiB` 且覆盖已有文件时不返回 diff，导致前端无法展示 Diff 卡片。
- [ ] **补齐 `mcp` 分页与资源/提示词发现，消除 `hooks`/`rules` 静默吞错。**
  - `ManagedMcpManager._list_tools_for_server_owned` 未处理 `nextCursor` 分页，且未暴露 MCP `resources`/`prompts`。
  - `ToolHooks.post_tool`（`plugins/hooks/hooks_plugin.py`）在执行失败时返回 `None` 静默吞错；`plugins/skills/rules.py` 读取规则文件失败直接 `continue`；`ast_grep_tool.py` 残留 `_MATCH_PREVIEW_LIMIT = 50`。
- [ ] **收敛 Provider 适配器重复实现并补齐现代特性。**
  - 消除 `openai_native.py` 与 `anthropic_native.py` 重复的 `_tool_maps` 及重复 `_MAX_TOOL_NAME_LENGTH` 常量（收敛至 `_wire_common.py`）。
  - `anthropic_native.py` 仅在 `tools[-1]`/`system` 上设置 `cache_control`，应为多轮历史消息前缀设置缓存断点；`openai_native.py` 补充 OpenAI Responses API 支持（推理摘要与状态化推理）。
