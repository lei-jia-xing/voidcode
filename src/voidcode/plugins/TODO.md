# Plugins TODO

## 插件生产化与能力收敛

- [ ] **为官方插件建立真实任务的生产化门槛，先覆盖 files、exec、mcp、lsp、subagents、workflows。**
- [x] **让正常截图/图片读取真正可用，贯通工具结果和客户端显示。**
  - `capabilities/images.py` 接入 Pillow 共享栅格检查与 Kitty PNG 转码：`capabilities/files/read_tool.py` 在发布前执行容器完整性校验（`verify=True`）及尺寸/像素/体积护栏（`MAX_RASTER_DIMENSION` / `MAX_RASTER_PIXELS` / `MAX_RASTER_BYTES`），拒绝损坏图像与解压炸弹并避免延迟失败；`tui/artifact_viewer.py` 支持 PNG/JPEG/WebP/GIF（含 EXIF 方向校正）的 Kitty Graphics 预览并保留原字节外部打开。
- [x] **删除没有边界依据的 PREVIEW/MAX，以及同一数据链路的重复截断。**
  - `ast_grep_tool.py` 删除固定 `_MATCH_PREVIEW_LIMIT = 50`，改为按 `TOOL_OUTPUT_BYTES` 留出字节余量动态打包；成功结果保留过滤后 `native_jsonl` artifact URI，零匹配不再产生空 artifact，进程输出超限/超时/取消时返回带 partial artifact 的 typed failure 而不是丢弃结果或伪称完整。`user_shell_plugin` 事件载荷移除第三份冗余 `"output"` 拷贝（保留 `stdout`/`stderr` 与 `record`）。
- [ ] **提供面向模型的持久 Python `eval`，而不只是 shell 的另一层包装。**
- [ ] **让 URI 成为跨工具的资源身份，而不只是一条 `read` 特例。**
- [ ] **把现有子 Agent 生命周期做成持续交互、可寻址结果的闭环。**
  - [x] `subagents` 提供可供客户端渲染的实时子任务状态投影（`SubagentStatusView`，含运行/审批/排队/失败状态、最近活动、已用时间 `elapsed_ms` 及 `Alt+A monitor` 入口）。
- [x] **提供工作区路径补全查询。**
  - files 能力经 `workspace.path_completion` 回答路径前缀/模糊匹配，支持递归深度、默认/嵌套 `.gitignore` 与根 `.git/info/exclude` 忽略、隐藏项显式请求、确定排序与 64 KiB 结果上限；模糊模式匹配请求目录下的相对路径，便于递归命中嵌套文件；路径始终相对服务端工作区，越界符号链接不返回。插件禁用即 `QueryUnavailable`，客户端不再直接读本地文件系统。
- [ ] **统一有限命令、后台任务与长期服务的使用和收尾体验。**
- [ ] **增加模型可调用的结构化问答，而不滥用确认门。**
  - Core `suspend_for_ask` 与客户端已接通，但尚无官方插件暴露通用 `ask` 工具；后续优先以单一可复用插件/Core 契约提供，避免各插件重复造轮子。
- [ ] **扩展统一读取入口，覆盖常见工作材料而不是一律退回 shell。**
- [ ] **把 LSP 语义重构、安全编辑和诊断连成一次可靠操作（部分完成）。**
  - `[x]` `lsp` 已迁移至 Core `spawn_query_service_process` 并声明纯 `ToolEffect.READ`，`plan`/只读模式与 `ask`/`write` 模式下均免审批可用（受只读沙箱与 generation 托管约束）。
  - 残留：仍需补齐端到端重构编辑协同体验，避免大改动编辑链路割裂。
- [ ] **削平四大巨石插件（`workflows`、`subagents`、`goal`、`todo`）的重复实现与历史包袱。**

## P0/P1：安全、正确性与能力缺口

- [x] **修复 `compaction` 默认配置下自动压缩失效。**
  - `compaction_observer` 现从 `UsageFact` 接收 `context_window` 并传入 `resolve_compaction_budget`，保证默认配置下能正确算出压缩触发门限。
