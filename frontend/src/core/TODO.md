# TODO

- [x] Web `ToolResultCard` pages immutable `OutputBounds.reference` artifacts and decodes real binary image pages instead of expanding the same preview text. Authenticated Chromium against real Core/RPC passed the actual immutable PNG scene (1 passed/14.1s; 346,073 bytes, 384×300); the current browser screenshot was inspected. Page errors, source identity and byte ranges remain explicit.

## 清算：Web 前端巨石与双栈逻辑重复（P0，零向后兼容）

- [x] **拆分 `CoreApp.tsx`（2601 行）上帝组件，消除与 TUI 的三重逻辑克隆。**
  - `CoreApp.tsx` 只剩组合：`hooks/useSession`（连接/token/会话/事件流/健康）、`hooks/useCatalog`（catalog+slash）、`hooks/useGates`（事件门控投影）、`hooks/useViews`（面板/命令/表单）、`hooks/useComposer`（输入框/slash 分发）；竞态守卫改用 `AbortController` + scope 对象标识，无 epoch/clientKey/serial 计数器。`ToolResultCard`/`ArtifactViewer` 移至 `ToolResultCard.tsx`。
  - `toolOutput.ts`：核实后端 `body` 内确有 `ArtifactReference`/`artifact://`（shell_exec/read_tool/mcp/review/subagents/user_shell/web/workflows 及 e2e scene），保留结构化消费但删除 sha256 正则（改用显式 hex 检查，与 TUI `_artifact_ref_uri` 对齐），从不做自由文本子串扫描。
  - Token 不再经 URL query 传递：`token` prop 或粘贴进 token 门（per-tab sessionStorage），`?token=` 被忽略；e2e 改用 `e2e/auth.ts` 预置。`tsc --noEmit` + vitest 全绿。
