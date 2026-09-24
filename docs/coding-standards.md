# 代码标准

VoidCode 当前更重视代码的清晰性、小规模变更以及可重复的验证，而不是巧妙但难以维护的实现。

## 一般预期

- 保持变更聚焦且易于评审。
- 避免在功能分支中进行无关的重构。
- 当行为、工作流或 CLI 界面发生变化时，更新文档。
- 相比于隐式行为，更倾向于显式的、类型化的代码。

## Python

- 符合现有的运行时 (runtime)/图 (graph)/工具 (tools) 边界。
- 在可行的情况下保持函数短小且确定性。
- Runtime changes must follow the current contracts in `docs/contracts/`; implementation ownership remains in `src/voidcode/runtime/`.
- 使用 Ruff 进行格式化/代码检查，并保持 ty 类型检查清理。
- 当行为改变时增加或更新测试。
- 除非必要，避免引入新的依赖。

## cast 与 fallback

- `cast` 只在**命名边界**上使用，并在代码里写明边界：JSON/HTTP 解码（`json.loads` → `dict[str, object]`）、第三方 SDK 桩（`Any` kwargs、`Omit` 哨兵、联合返回）、无类型动态查找（ASGI `Scope`、`__dict__` monkeypatch）、pydantic `errors()` 字典、**持久化/解码读取**（SQLite `sqlite3.Row` 的 `row["col"]`，以及已落盘 JSON blob 上对 `raw_*`/`payload`/`metadata`/`state` 的 `.get(...)`/`["..."]`）。
- `runtime/storage/**` 有 110 处这类 `cast`，因为行本身没有类型。**已知上限**：升级路径是每个表一个 `row → TypedDict` 解码函数，把 `cast` 收进解码处；本轮不做，避免动落盘契约。
- fallback 判定已定案，不再重开：真正可选值上的 `is not None else`、带声明优先级的来源合并、恒等映射（`.get(name, name)`）都合法；约 40 处"替换成具体值"的三元与 3 处契约键 `.get(k, default)` 候选是低风险保留。
- 其余已知上限（本轮明确接受，不是遗忘）：约 152 处对内部无类型 `dict[str, object]` 状态（graph/todo/hook/config 的 `state[...]`/`phase["tasks"]`/`preset[...]`）的容器 `cast`，升级路径是每个状态对象一个 TypedDict/dataclass；10 处 TypedDict 断言（`RuntimeRequestMetadataPayload` 等，指对**未校验** dict 的断言，不含对已验证 payload 的 `dict → TypedDict` 转换），升级路径是 TypedDict 构造/校验；以及少数 literal/enum 形状的站点，各自需要一个契约决定（持久化 token 的校验、CLI 参数边界）。
- 读无类型 payload 时，只读用途优先标注 `Mapping[str, object]`：它表达的是**只读、协变的契约**（值类型更窄的 `dict[str, str]` 之类源仍可赋值），而不是类型检查器的硬性要求；源本身就是本地新建的 `dict` 时，直接写 `dict[str, object]`。
- 边界解析的范例是 `src/voidcode/provider/provider_table.py`：每个字段一种校验器（`_string_tuple` 形状），`Literal` 集合用 `TypeIs` 谓词收窄（`model_match.is_matcher`），而不是每个调用点一个 `cast`；容器边界处的 `cast(dict[str, object], ...)` 用来阻止 `Any` 向下游扩散。
- 可选性用 `T | None` 表达并由消费方显式处理；契约要求的值缺失就直接 `raise`。请求体里"必填、但要有自己错误句"的字段用 `Field(default=None, validate_default=True)` + before 校验器，而不是 `T | None` 加路由处 `cast(T, ...)`。
- fallback 只在**带声明优先级的来源合并**时合法：discovered 拥有 X，shipped 只填未设置的 Y（范例 `src/voidcode/provider/registry.py` 的 `ModelProviderRegistry.model_metadata_for_model`），合并处写清谁优先；`.get(name, name)` 这类恒等映射同样合法。
- 契约键缺失必须抛出，不得 `.get(k, default)` / `getattr(obj, name, default)` 兜底："缺失 ≠ 零/空"，`or 0` / `or ""` 把未知当成已知。确需保留的默认值（`_OUTPUT_CAP_WHEN_UNKNOWN`、`-1` 预算哨兵、未知计费桶写 `0.0`）必须用一行注释写明优先级与已知上限。
- 静默 `except: pass` 只允许作为有意的探测并注释说明；其余异常必须记录、重抛或转成有类型的错误。

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
- `feat(api)!: require response schema v2`
