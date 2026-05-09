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


def test_command_resolution_preserves_workflow_mode_frontmatter(tmp_path: Path) -> None:
    commands_dir = tmp_path / "commands"
    commands_dir.mkdir()
    (commands_dir / "echo.md").write_text(
        "\n".join(
            (
                "---",
                "description: Echo the arguments",
                "workflow_mode: review",
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
    assert resolution.definition.workflow_mode == "review"
    assert resolution.definition.workflow_preset is None
    assert resolution.invocation.rendered_prompt == "expanded target.py from target.py --flag"


def test_workflow_mode_frontmatter_is_preserved_without_legacy_preset(tmp_path: Path) -> None:
    commands_dir = tmp_path / "commands"
    commands_dir.mkdir()
    (commands_dir / "review.md").write_text(
        "---\ndescription: Review target\nworkflow_mode: review\n---\nReview $ARGUMENTS\n",
        encoding="utf-8",
    )

    command = load_markdown_commands(commands_dir, source="project")[0]
    resolution = resolve_prompt_command("/review src/app.py", CommandRegistry((command,)))

    assert resolution is not None
    assert resolution.definition.workflow_mode == "review"
    assert resolution.definition.workflow_preset is None
    assert resolution.invocation.rendered_prompt == "Review src/app.py"


def test_legacy_workflow_preset_frontmatter_is_preserved_without_mode(
    tmp_path: Path,
) -> None:
    commands_dir = tmp_path / "commands"
    commands_dir.mkdir()
    (commands_dir / "start.md").write_text(
        "---\n"
        "description: Start legacy workflow\n"
        "workflow_preset: implementation\n"
        "---\n"
        "Start $ARGUMENTS\n",
        encoding="utf-8",
    )

    command = load_markdown_commands(commands_dir, source="project")[0]
    resolution = resolve_prompt_command("/start accepted plan", CommandRegistry((command,)))

    assert resolution is not None
    assert resolution.definition.workflow_mode is None
    assert resolution.definition.workflow_preset == "implementation"
    assert resolution.invocation.rendered_prompt == "Start accepted plan"


def test_runtime_request_metadata_accepts_valid_workflow_mode() -> None:
    metadata = validate_runtime_request_metadata({"workflow_mode": "review"})

    assert metadata["workflow_mode"] == "review"


def test_runtime_request_metadata_rejects_unknown_workflow_mode() -> None:
    try:
        _ = validate_runtime_request_metadata({"workflow_mode": "banana"})
    except RuntimeRequestError as exc:
        assert "unknown workflow_mode: banana" in str(exc)
    else:
        raise AssertionError("unknown workflow_mode should fail request metadata validation")


def test_runtime_request_metadata_preserves_legacy_workflow_preset() -> None:
    metadata = validate_runtime_request_metadata({"workflow_preset": "review"})

    assert metadata["workflow_preset"] == "review"


def test_runtime_request_metadata_rejects_conflicting_workflow_mode_and_preset() -> None:
    try:
        _ = validate_runtime_request_metadata(
            {"workflow_mode": "deep_work", "workflow_preset": "review"}
        )
    except RuntimeRequestError as exc:
        assert "workflow_mode and workflow_preset resolve to different modes" in str(exc)
    else:
        raise AssertionError("conflicting workflow mode and preset should fail validation")


def test_runtime_command_metadata_accepts_valid_workflow_mode() -> None:
    metadata = validate_runtime_request_metadata(
        {
            "command": {
                "name": "review",
                "source": "builtin",
                "arguments": ["src/app.py"],
                "raw_arguments": "src/app.py",
                "original_prompt": "/review src/app.py",
                "workflow_mode": "review",
            }
        }
    )

    command = cast(dict[str, object], metadata["command"])
    assert command["workflow_mode"] == "review"


def test_runtime_command_metadata_rejects_unknown_workflow_mode() -> None:
    try:
        _ = validate_runtime_request_metadata(
            {
                "command": {
                    "name": "custom",
                    "source": "project",
                    "arguments": [],
                    "raw_arguments": "",
                    "original_prompt": "/custom",
                    "workflow_mode": "banana",
                }
            }
        )
    except RuntimeRequestError as exc:
        assert "unknown workflow_mode: banana" in str(exc)
    else:
        raise AssertionError("unknown command workflow_mode should fail validation")


def test_command_workflow_mode_precedes_request_metadata_mode(tmp_path: Path) -> None:
    commands_dir = tmp_path / "commands"
    commands_dir.mkdir()
    (commands_dir / "deep.md").write_text(
        "---\ndescription: Deep work\nworkflow_mode: deep_work\n---\nDeep $ARGUMENTS\n",
        encoding="utf-8",
    )
    runtime = VoidCodeRuntime(workspace=tmp_path, graph=_EchoPromptGraph())

    response = runtime.run(
        RuntimeRequest(
            prompt="/deep target.py",
            metadata=cast(RuntimeRequestMetadataPayload, {"workflow_mode": "review"}),
        )
    )

    assert response.session.metadata["workflow_mode"] == "deep_work"
    assert response.output == "Deep target.py"


def test_command_workflow_mode_skips_legacy_preset_conflict_when_command_wins(
    tmp_path: Path,
) -> None:
    commands_dir = tmp_path / "commands"
    commands_dir.mkdir()
    (commands_dir / "start-work.md").write_text(
        "---\n"
        "description: Start work\n"
        "workflow_mode: sustain\n"
        "workflow_preset: implementation\n"
        "---\n"
        "Start $ARGUMENTS\n",
        encoding="utf-8",
    )
    runtime = VoidCodeRuntime(workspace=tmp_path, graph=_EchoPromptGraph())

    response = runtime.run(
        RuntimeRequest(
            prompt="/start-work accepted plan",
            metadata=cast(RuntimeRequestMetadataPayload, {"workflow_mode": "review"}),
        )
    )

    assert response.session.metadata["workflow_mode"] == "sustain"
    assert response.session.metadata["workflow_preset"] == "implementation"
    assert response.output == "Start accepted plan"


def test_request_workflow_mode_survives_project_command_without_frontmatter(
    tmp_path: Path,
) -> None:
    commands_dir = tmp_path / "commands"
    commands_dir.mkdir()
    (commands_dir / "echo.md").write_text(
        "---\ndescription: Echo without workflow selectors\n---\nEcho $ARGUMENTS\n",
        encoding="utf-8",
    )
    runtime = VoidCodeRuntime(workspace=tmp_path, graph=_EchoPromptGraph())

    response = runtime.run(
        RuntimeRequest(
            prompt="/echo target.py",
            metadata=cast(RuntimeRequestMetadataPayload, {"workflow_mode": "review"}),
        )
    )
    runtime_config = cast(dict[str, object], response.session.metadata["runtime_config"])
    workflow = cast(dict[str, object], runtime_config["workflow"])

    assert response.session.metadata["workflow_mode"] == "review"
    assert cast(dict[str, object], workflow["effective"])["mode"] == "review"
    assert response.output == "Echo target.py"


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
                "verification_status": "not_required",
                "verification_promise": "VERIFIED",
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


def test_runtime_rejects_invalid_command_workflow_mode_during_request_normalization(
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
                            "workflow_mode": "banana",
                        }
                    },
                ),
            )
        )
    except RuntimeRequestError as exc:
        assert "unknown workflow_mode: banana" in str(exc)
    else:
        raise AssertionError(
            "invalid command workflow_mode should fail runtime request normalization"
        )


