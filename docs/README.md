# 文档入口

VoidCode 的行为以源码和测试为准。仓库只保留以下需要稳定引用的边界文档：

- [`coding-standards.md`](./coding-standards.md) — 贡献、编码与提交规范。
- [`contracts/README.md`](./contracts/README.md) — runtime、客户端与 agent-facing 契约总表。
- [`testing.md`](./testing.md) — 测试策略：核心范围、新增测试的规则、刻意不覆盖的区域。

契约目录中的文件描述稳定的 runtime 边界；实现细节请直接阅读 `src/voidcode/`，不要从历史设计或审计记录推断当前行为。

## 审计记录

- [`audits/omp-alignment.md`](./audits/omp-alignment.md) — 与 omp 对齐审计（工具面 / runtime / CLI / TUI），记录已对齐项、未对齐缺口与文档-代码不一致。**该审计是写定时刻的历史快照，不随代码演进维护：禁止把它与当前代码同步、也禁止依据它更新它；当前行为只以 `src/` 与 `tests/` 为准，审计中任何过期的行都不构成 bug 证据。**