- [x] **为 `web_fetch` 补齐 SSRF 防护，消除 `web_search` 静默降级。**
  - `_resolve_public_ips` 统一按 `not is_global or is_multicast` 拒绝回环/私有/链路本地/CGNAT/保留/组播/云元数据并解包 IPv4-mapped IPv6，DNS 失败 fail closed；`_PinnedPublicNetworkBackend` 在 `connect_tcp` 阶段单次解析并固定连接已校验 IP 字面量（保持 `Host` 与 TLS SNI 原样，消除 DNS rebinding TOCTOU）；重定向每跳复用同一校验。
  - `_read_bounded_body` 通过 `response.iter_raw()` + 增量有界 `zlib`（`max_length`）为 `gzip`/`deflate`/`identity` 同时约束压缩线流与解压字节内存峰值，并显式拒绝不支持的 `Content-Encoding`；客户端设置 `trust_env=False`；`VOIDCODE_ALLOW_LOCAL_NETWORK_FETCH=1` 仅为危险测试/开发开关。`web_search` 已消除 DeepSeek 静默截断并标注 keyless Jina（`web_search` 响应解压界限仍保留在 `plugins/web/README.md` 备注中）。
- [x] **完善 `files` 的忽略规则。**
  - `GitIgnoreMatcher`（`capabilities/files/gitignore.py`）现按目录惰性加载搜索根 `.git/info/exclude`、根 `.gitignore` 与嵌套 `.gitignore`；每条规则相对其所在目录解释（锚定/浮动/`**`/目录专属/`!` 取反），深层文件覆盖祖先、最后匹配规则获胜，被忽略子树不再扫描。pathspec 拒绝的行按字面量模式应用而非中止搜索；不可读的忽略文件仍是类型化失败。`glob`/`grep`/路径补全与 `find`（经 grep 查询）共用该匹配器；hidden 默认包含、`respect_gitignore` 默认 false 的既有策略不变，全局 excludes 不读取。
  - `write`/`edit` 共用 64 KiB mutation-content 上限（按 UTF-8 字节计：`write.content`、文本模式 `newString` 之和、range 模式 `newText` 之和），覆盖写按流式哈希复核，仅对不超过 64 KiB 的旧文件回传 unified `diff`/`additions`/`deletions`。
- [ ] **补齐 `mcp` 分页与资源/提示词发现，迁移进程生命周期管理，消除 `hooks`/`rules` 静默吞错（部分完成）。**
  - `[x]` `ManagedMcpManager._list_tools_pages_owned` 已支持 `nextCursor` 分页和上限防护。
  - `[x]` `ToolHooks.post_tool`（`plugins/hooks/hooks_plugin.py`）在执行失败时记录 warning 日志与 visible failure；`plugins/skills/rules.py` 诊断和读取失败时均显式记录 warning 日志。
  - 残留：MCP 仅实现 `tools/list` 分页（见 `plugins/mcp/README.md`），`resources/list`、`read_resource` 与 `prompts/list`/`get_prompt` 尚未实现；MCP 仍自行启动 stdio 子进程并丢弃 stderr 到 `/dev/null`，需待 Core 提供统一服务进程原语后迁入。
- [ ] **收敛 Provider 适配器重复实现并补齐现代特性。**
  - [x] 消除 `openai_native.py` 与 `anthropic_native.py` 重复的 `_tool_maps` 及重复 `_MAX_TOOL_NAME_LENGTH` 常量（已收敛至 `_wire_common.py` 的 `tool_name_maps` 与 `_MAX_TOOL_NAME_LENGTH`）。
  - [x] `anthropic_native.py` 现对 `tools[-1]`、`system` 与多轮历史消息前缀（保留最近上轮边界与当前尾块，跳过 `thinking`/`redacted_thinking` 及空 `text` 块，最多 4 处断点）设置 `cache_control`。
  - 残留：`openai_native.py` 补充 OpenAI Responses API 支持（推理摘要与状态化推理）。
