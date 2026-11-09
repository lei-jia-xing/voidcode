# TODO

- [x] 让 `search`/`preview` 在 read-only lease 下可用，同时继续拒绝 `replace`；`ast_grep` 当前整体声明 `WRITE`，且 `_run_ast_grep` 调用的 `run_process` 拒绝只读 lease。验收：只开放受治理的只读查询，不以改效果标签绕过进程权限。
