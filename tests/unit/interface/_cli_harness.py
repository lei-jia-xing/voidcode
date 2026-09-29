"""Shared harness for the CLI contract tests.

The contract tests exercise the CLI the way a user does (exit code, stdout,
stderr, persisted state) and, where a state is not reachable with the
deterministic engine, as ``main(argv)`` with a stubbed runtime seam.

``run_cli`` invokes the CLI in-process; ``run_cli_process`` runs the real
``python -m voidcode`` entrypoint and is reserved for the tests whose subject is
the process itself.

``RUNTIME_SEAM`` is the single module that constructs the runtime for the CLI.
All runtime patches route through it so the seam has exactly one definition.
"""

from __future__ import annotations

import importlib
import io
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from tests.unit._paths import with_src_pythonpath

CLI_APP: Any = importlib.import_module("voidcode.cli.app")
RUNTIME_SEAM: Any = importlib.import_module("voidcode.cli.runtime_gateway")

#: Database file ``run_cli`` isolates each workspace against.
DB_FILE_NAME = ".cli-contracts.sqlite3"


@dataclass(frozen=True, slots=True)
class CliRun:
    """What one CLI invocation observably produced.

    ``returncode``, ``stdout`` and ``stderr`` are the fields the spawned
    ``subprocess.CompletedProcess`` exposed; tests read only those.
    """

    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class StubSessionRef:
    id: str
    parent_id: str | None = None


@dataclass(frozen=True)
class StubSession:
    session: StubSessionRef
    status: str
    turn: int = 1
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class StubEvent:
    event_type: str
    payload: dict[str, object]
    source: str = "runtime"
    sequence: int = 0


@dataclass(frozen=True)
class StubChunk:
    session: StubSession
    event: StubEvent | None = None
    output: str | None = None

    @property
    def kind(self) -> str:
        return "output" if self.output is not None else "event"


def event(event_type: str, **payload: object) -> StubEvent:
    return StubEvent(event_type=event_type, payload=dict(payload))


def chunk(
    *,
    session_id: str = "s1",
    status: str = "completed",
    events: tuple[StubEvent, ...] = (),
    output: str | None = None,
    metadata: dict[str, object] | None = None,
) -> StubChunk:
    """One runtime stream chunk: an event, or the terminal output chunk."""
    streams = list(events)
    if output is not None:
        assert not streams, "an output chunk cannot also carry an event"
        return StubChunk(
            session=StubSession(StubSessionRef(session_id), status, metadata=metadata or {}),
            output=output,
        )
    assert streams, "a chunk needs an event or an output"
    return StubChunk(
        session=StubSession(StubSessionRef(session_id), status, metadata=metadata or {}),
        event=streams[-1],
    )


def stream(*chunks: StubChunk) -> list[StubChunk]:
    return list(chunks)


class StubRuntime:
    """Deterministic stand-in for ``VoidCodeRuntime`` driven by canned streams."""

    requests: list[object]
    cancellations: list[dict[str, object]]

    def __init__(self, *streams: list[StubChunk], debug_snapshot: object | None = None) -> None:
        self._streams = list(streams)
        self.requests: list[object] = []
        self.cancellations: list[dict[str, object]] = []
        self.debug_snapshot = debug_snapshot

    def cancel_session(self, session_id: str, **kwargs: object) -> None:
        self.cancellations.append({"session_id": session_id, **kwargs})

    def run_stream(self, request: object) -> Iterator[StubChunk]:
        self.requests.append(request)
        return iter(self._streams.pop(0))

    def resume_stream(self, **kwargs: object) -> Iterator[StubChunk]:
        self.requests.append(kwargs)
        return iter(self._streams.pop(0))

    def answer_question_stream(self, *args: object, **kwargs: object) -> Iterator[StubChunk]:
        self.requests.append((args, kwargs))
        return iter(self._streams.pop(0))

    def session_debug_snapshot(self, *, session_id: str) -> object:
        if self.debug_snapshot is None:
            raise ValueError(f"unknown session: {session_id}")
        return self.debug_snapshot

    def __enter__(self) -> StubRuntime:
        return self

    def __exit__(self, *exc: object) -> None:
        del exc
        return None


