# Product, Transport, Web & Engineering TODO

涵盖外层产品装配（`bootstrap.py`、`config.py`、`cli.py`）、传输与 Web 前端（`transport/`、`frontend/`）、文档与打包（`docs/`、`pyproject.toml`、`tests/`）。

## P0：开箱即用与能力接线

- [x] **修复 `bootstrap.py` 遗漏 bundled 插件 `find`。**
  - `src/voidcode/plugins/find/` 已有完整实现、README 与单测，但 `BUNDLED_PLUGIN_IDS`（`bootstrap.py`）此前漏掉了 `"find"`；现已接入并验证。
- [ ] **首次运行引导与诊断归入未来 TUI，CLI 不再新增 `init`、不保留 `doctor`。**
  - [x] bare CLI 交互式启动异常已改为向 stderr 打印明文错误，不再输出裸 JSON。
  - 不再在 CLI 新增 `init` / `doctor`：首次运行配置引导与完整诊断属于未来 TUI 的能力，将在 TUI 中实现。

## P1：Web 前端与 CLI 完整度

- [ ] **补齐 Web 客户端（`frontend/src/core/`）与 Core/TUI 的功能对等。**
  - [x] `CoreApp.tsx` 已从持久 `TranscriptRun` 渲染 `failed` 运行失败态与 `interrupted` 详细原因。
  - `CoreApp.tsx` 渲染 `turn` 时丢弃 `reasoning` 思考内容，且未接入 `provider_progress` / `tool_progress` 流式输出。
  - `CoreApp.tsx` 在 `isRunning` 时禁用输入框与发送按钮，无法发送排队追问。
  - 补齐 `core.ask_requested` / `pending_ask` 问答交互与图片附件粘贴/上传。
- [ ] **补齐 `cli.py` 子命令参数与交互能力。**
  - `resume` 子命令补充 `--interaction`、`--root-action-grant-id` 与（待 Core 落地后的）拒绝理由；补充 `answer-ask` / `cancel-ask` 子命令与已有的 RPC 能力对齐。

## P2：配置收敛、文档一致性与工程卫生

- [ ] **清理 `bootstrap.py`、`config.py`、`cli.py` 的配置多重投影与重复解析。**
  - 例如统一 `cli.py` 的 `_session_payload` 与 `transport/projection.py` 的 `session_payload`，消除外层多处重复拼装。
- [ ] **修正过时与自相矛盾的文档。**
  - 修复 `docs/README.md:45` 与 `README.md:20` 指向已删除 `docs/TODO.md` 的失效链接。
  - 消除 `docs/configuration.md` 第 5 行（支持无配置文件从环境变量发现 provider）与第 244 行（“仅设置 API key 环境变量不会配置 provider”）的矛盾。
  - 删除 `docs/command-ux.md` 第 20 行与第 26 行重复的 `skills, skills.show` 条目。
- [ ] **清理 `pyproject.toml` 未用依赖并收敛剩余测试夹具副本。**
  - `click`、`tenacity`、`pydantic-settings`、`rapidfuzz` 在 `src/`、`tests/`、`scripts/` 中均无引用。
  - 将仍保留私有 core/loopback 夹具副本的 6 个测试文件（`tests/fixtures/s5_plugin.py`、`test_goal_plugin.py`、`test_cancellation.py`、`test_followup_image_refs.py`、`test_process_lease.py`、`test_find_plugin.py`）收敛至 `tests/fixtures/core_factory.py`。
