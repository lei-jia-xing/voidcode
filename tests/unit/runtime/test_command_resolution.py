from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from voidcode.command.loader import load_markdown_commands
from voidcode.command.registry import CommandRegistry
from voidcode.command.resolver import resolve_prompt_command
from voidcode.graph.contracts import GraphEvent, GraphRunRequest
from voidcode.runtime.contracts import (
    RuntimeRequest,
    RuntimeRequestError,
    RuntimeRequestMetadataPayload,
    runtime_read_only_from_metadata,
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
        _ = tool_results, session
        return _FinishedStep(output=request.prompt)


def test_command_resolution_preserves_mode_frontmatter(tmp_path: Path) -> None:
    commands_dir = tmp_path / "commands"
    commands_dir.mkdir()
    (commands_dir / "echo.md").write_text(
        "\n".join(
            (
                "---",
                "description: Echo the arguments",
                "mode: plan",
                "---",
                "expanded $1 from $ARGUMENTS",
                "",
            )
        ),
        encoding="utf-8",
    )

    command = load_markdown_commands(commands_dir, source="project")[0]
    resolution = resolve_prompt_command("/echo target.py --flag", CommandRegistry((command,)))

    assert resolution is not None
    assert resolution.definition.mode == "plan"
    assert resolution.invocation.rendered_prompt == "expanded target.py from target.py --flag"


def test_mode_frontmatter_is_preserved(tmp_path: Path) -> None:
    commands_dir = tmp_path / "commands"
    commands_dir.mkdir()
    (commands_dir / "plan.md").write_text(
        "---\ndescription: Plan this target\nmode: plan\n---\nPlan $ARGUMENTS\n",
        encoding="utf-8",
    )

    command = load_markdown_commands(commands_dir, source="project")[0]
    resolution = resolve_prompt_command("/plan src/app.py", CommandRegistry((command,)))

    assert resolution is not None
    assert resolution.definition.mode == "plan"
    assert resolution.invocation.rendered_prompt == "Plan src/app.py"


def test_runtime_command_metadata_accepts_valid_mode() -> None:
    metadata = validate_runtime_request_metadata(
        {
            "command": {
                "name": "plan",
                "source": "builtin",
                "arguments": ["src/app.py"],
                "raw_arguments": "src/app.py",
                "original_prompt": "/plan src/app.py",
                "mode": "plan",
            }
        }
    )

    command = cast(dict[str, object], metadata["command"])
    assert command["mode"] == "plan"


def test_runtime_command_metadata_rejects_unknown_mode() -> None:
    try:
        _ = validate_runtime_request_metadata(
            {
                "command": {
                    "name": "custom",
                    "source": "project",
                    "arguments": [],
                    "raw_arguments": "",
                    "original_prompt": "/custom",
                    "mode": "banana",
                }
            }
        )
    except RuntimeRequestError as exc:
        assert "request metadata 'mode' must be 'normal' or 'plan'" in str(exc)
    else:
        raise AssertionError("unknown command mode should fail validation")


def test_command_mode_precedes_request_metadata_mode(tmp_path: Path) -> None:
    commands_dir = tmp_path / "commands"
    commands_dir.mkdir()
    (commands_dir / "plan.md").write_text(
        "---\ndescription: Plan work\nmode: plan\n---\nPlan $ARGUMENTS\n",
        encoding="utf-8",
    )
    runtime = VoidCodeRuntime(workspace=tmp_path, graph=_EchoPromptGraph())

    response = runtime.run(
        RuntimeRequest(
            prompt="/plan target.py",
            metadata=cast(RuntimeRequestMetadataPayload, {"mode": "normal"}),
        )
    )

    assert response.session.metadata["mode"] == "plan"
    assert response.output == "Plan target.py"


def test_request_mode_survives_project_command_without_frontmatter(
    tmp_path: Path,
) -> None:
    commands_dir = tmp_path / "commands"
    commands_dir.mkdir()
    (commands_dir / "echo.md").write_text(
        "---\ndescription: Echo without mode selectors\n---\nEcho $ARGUMENTS\n",
        encoding="utf-8",
    )
    runtime = VoidCodeRuntime(workspace=tmp_path, graph=_EchoPromptGraph())

    response = runtime.run(
        RuntimeRequest(
            prompt="/echo target.py",
            metadata=cast(RuntimeRequestMetadataPayload, {"mode": "plan"}),
        )
    )

    assert response.session.metadata["mode"] == "plan"
    assert runtime_read_only_from_metadata(response.session.metadata) is True
    assert response.output == "Echo target.py"


def test_runtime_rejects_invalid_command_mode_during_request_normalization(
    tmp_path: Path,
) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path, graph=_EchoPromptGraph())

    try:
        _ = runtime.run(
            RuntimeRequest(
                prompt="plain prompt",
                metadata=cast(
                    RuntimeRequestMetadataPayload,
                    {
                        "command": {
                            "name": "custom",
                            "source": "project",
                            "arguments": [],
                            "raw_arguments": "",
                            "original_prompt": "/custom",
                            "mode": "banana",
                        }
                    },
                ),
            )
        )
    except RuntimeRequestError as exc:
        assert "request metadata 'mode' must be 'normal' or 'plan'" in str(exc)
    else:
        raise AssertionError("invalid command mode should fail runtime request normalization")