def test_runtime_preserves_command_only_workflow_mode_for_structured_unregistered_command(
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
                        "workflow_mode": "review",
                    }
                },
            ),
        )
    )

    runtime_config = cast(dict[str, object], response.session.metadata["runtime_config"])
    workflow = cast(dict[str, object], runtime_config["workflow"])

    assert response.session.metadata["workflow_mode"] == "review"
    assert cast(dict[str, object], workflow["effective"])["mode"] == "review"
    assert response.output == "plain prompt"


def test_runtime_command_workflow_mode_overrides_stale_top_level_mode_for_structured_command(
    tmp_path: Path,
) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path, graph=_EchoPromptGraph())

    response = runtime.run(
        RuntimeRequest(
            prompt="plain prompt",
            metadata=cast(
                RuntimeRequestMetadataPayload,
                {
                    "workflow_mode": "deep_work",
                    "command": {
                        "name": "custom",
                        "source": "project",
                        "arguments": [],
                        "raw_arguments": "",
                        "original_prompt": "/custom",
                        "workflow_mode": "review",
                    },
                },
            ),
        )
    )

    runtime_config = cast(dict[str, object], response.session.metadata["runtime_config"])
    workflow = cast(dict[str, object], runtime_config["workflow"])

    assert response.session.metadata["workflow_mode"] == "review"
    assert cast(dict[str, object], workflow["effective"])["mode"] == "review"
    assert response.output == "plain prompt"


