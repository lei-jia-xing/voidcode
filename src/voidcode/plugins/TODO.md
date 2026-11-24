# Plugins TODO

## 插件生产化与能力收敛

- [ ] **为官方插件建立真实任务的生产化门槛，先覆盖 files、exec、mcp、lsp、subagents、workflows。**
- [ ] **让正常截图/图片读取真正可用，贯通工具结果和客户端显示。**
- [x] **删除没有边界依据的 PREVIEW/MAX，以及同一数据链路的重复截断。**
  - `ast_grep_tool.py` 删除固定 `_MATCH_PREVIEW_LIMIT = 50`，改为按 `TOOL_OUTPUT_BYTES` 留出字节余量动态打包；成功结果保留过滤后 `native_jsonl` artifact URI，零匹配不再产生空 artifact，进程输出超限/超时/取消时返回带 partial artifact 的 typed failure 而不是丢弃结果或伪称完整。`user_shell_plugin` 事件载荷移除第三份冗余 `"output"` 拷贝（保留 `stdout`/`stderr` 与 `record`）。
- [ ] **提供面向模型的持久 Python `eval`，而不只是 shell 的另一层包装。**
- [ ] **让 URI 成为跨工具的资源身份，而不只是一条 `read` 特例。**
- [ ] **把现有子 Agent 生命周期做成持续交互、可寻址结果的闭环。**
  - [x] `subagents` 提供可供客户端渲染的实时子任务状态投影（`SubagentStatusView`，含运行/审批/排队/失败状态、最近活动、已用时间 `elapsed_ms` 及 `Alt+A monitor` 入口）。
- [ ] **提供工作区路径补全查询。**
  - files 能力经 `workspace_queries` 回答路径前缀/模糊匹配，支持递归与忽略规则；插件禁用即不可用，客户端不再直接读本地文件系统。
- [ ] **统一有限命令、后台任务与长期服务的使用和收尾体验。**
- [ ] **增加模型可调用的结构化问答，而不滥用确认门。**
- [ ] **扩展统一读取入口，覆盖常见工作材料而不是一律退回 shell。**
- [ ] **把 LSP 语义重构、安全编辑和诊断连成一次可靠操作。**
  - 需要产品先决定只读 managed-service 策略：`lsp` 目前保持 `READ` + `EXECUTE`，因为每个查询都会惰性启动受监管的语言服务器进程；`plan`/只读模式因此不可用，`ask` 和 `write` 模式每次调用都要审批。仅改 `ToolEffect` 会让运行时把租约标为只读，从而使所有 LSP 调用在 `spawn_service_process` 处失败。若要让只读 LSP 可用，需要新增一个窄的“只读受监管服务”核心能力（只读租约可启动/读写给定时进程、强制只读沙箱、仍按 generation 收尾），而不是在插件内改效果声明。
- [ ] **削平四大巨石插件（`workflows`、`subagents`、`goal`、`todo`）的重复实现与历史包袱。**

## P0/P1：安全、正确性与能力缺口

- [x] **修复 `compaction` 默认配置下自动压缩失效。**
  - `compaction_observer` 现从 `UsageFact` 接收 `context_window` 并传入 `resolve_compaction_budget`，保证默认配置下能正确算出压缩触发门限。
- [x] **为 `web_fetch` 补齐 SSRF 防护，消除 `web_search` 静默降级。**
  - `plugins/web/web_fetch.py` 的 `_validated_url` 现校验回环地址、私有网段、云元数据（`169.254.169.254`）与重定向目标内网，重定向目标同样经过严格 SSRF 验证。
- [ ] **完善 `files` 的忽略规则。**
  - `GitIgnoreMatcher`（`capabilities/files/gitignore.py`）仅读根目录 `.gitignore`（忽略嵌套 `.gitignore` 与 `.git/info/exclude`），`glob`/`grep` 共享同一默认策略（hidden 默认包含、`respect_gitignore` 默认 false）并有测试覆盖。
  - `write`/`edit` 共用 64 KiB mutation-content 上限（按 UTF-8 字节计：`write.content`、文本模式 `newString` 之和、range 模式 `newText` 之和），覆盖写按流式哈希复核，仅对不超过 64 KiB 的旧文件回传 unified `diff`/`additions`/`deletions`。
- [x] **补齐 `mcp` 分页与资源/提示词发现，消除 `hooks`/`rules` 静默吞错。**
  - `ManagedMcpManager._list_tools_pages_owned` 已支持 `nextCursor` 分页和上限防护。
  - `ToolHooks.post_tool`（`plugins/hooks/hooks_plugin.py`）在执行失败时记录 warning 日志与 visible failure；`plugins/skills/rules.py` 诊断和读取失败时均显式记录 warning 日志。
- [ ] **收敛 Provider 适配器重复实现并补齐现代特性。**
  - 消除 `openai_native.py` 与 `anthropic_native.py` 重复的 `_tool_maps` 及重复 `_MAX_TOOL_NAME_LENGTH` 常量（收敛至 `_wire_common.py`）。
  - `anthropic_native.py` 仅在 `tools[-1]`/`system` 上设置 `cache_control`，应为多轮历史消息前缀设置缓存断点；`openai_native.py` 补充 OpenAI Responses API 支持（推理摘要与状态化推理）。
