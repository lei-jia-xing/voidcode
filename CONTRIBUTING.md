# 贡献指南

感谢你对 VoidCode 的贡献。本项目目前仍处于 pre-MVP 阶段，因此清晰的沟通、小的可评审变更以及可重复的本地验证比开发速度更重要。

## 开发环境搭建

推荐的本地环境使用 uv 管理的 Python 环境。支持的 Python 版本为 3.14。

```bash
mise install
uv sync --extra dev
uv run voidcode --help
```

可选但推荐的操作：

```bash
uv run pre-commit install
```

## 代码风格与质量门禁

请参阅 [`docs/coding-standards.md`](./docs/coding-standards.md) 了解仓库的代码标准。

VoidCode 目前使用：

### Python
- **Ruff** 用于 lint 检查和代码格式化
- **basedpyright** 用于静态类型检查
- **pytest** 用于测试

### 前端 (Bun)
- **ESLint** 用于 lint 检查
- **Prettier** 用于代码格式化
- **TypeScript** 用于类型检查

使用 `mise` 运行标准检查：

```bash
mise run lint
mise run format
mise run typecheck
mise run test
mise run check
mise run pre-commit
```

需要时也可以直接使用 `uv` 命令：

```bash
uv run ruff check .
uv run ruff format .
uv run basedpyright --warnings src
uv run pytest
uv run pre-commit run --all-files
```

## 测试预期

请在可行的情况下，为行为变更添加或更新测试。

- 在开启 Pull Request 之前，请在本地运行 `pytest`。
- 保持类型检查和 lint 检查无错误。
- 如果你添加或更改了 CLI、运行时、图（graph）或工具的行为，请在存在测试覆盖面时为新行为包含测试覆盖。

## Pull Request 流程

1. 从最新的分支开始开发。
2. 保持变更聚焦，并在 PR 描述中解释原因。
3. 在请求评审前，先在本地运行 lint 检查、类型检查、测试和 pre-commit。
4. 当行为或工作流发生变化时，更新面向用户的文档。
5. 等待评审，并通过后续提交来解决反馈意见。

## 行为准则

通过参与本项目，你同意遵守 [`CODE_OF_CONDUCT.md`](./CODE_OF_CONDUCT.md) 中的准则。

## 安全问题

请不要为安全性敏感的报告开启公开 Issue。请遵循 [`SECURITY.md`](./SECURITY.md) 中的报告指南。
