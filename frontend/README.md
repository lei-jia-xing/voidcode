# VoidCode Web 前端

VoidCode 的 Web 前端，基于 React、TypeScript 和 Bun 构建。

## 快速上手

```bash
# 安装依赖 (使用 bun)
bun install

# 启动开发服务器
bun run dev

# 生产环境构建
bun run build

# 预览生产环境构建
bun run preview
```

## 开发

### 可用脚本

- `bun run dev` - 启动带有 HMR 的 Vite 开发服务器
- `bun run build` - 类型检查并进行生产环境构建
- `bun run preview` - 在本地预览生产环境构建结果
- `bun run lint` - 运行 ESLint
- `bun run format` - 使用 Prettier 格式化代码
- `bun run typecheck` - 运行 TypeScript 类型检查
- `bun run test` - 使用 Vitest 在 jsdom 环境中运行一次测试
- `bun run test:watch` - 使用 Vitest 进入监听模式
- `bun run test:coverage` - 运行测试并生成覆盖率报告
- `bun run test:e2e` - 使用 Playwright 运行 Web launcher 端到端测试
    > 不要使用裸 `bun test` 运行 Vitest 测试；它会调用 Bun 原生 runner，绕过 Vite/Vitest 的 jsdom 配置。

### 技术栈

- **构建工具**: [Vite](https://vitejs.dev/) + [Bun](https://bun.sh/)
- **框架**: [React](https://react.dev/) 19
- **语言**: [TypeScript](https://www.typescriptlang.org/)
- **样式**: [Tailwind CSS](https://tailwindcss.com/)
- **状态管理**: [Zustand](https://github.com/pmndrs/zustand)
- **图标**: [Lucide React](https://lucide.dev/)

### 项目结构

> **注意：** 下方描述的是当前真实目录树，而不是预期中的未来结构。前端目前仍然相对扁平，核心逻辑主要集中在 `src/App.tsx`、`src/main.tsx`、`src/store/`、`src/lib/runtime/`、`src/i18n/` 与 `src/components/`。

```
frontend/
├── src/
│   ├── App.tsx           # 当前主界面壳层
│   ├── main.tsx          # React 入口
│   ├── components/       # 运行时驱动面板与子组件
│   ├── i18n/             # 国际化初始化与文案
│   ├── lib/runtime/      # 运行时 HTTP/SSE 客户端
│   ├── store/            # Zustand 状态存储
│   └── index.css         # 全局样式入口
├── public/               # 静态资源
└── index.html            # 入口 HTML
```

## 实现状态

> **重要提示：** 当前前端仍然是一个偏轻量的 Web 客户端，但已经具备最小可用的运行时传输路径：会话列表、会话重放、流式运行、审批/question 处理、review tree / diff、workspace 切换与 runtime status 都可以通过本地 HTTP/SSE 后端完成。更完整的运行时驱动交互体验仍在继续演进。

- [x] UI 壳程序与导航
- [x] 运行时驱动的会话列表 / 会话重放 / 流式运行 / 审批处理 / question answer 基础路径
- [x] 运行时驱动的 review tree / diff 面板与轻量 file-tree 入口
- [x] 以规范 `tool_call_id` 关联工具活动、按事件类型读取名称和生命周期状态，并以匹配的 `tool_status` 补充展示
- [ ] 更完整的运行时驱动任务体验
- [ ] 更丰富的客户端交互与状态打磨

## 架构

前端设计通过以下方式与 VoidCode 运行时进行通信：

1. **HTTP API / SSE** - 用于会话管理、会话重放和流式运行事件交付

工具事件按类型消费，调用身份始终来自事件 payload 的 `tool_call_id`。持久化的 `core.tool_request_created`、`runtime.tool_started` 和 `runtime.tool_completed` 使用 `tool`；provider 流式 `core.tool_call_start/delta/end` 使用可缺省的 `tool_name`，并按 `tool_call_id` 延续同一条流；`runtime.tool_progress` 也使用 `tool_call_id`。请求和开始状态由事件类型决定，完成状态只认 `runtime.tool_completed` 的 `ok` / `error`；`tool_status` 仅补充匹配调用的展示信息。`runtime.todo_updated` 更新独立的 todo 面板状态，不会创建或改写工具活动行；审批参数读取 payload 的 `arguments`。

单次运行仅在当前运行的会话状态报告 `completed` 时才视为完成；`core.response_ready` 与 SSE EOF 本身都不能证明运行已完成。事件流结束但未收到当前运行的会话状态时，界面保留“结果尚未确认”；provider 尝试错误仅作为诊断，传输错误与运行时任务失败分开展示。provider usage 的 `null` 代表未知，不按零累计或标注为完整总计。

**后端启动与 provider 选择：** 前端不会把 API key 写入浏览器状态或请求体。运行需要已配置的 provider/model；启动仅用于 API / SSE 联调的后端：

```bash
uv run voidcode serve --workspace . --port 8765
```

如果后端使用其他地址，设置 `VITE_BACKEND_URL` 后再启动 Vite；该值必须是后端的完整 HTTP(S) URL：

```bash
VITE_BACKEND_URL=http://127.0.0.1:9000 bun run dev
```

需要真实 provider 时只配置一个明确的 provider/model，例如：

```bash
OPENAI_API_KEY=<your-key> VOIDCODE_MODEL=openai/gpt-4o-mini \
  uv run voidcode serve --workspace . --port 8765
```

如果需要启动完整 Web launcher，先构建前端资源，然后运行。launcher 是独立于 Vite dev proxy 的单一服务，因此可继续使用显式指定的 launcher 端口：

```bash
mise run frontend:build
uv run voidcode web --workspace . --port 8000 --no-open
```

自动化测试或脚本应使用 `--no-open`，避免 launcher 在 Playwright 或 CI 流程中额外弹出浏览器窗口。

## 贡献

请遵循与主项目相同的指南：

- 从仓库根目录操作时，优先使用根目录的 `mise run frontend:*` 任务；在 `frontend/` 目录直接使用 `bun install` 和 `bun run ...` 也是等价的。
- 提交前运行 `bun run lint`
- 运行 `bun run typecheck` 以确保类型安全
- 针对组件覆盖率变更运行 `bun run test`
- 针对 launcher / 浏览器路径变更运行 `bun run test:e2e`
- 遵循现有的代码风格
