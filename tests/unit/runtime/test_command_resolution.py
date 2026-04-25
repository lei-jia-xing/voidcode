from __future__ import annotations

from dataclasses import dataclass

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


def test_runtime_resolves_project_prompt_command_before_graph_execution(tmp_path) -> None:
    commands_dir = tmp_path / "commands"
    commands_dir.mkdir()
    (commands_dir / "echo.md").write_text(
        "---\ndescription: Echo the arguments\n---\nexpanded $1 from $ARGUMENTS\n",
        encoding="utf-8",
    )
    runtime = VoidCodeRuntime(workspace=tmp_path, graph=_EchoPromptGraph())

    response = runtime.run(RuntimeRequest(prompt="/echo target.py --flag"))

    assert response.output == "expanded target.py from target.py --flag"
    assert response.session.metadata["command"] == {
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

    assert metadata["command"]["name"] == "review"


def test_runtime_ignores_malformed_command_files_for_non_slash_prompt(tmp_path) -> None:
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


def test_runtime_still_validates_command_files_for_slash_prompt(tmp_path) -> None:
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
