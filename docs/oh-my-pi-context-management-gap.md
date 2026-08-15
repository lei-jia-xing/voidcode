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

VoidCode 缺失的能力：rulebook、memory 整合管线、模型化 compaction、内部 URL 命名空间。

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

- **omp**：`rule://<name>` 按需读取；always-apply 规则全文注入且 sticky；glob 条件规则（`applyTo`）。
- **voidcode**：无 rulebook；只有 AGENTS.md-as-rules + hook presets（guidance-only snapshot，概念不同）。
- **判定**：真实差距。

### 4. Memory

- **omp**：`memory://root` 摘要启动注入（`summaryInjectionTokenLimit` 5000 上限）+ `MEMORY.md`、`learned.md` 按需；后端 local 整合管线 / hindsight / mnemopi；工具 learn/recall/retain/reflect/memory_edit。
- **voidcode**：`_KeywordMemoryManager` 关键词检索；`MemoryRecallConfig`（默认 `enabled=False`、`limit=5`、`max_chars=2000`）；可选 sqlite-vec；workspace 级；注入为 `workspace_memory_context`。
- **判定**：voidcode 较原始。

### 5. Compaction / 上下文窗口

- **omp**：模型驱动（LLM 摘要 context-full、snapcompact 位图、handoff 新会话、shake `artifact://` 省略、branch summary）；工具输出 pruning（保护 40k、≥20k 节省、useless-result 省略）；多触发（overflow/incomplete/threshold/mid-turn/idle）。
- **voidcode**：确定性（`prepare_provider_context` 按 token budget 丢弃/截断 tool results；`ContextProjection` continuity summary——确定性 facts progress/blockers/refs，可选 `model_assisted`）；无对话级 LLM 摘要、无 branch summary、无位图归档。
- **判定**：voidcode 保守/确定性；omp 激进/模型驱动。

### 6. 内部 URL 命名空间 / 按需工具文档

- **omp**：`skill://` `rule://` `memory://` `artifact://` `xd://` `omp://` `agent://` `history://` `local://` `mcp://`；工具文档按需 `xd://<tool>`。
- **voidcode**：无内部 URL 命名空间；用专用工具（`skill`、`background_output`）+ `artifact:` 字符串前缀；工具 schema 恒在 provider tool list。
- **判定**：设计取舍；voidcode 用工具而非 URL。

## 结论

不改变其他维度的总体判断（见 `docs/oh-my-pi-comparison-priorities.md`），仅就提示词/上下文管理而言：

- 差异不在「是否按需加载」，而在**eager/按需的边界划在哪里**。
- voidcode 的响应式 context files 是刻意的 token 经济选择，但代价是缺少 user 级规则、@import、sticky 规则与指针能力。
- 按需能力差距集中在 rulebook、memory 整合管线与内部 URL 命名空间；compaction 的差距是「确定性 vs 模型驱动」的哲学差异，而非能力缺失。

## 证据索引

### VoidCode（本仓库源码直读核验）

- `src/voidcode/runtime/context_rules.py` — `RULE_FILE_NAME="AGENTS.md"`、`MAX_RULE_FILES=8`、`MAX_RULE_FILE_CHARS=12_000`；`runtime_file_rule_contexts()` 以 `tool_results` 触碰路径为输入（响应式）。
- `src/voidcode/runtime/skills.py` — `build_skill_prompt_context`、`SkillRuntimeContext`、`SkillExecutionSnapshot`。
- `src/voidcode/runtime/service.py` — `_applied_skill_contexts`、`force_load_skills` 注入、`workspace_memory_prompt_context`。
- `src/voidcode/runtime/memory.py` — `_KeywordMemoryManager`、`MemoryRecallConfig(enabled=False, limit=5, max_chars=2000)`、workspace scope。
- `src/voidcode/runtime/context_projection.py` — `project_summary(strategy="deterministic"|"model_assisted"|fallback)`。
- `src/voidcode/runtime/context_window.py` — `prepare_provider_context` 按 token budget 丢弃/截断；`ContextProjection` continuity summary。
- `src/voidcode/runtime/prompt_assembly.py` — `workspace_memory_context` 注入段（source `runtime_workspace_memory`）。
- `src/voidcode/tools/skill.py` — `<available_skills>` 目录置于 `definition.description`。
- `src/voidcode/tools/task.py` — `load_skills` 强制子会话 skill 正文加载。

### Oh My Pi（依据仓库路径，commit 见 `docs/oh-my-pi-comparison-priorities.md`）

- `packages/coding-agent/src/system-prompt.ts`
- `extensibility/skills.ts`
- `internal-urls/skill-protocol.ts`
- `discovery/agents-md.ts`
- `packages/agent/src/compaction/*`