def run_cli(
    *args: str,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> CliRun:
    """Invoke the CLI in this process against an isolated workspace and database.

    The observable contract is the one the spawned process had — exit status,
    stdout, stderr, and the state the command persisted — without paying a
    Python interpreter and full CLI import per call. Everything the spawn gave
    for free is reset by ``_cli_invocation``: the whole environment, the working
    directory, and the two output streams.

    ``run_cli_process`` is the real ``python -m voidcode`` spawn, kept for the
    tests whose subject is the entrypoint itself.
    """
    effective_env = _invocation_env(env, cwd=cwd)
    with _cli_invocation(cwd=cwd, env=effective_env) as (stdout, stderr):
        code = CLI_APP.main(list(args))
    return CliRun(returncode=code, stdout=stdout.text(), stderr=stderr.text())


def run_cli_process(
    *args: str,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the real ``python -m voidcode`` entrypoint against an isolated workspace."""
    return subprocess.run(
        [sys.executable, "-m", "voidcode", *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
        env=_invocation_env(env, cwd=cwd),
    )


def _invocation_env(env: dict[str, str] | None, *, cwd: Path) -> dict[str, str]:
    """The environment one CLI invocation runs with, in-process or spawned."""
    effective_env = with_src_pythonpath(env)
    effective_env.setdefault("VOIDCODE_EXECUTION_ENGINE", "deterministic")
    effective_env.setdefault("VOIDCODE_DB_PATH", str(cwd / DB_FILE_NAME))
    effective_env.setdefault("HOME", str(cwd))
    effective_env.setdefault("XDG_CONFIG_HOME", str(cwd / ".config"))
    effective_env.setdefault("XDG_STATE_HOME", str(cwd / ".state"))
    effective_env.setdefault("XDG_CACHE_HOME", str(cwd / ".cache"))
    explicit = set(env or {})
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "VOIDCODE_MODEL"):
        if key not in explicit:
            effective_env.pop(key, None)
    return effective_env


class _CapturedText(io.TextIOWrapper):
    """A text stream click can write through (it needs a binary ``.buffer``)."""

    def __init__(self) -> None:
        super().__init__(io.BytesIO(), encoding="utf-8", newline="", write_through=True)

    def text(self) -> str:
        buffer = self.buffer
        assert isinstance(buffer, io.BytesIO)
        return buffer.getvalue().decode("utf-8", "replace")


@contextmanager
def _cli_invocation(*, cwd: Path, env: dict[str, str]) -> Iterator[tuple[_CapturedText, _CapturedText]]:
    """Isolate one in-process CLI call the way a fresh process did.

    Replaces the environment outright (the spawn passed ``env=`` rather than
    merging), restores the working directory, and captures both output streams.
    """
    stdout, stderr = _CapturedText(), _CapturedText()
    saved_env = dict(os.environ)
    saved_cwd = os.getcwd()
    os.environ.clear()
    os.environ.update(env)
    os.chdir(cwd)
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            yield stdout, stderr
    finally:
        os.chdir(saved_cwd)
        os.environ.clear()
        os.environ.update(saved_env)


@contextmanager
def cli_boundary(
    *,
    config: object,
    runtime: object | None = None,
    stdin: object | None = None,
    stderr: object | None = None,
) -> Iterator[None]:
    """Patch the CLI runtime seam for a ``main(argv)`` invocation."""
    with ExitStack() as stack:
        stack.enter_context(patch.object(RUNTIME_SEAM, "load_runtime_config", return_value=config))
        if runtime is not None:
            stack.enter_context(patch.object(RUNTIME_SEAM, "VoidCodeRuntime", return_value=runtime))
        if stdin is not None:
            stack.enter_context(patch.object(sys, "stdin", stdin))
        if stderr is not None:
            stack.enter_context(patch.object(sys, "stderr", stderr))
        yield


def deterministic_config(**overrides: Any) -> Any:
    """A ``RuntimeConfig`` shaped like the deterministic offline harness."""
    config_module = importlib.import_module("voidcode.runtime.config")
    return config_module.RuntimeConfig(approval_mode="yolo", execution_engine="deterministic", **overrides)


def session_snapshot(**overrides: object) -> SimpleNamespace:
    fields: dict[str, object] = {"resumable": False, "last_tool": None}
    fields.update(overrides)
    return SimpleNamespace(**fields)
