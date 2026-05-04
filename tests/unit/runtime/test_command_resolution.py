from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from voidcode.command import COMMAND_RESOLVED
from voidcode.graph.contracts import GraphEvent, GraphRunRequest
from voidcode.runtime.contracts import (
    RuntimeRequest,
    RuntimeRequestError,
    validate_runtime_request_metadata,
)
from voidcode.runtime.service import VoidCodeRuntime
from voidcode.runtime.session import SessionState
from voidcode.tools.contracts import ToolCall, ToolResult


@dataclass(frozen=True, slots=True)
class _FinishedStep:
    output: str
    events: tuple[GraphEvent, ...] = ()
    tool_call: ToolCall | None = None
    is_finished: bool = True


class _EchoPromptGraph:
    def step(
        self,
        request: GraphRunRequest,
        tool_results: tuple[ToolResult, ...],
        *,
        session: SessionState,
    ) -> _FinishedStep:
        return _FinishedStep(output=request.prompt)


def test_runtime_resolves_project_prompt_command_before_graph_execution(tmp_path: Path) -> None:
    commands_dir = tmp_path / "commands"
    commands_dir.mkdir()
    (commands_dir / "echo.md").write_text(
        "---\ndescription: Echo the arguments\n---\nexpanded $1 from $ARGUMENTS\n",
        encoding="utf-8",
    )
    runtime = VoidCodeRuntime(workspace=tmp_path, graph=_EchoPromptGraph())

    response = runtime.run(RuntimeRequest(prompt="/echo target.py --flag"))

    assert response.output == "expanded target.py from target.py --flag"
    assert response.session.metadata.get("command") == {
        "name": "echo",
        "source": "project",
        "arguments": ["target.py", "--flag"],
        "raw_arguments": "target.py --flag",
        "original_prompt": "/echo target.py --flag",
    }
    command_events = [event for event in response.events if event.event_type == COMMAND_RESOLVED]
    assert len(command_events) == 1
    assert (
        command_events[0].payload["rendered_prompt"] == "expanded target.py from target.py --flag"
    )


def test_runtime_command_metadata_validates_structured_shape() -> None:
    metadata = validate_runtime_request_metadata(
        {
            "command": {
                "name": "review",
                "source": "builtin",
                "arguments": ["src"],
                "raw_arguments": "src",
                "original_prompt": "/review src",
            }
        }
    )

    command_metadata = cast(dict[str, object], metadata.get("command"))
    assert command_metadata["name"] == "review"


def test_runtime_command_metadata_validates_continuation_loop_shape() -> None:
    metadata = validate_runtime_request_metadata(
        {
            "continuation_loop": {
                "loop_id": "loop-123",
                "status": "active",
                "prompt": "finish the migration",
                "session_id": None,
                "completion_promise": "DONE",
                "max_iterations": 100,
                "iteration": 0,
                "intensive": False,
                "strategy": "continue",
                "created_at": 1,
                "updated_at": 1,
                "finished_at": None,
                "cancel_requested_at": None,
                "error": None,
            }
        }
    )

    loop_metadata = cast(dict[str, object], metadata.get("continuation_loop"))
    assert loop_metadata["loop_id"] == "loop-123"
    assert loop_metadata["status"] == "active"


def test_continuation_loop_command_creates_runtime_owned_loop(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path, graph=_EchoPromptGraph())

    response = runtime.run(RuntimeRequest(prompt="/continuation-loop finish the migration"))

    loop_metadata = cast(dict[str, object], response.session.metadata.get("continuation_loop"))
    loop_id = cast(str, loop_metadata["loop_id"])
    persisted_loop = runtime.load_continuation_loop(loop_id)
    assert response.session.metadata.get("command") == {
        "name": "continuation-loop",
        "source": "builtin",
        "arguments": ["finish", "the", "migration"],
        "raw_arguments": "finish the migration",
        "original_prompt": "/continuation-loop finish the migration",
    }
    assert loop_metadata["status"] == "active"
    assert loop_metadata["prompt"] == "finish the migration"
    assert loop_metadata["intensive"] is False
    assert persisted_loop.prompt == "finish the migration"
    assert persisted_loop.intensive is False
    assert persisted_loop.max_iterations == 100
    assert response.output is not None
    assert "Runtime continuation loop state:" in response.output
    assert "Runtime continuation mode: intensive." not in response.output
    assert loop_id in response.output


