# Agent Tooling 采纳计划

**日期：** 2026-04-15
**状态：** 部分已落地；其余条目为后续提案
**作者：** 基于 oh-my-openagent 模式综合整理
**仓库：** `/home/hunter/Workspace/voidcode`

---

## 概览

本计划整理了从 oh-my-openagent 引入到 voidcode 现有 agent 工作流中的三类高价值 tooling 思路。目标是让编辑过程更稳健，让 agent 能感知自身工具就绪状态，并最终支持更强的结构化代码变换能力。

Voidcode 已经拥有较成熟的文本编辑基础设施，位于 `src/voidcode/tools/edit.py`（包含 9 种 replacer，例如 `BlockAnchorReplacer`、`IndentationFlexibleReplacer`）。同时，`src/voidcode/hook/config.py` 中已经存在 `formatter_presets`，并且 `ShellExecTool` 也能执行格式化工具。自本计划首次起草以来，以下能力已经落地：

1. AST-aware 结构化搜索工具（`ast_grep`）
2. formatter-aware 的编辑流与 re-read 对齐
3. 面向外部工具就绪性的 runtime capability doctor

因此，这份文档剩余的价值，更多是记录设计动机、已落地范围，以及仍然值得继续推进的 follow-up 方向，而不是再把所有条目描述成“尚未实现”。

本计划还记录了一条应持续约束未来 capability 工作的战略判断：**MCP 仍然有用，但不应成为引入核心 agent 行为的默认方式。** 对 `voidcode` 来说，更合理的优先顺序是：

1. **原生 runtime-managed tool**：用于本地、高频、定义产品体验的行为
2. **Skill + CLI/native tool 组合**：用于可复用工作流与可选的高层行为
3. **MCP**：仅用于外部所有权明确、需要认证或面向用户可插拔的集成

这并不意味着 MCP 已经没有价值，而是明确降低其“默认底座”的优先级。实践上应遵循：

- 文件 / 搜索 / 编辑 / 重构 / 构建 / 测试 / Git 流程优先使用原生工具或 CLI-backed skills
- 类似 Context7 或 Exa 的能力仍然可以作为可选的外部上下文提供者
- runtime-managed MCP 应继续保持 config-gated、boundary-first，除非未来真的出现值得深度投入的扩展生态

---

## 文档定位

这份文档是仓库级的实现/采纳计划，当前直接放在 `docs/` 下维护。它的定位不是产品路线图，也不是某个 superpowers/plugin 的专属工件，而是记录 tooling 采纳的设计动机、已落地范围和后续提案。

---

## 第 0 阶段（P0）：基础设施

### P0.1：ast-grep 结构化搜索工具 *(已落地)*

**是什么：** 当前仓库已经落地一组基于 ast-grep CLI 的结构化工具面，而不是单个 `AstGrepTool`。现有实现包括：

- `ast_grep_search`
- `ast_grep_preview`
- `ast_grep_replace`

它们共同提供了结构化搜索、预览与替换能力，并保持了 tool contract 风格与 workspace-boundary 约束。

**为什么：** 现有 `GrepTool` 只能做文本搜索，`CodeSearchTool` 面向 web 搜索。两者都不理解语法结构。ast-grep 填补了这个空缺，让结构化查找/替换成为可能，而不需要 VoidCode 自己实现 AST parser 层。

**已落地位置：**

- `src/voidcode/tools/ast_grep.py`
- `src/voidcode/runtime/tool_provider.py`
- `tests/unit/tools/test_ast_grep_tool.py`

**当前实现说明：**

- 仓库并不是暴露一个统一的 `AstGrepTool`
- 而是把搜索、预览、替换拆成了更清晰的三个工具定义
- `tool_provider` 已直接注册这些工具
- 单元测试已经覆盖当前工具形态

**当前验证方式：**

```bash
uv run pytest tests/unit/tools/test_ast_grep_tool.py -v
mise run typecheck
```

**当前已验证的价值：**

- 在仓库内提供 AST-aware 的结构化搜索/替换能力
- 和现有 runtime tool surface 对齐，而不是额外发明一层平行接口
- 在缺少外部 ast-grep CLI 时保持可诊断、可降级的行为

---

### P0.2：Formatter-Aware Edit Closure *(已落地)*

**是什么：** 当前 `EditTool` 已经支持在编辑后执行 formatter，并基于格式化后的最终文件状态闭合 edit loop。

**为什么：** 这一步直接回答了“代码改完以后如何保持格式化、读取结果和实际文件状态一致”的问题，也是后续稳定编辑链路的关键基础。

**已落地位置：**

- `src/voidcode/tools/edit.py`
- `src/voidcode/tools/_formatter.py`
- 相关测试：`tests/unit/tools/test_edit_tool.py`

**当前实现说明：**

- `EditTool` 已经直接接收 `hooks_config`
- 工具内部使用 `FormatterExecutor`
- 编辑后的 formatter 执行与结果处理，已经不是最初草案里“从 `.voidcode.json` 手动查 preset，再调用 subprocess”的第一版思路

**当前验证方式：**