def test_runtime_stream_rejects_invalid_top_level_workflow_mode_type(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path, graph=_EchoPromptGraph())

    try:
        _ = tuple(
            runtime.run_stream(
                RuntimeRequest(
                    prompt="plain prompt",
                    metadata=cast(RuntimeRequestMetadataPayload, {"workflow_mode": 123}),
                )
            )
        )
    except RuntimeRequestError as exc:
        assert "request metadata 'workflow_mode' must be a non-empty string" in str(exc)
    else:
        raise AssertionError("invalid workflow_mode type should fail run_stream validation")


def test_compact_command_renders_runtime_continuity_prompt(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path, graph=_EchoPromptGraph())

    response = runtime.run(RuntimeRequest(prompt="/compact preserve test results"))

    assert response.session.metadata.get("command") == {
        "name": "compact",
        "source": "builtin",
        "arguments": ["preserve", "test", "results"],
        "raw_arguments": "preserve test results",
        "original_prompt": "/compact preserve test results",
    }
    assert response.output is not None
    assert "runtime-owned continuity summary" in response.output
    assert "preserve test results" in response.output


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
    assert "AGENTS.md at the workspace root" in response.output
    assert "PROJECT KNOWLEDGE BASE" in response.output
    assert "focus on runtime boundaries" in response.output


def test_memory_command_resolves_through_runtime_prompt_pipeline(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path, graph=_EchoPromptGraph())

    response = runtime.run(RuntimeRequest(prompt="/memory remember preferred test command"))

    assert response.session.metadata.get("command") == {
        "name": "memory",
        "source": "builtin",
        "arguments": ["remember", "preferred", "test", "command"],
        "raw_arguments": "remember preferred test command",
        "original_prompt": "/memory remember preferred test command",
    }
    assert response.output is not None
    assert "memory_search" in response.output
    assert "memory_add" in response.output
    assert "do not create another persistence path" in response.output


def test_memory_subcommands_are_not_builtin_prompt_commands(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path, graph=_EchoPromptGraph())

    for prompt in ("/memory/search coding style", "/memory/add prefers short summaries"):
        try:
            _ = runtime.run(RuntimeRequest(prompt=prompt))
        except RuntimeRequestError as exc:
            assert "unknown command" in str(exc)
        else:
            raise AssertionError(f"{prompt} should not resolve as a builtin command")


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
    assert loop_metadata["verification_status"] == "not_required"
    assert persisted_loop.prompt == "finish the migration"
    assert persisted_loop.intensive is False
    assert persisted_loop.verification_status == "not_required"
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
    assert response.session.metadata.get("workflow_mode") == "deep_work"
    assert response.session.metadata.get("workflow_preset") == "research"
    assert response.session.metadata.get("command") == {
        "name": "intensive-loop",
        "source": "builtin",
        "arguments": ["finish", "the", "migration"],
        "raw_arguments": "finish the migration",
        "original_prompt": "/intensive-loop finish the migration",
    }
    assert loop_metadata["intensive"] is True
    assert loop_metadata["max_iterations"] == 500
    assert loop_metadata["verification_status"] == "pending"
    assert loop_metadata["verification_promise"] == "VERIFIED"
    assert persisted_loop.intensive is True
    assert persisted_loop.max_iterations == 500
    assert persisted_loop.verification_status == "pending"
    assert response.output is not None
    assert "Runtime continuation mode: intensive." in response.output
    assert "Iteration budget: 500." in response.output
    assert "Verification status: pending." in response.output
    assert "<promise>DONE</promise>" in response.output
    assert "<promise>VERIFIED</promise>" in response.output


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
