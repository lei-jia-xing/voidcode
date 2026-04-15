# TUI 偏好系统设计

## 状态

本文档对应的第一阶段能力已完成实现。

当前已落地内容包括：

- 全局默认值 + workspace override 的 TUI 偏好解析
- `theme.name` + `theme.mode` 的持久化与运行时应用
- `reading.wrap` 与 `reading.sidebar_collapsed` 的持久化与运行时应用
- command palette 中的主题切换、主题模式设置、wrap 切换、sidebar 切换、保存为全局默认值

后续如需继续扩展，应在此设计基础上追加新切片，而不是退回到一次性 toggle。

## 目标

为当前 Textual TUI 引入一个真正的偏好系统，而不是继续堆叠一次性、临时性的切换开关。

第一阶段需要支持以下能力：

- 持久化保存 Textual 内建主题中的具体主题选择
- 持久化保存主题模式策略
- 持久化保存阅读偏好，包括 transcript 自动换行与侧边栏折叠状态
- 明确偏好优先级：全局默认值与 workspace 覆盖

这个设计是明确以产品体验为导向的：TUI 应该像一个会被长期使用的界面，记住用户希望它如何显示、如何阅读，而不是每次启动都重新调整。

## 非目标

本阶段不包含以下内容：

- 终端透明背景 hack，或尝试继承 terminal emulator 透明效果
- 一个大型的 settings center / 偏好设置页面
- session 级别的 UI 偏好覆盖
- 自定义主题创作界面
- 主题预览图库或缩略图预览
- 将任意主题强行变形成 synthetic light/dark 变体

## 已锁定的产品决策

### 偏好层级

偏好系统分为两层持久化来源：

1. 全局默认值
2. workspace 覆盖值

最终生效的 TUI 偏好解析顺序为：

`workspace override > global default > built-in defaults`

### 第一批偏好范围

第一阶段只包含两组偏好：

1. 主题系统
2. 阅读偏好

### 主题模型

主题系统采用混合模型：

- `theme.name`：一个来自 Textual 已注册主题集合的具体主题名
- `theme.mode`：一个策略值，取值为 `auto | light | dark`

这里的 `theme.mode` 是**筛选与选择策略**，不是一个“强制转换器”。

具体含义如下：

- `auto`：保持所选主题本身的表现，不额外干预
- `light`：只从 light 主题集合中选择主题
- `dark`：只从 dark 主题集合中选择主题

如果当前主题与选定的 mode 策略冲突，应用应当解析到一个在该策略下合法的候选主题，而不是试图把该主题“改造”为另一个明暗版本。

### 阅读模型

第一阶段的阅读偏好包括：

- `reading.wrap`
- `reading.sidebar_collapsed`

这两个值需要被持久化，因为它们会直接影响日常使用中的阅读舒适度。

## 配置模型

### 全局默认值

需要引入一个用户级配置文件，用来保存全局 TUI 默认偏好。

位置固定为：

- `~/.config/voidcode/config.json`

### Workspace 覆盖值

继续扩展现有的 workspace 本地配置文件 `.voidcode.json`。

### 配置形状

```json
{
  "tui": {
    "leader_key": "alt+x",
    "keymap": {
      "alt+x": "command_palette"
    },
    "preferences": {
      "theme": {
        "name": "tokyo-night",
        "mode": "dark"
      },
      "reading": {
        "wrap": true,
        "sidebar_collapsed": false
      }
    }
  }
}
```

### 生效后的有效模型

运行时应解析并合并出一个有效对象，形状类似：

```json
{
  "theme": {
    "name": "tokyo-night",
    "mode": "dark"
  },
  "reading": {
    "wrap": true,
    "sidebar_collapsed": false
  }
}
```

这个 effective object 才是最终交给 TUI app 消费的配置结果。

## Runtime / Config 与 TUI 的职责划分

### Runtime config 层

runtime config 层负责：

- 解析全局 TUI 默认值
- 解析 workspace 本地 TUI 覆盖值
- 校验 `preferences` 结构是否合法
- 合并并产出最终有效的 TUI 偏好对象

runtime config 层不负责：

- 直接理解 Textual widget 的具体实现细节
- 把 UI 偏好写入 session metadata

### TUI app 层

TUI app 负责：

- 在启动时接收最终生效的偏好
- 在 mount 时立即应用这些偏好
- 在 command palette 中暴露可操作的偏好命令
- 在用户修改偏好后，将结果写回选定的持久化层

## Command Palette 交互设计

当前那个过于简陋的 `theme: toggle` 应该被真正的偏好命令替换。

第一阶段至少应提供以下命令：

- `Switch theme`
- `Set theme mode`
- `Toggle wrap`
- `Toggle sidebar`

### Switch theme

- 打开主题选择器
- 列出当前运行时已注册的 Textual 主题
- 遵循当前 `theme.mode` 的筛选规则
- 用户选择后立即应用，并持久化新的 `theme.name`

### Set theme mode

