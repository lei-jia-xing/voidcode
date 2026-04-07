# VoidCode Web 前端

VoidCode 的现代 Web 界面 - 基于 React、TypeScript 和 Bun 构建。

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

### 技术栈

- **构建工具**: [Vite](https://vitejs.dev/) + [Bun](https://bun.sh/)
- **框架**: [React](https://react.dev/) 18
- **语言**: [TypeScript](https://www.typescriptlang.org/)
- **样式**: [Tailwind CSS](https://tailwindcss.com/)
- **状态管理**: [Zustand](https://github.com/pmndrs/zustand)
- **数据获取**: [TanStack Query](https://tanstack.com/query)
- **图标**: [Lucide React](https://lucide.dev/)

### 项目结构

> **注意：** 下方的结构描述了前端预期的增长路径，而非当前目录树的完整呈现。目前前端仍相对扁平，主要集中在 `src/App.tsx`、`src/main.tsx`、`src/store/`、`src/i18n/` 和 `src/types/`。

```
frontend/
├── src/
│   ├── components/        # 可复用 UI 组件
│   ├── pages/            # 路由页面
│   ├── stores/           # Zustand 状态存储
│   ├── hooks/            # 自定义 React hooks
│   ├── lib/              # 工具类和 API 客户端
│   ├── types/            # TypeScript 类型定义
│   └── styles/           # 全局样式和 Tailwind 配置
├── public/               # 静态资源
└── index.html            # 入口 HTML
```

## 实现状态

> **重要提示：** 目前的前端主要仍是一个 **UI 壳程序**。主要的任务/活动体验仍是由 Mock 数据驱动的，尽管仓库现在包含了一个薄的运行时传输客户端/调试路径。

- [x] UI 壳程序与导航
- [x] Mock 会话视图
- [x] Mock 智能体交互
- [ ] 实时 API 集成（计划中）
- [ ] WebSocket 事件流（计划中）

## 架构

前端设计通过以下方式与 VoidCode 运行时进行通信：

1. **HTTP API** - 用于会话管理和配置
2. **WebSocket** - 用于实时事件流（智能体思考过程、工具调用、审批）

**注意：** 这些接口目前仅 **部分实现**。仓库现在包含了一个薄的运行时传输客户端和本地后端服务器路径用于传输测试，但前端主要的会话/任务/活动 UI 仍由 Mock 状态驱动。

## 贡献

请遵循与主项目相同的指南：
- 从仓库根目录操作时，优先使用根目录的 `mise run frontend:*` 任务；在 `frontend/` 目录直接使用 `bun install` 和 `bun run ...` 也是等价的。
- 提交前运行 `bun run lint`
- 运行 `bun run typecheck` 以确保类型安全
- 针对组件覆盖率变更运行 `bun run test:run`
- 遵循现有的代码风格
