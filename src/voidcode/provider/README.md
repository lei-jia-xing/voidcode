# `voidcode.provider`

这里是 provider 能力层的预期目录，用来承载 provider/model 解析契约、provider 注册中心、fallback 语义以及可复用的 provider 配置辅助逻辑。

## 负责什么

- provider 和 model 引用 schema
- resolved provider config 模型
- provider registry 与 fallback 解析辅助逻辑
- 不依赖 runtime session 状态的 provider 配置校验

## 不负责什么

- graph 执行编排
- runtime 重试循环和 provider attempt 状态
- session metadata 持久化
- runtime 错误与事件路由

## 与 runtime 的边界

runtime 继续持有 session 内的生效 provider config 解析、provider attempt 跟踪以及 fallback 执行流程。这个包的目标是承载那些可被 runtime 消费的纯 provider control-plane 原语。

## 当前状态

provider 纯能力原语已按职责拆分到：

- `src/voidcode/provider/protocol.py`
- `src/voidcode/provider/config.py`
- `src/voidcode/provider/models.py`
- `src/voidcode/provider/registry.py`
- `src/voidcode/provider/resolution.py`
- `src/voidcode/provider/snapshot.py`
- `src/voidcode/provider/errors.py`

`runtime/service.py` 负责消费这些原语并持有运行期状态（如 session metadata、provider attempt 与 fallback 执行流程）。
