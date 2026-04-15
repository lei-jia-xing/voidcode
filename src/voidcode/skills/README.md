# `voidcode.skills`

这里是 `skills` 的纯能力层目录。当前它已经承载 skill manifest、发现规则、元数据解析以及可复用的 skill registry primitive，不再只是预期中的规划目录。

## 负责什么

- skill manifest 格式和解析规则
- skill 搜索路径约定
- 可复用的 skill 元数据模型
- 不依赖 runtime session 状态的纯 registry 逻辑
- 面向 workspace 的本地 discovery helper

## 不负责什么

- runtime prompt 组装
- 与 session 绑定的 applied skill 状态
- runtime 事件发射
- 请求执行语义

## 与 runtime 的边界

`src/voidcode/runtime/skills.py` 现在是一个很薄的 runtime 集成层，而不是 skill 主实现层。

当前边界如下：

- `src/voidcode/skills/`
  负责 skill 的纯能力实现：
  - metadata model
  - manifest / frontmatter 解析
  - 本地 discovery
  - registry primitive

- `src/voidcode/runtime/skills.py`
  负责 runtime 专属表达：
  - `SkillRuntimeContext`
  - `build_runtime_contexts()`

- `src/voidcode/runtime/service.py`
  负责 runtime 主流程中的 skill 接入：
  - skills enabled / paths 的生效配置
  - `runtime.skills_loaded` / `runtime.skills_applied`
  - applied skill payload persistence / replay
  - run / stream / resume 路径中的 skill 传递

换句话说，runtime 不再通过 `runtime/skills.py` 持有 skill 主实现，而是直接消费 `voidcode.skills` 的纯能力结果，并在需要 runtime-facing context 时调用 `runtime/skills.py` 做转换。

## 当前状态

当前这个目录已经包含以下实现：

- [models.py](./models.py)
  - `SkillMetadata`

- [manifest.py](./manifest.py)
  - `parse_skill_frontmatter()`
  - `parse_skill_body()`

- [discovery.py](./discovery.py)
  - `LocalSkillMetadataLoader`
  - `resolve_workspace_relative_path()`
  - `DEFAULT_SKILL_SEARCH_PATHS`

- [registry.py](./registry.py)
  - `SkillRegistry`

- [__init__.py](./__init__.py)
  - 能力层稳定导出入口

因此，`#96` 的核心目标已经实现：纯 discovery / parsing / registry 实现已经从 `runtime/skills.py` 中抽离出来。

## runtime 集成层现状

当前的 [runtime/skills.py](../runtime/skills.py) 只保留 runtime 专属内容：

- `SkillRuntimeContext`
- `build_runtime_contexts()`

它的作用是把 `voidcode.skills.SkillRegistry` 中的纯 skill 元数据，转换成 runtime 主流程可消费的 `SkillRuntimeContext`。

## 测试策略

当前测试分层如下：

- [tests/unit/skills/test_skills.py](../../../tests/unit/skills/test_skills.py)
  - 更偏向 `voidcode.skills` 能力层测试
  - 关注 manifest、discovery、registry 这类纯 skill 功能

- [tests/unit/runtime/test_skills.py](../../../tests/unit/runtime/test_skills.py)
  - 更偏向 runtime 侧检测
  - 虽然仍覆盖了一部分 skill 相关内容，但语义上更接近 runtime 集成验证

- [tests/unit/runtime/test_runtime_service_extensions.py](../../../tests/unit/runtime/test_runtime_service_extensions.py)
  - 关注 runtime 主流程中的 skill 接入
  - 包括 `runtime.skills_loaded`
  - `runtime.skills_applied`
  - applied skill payload persistence
  - resume 使用 frozen skill payload

## 当前结论

从当前代码状态看，`#96` 的主目标已经达成：

- `voidcode.skills` 已经成为 skill 纯能力层主实现
- `runtime/skills.py` 已经降级为 runtime 集成层
- runtime 主流程继续保持既有行为
- 测试分层已经初步形成

后续如果继续整理，重点应放在文档同步和测试归属细化，而不是再做大的结构迁移。
