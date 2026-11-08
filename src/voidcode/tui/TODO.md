# TODO

- [x] 中段 slash 补全只替换光标处 token，保留前后草稿和已有参数，不误提交；当前 `Composer._accept_completion` 整体替换文本，`CoreTuiApp._completion_candidates` 只在光标末尾给候选。
- [x] 为 composer 增加 workspace 内 `@path`/Tab 路径建议；接受后只插入路径文本，不隐式读取或附加文件，并保留光标两侧内容。参考 OMP [`@` 搜索、路径补全与 token 替换](https://github.com/can1357/oh-my-pi/blob/45c35e40c46166dc726ccc52f5d27f40f2449f99/packages/tui/src/autocomplete.ts#L751-L915)。
- [x] `/tui sessions` 提供可选列表而非只打印 ID，切换仍沿用 `_switch_session` 的 generation 与已提交游标保护。参考 OMP [session selector](https://github.com/can1357/oh-my-pi/blob/45c35e40c46166dc726ccc52f5d27f40f2449f99/packages/coding-agent/src/modes/controllers/selector-controller.ts#L1768-L1919)。
- [x] `_footer_context` 分开显示当前 dispatch context/window 与累计 session usage；不拿累计 token 伪装上下文占用，实测不足时保持未知，40 列下保留模型信息。参考 OMP [当前 context 与 token segments](https://github.com/can1357/oh-my-pi/blob/45c35e40c46166dc726ccc52f5d27f40f2449f99/packages/tui/src/status-line/segments.ts#L738-L895)。
- [ ] 为被截断的 tool 输出提供只读分页查看，消费 artifact reference 与现有资源页；`render_tool_result` 的成功结果仅预览 8 行。接受准则是不重写 append-only scrollback。参考 OMP [tool 输出展开输入](https://github.com/can1357/oh-my-pi/blob/45c35e40c46166dc726ccc52f5d27f40f2449f99/packages/coding-agent/src/modes/controllers/input-controller.ts#L2722-L2760)，仅借鉴可发现性，不复制历史回放语义。
