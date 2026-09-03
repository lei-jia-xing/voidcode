# 文档入口

VoidCode 的行为以源码和测试为准。仓库只保留以下需要稳定引用的边界文档：

- [`coding-standards.md`](./coding-standards.md) — 贡献、编码与提交规范。
- [`contracts/README.md`](./contracts/README.md) — runtime、客户端与 agent-facing 契约总表。

契约目录中的文件描述稳定的 runtime 边界；实现细节请直接阅读 `src/voidcode/`，不要从历史设计或审计记录推断当前行为。
