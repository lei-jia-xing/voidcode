# Product, Transport, Web & Engineering TODO

涵盖外层产品装配（`bootstrap.py`、`config.py`、`cli.py`）、传输与 Web 前端（`transport/`、`frontend/`）、文档与打包（`docs/`、`pyproject.toml`、`tests/`）。

## P0：开箱即用与能力接线

- [x] **修复 `bootstrap.py` 遗漏 bundled 插件 `find`。**
  - `src/voidcode/plugins/find/` 已有完整实现、README 与单测，但 `BUNDLED_PLUGIN_IDS`（`bootstrap.py`）此前漏掉了 `"find"`；现已接入并验证。
- [ ] **首次运行引导与诊断归入未来 TUI，CLI 不再新增 `init`、不保留 `doctor`。**
  - [x] bare CLI 交互式启动异常已改为向 stderr 打印明文错误，不再输出裸 JSON。
  - 不再在 CLI 新增 `init` / `doctor`：首次运行配置引导与完整诊断属于未来 TUI 的能力，将在 TUI 中实现。

## P1：配置迁移、CLI 完整度与 Web 现状

- [~] **Web 前端（`frontend/`）已停止维护与功能追赶。**
  - 用户决策不再继续维护前端页面（维护成本过高）；原计划的 `reasoning`/`provider_progress`/`tool_progress` 流式输出、`isRunning` 排队追问、`pending_ask` 问答交互与图片上传等前端对等项**不再实施**。
  - `[x]` `CoreApp.tsx` 已从持久 `TranscriptRun` 渲染 `failed` 运行失败态与 `interrupted` 详细原因；`transport/http.py` 仍作为底层 HTTP/RPC 入口保留，已修复 CORS `OPTIONS` 预检先于 Bearer 鉴权、`401` 响应附带 CORS 头、`frontend_dist` 缺 `index.html`/缺失 `assets/*` 误返 `200` 与 `ParkRevisionError` -> `409 stale_revision` 映射。
- [x] **产品配置直接切换为 TOML（`voidcode.toml`），不保留任何 JSON 过渡或历史包袱。**
  - `config.py` 新增标准库 `tomllib` 解析与 `dumps_toml` 规范序列化，`CONFIG_FILE_NAME = "voidcode.toml"`；支持 `tui.banner = false` 与 `providers.<name>.api_key = false` 原生 TOML 语义；同步更新 `bootstrap.py`、`cli.py`、`core/engine/__init__.py`、`tui/`、`plugins/`、示例、文档与全量测试。
- [x] **补齐 `cli.py` 子命令参数与交互能力。**
  - `resume` 子命令现支持 `--reason`、`--grant-policy` 与 `--interaction`；`run` 现补齐 `--interaction`；新增 `approval-policies` 查询子命令与 `answer-ask` / `cancel-ask` 子命令，并在 `open_core` 前校验 `answer-ask --values` JSON。

## P2：配置收敛、文档一致性与工程卫生

- [ ] **清理 `bootstrap.py`、`config.py`、`cli.py` 的配置多重投影与重复解析。**
  - `[x]` `cli.py` 已复用 `transport/projection.py` 的 `session_payload`，移除重复 `_session_payload`。
  - 残留：进一步收敛 `bootstrap.py` 与 `config.py` 中重复的 provider/plugin 装配校验路径。
- [ ] **修正过时与自相矛盾的文档。**
  - `[x]` 已修复 `docs/README.md:45` 与 `README.md:20` 失效链接、`docs/configuration.md` provider 描述、`docs/command-ux.md` 重复 `skills, skills.show` 条目。
  - `[x]` 已修复 `docs/README.md:45`、`README.md:20`、`docs/configuration.md:65` 与 `docs/command-ux.md` 过时/矛盾描述；仅剩 `docs/CHANGELOG.md:276` 历史记录条目标注。
- [ ] **清理 `pyproject.toml` 未用依赖并收敛剩余测试夹具副本。**
  - [x] `click`、`tenacity`、`pydantic-settings`、`rapidfuzz` 在 `src/`、`tests/`、`scripts/` 中均无引用；已从 `pyproject.toml` 移除并由 `uv lock` 更新锁文件。
  - [x] 已核查列出的 6 个文件并收敛可机械替换的副本：`test_process_lease.py`、`test_cancellation.py`、`test_followup_image_refs.py`、`test_find_plugin.py` 与 integration `test_goal_plugin.py` 改用 `core_factory`；`tests/fixtures/s5_plugin.py` 与 unit `test_goal_plugin.py` 不含 core/loopback 夹具。goal/find 的配置内联保留（分别需要 files 启用与无 path 插件），其余测试文件的本地 loopback 副本不在本项列出的 6 个文件内。
