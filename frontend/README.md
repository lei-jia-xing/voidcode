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
- `bun run test` - 使用 Vitest 运行测试
- `bun run test:run` - 运行一次测试（不进入监听模式）
- `bun run test:coverage` - 运行测试并生成覆盖率报告
- `bun run test:e2e` - 使用 Playwright 运行 Web launcher 端到端测试

### 技术栈

- **构建工具**: [Vite](https://vitejs.dev/) + [Bun](https://bun.sh/)
- **框架**: [React](https://react.dev/) 18
- **语言**: [TypeScript](https://www.typescriptlang.org/)
- **样式**: [Tailwind CSS](https://tailwindcss.com/)
- **状态管理**: [Zustand](https://github.com/pmndrs/zustand)
- **数据获取**: [TanStack Query](https://tanstack.com/query)
- **图标**: [Lucide React](https://lucide.dev/)

### 项目结构

> **注意：** 下方描述的是当前真实目录树，而不是预期中的未来结构。前端目前仍然相对扁平，核心逻辑主要集中在 `src/App.tsx`、`src/main.tsx`、`src/store/`、`src/lib/runtime/`、`src/i18n/`、`src/types/` 与 `src/components/RuntimeDebug.tsx`。

```
frontend/
├── src/
│   ├── App.tsx           # 当前主界面壳层
│   ├── main.tsx          # React 入口
│   ├── components/       # 当前仅包含 RuntimeDebug 等少量组件
│   ├── i18n/             # 国际化初始化与文案
│   ├── lib/runtime/      # 运行时 HTTP/SSE 客户端
│   ├── store/            # Zustand 状态存储
│   ├── types/            # 前端类型定义（当前仍较薄）
│   └── index.css         # 全局样式入口
├── public/               # 静态资源
└── index.html            # 入口 HTML
```

## 实现状态

> **重要提示：** 当前前端仍然是一个偏轻量的 Web 客户端，但已经具备最小可用的运行时传输路径：会话列表、会话重放、流式运行、审批/question 处理、review tree / diff、workspace 切换与 runtime status 都可以通过本地 HTTP/SSE 后端完成。更完整的运行时驱动交互体验仍在继续演进。

- [x] UI 壳程序与导航
- [x] 运行时驱动的会话列表 / 会话重放 / 流式运行 / 审批处理 / question answer 基础路径
- [x] 运行时驱动的 review tree / diff 面板与轻量 file-tree 入口
- [x] 后端 `tool_status` / `display` payload 驱动的工具活动渲染基线
- [ ] 更完整的运行时驱动任务体验
- [ ] 更丰富的客户端交互与状态打磨

## 架构

前端设计通过以下方式与 VoidCode 运行时进行通信：

1. **HTTP API / SSE** - 用于会话管理、会话重放和流式运行事件交付

**注意：** 当前前端已经具备可工作的运行时传输客户端和本地后端路径，能够消费会话列表、重放数据、流式运行事件、审批/question 处理结果、review tree / diff、workspace registry、runtime status 和设置投影。它仍然不是一个完整产品化的 runtime-driven Web 客户端，但已经不只是概念验证或纯静态壳层。

默认 Web 提交会使用 `leader` 单 agent 路径和 `opencode-go/glm-5.1` 模型；除非调用方显式传入正整数预算，否则前端不会向运行时发送 `max_steps`。API Key 不会进入浏览器状态或请求体；启动后端时通过环境变量提供：

```bash
OPENCODE_API_KEY=<your-key> uv run voidcode serve --workspace . --port 8000
```

如果需要启动完整 Web launcher，则先构建前端资源，然后运行：

```bash
mise run frontend:build
uv run voidcode web --workspace . --port 8000
```

自动化测试或脚本应使用 `uv run voidcode web --no-open ...`，避免 launcher 在 Playwright 或 CI 流程中额外弹出浏览器窗口。

## 贡献

请遵循与主项目相同的指南：
- 从仓库根目录操作时，优先使用根目录的 `mise run frontend:*` 任务；在 `frontend/` 目录直接使用 `bun install` 和 `bun run ...` 也是等价的。
- 提交前运行 `bun run lint`
- 运行 `bun run typecheck` 以确保类型安全
- 针对组件覆盖率变更运行 `bun run test:run`
- 针对 launcher / 浏览器路径变更运行 `bun run test:e2e`
- 遵循现有的代码风格
