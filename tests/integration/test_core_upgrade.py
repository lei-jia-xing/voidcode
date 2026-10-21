"""Backend proof for the controlled core upgrade: two installations, one session.

Each case drives the real entry points - the supervisor process, the upgrade CLI, the HTTP API,
and a loopback provider - with no mocks: a candidate installation carrying a real core-behavior
change is elected while the original session continues, side effects happen exactly once, paused
runs resume exactly once, failures recover onto the old image, and unsupported state refuses
before anything stops.
"""

from __future__ import annotations

import http.client
import json
import os
import queue
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path

from tests.fixtures.reload_plugin import PROCESS_TOOL, write_reload_plugin
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.core.activation import bundled_sources
from voidcode.core.images import running_package_root
from voidcode.core.supervisor import control_socket_path

PLUGIN_ID = "reload-fixture"
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "reasoning_content": "upgrade smoke", "tool_calls": list(calls)},
        finish="tool_calls" if calls else "stop",
    )


@contextmanager
def _provider(replies: list[dict[str, object]]) -> Iterator[OpenAILoopbackServer]:
    server = OpenAILoopbackServer(replies)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(timeout=10)
        server.server_close()


def _configure(workspace: Path, server: OpenAILoopbackServer, source: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    plugins: dict[str, object] = {name: {"enabled": False} for name in bundled_sources() if name != "providers"}
    plugins[PLUGIN_ID] = {"path": str(source), "enabled": True}
    (workspace / "voidcode.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model": {"model": "endpoint/exact-model"},
                "policy": {"approval_mode": "yolo", "read_only": False},
                "providers": {
                    "endpoint": {
                        "api_key": "loopback-secret-not-for-storage",
                        "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                    }
                },
                "plugins": plugins,
            }
        ),
        encoding="utf-8",
    )


def _api(endpoint: str, token: str, method: str, path: str, body: object = None) -> tuple[int, dict[str, object]]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(endpoint + path, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.loads(response.read().decode("utf-8"))
            assert isinstance(payload, dict)
            return response.status, payload
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read().decode("utf-8"))
        assert isinstance(payload, dict)
        return exc.code, payload


def _health(endpoint: str, token: str) -> dict[str, object]:
    status, payload = _api(endpoint, token, "GET", "/api/health")
    assert status == 200, payload
    return payload


