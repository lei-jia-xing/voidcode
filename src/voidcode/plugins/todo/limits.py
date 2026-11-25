"""Bounded ingress and rendering limits shared by the todo plugin modules.

These names are also re-exported from :mod:`voidcode.plugins.todo.todo_plugin`
so existing callers and tests keep patching the documented facade path. The
facade forwards those writes here; consumers must read this module at call time.
"""

from __future__ import annotations

#: Ingress and rendering bounds. State is still one session-scoped row; these keep
#: provider context, continuations, and command receipts bounded and reviewable.
MAX_TODO_PHASES = 64
MAX_TODO_TASKS = 512
MAX_TODO_TASKS_PER_PHASE = 256
MAX_TODO_PHASE_NAME_BYTES = 512
MAX_TODO_TASK_CONTENT_BYTES = 2048
MAX_TODO_BLOCKER_BYTES = 512
MAX_TODO_CONTEXT_BYTES = 16 * 1024
MAX_TODO_REMINDER_BYTES = 16 * 1024
MAX_TODO_COMMAND_OUTPUT_BYTES = 16 * 1024
MAX_TODO_STATE_CONFLICT_ATTEMPTS = 3
