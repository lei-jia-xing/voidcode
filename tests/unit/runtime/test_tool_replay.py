from voidcode.runtime.interaction_queue import drain_runtime_messages, enqueue_runtime_message
from voidcode.runtime.tool_replay import ToolExecutionIntent, interrupted_tool_result_message, recovery_action
from voidcode.tools.contracts import ToolCall, ToolDefinition


def test_tool_definition_defaults_replay_policy_from_mutability() -> None:
    assert ToolDefinition(name="read", description="read", read_only=True).effective_replay_policy == "safe"
    assert ToolDefinition(name="write", description="write", read_only=False).effective_replay_policy == "never"


def test_tool_execution_intent_sanitizes_arguments_and_records_policy() -> None:
    intent = ToolExecutionIntent.from_call(
        ToolCall("write", {"path": "a.txt", "content": "new content"}),
        ToolDefinition(name="write", description="write", read_only=False),
        tool_call_id="call-1",
    )
    assert intent.replay_policy == "never"
    assert intent.arguments["path"] == "a.txt"
    assert "did not replay" in interrupted_tool_result_message(intent)
    assert recovery_action(intent) == "interrupted"


def test_safe_pending_tool_intent_requests_replay() -> None:
    intent = ToolExecutionIntent(
        tool_call_id="call-1",
        tool_name="read",
        arguments={"path": "a.txt"},
        replay_policy="safe",
    )
    assert recovery_action(intent) == "replay"


def test_runtime_message_queue_preserves_kind_and_drains_only_requested_kind() -> None:
    metadata = enqueue_runtime_message({}, content="finish the current turn", kind="steering")
    metadata = enqueue_runtime_message(metadata, content="write a summary", kind="follow_up")
    metadata, steering = drain_runtime_messages(metadata, kind="steering")
    assert [item.content for item in steering] == ["finish the current turn"]
    assert metadata["pending_messages"][0]["kind"] == "follow_up"
    metadata, follow_up = drain_runtime_messages(metadata, kind="follow_up")
    assert [item.content for item in follow_up] == ["write a summary"]
    assert "pending_messages" not in metadata