def test_runtime_preserves_command_only_mode_for_structured_unregistered_command(
    tmp_path: Path,
) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path, graph=_EchoPromptGraph())

    response = runtime.run(
        RuntimeRequest(
            prompt="plain prompt",
            metadata=cast(
                RuntimeRequestMetadataPayload,
                {
                    "command": {
                        "name": "custom",
                        "source": "project",
                        "arguments": [],
                        "raw_arguments": "",
                        "original_prompt": "/custom",
                        "mode": "plan",
                    }
                },
            ),
        )
    )

    assert response.session.metadata["mode"] == "plan"
    assert runtime_read_only_from_metadata(response.session.metadata) is True
    assert response.output == "plain prompt"


def test_runtime_command_mode_overrides_top_level_mode_for_structured_command(
    tmp_path: Path,
) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path, graph=_EchoPromptGraph())

    response = runtime.run(
        RuntimeRequest(
            prompt="plain prompt",
            metadata=cast(
                RuntimeRequestMetadataPayload,
                {
                    "mode": "normal",
                    "command": {
                        "name": "custom",
                        "source": "project",
                        "arguments": [],
                        "raw_arguments": "",
                        "original_prompt": "/custom",
                        "mode": "plan",
                    },
                },
            ),
        )
    )

    assert response.session.metadata["mode"] == "plan"
    assert runtime_read_only_from_metadata(response.session.metadata) is True
    assert response.output == "plain prompt"


def test_runtime_stream_rejects_invalid_top_level_mode_type(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path, graph=_EchoPromptGraph())

    try:
        _ = tuple(
            runtime.run_stream(
                RuntimeRequest(
                    prompt="plain prompt",
                    metadata=cast(RuntimeRequestMetadataPayload, {"mode": 123}),
                )
            )
        )
    except RuntimeRequestError as exc:
        assert "request metadata 'mode' must be 'normal' or 'plan'" in str(exc)
    else:
        raise AssertionError("invalid mode type should fail run_stream validation")


def test_init_command_renders_agents_md_generation_prompt(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path, graph=_EchoPromptGraph())

    response = runtime.run(RuntimeRequest(prompt="/init focus on runtime boundaries"))

    assert response.session.metadata.get("command") == {
        "name": "init",
        "source": "builtin",
        "arguments": ["focus", "on", "runtime", "boundaries"],
        "raw_arguments": "focus on runtime boundaries",
        "original_prompt": "/init focus on runtime boundaries",
    }
    assert response.output is not None
    assert "Generate or refresh the project knowledge base (AGENTS.md)" in response.output
    assert "WHERE TO LOOK" in response.output
    assert "Never store secrets" in response.output
    assert "focus on runtime boundaries" in response.output


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
