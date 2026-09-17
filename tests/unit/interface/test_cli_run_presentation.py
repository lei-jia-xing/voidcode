"""Rendering contract for ``voidcode run --trace``.

The trace stream is the CLI's tool-call/streaming presentation surface. These
tests drive a fixed event fixture through ``main()`` and assert the exact
rendered transcript, so the presentation cannot drift silently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.cli_support import EXIT_RUNTIME_ERROR

from ._cli_harness import (
    StubChunk,
    StubEvent,
    StubRuntime,
    TtyInput,
    TtyStderr,
    chunk,
    cli_boundary,
    deterministic_config,
    event,
    stream,
)

WORKSPACE = Path("/tmp/cli-trace-workspace")


def tool_call_stream(*, stdout: str = "line one\nline two\n", stderr: str = "") -> list[StubChunk]:
    """A model turn that reads one file, with streamed tool output and a TODO update."""
    events = [
        event("graph.model_turn", provider="openai", model="gpt-4o", turn=1),
        event("graph.provider_stream", channel="text", kind="delta", text="Reading the file."),
        event(
            "graph.tool_request_created",
            tool="read",
            arguments={"path": "README.md"},
            display={"summary": "README.md"},
        ),
        event("runtime.tool_started", tool="read", display={"summary": "README.md"}),
    ]
    if stdout:
        events.append(event("runtime.tool_progress", tool="read", stream="stdout", chunk=stdout))
    if stderr:
        events.append(event("runtime.tool_progress", tool="read", stream="stderr", chunk=stderr))
    events.append(event("runtime.tool_completed", tool="read", status="ok"))
    events.append(
        event(
            "runtime.todo_updated",
            phases=[
                {
                    "name": "Read",
                    "tasks": [
                        {"status": "completed", "content": "read readme"},
                        {"status": "blocked", "content": "write file", "blocker": "no approval"},
                    ],
                }
            ],
        )
    )
    return [
        *(chunk(events=(single,)) for single in events),
        chunk(status="completed", output="Done."),
    ]


def run_trace(capsys: pytest.CaptureFixture[str], *streams: list[StubChunk], extra: tuple[str, ...] = ()) -> str:
    from voidcode.cli import app

    runtime = StubRuntime(*streams)
    with cli_boundary(config=deterministic_config(), runtime=runtime):
        assert app.main(["run", "read the readme", "--workspace", str(WORKSPACE), "--trace", *extra]) == 0
    return capsys.readouterr().out


def test_tool_call_renders_one_block_per_call(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_trace(capsys, stream(*tool_call_stream())) == (
        "\n● Model turn 1: openai · gpt-4o\n"
        "Reading the file."
        "\n"
        "\n▸ Tool call: read\n"
        "  README.md\n"
        "  │ line one\n"
        "  │ line two\n"
        "  ✓ read ok\n"
        "\nTODO\n"
        "  Read\n"
        "    [x] read readme\n"
        "    [!] write file — no approval\n"
        "Session id: s1\n"
        "\nResult\n"
        "Done.\n"
    )


def test_tool_started_alone_renders_no_tool_row(capsys: pytest.CaptureFixture[str]) -> None:
    fixtures = [
        chunk(events=(event("runtime.tool_started", tool="read", display={"summary": "README.md"}),)),
        chunk(status="completed", output="ok."),
    ]

    assert run_trace(capsys, stream(*fixtures)) == "Session id: s1\n\nResult\nok.\n"


def test_tool_progress_marks_stderr_rows(capsys: pytest.CaptureFixture[str]) -> None:
    out = run_trace(capsys, stream(*tool_call_stream(stdout="", stderr="warning: slow\n")))
    assert "  ┃ warning: slow\n" in out
    assert "  │ warning: slow" not in out


def test_tool_completed_error_row_carries_the_message(capsys: pytest.CaptureFixture[str]) -> None:
    fixtures = [
        chunk(events=(event("graph.tool_request_created", tool="write", display={"summary": "a.txt"}),)),
        chunk(events=(event("runtime.tool_completed", tool="write", status="error", error="disk full"),)),
        chunk(status="completed", output="gave up."),
    ]
    out = run_trace(capsys, stream(*fixtures))
    assert "  ✖ write error\n    disk full\n" in out


def test_shell_exec_call_shows_the_command(capsys: pytest.CaptureFixture[str]) -> None:
    fixtures = [
        chunk(events=(event("graph.tool_request_created", tool="shell_exec", arguments={"command": "pytest -q"}),)),
        chunk(events=(event("runtime.tool_completed", tool="shell_exec", status="ok"),)),
        chunk(status="completed", output="ok."),
    ]
    out = run_trace(capsys, stream(*fixtures))
    assert "▸ Tool call: shell_exec\n  $ pytest -q\n" in out


def test_reasoning_text_is_hidden_unless_requested(capsys: pytest.CaptureFixture[str]) -> None:
    fixtures = [
        chunk(events=(event("runtime.reasoning_part", text="secret chain of thought"),)),
        chunk(events=(event("graph.provider_stream", channel="reasoning", kind="delta", text="more thinking"),)),
        chunk(status="completed", output="answer."),
    ]
    hidden = run_trace(capsys, stream(*fixtures))
    shown = run_trace(capsys, stream(*fixtures), extra=("--show-thinking",))

    assert "secret chain of thought" not in hidden
    assert "more thinking" not in hidden
    assert "[thinking] secret chain of thought" in shown
    assert "more thinking" in shown


def test_incomplete_stream_is_reported_as_failure(capsys: pytest.CaptureFixture[str]) -> None:
    from voidcode.cli import app

    runtime = StubRuntime(stream(chunk(status="running", events=(event("runtime.tool_started", tool="read"),))))
    with cli_boundary(config=deterministic_config(), runtime=runtime):
        code = app.main(["run", "read", "--workspace", str(WORKSPACE), "--trace"])
    captured = capsys.readouterr()
    assert code == EXIT_RUNTIME_ERROR
    assert "runtime stream ended without a terminal outcome" in captured.out


RETRY_NOTICE = "↻ Provider retry: the failed attempt's partial output was discarded; restarting the turn."
FALLBACK_NOTICE = "↻ Provider fallback to anthropic: the failed attempt's partial output was discarded; restarting the turn."


def transient_retry_event(**overrides: object) -> StubEvent:
    payload: dict[str, object] = {
        "reason": "timeout",
        "provider": "openai",
        "model": "gpt-4o",
        "retry_attempt": 1,
        "max_retries": 2,
        "delay_ms": 0,
        "discarded_streamed_output": True,
    }
    payload.update(overrides)
    return event("runtime.provider_transient_retry", **payload)


def fallback_event(**overrides: object) -> StubEvent:
    payload: dict[str, object] = {
        "reason": "rate_limit",
        "from_provider": "openai",
        "from_model": "gpt-4o",
        "to_provider": "anthropic",
        "to_model": "claude-3-5-sonnet",
        "attempt": 1,
        "discarded_streamed_output": True,
    }
    payload.update(overrides)
    return event("runtime.provider_fallback", **payload)


def restarted_attempt_stream(restart: StubEvent) -> list[StubChunk]:
    """Attempt 1 streams a partial answer, the runtime restarts it, attempt 2 succeeds."""
    return [
        chunk(events=(event("graph.model_turn", provider="openai", model="gpt-4o", turn=1),)),
        chunk(events=(event("graph.provider_stream", channel="text", kind="delta", text="First attempt: half a sen"),)),
        chunk(events=(restart,)),
        chunk(events=(event("graph.provider_stream", channel="text", kind="delta", text="Second attempt: the whole answer."),)),
        chunk(events=(event("graph.tool_request_created", tool="read", display={"summary": "note.txt"}),)),
        chunk(events=(event("runtime.tool_completed", tool="read", status="ok"),)),
        chunk(status="completed", output="Second attempt: the whole answer."),
    ]


def test_restarted_attempt_announces_the_discarded_output(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_trace(capsys, stream(*restarted_attempt_stream(transient_retry_event()))) == (
        "\n● Model turn 1: openai · gpt-4o\n"
        "First attempt: half a sen"
        "\n"
        f"\n{RETRY_NOTICE}\n"
        "Second attempt: the whole answer."
        "\n"
        "\n▸ Tool call: read\n"
        "  note.txt\n"
        "  ✓ read ok\n"
        "Session id: s1\n"
        "\nResult\n"
        "Second attempt: the whole answer.\n"
    )


def test_fallback_attempt_announces_the_discarded_output(capsys: pytest.CaptureFixture[str]) -> None:
    out = run_trace(capsys, stream(*restarted_attempt_stream(fallback_event())))

    assert out.count(FALLBACK_NOTICE) == 1
    assert out.index("First attempt: half a sen") < out.index(FALLBACK_NOTICE) < out.index("Second attempt: the whole answer.")


def test_restart_without_discarded_output_stays_silent(capsys: pytest.CaptureFixture[str]) -> None:
    out = run_trace(capsys, stream(*restarted_attempt_stream(transient_retry_event(discarded_streamed_output=None))))

    assert "discarded" not in out
    assert "First attempt: half a sen" in out
    assert "Second attempt: the whole answer." in out


def test_live_event_transcript_announces_the_restart_once(capsys: pytest.CaptureFixture[str]) -> None:
    from voidcode.cli import app

    runtime = StubRuntime(stream(*restarted_attempt_stream(transient_retry_event())))
    with cli_boundary(
        config=deterministic_config(),
        runtime=runtime,
        stdin=TtyInput(),
        stderr=TtyStderr(),
    ):
        code = app.main(["run", "read note.txt", "--workspace", str(WORKSPACE)])

    out = capsys.readouterr().out
    assert code == 0
    assert out.count(RETRY_NOTICE) == 1
    assert out.index("First attempt: half a sen") < out.index(RETRY_NOTICE) < out.index("Second attempt: the whole answer.")
    assert "discarded_streamed_output=True" in out
    assert "RESULT" in out