```bash
uv run pytest tests/unit/tools/test_edit_tool.py -v
mise run lint
```

**当前已验证的价值：**

- formatter 缺失或失败时保持可诊断、可降级
- 编辑后的最终文件状态与返回结果保持更高一致性
- 避免“写入成功但格式化后实际文件内容与预期脱节”的问题

---

### P0.3：Doctor / Capability Check Tool *(已落地，且仓库中采用了特定落点)*

**是什么：** 当前仓库已经落地 runtime capability doctor，用于检查外部工具与 capability 是否就绪，包括 ast-grep、formatter presets、LSP servers 与 MCP servers。

**为什么：** 这一步直接回答“当前环境为什么不能工作、缺什么、该怎么修”，降低首次使用门槛，也为 agent / operator 提供统一的 readiness diagnostics。

**仓库中的实际落点：**

- `src/voidcode/doctor/`
- `src/voidcode/cli.py`
- 相关测试：`tests/unit/doctor/test_doctor.py`

**当前实现说明：**

- doctor 并不是 `src/voidcode/tools/doctor.py` 下的一个普通工具
- 它以 `CapabilityDoctor` 的形式存在，并通过 `voidcode doctor` CLI surface 暴露
- 当前能力检查范围已覆盖外部可执行文件、formatter presets、LSP server 与 MCP server

**当前验证方式：**

```bash
uv run pytest tests/unit/doctor/test_doctor.py -v
uv run voidcode doctor --workspace .
```

**当前已验证的价值：**

- 对外部能力就绪性给出统一诊断入口
- 保持 non-blocking、结构化输出风格
- 能清晰区分哪些能力可用、哪些缺失、以及缺失点集中在哪个层级

---

## 第 1 阶段（P1）：增强编辑能力

### P1.1：轻量级重构工作流

**是什么：** 把 P0.1 中已经落地的 `ast_grep_search` / `ast_grep_preview` / `ast_grep_replace` 与 `EditTool` 或 `MultiEditTool` 组合起来，形成一个 read-then-edit 的 refactor workflow。新的 `RefactorTool` 提供 `pattern` + `rewrite` 形式的接口，底层复用现有 ast-grep 系列工具，但输出保持 voidcode 自己的 tool result 格式。

**要创建的文件：** `src/voidcode/tools/refactor.py`

**实现方式：** 这是一个组合型工具，负责：
1. 使用 pattern 和 `replace` 参数调用现有 ast-grep 系列工具
2. 如果 ast-grep 不可用，返回附带说明的错误
3. 如果 ast-grep 可用，则返回结构化匹配数据并应用 rewrite

**为什么不直接用 ast-grep？** 因为这个工具可以提供 voidcode-native 的错误处理、diff 生成，以及基于现有 `multi_edit` 模式的多文件 rewrite 协调。

**要创建的测试文件：** `tests/unit/tools/test_refactor_tool.py`

---

### P1.2：更丰富的 Edit Mismatch Diagnostics

**是什么：** 当 `edit.py` 找不到 `oldString` 时，输出更多诊断信息：附近代码片段、如果 `BlockAnchorReplacer` 几乎匹配时给出建议，以及列出所有尝试过的 replacer。

**要修改的文件：** `src/voidcode/tools/edit.py`

**插入位置：** 原计划是在 line 305 的 `ValueError`：`raise ValueError("Could not find oldString in the file using replacers.")`

**实现草图：**

```python
# At line 305 in _replace()
# Build diagnostic message
diagnostic_lines = [f"Could not find oldString in the file. Tried {len(replacers)} replacers:"]
for replacer in replacers:
    diagnostic_lines.append(f"  - {replacer.__name__}")

# Try BlockAnchorReplacer even if it didn't match, to give a hint
if "BlockAnchorReplacer" not in [r.__name__ for r in replacers]:
    block_hints = BlockAnchorReplacer.find(content, old_string)
    if block_hints:
        diagnostic_lines.append(f"Hint: BlockAnchorReplacer found {len(block_hints)} near-matches")

raise ValueError("\n".join(diagnostic_lines))
```

**验证：**

```bash
uv run pytest tests/unit/tools/test_edit_tool.py -v
# Run a test where oldString has a slight mismatch and confirm diagnostic output
```

**验收标准：**
- 错误消息列出所有尝试过的 replacer
- 当存在近似匹配时，会提示 `BlockAnchorReplacer` 的 near-match
- 错误消息可读且具备可操作性

---

## 第 2 阶段（P2）：高级编辑能力

### P2.1：Hashline / Hash-Anchored Editing

**是什么：** 一个新的 `HashAnchorEditTool`，使用内容哈希（或 line-number + content-hash）作为编辑锚点，使编辑对目标上方的插入/删除更加稳健。

**为什么：** 当前 replacer 通过文本内容定位编辑位置。如果 agent 在目标区域上方插入了新行，行号会漂移，后续编辑可能误命中。hash-anchored 方案的思路是：对目标区域计算稳定哈希，并将其作为锚点。