- 提供 `auto`、`light`、`dark` 三个选项
- 切换 mode 后，更新可用主题的选择空间
- 如果当前主题在新的 mode 下仍然合法，则保留不变
- 如果不合法，则自动选择一个合法 fallback，并同时持久化新的 mode 与解析后的 theme name

### Toggle wrap

- 翻转 `reading.wrap`
- 立即作用于 transcript log
- 将结果持久化到选定的偏好层

### Toggle sidebar

- 翻转 `reading.sidebar_collapsed`
- 立即作用于布局
- 将结果持久化到选定的偏好层

## 偏好写回规则

默认情况下，TUI 内发起的偏好修改应写入 **global default** 层。

原因如下：

- theme / theme mode / wrap / sidebar 本质上都更接近用户级偏好，而不是项目级配置
- 用户在一个仓库里调整 TUI 外观和阅读习惯时，通常预期它会成为自己后续所有仓库的默认体验
- 这样可以避免仅仅因为进入了某个项目，就在 workspace 下生成并长期保留一份本地 UI 偏好覆盖

workspace override 仍然保留读取能力，用于未来的显式项目级覆盖，但它不再是默认写回目标。

### 第一阶段的写回规则

第一版实现中：

- 命令面板内发生的普通偏好修改，默认写回 global default
- global default 的读取、解析与持久化能力必须在底层支持
- workspace override 继续参与优先级解析，但不会因普通 TUI 偏好修改而被默认写回

这样可以让产品行为更符合用户直觉：TUI 偏好默认属于“我这个用户”，而不是“这个仓库”。

## 启动行为

TUI 启动时的行为顺序应为：

1. 读取 global defaults
2. 读取 workspace override
3. 解析最终 effective TUI preferences
4. 将阅读偏好应用到布局与 widget
5. 将主题偏好应用到 Textual app

### 主题应用规则

- 若 `theme.mode = auto`，则直接应用 `theme.name`
- 若 `theme.mode = light`，则只允许选择 light 主题
- 若 `theme.mode = dark`，则只允许选择 dark 主题
- 若 `theme.name` 缺失或无效，则回退到与 mode 一致的内建默认主题

推荐的保守默认值如下：

- `auto` -> `textual-dark`
- `light` -> `textual-light`
- `dark` -> `textual-dark`

这些默认值是刻意保守的，不依赖透明背景，也不试图和 terminal emulator 的视觉效果做强绑定。

## 失败处理

### 持久化的 theme name 无效

如果某个已保存的主题名在未来不存在了：

- 不应导致 TUI 启动失败
- 应回退到当前 mode 对应的默认主题
- 只有在产品已经有合适位置显示提示时，才附带一个轻量的用户可见说明

### mode 值无效

如果配置中的 mode 不合法：

- 应在 runtime-config 的正常校验路径中直接校验失败

### 阅读偏好值无效

如果 `wrap` 或 `sidebar_collapsed` 不是布尔值：

- 应在 runtime-config 的正常校验路径中直接校验失败

## 架构影响

这个设计要求扩展当前的 `RuntimeTuiConfig`。它现在只建模了：

- `leader_key`
- `keymap`

下一步不应该继续往上面堆散乱的布尔值和字符串，而应引入结构化的 typed preferences model。

建议新增如下配置结构：

- `RuntimeTuiThemePreferences`
- `RuntimeTuiReadingPreferences`
- `RuntimeTuiPreferences`
- `EffectiveRuntimeTuiPreferences`

## 建议的实现落点

大概率会涉及以下位置：

- `src/voidcode/runtime/config.py`
- `docs/contracts/runtime-config.md`
- `src/voidcode/tui/app.py`
- `src/voidcode/tui/screens.py`
- 新增或扩展的 TUI 测试
- config parsing / validation 测试

如果实现中引入用户级配置加载路径，必须保证这不会削弱现有 workspace-local config 的语义边界。

## 测试预期

第一版实现计划中应包含以下测试：

- global + workspace TUI preferences 的解析
- effective TUI preferences 的优先级解析
- 非法偏好值的校验失败测试
- 主题选择器在不同 mode 下的筛选行为
- 启动时对 wrap / sidebar / theme 偏好的正确应用
- command palette 修改偏好后的即时 UI 更新
- workspace override 写回行为

## 明确延后的问题

以下内容明确不属于第一阶段：

- 完整的偏好设置页面
- 主题预览
- 与 terminal / system theme 的外部同步
- session 级的临时覆盖
- 更丰富的 transcript 渲染策略偏好

## 推荐实施顺序

在同一个产品设计下，建议内部按两个阶段推进：

1. 先建立 typed config + effective preference resolution
2. 再把当前的一次性 TUI toggle 命令替换为持久化偏好命令

这样可以先把偏好模型打稳，再把用户入口接上去。

## 为什么采用这个设计

- 它符合用户对 TUI 偏好“必须记住”的直觉预期
- 它利用了 Textual 真实存在的主题目录，而不是继续假装产品只有 dark/light 两个模式
- 它让第一阶段足够小，能落地，但又已经具备真实产品的味道
- 它避免引入错误抽象，例如强行把任意主题转换成 synthetic light/dark 变体
