# 文档入口

VoidCode 的行为以源码和测试为准。仓库只保留以下需要稳定引用的边界文档：

- [`coding-standards.md`](./coding-standards.md) — 贡献、编码与提交规范。
- [`contracts/README.md`](./contracts/README.md) — runtime、客户端与 agent-facing 契约总表。
- [`testing.md`](./testing.md) — 测试策略：核心范围、新增测试的规则、刻意不覆盖的区域。

契约目录中的文件描述稳定的 runtime 边界；实现细节请直接阅读 `src/voidcode/`，不要从历史设计或审计记录推断当前行为。

## 审计记录

- [`audits/pi-voidcode-extensibility.md`](./audits/pi-voidcode-extensibility.md) — pi × VoidCode 可扩展性架构审计，记录 lower-core、组合边界与迁移缺口。**审计是写定时刻的历史快照，不随代码演进维护；当前行为只以 `src/` 与实际验证为准，禁止把历史审计与当前实现同步改写。**

## 实施计划

- [`plans/agent-core-refactor.md`](./plans/agent-core-refactor.md) — P0–P6 重构计划与验收证据；实现路径已完成，具体验证范围与截图恢复限制见计划当前状态。
- [`plans/tui-productization.md`](./plans/tui-productization.md) — TUI P0–P5 产品化与 clean-cutover 计划，涵盖主题/配置、原生光标、布局/状态栏、操作、审批/detail viewer 与验收边界；当前实现与已运行证据见计划末尾，Windows/macOS 及完整发布门禁仍未验收。
