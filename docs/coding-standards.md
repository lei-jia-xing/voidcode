# 代码标准

VoidCode 仍处于 pre-MVP 阶段。相比于巧妙的代码，我们更倾向于代码的清晰性、小规模变更以及可重复的验证。

## 一般预期

- 保持变更聚焦且易于评审。
- 避免在功能分支中进行无关的重构。
- 当行为、工作流或 CLI 界面发生变化时，更新文档。
- 相比于隐式行为，更倾向于显式的、类型化的代码。

## Python

- 符合现有的运行时 (runtime)/图 (graph)/工具 (tools) 边界。
- 在可行的情况下保持函数短小且确定性。
- 使用 Ruff 进行格式化/代码检查，并保持 basedpyright 清理。
- 当行为改变时增加或更新测试。
- 除非必要，避免引入新的依赖。

## 前端

- 保持 Bun/Vite/React 技术栈与当前外壳一致。
- 更改面向用户的文本时，保留 EN/zh-CN 支持。
- 保持状态流简单且显式。
- 不要提交生成的前端产物。

## Pull Requests

- 在开启 PR 之前运行相关检查。
- 包含 CLI 或工作流变更的手动 QA 证据。
- 保持提交 (commit) 是原子的，以便可以独立评审和回滚。

## 提交 (Commits)

- 遵循 [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/) 格式。
- 使用结构 `<type>[optional scope][!]: <description>`。
- `type` 是必须的。`scope` 是可选的。描述要简洁且使用祈使句。
- 使用 `feat` 表示新功能，`fix` 表示错误修复。常见的其他类型包括 `docs`、`refactor`、`test`、`build`、`ci`、`chore`、`perf` 和 `style`。
- 使用冒号前的 `!`、`BREAKING CHANGE:` 页脚或两者来标记重大变更 (breaking changes)。
- 仅当额外上下文有用时才添加正文或页脚。

示例：

- `feat(runtime): persist sessions in sqlite`
- `fix(cli): handle unknown session ids`
- `docs: update development guide`
- `feat(api)!: remove deprecated response shape`
