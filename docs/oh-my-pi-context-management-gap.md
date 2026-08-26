# VoidCode 与 Oh My Pi 提示词/上下文管理差距

> 调研日期：2026-08-15
>
> 范围：仅比较两项目的提示词装配与上下文管理机制（skills / context files / rulebook / memory / compaction / 内部 URL 命名空间），不涉及编辑协议、会话持久化、客户端等其他维度（见 `docs/oh-my-pi-comparison-priorities.md`）。

## 结论

两者都是混合式（eager + 按需），但 eager/按需的边界完全不同。一个常见记忆「omp 尽可能按需加载」只对了一半：

| | eager | 按需 |
|---|---|---|
| **Oh My Pi** | context files（`AGENTS.md` 等全文，会话启动注入） | skills、rulebook、memory、工具文档（经 `skill://` `rule://` `memory://` `xd://` 内部 URL） |
| **VoidCode** | skills 可选 `force_load_skills`；无其他 eager 注入 | skills 正文按需；context files 为**响应式**（按触碰路径注入） |

即：对 context files 而言，方向正好相反——omp 是 eager 全文注入，VoidCode 是响应式按需注入。

VoidCode 的 rulebook 已以 workspace-only 的 bounded catalog 形式补齐最小 P1：always-apply 全文、discoverable metadata 与 `voidcode://rule/<name>` 按需读取；仍缺少 OMP 的 glob 条件规则、imports 和完整 memory/compaction 管线。

## 逐维度对比

### 1. Skills（两边都按需，差距最小）

- **omp**：目录（name + description）放在 system prompt 的 `<skills>` 块（`hide:true` 排除；仅当 `read` 工具存在时注入）；正文按需经 `read skill://<name>` 或 `/skill:<name>` 读取；无 eager force-load。
- **voidcode**：目录放在 `skill` 工具 `definition.description` 的 `<available_skills>`（name + description + location）；正文按需经 `skill` 工具调用，或经 `force_load_skills`（request 级）/ `load_skills`（task 工具子会话级）eager 注入。
- **判定**：按需能力对等；voidcode 多一个 eager force-load；差别仅在目录放工具描述还是 system prompt。

### 2. Context files（AGENTS.md）—— 核心差距，方向相反

- **omp**：会话启动 **eager 全文注入**；多 provider（native/claude/codex/gemini/opencode/github/agents/agents-md）；user 级 + project 级；provider 优先级 shadowing；depth 去重；`@` import（相对导入文件目录，递归 ≤5 跳，环跳过）；深层 AGENTS.md 用 `<dir-context>` 指针（只列路径不注入）；sticky `RULES.md` always-apply。
- **voidcode**：**响应式**（`runtime_file_rule_contexts()` 按 tool_results 触碰路径注入）；仅 `AGENTS.md`（`RULE_FILE_NAME`）；仅 workspace 级；`MAX_RULE_FILES=8`、`MAX_RULE_FILE_CHARS=12_000`；无 @import、无 sticky 规则、无指针、无多 provider。
- **判定**：voidcode 更省上下文（响应式），但能力差一截（单约定、无 user 级、无 @import、无 sticky、无指针）。

### 3. Rulebook / 按需规则

- **voidcode**：`.voidcode/rules/**/*.md` 构成 workspace-only catalog；always-apply 规则全文注入，discoverable 规则只注入 metadata，正文经 bounded `voidcode://rule/<name>` 读取。现有 `AGENTS.md` 仍保持响应式规则行为。
- **判定**：最小 rulebook disclosure 已落地；glob 条件规则、imports、user scope 与完整 TTSR 仍省略。



### 5. Compaction / 上下文窗口

