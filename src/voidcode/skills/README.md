# `voidcode.skills`

这里是 skills 能力层的预期目录，用来承载 skill manifest、发现规则、元数据解析以及可复用的 skill 注册定义。

## 负责什么

- skill manifest 格式和解析规则
- skill 搜索路径约定
- 可复用的 skill 元数据模型
- 不依赖 runtime session 状态的纯 registry 辅助逻辑

## 不负责什么

- runtime prompt 组装
- 与 session 绑定的 applied skill 状态
- runtime 事件发射
- 请求执行语义

## 与 runtime 的边界

`src/voidcode/runtime/skills.py` 目前仍是 runtime 集成层，至少要等 manifest 解析和 discovery helper 被抽离后，才适合把更多实现放入这个包。runtime 仍应持有生效配置和面向 session 的行为。

## 当前状态

这个目录目前是规划中的能力层。现有实现仍然位于 `src/voidcode/runtime/skills.py`。