def test_intensive_loop_command_marks_runtime_loop_intensive(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path, graph=_EchoPromptGraph())

    response = runtime.run(RuntimeRequest(prompt="/intensive-loop finish the migration"))

    loop_metadata = cast(dict[str, object], response.session.metadata.get("continuation_loop"))
    loop_id = cast(str, loop_metadata["loop_id"])
    persisted_loop = runtime.load_continuation_loop(loop_id)
    assert response.session.metadata.get("workflow_preset") == "implementation"
    assert response.session.metadata.get("command") == {
        "name": "intensive-loop",
        "source": "builtin",
        "arguments": ["finish", "the", "migration"],
        "raw_arguments": "finish the migration",
        "original_prompt": "/intensive-loop finish the migration",
    }
    assert loop_metadata["intensive"] is True
    assert loop_metadata["max_iterations"] == 500
    assert persisted_loop.intensive is True
    assert persisted_loop.max_iterations == 500
    assert response.output is not None
    assert "Runtime continuation mode: intensive." in response.output
    assert "Iteration budget: 500." in response.output
    assert "strongest targeted checks" in response.output


def test_cancel_continuation_command_cancels_persisted_loop(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path, graph=_EchoPromptGraph())
    loop = runtime.start_continuation_loop(prompt="finish the migration")

    response = runtime.run(RuntimeRequest(prompt=f"/cancel-continuation {loop.loop.id}"))

    loop_metadata = cast(dict[str, object], response.session.metadata.get("continuation_loop"))
    persisted_loop = runtime.load_continuation_loop(loop.loop.id)
    assert response.session.metadata.get("command") == {
        "name": "cancel-continuation",
        "source": "builtin",
        "arguments": [loop.loop.id],
        "raw_arguments": loop.loop.id,
        "original_prompt": f"/cancel-continuation {loop.loop.id}",
    }
    assert loop_metadata["loop_id"] == loop.loop.id
    assert loop_metadata["status"] == "cancelled"
    assert persisted_loop.status == "cancelled"
    assert response.output is not None
    assert "Runtime continuation loop cancellation result:" in response.output


def test_cancel_continuation_command_without_id_cancels_latest_active_loop(
    tmp_path: Path,
) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path, graph=_EchoPromptGraph())
    older_loop = runtime.start_continuation_loop(prompt="older task")
    latest_loop = runtime.start_continuation_loop(prompt="latest task")

    response = runtime.run(RuntimeRequest(prompt="/cancel-continuation"))

    loop_metadata = cast(dict[str, object], response.session.metadata.get("continuation_loop"))
    older_persisted_loop = runtime.load_continuation_loop(older_loop.loop.id)
    latest_persisted_loop = runtime.load_continuation_loop(latest_loop.loop.id)
    assert response.session.metadata.get("command") == {
        "name": "cancel-continuation",
        "source": "builtin",
        "arguments": [],
        "raw_arguments": "",
        "original_prompt": "/cancel-continuation",
    }
    assert loop_metadata["loop_id"] == latest_loop.loop.id
    assert loop_metadata["status"] == "cancelled"
    assert older_persisted_loop.status == "active"
    assert latest_persisted_loop.status == "cancelled"


def test_cancel_continuation_command_without_active_loop_reports_clear_error(
    tmp_path: Path,
) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path, graph=_EchoPromptGraph())

    try:
        _ = runtime.run(RuntimeRequest(prompt="/cancel-continuation"))
    except RuntimeRequestError as exc:
        assert "no active continuation loop to cancel" in str(exc)
    else:
        raise AssertionError("cancel-continuation should require an active loop")


def test_runtime_ignores_malformed_command_files_for_non_slash_prompt(
    tmp_path: Path,
) -> None:
    commands_dir = tmp_path / "commands"
    commands_dir.mkdir()
    (commands_dir / "broken.md").write_text(
        "---\nenabled: sometimes\n---\nBroken command body\n",
        encoding="utf-8",
    )
    runtime = VoidCodeRuntime(workspace=tmp_path, graph=_EchoPromptGraph())

    response = runtime.run(RuntimeRequest(prompt="normal non-slash prompt"))

    assert response.output == "normal non-slash prompt"
    assert "command" not in response.session.metadata


def test_runtime_still_validates_command_files_for_slash_prompt(tmp_path: Path) -> None:
    commands_dir = tmp_path / "commands"
    commands_dir.mkdir()
    (commands_dir / "broken.md").write_text(
        "---\nenabled: sometimes\n---\nBroken command body\n",
        encoding="utf-8",
    )
    runtime = VoidCodeRuntime(workspace=tmp_path, graph=_EchoPromptGraph())

    try:
        _ = runtime.run(RuntimeRequest(prompt="/broken target"))
    except RuntimeRequestError as exc:
        assert "boolean frontmatter" in str(exc)
    else:
        raise AssertionError("slash prompt should validate command registry")