- **omp**：模型驱动（LLM 摘要 context-full、snapcompact 位图、handoff 新会话、shake `artifact://` 省略、branch summary）；工具输出 pruning（保护 40k、≥20k 节省、useless-result 省略）；多触发（overflow/incomplete/threshold/mid-turn/idle）。
- **voidcode**：确定性（`prepare_provider_context` 按 token budget 丢弃/截断 tool results；`ContextProjection` continuity summary——确定性 facts progress/blockers/refs，可选 `model_assisted`）；无对话级 LLM 摘要、无 branch summary、无位图归档。
- **判定**：voidcode 保守/确定性；omp 激进/模型驱动。

### 6. 内部 URL 命名空间 / 按需工具文档

- **voidcode**：提供 runtime-owned `voidcode://tool/<name>` 工具文档 URI。essential 工具（以及 allowlist 明确选中的工具）进入 provider 顶层；discoverable 工具仍在 live registry 中，通过 `read(path="voidcode://tool/<name>")` 按需返回完整 guidance、当前 `input_schema` 与治理 metadata，再经 `invoke_tool` 调用。provider-visible `ToolDefinition` 只携带 Python definition 的短 description 与 canonical schema；MCP/local 动态事实以 registry、`ToolResult.data` 和 runtime metadata/events 为准。
- **判定**：OMP 提供更广的通用内部 URL 命名空间；VoidCode 当前只为工具文档提供 runtime-owned URI，并以 essential/discoverable 分层控制 provider 暴露范围。

## 结论

不改变其他维度的总体判断（见 `docs/oh-my-pi-comparison-priorities.md`），仅就提示词/上下文管理而言：

- 差异不在「是否按需加载」，而在**eager/按需的边界划在哪里**。
- voidcode 的响应式 context files 是刻意的 token 经济选择，但代价是缺少 user 级规则、@import、sticky 规则与指针能力。
- 按需能力差距集中在 memory 整合管线与 OMP 的完整 rulebook/compaction 语义；VoidCode 已提供 bounded `voidcode://rule/<name>` 与工具文档 URI，并以 snapshot hash 保持 replay 稳定。compaction 的差距是「确定性 vs 模型驱动」的哲学差异。

## 证据索引

### VoidCode（本仓库源码直读核验）

- `src/voidcode/runtime/context_rules.py` — `RULE_FILE_NAME="AGENTS.md"`、`MAX_RULE_FILES=8`、`MAX_RULE_FILE_CHARS=12_000`；`runtime_file_rule_contexts()` 以 `tool_results` 触碰路径为输入（响应式）。
- `src/voidcode/runtime/skills.py` — `build_skill_prompt_context`、`SkillRuntimeContext`、`SkillExecutionSnapshot`。
- `src/voidcode/runtime/context_projection.py` — `project_summary(strategy="deterministic"|"model_assisted"|fallback)`。
- `src/voidcode/runtime/context_window.py` — `prepare_provider_context` 按 token budget 丢弃/截断；`ContextProjection` continuity summary。
- `src/voidcode/tools/skill.py` — `<available_skills>` 目录置于 `definition.description`。

P1 rulebook implementation notes:

- Catalog roots are workspace-local `.voidcode/rules/`; frontmatter uses a required exact `application` value (`always_apply` or `discoverable`) plus optional `name`, `description`, `scope` (`workspace` or `repo`), and bounded integer `precedence`.
- Prompt assembly injects bounded always-apply bodies and discoverable metadata only. Runtime policy remains authoritative; rule text cannot grant tools, approvals, delegation, or MCP capabilities.
- `read(path="voidcode://rule/<name>")` validates a single safe slug, resolves only the catalog, and applies line/byte bounds. Session metadata persists sorted rule metadata and a canonical snapshot hash; replay ignores entries whose current bytes no longer match the snapshot.
- `src/voidcode/tools/task.py` — `load_skills` 强制子会话 skill 正文加载。

### Oh My Pi（依据仓库路径，commit 见 `docs/oh-my-pi-comparison-priorities.md`）

- `packages/coding-agent/src/system-prompt.ts`
- `extensibility/skills.ts`
- `internal-urls/skill-protocol.ts`
- `discovery/agents-md.ts`
- `packages/agent/src/compaction/*`
