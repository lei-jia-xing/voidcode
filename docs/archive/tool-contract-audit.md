# Tool Contract Audit

审计范围：工具定义、参数 schema、`ToolResult` 展示约定、运行时权限元数据，以及 CLI/命令 shorthand。重点检查 `read` 的 XML-like 输出和 `grep` 的正则语义。

## Findings

| 优先级 | 问题 | 证据 | 影响 | 建议 | 状态 |
|---|---|---|---|---|---|
| P0 | guidance 注入曾丢失 `path_argument_keys` | `src/voidcode/tools/guidance.py:66-82`；字段定义见 `src/voidcode/tools/contracts.py:18-25`，权限读取见 `src/voidcode/runtime/permission_context.py:87` | agent-visible definition 与运行时权限/路径上下文不一致；涉及 `read`、`grep`、`glob`、写入类工具 | 已保留字段并添加回归测试 | 已修复 |
| P1 | `read` 在 `content` 中输出 XML-like 包装，而不是结构化 XML | `src/voidcode/tools/read.py`；契约文档 `docs/contracts/agent-tool-calling.md` | 包装与正文混杂，导致 UI 泄漏和解析负担 | 已移除 XML-like wrapper；正文由 `data.lines`/`data.raw_content` 提供结构化真源 | 已修复 |
| P1 | `grep` 支持正则，但只有显式 `regex=true` 才启用 | 实现 `src/voidcode/tools/grep.py:194-207`；说明 `src/voidcode/tools/grep.txt:1-5`；测试 `tests/unit/tools/test_grep_tool.py:58,293+` | 原生工具调用必须显式设置开关；schema 现已补充字段描述，shorthand 现支持 `grep --regex <pattern> <path>` | 已修复入口/schema 不一致；默认仍为字面匹配 |
| P1 | `ToolResult.content` 的语义不统一 | `read` 将完整行号化正文放入 `content`；`grep` 将人类摘要放入 `content`，匹配详情放入 `data.matches` | UI 必须按工具名称分支处理；通用渲染器容易显示错误层级或泄漏内部格式 | 约定 `content` 为摘要，正文/匹配项统一放入结构化 `data`；增加通用 presentation contract | 已修复 |
| P2 | 路径参数命名不统一：`read.filePath` vs 其他工具的 `path` | `docs/contracts/agent-tool-calling.md:224`；TUI 特殊分支 `src/voidcode/runtime/tool_display.py` | 增加模型参数错误率和客户端 special-case；新工具难以遵循单一约定 | 下一版统一为 `path`，旧字段提供兼容迁移；短期在 schema description 中显式说明 | 已修复（统一为 `path`） |
| P2 | `read` 行号前缀使输出不可直接复用 | `src/voidcode/tools/read.py:177`；`data.copy_guidance` 要求剥离 `<line>: `；`edit` guidance 也要求忽略前缀 | 从读取结果复制到编辑参数时容易把行号带入，XML wrapper 又增加一层解析负担 | 提供 `data.lines`（行号与原文分离）及 `data.raw_content`；保留 line-numbered presentation 仅用于展示 | 已修复 |

## 已核对、未发现实现缺陷

- `grep` 的 regex 编译、非法正则错误和测试覆盖是连贯的；默认字面匹配是明确设计，不是缺失实现。
- `read` 的 `offset`/`limit` 默认值、`next_offset` 与续读提示在实现和契约文档之间一致。
- `grep` 的最大匹配数、`truncated`/`partial`、诊断与重试提示在实现和测试之间一致。

## 与 TUI 的关系

TUI 对已知工具名做 XML-like 文本过滤只能降低展示泄漏，不能把 XML 文本变成可执行的工具调用。执行路径仍应使用 runtime 的结构化 `ToolCall`；因此根因应在结果契约和调用入口统一，而不是继续扩展 UI 端正则/XML解析。

## 修复顺序

1. 修复 guidance 丢失 `path_argument_keys`，补回归测试。
2. 统一 `ToolResult` 的摘要/正文边界，逐步淘汰 `read` XML-like 包装。
3. 补全 `grep` schema 描述，并让 shorthand 显式支持或拒绝 regex。
4. 规划路径字段和结构化行内容的兼容迁移。
