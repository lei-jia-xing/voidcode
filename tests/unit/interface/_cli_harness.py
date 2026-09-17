"""Shared harness for the CLI contract tests.

The contract tests exercise the CLI as a process (exit code, stdout, stderr) and,
where a state is not reachable with the deterministic engine, as ``main(argv)``
with a stubbed runtime seam.

``RUNTIME_SEAM`` is the single module that constructs the runtime for the CLI.
All runtime patches route through it so the seam has exactly one definition.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from tests.unit._paths import with_src_pythonpath

RUNTIME_SEAM: Any = importlib.import_module("voidcode.cli.runtime_gateway")
CLI_SUPPORT: Any = importlib.import_module("voidcode.cli_support")


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


class TtyInput:
    """Interactive stdin stub: ``isatty()`` plus scripted answers."""

    def __init__(self, *answers: str) -> None:
        self._answers = list(answers)

    def isatty(self) -> bool:
        return True

    def readline(self) -> str:
        return self._answers.pop(0) if self._answers else ""


class TtyStderr:
    def __init__(self, *, isatty: bool = True) -> None:
        self.writes: list[str] = []
        self._isatty = isatty

    def isatty(self) -> bool:
        return self._isatty

    def write(self, text: str) -> int:
        self.writes.append(text)
        return len(text)

    def flush(self) -> None:
        return None

    @property
    def text(self) -> str:
        return "".join(self.writes)


def run_cli(
    *args: str,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``python -m voidcode`` against an isolated workspace and database."""
    effective_env = with_src_pythonpath(env)
    effective_env.setdefault("VOIDCODE_EXECUTION_ENGINE", "deterministic")
    effective_env.setdefault("VOIDCODE_DB_PATH", str(cwd / ".cli-contracts.sqlite3"))
    effective_env.setdefault("HOME", str(cwd))
    effective_env.setdefault("XDG_CONFIG_HOME", str(cwd / ".config"))
    effective_env.setdefault("XDG_STATE_HOME", str(cwd / ".state"))
    effective_env.setdefault("XDG_CACHE_HOME", str(cwd / ".cache"))
    explicit = set(env or {})
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "VOIDCODE_MODEL"):
        if key not in explicit:
            effective_env.pop(key, None)
    return subprocess.run(
        [sys.executable, "-m", "voidcode", *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
        env=effective_env,
    )


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
    return config_module.RuntimeConfig(approval_mode="deny", execution_engine="deterministic", **overrides)


def session_snapshot(**overrides: object) -> SimpleNamespace:
    fields: dict[str, object] = {"resumable": False, "last_tool": None}
    fields.update(overrides)
    return SimpleNamespace(**fields)