**实现方式：** 这一阶段仍然是推测性的，只有在 P0 与 P1 验证后仍发现多步编辑容易脆弱时，才应继续推进。大致方向是：

1. 对给定 `oldString` 计算每行或某个行范围的 rolling hash
2. 存储 `(start_line, content_hash, end_line)` 作为锚点
3. 在应用时先按 hash 找到锚定区域，再在该区域内做文本替换
4. 这和 `BlockAnchorReplacer` 的思路类似，但把 fuzzy string matching 换成了更稳定的哈希锚定

**未来要创建的文件：** `src/voidcode/tools/hash_anchor_edit.py`

**风险：** 复杂度较高。只有当 P0 + P1 的编辑链路对多步编辑场景仍然明显脆弱时，才值得继续投入。

---

## 文件汇总

| 文件 | 动作 | 阶段 |
|------|------|------|
| `src/voidcode/tools/ast_grep.py` | Landed | P0 |
| `tests/unit/tools/test_ast_grep_tool.py` | Landed | P0 |
| `src/voidcode/tools/edit.py` | Modify (formatter closure + diagnostics) | P0 + P1 |
| `src/voidcode/doctor/` | Landed | P0 |
| `tests/unit/doctor/test_doctor.py` | Landed | P0 |
| `src/voidcode/runtime/tool_provider.py` | Landed (register ast_grep tools) | P0 |
| `tests/unit/tools/test_edit_tool.py` | Modify (add formatter tests) | P0 |
| `src/voidcode/tools/refactor.py` | Create | P1 |
| `tests/unit/tools/test_refactor_tool.py` | Create | P1 |
| `src/voidcode/tools/hash_anchor_edit.py` | Create (future) | P2 |

---

## 验证策略

1. **P0.1（ast-grep）：** `uv run pytest tests/unit/tools/test_ast_grep_tool.py -v`
   `mise run typecheck` 通过

2. **P0.2（formatter-aware edit）：**
   `uv run pytest tests/unit/tools/test_edit_tool.py -v`（所有现有测试仍通过）
   新增带 formatter preset 的测试，验证 re-read 行为
   `mise run lint`

3. **P0.3（doctor）：**
   `uv run pytest tests/unit/doctor/test_doctor.py -v`
   `uv run voidcode doctor --workspace .` 返回 capability doctor 结果

4. **P1.1（refactor）：** `uv run pytest tests/unit/tools/test_refactor_tool.py -v`
   工具会出现在 `ToolRegistry.with_defaults().definitions()` 中

5. **P1.2（diagnostics）：** 失败的编辑错误消息包含 replacer 列表和提示信息

---

## 需要避免的风险

1. **不要宣称仓库拥有其实并不存在的 AST 能力。** Voidcode 目前没有内建 AST parser。ast-grep 工具只是封装外部 CLI，不代表仓库内部获得了 AST parsing 能力。

2. **不要把 formatter 变成强依赖。** formatter-aware edit 必须保持 opt-in 且可优雅降级。formatter 缺失时，编辑仍应成功，只输出 warning。

3. **不要在没有充分审查的情况下修改 `src/voidcode/runtime/service.py`。** `_execute_graph_loop` 是 hot path，任何改动都必须保持最小化并带测试。

4. **不要把范围扩展到 multi-agent orchestration。** 本计划聚焦的是单 agent 的 tooling，不要跑偏。

5. **不要为了推进这份计划而顺手改写 `docs/roadmap.md` 或 `docs/architecture.md`。** 这份文档的目标是记录 tooling 采纳本身，而不是借机扩大成路线图或架构重写。

6. **不要在 P0 和 P1 验证前就实现 P2 的 hash-anchored editing。** P2 是探索性的；当前代码库的文本编辑稳健性可能已经足够，不一定真的需要这一层。

---

## 推荐的提交边界

| Commit | 内容 |
|--------|------|
| 1 | `src/voidcode/tools/ast_grep.py` + `tests/unit/tools/test_ast_grep_tool.py` |
| 2 | `src/voidcode/doctor/` + `tests/unit/doctor/test_doctor.py` |
| 3 | `src/voidcode/runtime/tool_provider.py`（register ast_grep tools） |
| 4 | `src/voidcode/tools/edit.py`（P0.2 formatter-aware closure） |
| 5 | `tests/unit/tools/test_edit_tool.py`（add formatter + diagnostics tests） |
| 6 | `src/voidcode/tools/edit.py`（P1.2 richer diagnostics） |
| 7 | `src/voidcode/tools/refactor.py` + tests（P1.1） |
| 8 | `src/voidcode/tools/hash_anchor_edit.py` + tests（P2，仅在确有需要时） |

每个提交都应在提交前通过 `mise run check`。提交信息建议使用 Conventional Commits，例如：`feat:`、`fix:`、`test:`。

---

## 依赖

- 为了让 P0.1 完整可用，需要安装 ast-grep CLI（`brew install ast-grep` / `pip install ast-grep`）；但仍必须支持 graceful degradation，使相关工具在缺少 ast-grep 时依然可用。
- 其余工作均为纯 Python 且自包含。
