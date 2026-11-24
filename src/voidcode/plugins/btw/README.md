# btw

Answer a quick side question from conversation context without interrupting the main turn.

## Surface

- Command `/btw` with required `question` (non-empty string, bounded by the 64 KiB prompt ceiling). Runs with `busy_gate=False`, so it is available while a session is running or waiting.
- Returns the side answer once as command `output`. `data` carries `{"question", "truncated"}` and never a second copy of the answer.
- Stable error codes live in `data.error`: `empty_question`, `question_too_long`, `cancelled`, `empty_answer`, `tool_call`, `budget`, `model`, `context`, `unavailable`, `provider_error`, `failed`. The human message is fixed per code; raw provider exception text is never persisted.

## Configuration

Takes no plugin config; `create_plugin` ignores it. See [configuration](../../../../docs/configuration.md) for global config.

## Behavior and limits

- One provider turn via `services.model_query(..., include_context=True, max_output_tokens=2048)`. The adapter may honor the token cap; the answer is always clipped to 16 KiB of UTF-8 with `[answer truncated by /btw]` when needed.
- Context is the current conversation window: the verified compaction summary, committed user/assistant/tool events, and contributed plugin context. An unfinished native tool batch is omitted as a whole.
- Cancellation uses the invoking command/run abort signal rather than the core-wide default. A cancelled query returns `cancelled` with no answer; a provider turn that already committed its usage fact keeps that truthful charge, but the answer is never written into the cancellation receipt.
- Effect declaration is exactly `NETWORK`. It does not deny read-only sessions, and commands have no approval-mode gate, so `/btw` is available under `read_only` and `ask` without a human confirmation. It sends conversation context to the configured model and spends provider budget.
- The plugin keeps no state and stages no plugin events. The core still commits the `core.model_query` usage fact and the `core.command_completed` receipt; usage observers (for example goal accounting or compaction) may act on that fact.
- A model-query usage fact is charged once to the session report and any active session/plugin budget. `/btw` does **not** reserve a root-action unit: `root_action_budget` and `max_steps` do not limit side queries. This is a human-invoked side channel, not part of the run's action chain.
- There is no idempotency key. Every invocation is a new paid provider turn; a client retry after a timeout can bill twice.