class _Supervisor:
    """A real supervisor process: parsed endpoint/token, health-gated readiness, clean teardown."""

    def __init__(self, workspace: Path, state_dir: Path, *, session_id: str | None = None) -> None:
        self.workspace = workspace
        self.state_dir = state_dir
        command = [
            sys.executable,
            "-m",
            "voidcode.core",
            "serve",
            f"--workspace={workspace}",
            f"--state-dir={state_dir}",
            "--host=127.0.0.1",
            "--port=0",
        ]
        if session_id is not None:
            command.append(f"--session-id={session_id}")
        env = dict(__import__("os").environ)
        env["PYTHONPATH"] = str(_REPO_ROOT / "src")
        self._stderr_path = state_dir.parent / "supervisor.stderr.log"
        self._stderr = open(self._stderr_path, "wb")  # noqa: PTH123 - test scratch
        self.proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            text=True,
            bufsize=1,
            cwd=state_dir.parent,
            env=env,
        )
        self.endpoint = ""
        self.token = ""
        self._lines: list[str] = []
        try:
            self._await_ready()
        except BaseException:
            self.stop()
            raise

    def _await_ready(self) -> None:
        assert self.proc.stdout is not None
        deadline = time.monotonic() + 240.0
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"supervisor exited early with status {self.proc.returncode}:\n{''.join(self._lines[-20:])}\nstderr:\n{self._stderr_tail()}"
                )
            line = self.proc.stdout.readline()
            if not line:
                time.sleep(0.05)
                continue
            self._lines.append(line)
            text = line.strip()
            if text.startswith("core endpoint: "):
                self.endpoint = text.partition(": ")[2].strip()
            elif text.startswith("core bearer token: "):
                self.token = text.partition(": ")[2].strip()
            elif text.startswith("core ready: "):
                break
        else:
            raise TimeoutError(f"supervisor never became ready:\n{''.join(self._lines[-20:])}")
        assert self.endpoint and self.token
        deadline = time.monotonic() + 240.0
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("supervisor exited while starting its child")
            try:
                if _health(self.endpoint, self.token).get("stage") == "serving":
                    return
            except Exception:
                pass
            time.sleep(0.5)
        raise TimeoutError("child never reached serving state")

    def _stderr_tail(self) -> str:
        try:
            with open(self._stderr_path, encoding="utf-8", errors="replace") as stream:
                return "".join(stream.readlines()[-40:])
        except OSError:
            return "<unreadable supervisor stderr>"

    def stop(self) -> None:
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=30)
        finally:
            if self.proc.stdout is not None:
                self.proc.stdout.close()
            self._stderr.close()

    def upgrade(self, candidate: Path, *, session_id: str | None, timeout_seconds: float = 180.0) -> tuple[int, dict[str, object]]:
        command = [
            sys.executable,
            "-m",
            "voidcode.core",
            "upgrade",
            "--candidate",
            str(candidate),
            "--timeout",
            str(timeout_seconds),
            "--workspace",
            str(self.workspace),
            "--state-dir",
            str(self.state_dir),
        ]
        if session_id is not None:
            command.extend(("--session-id", session_id))
        env = dict(__import__("os").environ)
        env["PYTHONPATH"] = str(_REPO_ROOT / "src")
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout_seconds + 120, check=False)
        payload = json.loads(completed.stdout.strip().splitlines()[-1]) if completed.stdout.strip() else {}
        assert isinstance(payload, dict), completed.stdout + completed.stderr
        return completed.returncode, payload

    def status(self) -> dict[str, object]:
        """One status round trip with the supervisor's own socket (the surface a launcher uses)."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(120)
            client.connect(str(control_socket_path(self.state_dir)))
            client.sendall(json.dumps({"op": "status"}).encode("utf-8") + b"\n")
            buffer = b""
            while b"\n" not in buffer:
                chunk = client.recv(65536)
                if not chunk:
                    break
                buffer += chunk
        payload = json.loads(buffer.split(b"\n", 1)[0])
        assert isinstance(payload, dict), payload
        return payload

    def revision_pin(self) -> dict[str, object]:
        database = self.state_dir / "kernel-v1.sqlite3"
        with sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True) as raw:
            revision = raw.execute("SELECT active_revision_id FROM workspace_state").fetchone()[0]
            payload = json.loads(raw.execute("SELECT payload_json FROM revisions WHERE revision_id = ?", (revision,)).fetchone()[0])
        assert isinstance(payload, dict) and isinstance(payload.get("image"), dict)
        return dict(payload["image"])


def _candidate_installation(root: Path, *, prompt_limit: int | None = None, break_startup: bool = False, name: str = "candidate") -> Path:
    """A throwaway second installation: the running tree copied, with one real behavior patched."""
    destination = root / name / "src" / "voidcode"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(running_package_root(), destination, ignore=shutil.ignore_patterns("__pycache__"))
    engine = destination / "core" / "engine.py"
    if prompt_limit is not None:
        text = engine.read_text(encoding="utf-8")
        anchor = "CLIENT_PROMPT_MAX_BYTES = 64 * 1024"
        assert anchor in text
        engine.write_text(text.replace(anchor, f"CLIENT_PROMPT_MAX_BYTES = {prompt_limit}", 1), encoding="utf-8")
    if break_startup:
        with engine.open("a", encoding="utf-8") as stream:
            stream.write('\nraise RuntimeError("candidate cannot start")\n')
    return root / name


def _show_events(endpoint: str, token: str, session_id: str) -> list[dict[str, object]]:
    status, payload = _api(endpoint, token, "GET", f"/api/sessions/{session_id}/show?events=true")
    assert status == 200, payload
    events = payload["events"]
    assert isinstance(events, list)
    return events


def test_two_installations_change_a_core_decision_in_the_same_session(tmp_path: Path) -> None:
    marker = tmp_path / "side-effect.txt"
    replies = [
        _turn(_call("call-1", PROCESS_TOOL, argv=["sh", "-c", f"echo side-effect >> {marker}"], timeout_seconds=60)),
        _turn(output="first turn done"),
        _turn(output="second turn done"),
    ]
    with _provider(replies) as server:
        workspace, state = tmp_path / "workspace", tmp_path / "state"
        source = write_reload_plugin(tmp_path / "plugin", process_tool=True, deactivation="pause")
        _configure(workspace, server, source)
        supervisor = _Supervisor(workspace, state)
        try:
            endpoint, token = supervisor.endpoint, supervisor.token
            first = _health(endpoint, token)
            assert first["fenced"] is False and first["stage"] == "serving" and first["generation"] == 1
            old_image = first["image_digest"]
            assert old_image

            status, _ = _api(endpoint, token, "POST", "/api/sessions", {"session_id": "smoke-session"})
            assert status == 201
            status, first_turn = _api(endpoint, token, "POST", "/api/sessions/smoke-session/run", {"prompt": "append the line", "max_steps": 8})
            assert status == 200, first_turn
            assert first_turn["status"] == "completed"
            assert marker.read_text(encoding="utf-8").splitlines() == ["side-effect"]
            prefix = [(event["sequence"], event["type"], event["payload"]) for event in _show_events(endpoint, token, "smoke-session")]
            assert len(server.requests) == 2

            candidate = _candidate_installation(tmp_path, prompt_limit=4 * 1024)
            code, upgraded = supervisor.upgrade(candidate, session_id="smoke-session")
            assert code == 0, upgraded
            assert upgraded["status"] == "upgraded", upgraded
            assert upgraded["from_image"] == old_image and upgraded["to_image"] != old_image

            # Same endpoint, same token, same session; the prefix is byte-identical.
            second = _health(endpoint, token)
            assert second["stage"] == "serving" and second["fenced"] is False
            assert second["generation"] == 2 and second["image_digest"] == upgraded["to_image"]
            events = _show_events(endpoint, token, "smoke-session")
            assert [(event["sequence"], event["type"], event["payload"]) for event in events[: len(prefix)]] == prefix
            adopted = [event for event in events if event["type"] == "kernel.revision_adopted"]
            assert len(adopted) == 1
            adopted_payload = adopted[0]["payload"]
            assert isinstance(adopted_payload, dict) and adopted_payload["image_digest"] == upgraded["to_image"]

            # The core decision changed: the same bounded prompt dispatches before, refuses after.
            big_prompt = "x" * 8192
            status, rejected = _api(endpoint, token, "POST", "/api/sessions/smoke-session/run", {"prompt": big_prompt, "max_steps": 8})
            assert status == 400, rejected
            assert len(server.requests) == 2, "a refused prompt must not dispatch"

            status, continued = _api(endpoint, token, "POST", "/api/sessions/smoke-session/run", {"prompt": "continue", "max_steps": 8})
            assert status == 200 and continued["status"] == "completed", continued
            assert len(server.requests) == 3

            # Exactly one side effect across the whole handoff.
            assert marker.read_text(encoding="utf-8").splitlines() == ["side-effect"]
            final = _show_events(endpoint, token, "smoke-session")
            assert len([event for event in final if event["type"] == "kernel.tool_completed"]) == 1

            # A later plugin-only revision keeps the elected pin across a cold restart.
            config_path = workspace / "voidcode.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["plugins"][PLUGIN_ID]["enabled"] = False
            config_path.write_text(json.dumps(config), encoding="utf-8")
            status, catalog = _api(endpoint, token, "GET", "/api/commands")
            assert status == 200
            status, reloaded = _api(
                endpoint,
                token,
                "POST",
                "/api/sessions/smoke-session/invoke/core.reload",
                {"arguments": {}, "expected_revision": catalog["revision_id"]},
            )
            assert status == 200 and reloaded["status"] == "ok", reloaded
            assert supervisor.revision_pin()["digest"] == upgraded["to_image"]
        finally:
            supervisor.stop()

        # Restarting from the original installation still serves the elected image.
        supervisor2 = _Supervisor(workspace, state)
        try:
            third = _health(supervisor2.endpoint, supervisor2.token)
            assert third["stage"] == "serving" and third["image_digest"] == upgraded["to_image"]
            status, listed = _api(supervisor2.endpoint, supervisor2.token, "GET", "/api/sessions")
            sessions = listed["sessions"]
            assert isinstance(sessions, list)
            assert status == 200 and any(isinstance(item, dict) and item["session_id"] == "smoke-session" for item in sessions)
        finally:
            supervisor2.stop()


def test_two_sessions_pause_and_resume_exactly_once_across_the_upgrade(tmp_path: Path) -> None:
    first_a = _turn(_call("call-a", PROCESS_TOOL, argv=["sh", "-c", "sleep 20"], timeout_seconds=60))
    first_b = _turn(_call("call-b", PROCESS_TOOL, argv=["sh", "-c", "sleep 20"], timeout_seconds=60))
    with _provider([first_a, first_b, _turn(output="a done"), _turn(output="b done")]) as server:
        workspace, state = tmp_path / "workspace", tmp_path / "state"
        source = write_reload_plugin(tmp_path / "plugin", process_tool=True)
        _configure(workspace, server, source)
        supervisor = _Supervisor(workspace, state)
        try:
            endpoint, token = supervisor.endpoint, supervisor.token
            for session_id in ("pause-a", "pause-b"):
                status, _ = _api(endpoint, token, "POST", "/api/sessions", {"session_id": session_id})
                assert status == 201

            results: dict[str, object] = {}

            def drive(session_id: str, prompt: str) -> None:
                results[session_id] = _api(endpoint, token, "POST", f"/api/sessions/{session_id}/run", {"prompt": prompt, "max_steps": 8})

            thread_a = threading.Thread(target=drive, args=("pause-a", "sleep a"))
            thread_a.start()

            def intent_seen(session_id: str) -> bool:
                try:
                    return any(event["type"] == "kernel.tool_intent" for event in _show_events(endpoint, token, session_id))
                except Exception:
                    return False

            deadline = time.monotonic() + 60.0
            while not intent_seen("pause-a") and time.monotonic() < deadline:
                time.sleep(0.1)
            assert intent_seen("pause-a")
            thread_b = threading.Thread(target=drive, args=("pause-b", "sleep b"))
            thread_b.start()
            deadline = time.monotonic() + 60.0
            while not intent_seen("pause-b") and time.monotonic() < deadline:
                time.sleep(0.1)
            assert intent_seen("pause-b")

            streams: dict[str, _StreamReader] = {}
            for stream_session in ("pause-a", "pause-b"):
                streams[stream_session] = _StreamReader(
                    endpoint,
                    f"/api/sessions/{stream_session}/events?after_sequence=0&follow=true",
                    token,
                )
            time.sleep(2.0)

            candidate = _candidate_installation(tmp_path, prompt_limit=4 * 1024)
            code, upgraded = supervisor.upgrade(candidate, session_id="pause-a", timeout_seconds=120.0)
            assert code == 0, upgraded
            assert upgraded["status"] == "upgraded", upgraded
            thread_a.join(timeout=120)
            thread_b.join(timeout=120)
            assert not thread_a.is_alive() and not thread_b.is_alive()

            assert _health(endpoint, token)["image_digest"] == upgraded["to_image"]
            # Resumption is asynchronous: the elected core's driver consumes each staged
            # intent and drives it in turn. Wait for both sessions to settle before asserting.
            deadline = time.monotonic() + 180.0
            while time.monotonic() < deadline:
                settled = True
                for session_id in ("pause-a", "pause-b"):
                    status, shown = _api(endpoint, token, "GET", f"/api/sessions/{session_id}/show")
                    assert status == 200
                    if shown["status"] not in {"completed", "failed"}:
                        settled = False
                        break
                if settled:
                    break
                time.sleep(0.5)
            for session_id in ("pause-a", "pause-b"):
                events = _show_events(endpoint, token, session_id)
                paused = [event for event in events if event["type"] == "kernel.upgrade_paused"]
                assert len(paused) == 1, session_id
                consumed = [event for event in events if event["type"] == "kernel.continuation_consumed"]
                assert len(consumed) == 1, session_id
                consumed_payload = consumed[0]["payload"]
                assert isinstance(consumed_payload, dict) and consumed_payload["cause"] == "upgrade", session_id
                assert len([event for event in events if event["type"] == "kernel.tool_completed"]) == 1, session_id
                reader = streams[session_id]
                assert isinstance(reader, _StreamReader)
                assert reader.wait_ended(timeout=60), f"stream for {session_id} never ended on its own after the restart"
                reader.close()
                delivered: list[int] = []
                restart_cursor: int | None = None
                collected = reader.frames()
                for frame in collected:
                    assert isinstance(frame, dict), session_id
                    if frame.get("kind") == "server_restart":
                        assert restart_cursor is None, session_id
                        cursor = frame["cursor"]
                        assert isinstance(cursor, int), session_id
                        restart_cursor = cursor
                    elif "events" in frame:
                        head_events = frame["events"]
                        assert isinstance(head_events, list), session_id
                        delivered.extend(int(event["sequence"]) for event in head_events)
                    elif frame.get("kind") == "event":
                        event = frame["event"]
                        assert isinstance(event, dict), session_id
                        delivered.append(int(event["sequence"]))
                errors = [frame for frame in collected if isinstance(frame, dict) and "stream_error" in frame]
                assert not errors, (session_id, errors)
                assert restart_cursor is not None, (session_id, len(collected), collected[:3])
                assert sorted(delivered) == list(range(1, restart_cursor + 1)), session_id
            status, shown_a = _api(endpoint, token, "GET", "/api/sessions/pause-a/show")
            status_b, shown_b = _api(endpoint, token, "GET", "/api/sessions/pause-b/show")
            assert status == 200 and shown_a["status"] == "completed"
            assert status_b == 200 and shown_b["status"] == "completed"
        finally:
            supervisor.stop()


def test_candidate_startup_failure_recovers_the_old_image(tmp_path: Path) -> None:
    with _provider([_turn(output="before"), _turn(output="after")]) as server:
        workspace, state = tmp_path / "workspace", tmp_path / "state"
        source = write_reload_plugin(tmp_path / "plugin", process_tool=True)
        _configure(workspace, server, source)
        supervisor = _Supervisor(workspace, state)
        try:
            endpoint, token = supervisor.endpoint, supervisor.token
            old_image = _health(endpoint, token)["image_digest"]
            status, _ = _api(endpoint, token, "POST", "/api/sessions", {"session_id": "fallback"})
            assert status == 201
            status, _ = _api(endpoint, token, "POST", "/api/sessions/fallback/run", {"prompt": "before", "max_steps": 4})
            assert status == 200

            candidate = _candidate_installation(tmp_path, break_startup=True)
            code, rolled_back = supervisor.upgrade(candidate, session_id="fallback", timeout_seconds=120.0)
            assert code == 1, rolled_back
            assert rolled_back["status"] == "rolled_back", rolled_back

            health = _health(endpoint, token)
            assert health["stage"] == "serving" and health["image_digest"] == old_image
            status, continued = _api(endpoint, token, "POST", "/api/sessions/fallback/run", {"prompt": "after", "max_steps": 4})
            assert status == 200 and continued["status"] == "completed", continued
            assert len(server.requests) == 2
        finally:
            supervisor.stop()


class _StreamReader:
    """One SSE stream collected on a thread, closed from the test thread.

    The read uses ``read1`` (one TCP chunk at a time) instead of ``read``: ``read`` waits for
    the full requested size or EOF, which never comes on an idle follow stream, and
    ``http.client`` cannot resume after a socket timeout.
    """

    def __init__(self, base: str, path: str, token: str) -> None:
        from urllib.parse import urlparse

        parts = urlparse(base)
        assert parts.hostname is not None
        self._connection = http.client.HTTPConnection(parts.hostname, parts.port)
        self._connection.request("GET", path, headers={"Authorization": f"Bearer {token}"})
        self._response = self._connection.getresponse()
        if self._response.status != 200:
            raise AssertionError(f"stream {path} returned HTTP {self._response.status}")
        self._sock = self._connection.sock
        assert self._sock is not None
        self.sink: queue.Queue[object] = queue.Queue()
        self._thread = threading.Thread(target=self._collect, daemon=True)
        self._thread.start()
        # Gate on the head: everything after open is ordered after it, so a slow server
        # delays the test instead of silently truncating the stream.
        try:
            head = self.sink.get(timeout=120)
        except queue.Empty:
            raise AssertionError(f"stream {path} delivered no head within 120s") from None
        assert isinstance(head, dict) and "events" in head, f"stream {path} first frame is not a head: {head!r}"
        self._head: object = head

    def _collect(self) -> None:
        buffer = b""
        try:
            while True:
                try:
                    chunk = self._response.read1(65536)
                except Exception:  # noqa: BLE001 - a closed or failed stream ends collection
                    break
                if not chunk:
                    break
                buffer += chunk
                while b"\n\n" in buffer:
                    raw, _, buffer = buffer.partition(b"\n\n")
                    for line in raw.split(b"\n"):
                        if line.startswith(b"data: "):
                            self.sink.put(json.loads(line[len(b"data: ") :].decode("utf-8")))
        finally:
            self.sink.put(None)

    def wait_ended(self, timeout: float) -> bool:
        """True once the server has ended the stream on its own (no shutdown sent)."""
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    def close(self) -> None:
        # Shut the socket down without touching the connection object: the reader owns the
        # in-flight response until it ends, and connection.close() would break its reads.
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._thread.join(timeout=15)
        assert not self._thread.is_alive(), "stream reader did not end after shutdown"
        self._connection.close()

    def frames(self) -> list[object]:
        # The reader thread is dead after close(), and its finally queued the terminator,
        # so the drain below cannot hang; a missing terminator fails loudly, never silently.
        collected: list[object] = [self._head]
        while True:
            try:
                frame = self.sink.get(timeout=60)
            except queue.Empty:
                raise AssertionError("stream ended without its terminator") from None
            if frame is None:
                break
            collected.append(frame)
        return collected


def test_drain_timeout_abort_keeps_streams_and_finishes_the_run(tmp_path: Path) -> None:
    replies = [_turn(_call("call-slow", PROCESS_TOOL, argv=["sleep", "25"], timeout_seconds=60)), _turn(output="slow done")]
    with _provider(replies) as server:
        workspace, state = tmp_path / "workspace", tmp_path / "state"
        source = write_reload_plugin(tmp_path / "plugin", process_tool=True)
        _configure(workspace, server, source)
        supervisor = _Supervisor(workspace, state)
        try:
            endpoint, token = supervisor.endpoint, supervisor.token
            status, _ = _api(endpoint, token, "POST", "/api/sessions", {"session_id": "abort-me"})
            assert status == 201

            worker = threading.Thread(target=_api, args=(endpoint, token, "POST", "/api/sessions/abort-me/run", {"prompt": "sleep", "max_steps": 8}))
            worker.start()
            deadline = time.monotonic() + 60.0
            while time.monotonic() < deadline:
                try:
                    if any(event["type"] == "kernel.tool_intent" for event in _show_events(endpoint, token, "abort-me")):
                        break
                except Exception:
                    pass
                time.sleep(0.1)

            reader = _StreamReader(endpoint, "/api/sessions/abort-me/events?after_sequence=0&follow=true", token)
            time.sleep(2.0)

            code, blocked = supervisor.upgrade(_candidate_installation(tmp_path, prompt_limit=4 * 1024), session_id="abort-me", timeout_seconds=2.0)
            assert code == 1, blocked
            assert blocked["status"] == "blocked", blocked

            worker.join(timeout=120)
            assert not worker.is_alive()
            deadline = time.monotonic() + 60.0
            while time.monotonic() < deadline:
                status, shown = _api(endpoint, token, "GET", "/api/sessions/abort-me/show")
                assert status == 200
                if shown["status"] == "completed":
                    break
                time.sleep(0.5)
            assert shown["status"] == "completed"
            assert not reader.wait_ended(timeout=5), "a failed drain must leave the stream connected"
            reader.close()

            collected = reader.frames()
            kinds = [frame.get("kind") for frame in collected if isinstance(frame, dict)]
            assert "server_restart" not in kinds, "a failed drain must leave streams connected with no restart frame"
            assert not [frame for frame in collected if isinstance(frame, dict) and "stream_error" in frame]
            assert any(isinstance(frame, dict) and "events" in frame for frame in collected), "the stream must deliver its head"
            assert any(isinstance(frame, dict) and frame.get("kind") == "event" for frame in collected), (
                "the stream must deliver follow-up events while the run finishes"
            )
            events = _show_events(endpoint, token, "abort-me")
            assert not [event for event in events if event["type"] == "kernel.upgrade_paused"]
            assert len([event for event in events if event["type"] == "kernel.tool_completed"]) == 1

            # The supervisor is healthy afterwards: an idle upgrade succeeds.
            status, _ = _api(endpoint, token, "POST", "/api/sessions", {"session_id": "after-abort"})
            assert status == 201
            code, upgraded = supervisor.upgrade(
                _candidate_installation(tmp_path, prompt_limit=4 * 1024, name="candidate-2"), session_id="after-abort"
            )
            assert code == 0, upgraded
            assert upgraded["status"] == "upgraded", upgraded
        finally:
            supervisor.stop()


def test_killed_child_is_recovered_by_the_elected_image_and_resumes_exactly_once(tmp_path: Path) -> None:
    """A hard-killed child never loses facts: the supervisor exits visibly, a cold start runs the
    elected image again, and the parked tool completes exactly once — with no candidate involved."""
    import signal

    replies = [_turn(_call("call-crash", PROCESS_TOOL, argv=["sleep", "5"], timeout_seconds=60)), _turn(output="recovered")]
    with _provider(replies) as server:
        workspace, state = tmp_path / "workspace", tmp_path / "state"
        source = write_reload_plugin(tmp_path / "plugin", process_tool=True)
        _configure(workspace, server, source)
        supervisor = _Supervisor(workspace, state)
        try:
            endpoint, token = supervisor.endpoint, supervisor.token
            status, _ = _api(endpoint, token, "POST", "/api/sessions", {"session_id": "crash-me"})
            assert status == 201
            elected = supervisor.revision_pin()["digest"]
            outcome: dict[str, object] = {}

            def run_turn() -> None:
                try:
                    outcome["run"] = _api(endpoint, token, "POST", "/api/sessions/crash-me/run", {"prompt": "work", "max_steps": 8})
                except Exception as exc:  # noqa: BLE001 - the killed child breaks its own response
                    outcome["error"] = f"{type(exc).__name__}: {exc}"

            worker = threading.Thread(target=run_turn)
            worker.start()
            deadline = time.monotonic() + 120.0
            while time.monotonic() < deadline:
                try:
                    if any(event["type"] == "kernel.tool_intent" for event in _show_events(endpoint, token, "crash-me")):
                        break
                except Exception:
                    pass
                time.sleep(0.1)
            killed_pid = _health(endpoint, token)["pid"]
            assert isinstance(killed_pid, int)
            os.kill(killed_pid, signal.SIGKILL)
            worker.join(timeout=120)
            assert not worker.is_alive()
            assert supervisor.proc.wait(timeout=120) != 0, "a serving child's death must stop the supervisor, never be replaced silently"
        finally:
            supervisor.stop()

        restarted = _Supervisor(workspace, state)
        try:
            endpoint, token = restarted.endpoint, restarted.token
            assert restarted.revision_pin()["digest"] == elected, "the elected image is what restarts"
            assert restarted.status()["active_image"] == elected
            status, _ = _api(endpoint, token, "POST", "/api/sessions/crash-me/resume", {"max_steps": 8})
            assert status == 200
            deadline = time.monotonic() + 180.0
            while time.monotonic() < deadline:
                status, shown = _api(endpoint, token, "GET", "/api/sessions/crash-me/show")
                assert status == 200
                if shown["status"] in ("completed", "failed"):
                    break
                time.sleep(0.5)
            assert shown["status"] == "completed", shown
            events = _show_events(endpoint, token, "crash-me")
            assert [event["type"] for event in events] == [
                "kernel.user_message",
                "kernel.provider_intent",
                "kernel.model_turn",
                "kernel.tool_intent",
                "kernel.run_resumed",
                "kernel.tool_interrupted",
                "kernel.provider_intent",
                "kernel.model_turn",
                "kernel.run_completed",
            ], "a crashed batch member is reported interrupted and never replayed"
        finally:
            restarted.stop()


def test_unsupported_schema_refuses_before_downtime(tmp_path: Path) -> None:
    with _provider([_turn(output="steady")]) as server:
        workspace, state = tmp_path / "workspace", tmp_path / "state"
        source = write_reload_plugin(tmp_path / "plugin", process_tool=True)
        _configure(workspace, server, source)
        supervisor = _Supervisor(workspace, state)
        try:
            endpoint, token = supervisor.endpoint, supervisor.token
            status, _ = _api(endpoint, token, "POST", "/api/sessions", {"session_id": "steady"})
            assert status == 201

            schema_sql = files("voidcode.core.storage").joinpath("schema.sql").read_text(encoding="utf-8")
            assert "CHECK (schema_version = 5)" in schema_sql
            foreign_dir = tmp_path / "foreign-state"
            foreign_dir.mkdir()
            foreign_db = foreign_dir / "kernel-v1.sqlite3"
            with sqlite3.connect(foreign_db) as raw:
                raw.executescript(schema_sql.replace("CHECK (schema_version = 5)", "CHECK (schema_version = 6)"))
                raw.execute("INSERT INTO kernel_identity (id, magic, schema_version) VALUES (1, 'voidcode-kernel-v1', 6)")
                raw.commit()
            before = foreign_db.read_bytes()
            env = dict(__import__("os").environ)
            env["PYTHONPATH"] = str(_REPO_ROOT / "src")
            foreign_workspace = tmp_path / "foreign-workspace"
            foreign_workspace.mkdir()
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "voidcode.core",
                    "preflight",
                    "--workspace",
                    str(foreign_workspace),
                    "--state-dir",
                    str(foreign_dir),
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
                env=env,
            )
            assert completed.returncode == 1, completed.stdout + completed.stderr
            refusal = json.loads(completed.stdout.strip().splitlines()[-1])
            assert refusal["ok"] is False and refusal["reason"] == "db_schema_newer", refusal
            assert foreign_db.read_bytes() == before, "a refused preflight must not touch the database file"

            health = _health(endpoint, token)
            assert health["stage"] == "serving" and health["fenced"] is False
            status, turned = _api(endpoint, token, "POST", "/api/sessions/steady/run", {"prompt": "steady", "max_steps": 4})
            assert status == 200 and turned["status"] == "completed", turned
        finally:
            supervisor.stop()


def test_crash_after_adoption_commit_recovers_the_candidate_image(tmp_path: Path) -> None:
    """Adoption committed but no supervisor journal update: recovery reads the revision pin alone."""
    with _provider([_turn(output="steady"), _turn(output="after")]) as server:
        workspace, state = tmp_path / "workspace", tmp_path / "state"
        source = write_reload_plugin(tmp_path / "plugin", process_tool=True)
        _configure(workspace, server, source)
        supervisor = _Supervisor(workspace, state)
        try:
            endpoint, token = supervisor.endpoint, supervisor.token
            old_image = _health(endpoint, token)["image_digest"]
            status, _ = _api(endpoint, token, "POST", "/api/sessions", {"session_id": "crash"})
            assert status == 201
            status, _ = _api(endpoint, token, "POST", "/api/sessions/crash/run", {"prompt": "steady", "max_steps": 4})
            assert status == 200
        finally:
            supervisor.stop()

        from voidcode.core.images import IMAGES_DIR_NAME, materialize, resolve_package_root

        candidate = _candidate_installation(tmp_path, prompt_limit=4 * 1024)
        # Faithful crash shape: the supervisor's pin step materialized the candidate before the
        # adoption committed; only the journal update is missing, never the image itself.
        materialized = materialize(resolve_package_root(candidate), state / IMAGES_DIR_NAME)
        assert (state / IMAGES_DIR_NAME / materialized.digest / "root").is_dir()
        env = dict(__import__("os").environ)
        env["PYTHONPATH"] = str(candidate / "src")
        script = (
            "from pathlib import Path; from voidcode.core.bootstrap import adopt_generation; import json; "
            f"print(json.dumps(adopt_generation(workspace=Path({str(workspace)!r}), "
            f"state_dir=Path({str(state)!r}), session_id='crash')))"
        )
        completed = subprocess.run(
            [sys.executable, "-P", "-c", script], capture_output=True, text=True, timeout=300, check=False, env=env, cwd=tmp_path
        )
        assert completed.returncode == 0, completed.stderr
        report = json.loads(completed.stdout.strip().splitlines()[-1])
        assert report["to_revision_id"]
        upgrades = state / "supervisor" / "upgrades"
        assert not upgrades.is_dir() or list(upgrades.iterdir()) == []

        supervisor2 = _Supervisor(workspace, state)
        try:
            health = _health(supervisor2.endpoint, supervisor2.token)
            assert health["stage"] == "serving" and health["image_digest"] != old_image
            database = state / "kernel-v1.sqlite3"
            with sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True) as raw:
                payload = json.loads(
                    raw.execute("SELECT payload_json FROM revisions WHERE revision_id = ?", (report["to_revision_id"],)).fetchone()[0]
                )
            assert payload["image"]["digest"] == health["image_digest"]
            status, shown = _api(supervisor2.endpoint, supervisor2.token, "GET", "/api/sessions/crash/show")
            assert status == 200 and shown["status"] == "completed"
            status, turned = _api(supervisor2.endpoint, supervisor2.token, "POST", "/api/sessions/crash/run", {"prompt": "after", "max_steps": 4})
            assert status == 200 and turned["status"] == "completed", turned
            assert len(server.requests) == 2
        finally:
            supervisor2.stop()
