# Runtime-Owned Todo State Design

## Status

- State: proposed
- Scope: design
- Related issue: [Feature]: Make todo state runtime-owned and provider-visible

## Problem

The `todo_write` tool currently returns only ephemeral tool feedback (`"Updated N todos"`), with no runtime-owned session state. This causes several issues:

1. **Context window loss**: After compaction, early `todo_write` results may be dropped, leaving the agent without its plan.
2. **Resume unreliability**: Session resume rebuilds tool results from `runtime.tool_completed` events; there's no independent todo state to restore.
3. **Agent confusion**: Models receive `"Updated N todos"` but the actual task list isn't a stable session truth, leading to repeated todo calls or plan drift.
4. **Documentation mismatch**: The skill file previously referenced `data.path` which was never returned.

Evidence in codebase:

- `src/voidcode/tools/todo_write.py` returns only `content=f"Updated {len(normalized)} todos"` with `data={"todos": normalized, "summary": summary}`
- `tests/unit/tools/test_todo_write_tool.py` asserts no `.voidcode/todos.json` is written
- `src/voidcode/runtime/context_window.py` uses bounded tool-result windows that compress early results
- `src/voidcode/runtime/storage.py` has no todo state persistence layer

## Design

### 1. Session-Scoped Todo State

Add a `session_todos` table to the SQLite session store:

```sql
CREATE TABLE session_todos (
    session_id TEXT NOT NULL,
    item_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL,  -- pending, in_progress, completed, cancelled
    priority TEXT NOT NULL,  -- high, medium, low
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (session_id, item_index)
)
```

Fields:
- `content`: todo item text
- `status`: `pending`, `in_progress`, `completed`, `cancelled`
- `priority`: `high`, `medium`, `low`
- `position`: implicit via `item_index` ordering
- `updated_at`: timestamp for last modification

### 2. TodoWrite Tool Updates

`TodoWriteTool.invoke()` should:
- Accept an optional `session_store` parameter to persist todos
- Update the session-scoped todo state (upsert all items for the session)
- Return a `ToolResult` with the full updated todo list and summary in `content` (not just `"Updated N todos"`)
- Emit a `runtime.todo_updated` event

### 3. Runtime Event: `runtime.todo_updated`

Add to `src/voidcode/runtime/events.py`:

```python
type PrototypeAdditiveEventType = Literal[
    # ... existing types ...
    "runtime.todo_updated",
]

RUNTIME_TODO_UPDATED: Final[PrototypeAdditiveEventType] = "runtime.todo_updated"
```

Payload:
- `session_id: str`
- `todo_count: int`
- `pending_count: int`
- `in_progress_count: int`
- `completed_count: int`
- `cancelled_count: int`
- `todos: list[dict]` — the full current todo list

### 4. Provider-Visible Injection

In `assemble_provider_context()` or a new context assembly step, inject current active todo state when:
- There are `pending` or `in_progress` todos
- The injection should be a system segment, not dependent on recent tool results

Example injection:
```
## Current Task Plan
- [pending] [high] Implement user authentication
- [in_progress] [medium] Add database migrations
- [completed] [low] Setup project structure
```

### 5. Resume / Compaction / Undo Semantics

- **Resume**: Restore todo state from `session_todos` table, not from replaying tool events
- **Compaction**: Todo state persists independently; active todos are always re-injected
- **Undo**: If conversation undo is implemented, todo state should revert to the checkpoint's todo snapshot or be explicitly invalidated

### 6. Sanitization Strategy

Todo item content is user/agent-explicit task state and should be treated as safe model-visible state. The global sanitizer should not redact todo content.

## Error Handling

When implementing this feature, ensure:

1. **Invalid input**: `todo_write` should reject malformed arguments with clear error messages:
   ```python
   raise ValueError("todo_write requires todos array")
   raise ValueError(f"todo_write item #{idx} invalid status: {status}")
   ```

2. **Storage failures**: If session store write fails, the tool should return an error result rather than silently dropping state:
   ```python
   ToolResult(
       tool_name="todo_write",
       status="error",
       error="Failed to persist todo state",
       data={"todos": normalized, "summary": summary},  # Return data even on error
   )
   ```

3. **Resume corruption**: If stored todo state is malformed during resume, fall back to empty state and log a warning:
   ```python
   try:
       todos = store.load_session_todos(session_id=session_id)
   except (json.JSONDecodeError, ValueError) as exc:
       logger.warning("Corrupted todo state for session %s: %s", session_id, exc)
       todos = []
   ```

4. **Concurrent updates**: Since todos are session-scoped and typically updated sequentially, explicit locking may not be needed, but the SQLite write path should use transactions.

## Test Coverage

- `todo_write` updates session todo state
- Provider context includes current todos even without recent `todo_write` tool result
- Context compaction preserves active todos via re-injection
- Resume restores todos from storage
- `runtime.todo_updated` event payload is deterministic
- Undo/checkpoint aligns todo state with conversation state

## Related Documents

- [`session-continuity-memory-design.md`](./session-continuity-memory-design.md) — compaction and continuity state
- [`contracts/runtime-events.md`](./contracts/runtime-events.md) — event vocabulary
- [`runtime-owned-scheduler-design.md`](./runtime-owned-scheduler-design.md) — runtime ownership patterns

## References

- OpenCode todo tool implementation: [packages/opencode/src/tool/todo.ts](https://github.com/anomalyco/opencode/blob/65ba1f6c138636e0e731905951845da2b76c9add/packages/opencode/src/tool/todo.ts#L42-L53)
- OpenCode session todo service: [packages/opencode/src/session/todo.ts](https://github.com/anomalyco/opencode/blob/65ba1f6c138636e0e731905951845da2b76c9add/packages/opencode/src/session/todo.ts#L46-L65)
- OMO compaction todo preserver: [src/hooks/compaction-todo-preserver/hook.ts](https://github.com/code-yeongyu/oh-my-openagent/blob/d65bc8730c0cfaa325d84954638959e10378e530/src/hooks/compaction-todo-preserver/hook.ts#L57-L104)
