"""Supervised kernel runtime: one stable listener and one elected image at a time.

The supervisor owns the loopback listener and both credentials, launches the elected image as a
fenced child, and drives the only supported transition: preflight, drain, stop, start, adopt,
promote. Durable authority stays in SQLite — the revision's ``image`` pin decides which bytes run —
so ``supervisor/state.json`` and ``supervisor/upgrades/*.json`` are observability only and never a
fallback election.

The CLI reaches this process over ``<state_dir>/supervisor.sock`` (``0600``, one newline-delimited
JSON frame per connection). That boundary is *same-OS-user*: it stops a client bearer token, a
remote HTTP caller, or a browser from driving an upgrade, not a trusted process running as the same
user.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from collections.abc import Mapping
from pathlib import Path

from .images import (
    IMAGES_DIR_NAME,
    Image,
    ensure_running_image,
    image_for_digest,
    launch_argv,
    launch_environment,
    materialize,
    validate_candidate,
)
from .storage.store import SCHEMA_VERSION, ForeignDatabaseError, inspect_existing_workspace, open_read_only

__all__ = ["run_supervisor"]

#: The CLI socket next to the state directory's database.
SOCKET_NAME = "supervisor.sock"
#: Observability journal directory (never an election authority).
SUPERVISOR_DIR_NAME = "supervisor"
UPGRADES_DIR_NAME = "upgrades"
#: The one kernel database file name; the store owns its schema, this module only locates it.
DATABASE_NAME = "kernel-v1.sqlite3"

#: One frame is one small JSON object; anything larger is refused instead of buffered.
MAX_FRAME_BYTES = 64 * 1024
#: Readiness polling: bounded by the caller's deadline, and it observes process exit every tick.
POLL_SECONDS = 0.05
#: How long a child gets to release the workspace after SIGTERM before SIGKILL.
STOP_GRACE_SECONDS = 15.0
DEFAULT_DRAIN_SECONDS = 120.0
DEFAULT_HEALTH_SECONDS = 90.0

#: Loopback only: the supervisor never binds a routable address.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})

#: Proxy-free opener: a loopback upgrade must never be routed through an environment proxy.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class SupervisorError(RuntimeError):
    """A named refusal or failure: ``reason`` is the stable machine-readable name."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class _Shutdown(Exception):
    """The supervisor was asked to stop; no new child may be started."""


class _OwnerGone(Exception):
    """The process that owns this supervisor's lifetime exited; stop without spawning anything."""


class _FrameError(Exception):
    """One malformed CLI frame; the reason is the exception argument."""


def run_supervisor(
    *,
    workspace: Path,
    state_dir: Path,
    host: str,
    port: int,
    bearer_token: str | None,
    trusted_plugins: frozenset[str],
    session_id: str | None,
    open_browser: bool,
    owner_pid: int | None = None,
) -> int:
    """Serve one workspace until SIGTERM/SIGINT, supervising the elected image.

    Binds the loopback listener once (``port`` 0 picks an ephemeral one) and keeps it for every
    child, so the endpoint and the API bearer stay stable across an upgrade. ``owner_pid`` names
    the process whose lifetime bounds this supervisor (an embedded TUI): when that process is gone,
    the supervisor stops like a SIGTERM and never starts another child. Standalone use passes
    nothing and is owned by its own terminal.
    """
    try:
        resolved_workspace = Path(workspace).resolve(strict=True)
        resolved_state = Path(state_dir).resolve()
    except OSError as exc:
        print(f"kernel supervisor: workspace: {exc}", file=sys.stderr, flush=True)
        return 1
    supervisor = _Supervisor(
        workspace=resolved_workspace,
        state_dir=resolved_state,
        host=host,
        port=port,
        bearer_token=bearer_token,
        trusted_plugins=trusted_plugins,
        session_id=session_id,
        open_browser=open_browser,
        owner_pid=owner_pid,
    )
    return supervisor.run()


def _new_token() -> str:
    """One unguessable credential that can never be mistaken for a command-line flag.

    ``token_urlsafe`` can begin with ``-`` (about one time in 64); a leading dash is harmless in an
    HTTP header but ambiguous in argv, so it is stripped here as well as bound with ``--flag=value``.
    """
    return secrets.token_urlsafe(32).lstrip("-")


def _endpoint_for(address: str, port: int) -> str:
    """The URI a client dials, bracketed when the bound family is IPv6."""
    return f"http://[{address}]:{port}" if ":" in address else f"http://{address}:{port}"


def _bind_listener(host: str, port: int) -> socket.socket:
    address = "127.0.0.1" if host == "localhost" else host
    if address not in _LOOPBACK_HOSTS:
        raise ValueError(f"kernel supervisor binds loopback only: {host!r}")
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    listener = socket.socket(family, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((address, port))
        # A deep backlog lets connections queue while no child accepts, so a client issued during
        # a swap is answered by the elected child instead of failing at connect.
        listener.listen(128)
    except OSError:
        listener.close()
        raise
    return listener


def _decode(raw: bytes) -> Mapping[str, object]:
    if not raw:
        return {}
    try:
        value = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError, json.JSONDecodeError:
        return {"error": raw.decode("utf-8", "replace")[:2000]}
    return value if isinstance(value, Mapping) else {"value": value}


def _request(
    endpoint: str,
    path: str,
    *,
    token: str,
    body: Mapping[str, object] | None = None,
    timeout: float = 30.0,
) -> tuple[int, Mapping[str, object]]:
    """One HTTP call to the child: ``(status, body)``, with HTTP errors returned, not raised."""
    data = None if body is None else json.dumps(dict(body), sort_keys=True, allow_nan=False).encode("utf-8")
    request = urllib.request.Request(f"{endpoint}{path}", data=data, method="GET" if data is None else "POST")
    request.add_header("authorization", f"Bearer {token}")
    if data is not None:
        request.add_header("content-type", "application/json")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return int(response.status), _decode(response.read())
    except urllib.error.HTTPError as exc:
        return int(exc.code), _decode(exc.read())


def _last_json_object(raw: str) -> Mapping[str, object] | None:
    for line in reversed([item for item in raw.splitlines() if item.strip()]):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping):
            return value
    return None


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(dict(payload), indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _bounded_seconds(value: object, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return min(max(float(value), 0.0), 3600.0)


def _error_frame(reason: str, detail: str) -> dict[str, object]:
    """A synchronous request error: no upgrade id, matching the CLI's error envelope."""
    return {"status": "error", "reason": reason, "detail": detail}


def _frame(result: Mapping[str, object], status: str) -> dict[str, object]:
    """Project one outcome onto its exact contracted shape: one shape per status, no variants."""
    common = {
        "status": status,
        "upgrade_id": result["upgrade_id"],
        "from_image": result["from_image"],
        "to_image": result["to_image"],
        "record": result["record"],
    }
    if status == "upgraded":
        return {
            **common,
            "from_revision_id": result["from_revision_id"],
            "to_revision_id": result["to_revision_id"],
            "readoption": result["readoption"],
        }
    if status == "refused":
        return {
            **common,
            "reason": result["reason"],
            "detail": result["detail"],
            "from_revision_id": result["from_revision_id"],
            "to_revision_id": None,
            "readoption": result["readoption"],
        }
    if status == "blocked":
        return {
            **common,
            "reason": result["reason"],
            "blockers": result.get("blockers", []),
            "from_revision_id": result["from_revision_id"],
            "to_revision_id": None,
            "readoption": False,
        }
    if status == "rolled_back":
        return {
            **common,
            "reason": result["reason"],
            "detail": result["detail"],
            "from_revision_id": result["from_revision_id"],
            "to_revision_id": None,
            "readoption": False,
        }
    return {
        **common,
        "reason": result["reason"],
        "detail": result["detail"],
    }


def _read_frame(connection: socket.socket) -> Mapping[str, object]:
    buffer = b""
    while b"\n" not in buffer:
        chunk = connection.recv(4096)
        if not chunk:
            if not buffer.strip():
                raise _FrameError("empty_request")
            break
        buffer += chunk
        if len(buffer) > MAX_FRAME_BYTES:
            raise _FrameError("frame_too_large")
    line = buffer.split(b"\n", 1)[0]
    try:
        value = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _FrameError("invalid_json") from exc
    if not isinstance(value, dict):
        raise _FrameError("invalid_request")
    return value


def _send_frame(connection: socket.socket, payload: Mapping[str, object]) -> None:
    connection.sendall(json.dumps(dict(payload), sort_keys=True, allow_nan=False).encode("utf-8") + b"\n")


class _Supervisor:
    def __init__(
        self,
        *,
        workspace: Path,
        state_dir: Path,
        host: str,
        port: int,
        bearer_token: str | None,
        trusted_plugins: frozenset[str],
        session_id: str | None,
        open_browser: bool,
        owner_pid: int | None = None,
    ) -> None:
        self._workspace = workspace
        self._state_dir = state_dir
        self._host = host
        self._port = port
        #: The process that owns this supervisor's lifetime (an embedded TUI), when one named it.
        self._owner_pid = owner_pid
        self._api_token = bearer_token or _new_token()
        #: Control authority for /api/kernel/*: generated per lifetime, never printed, never stored.
        self._control_token = _new_token()
        self._trusted = tuple(sorted(trusted_plugins))
        self._session_id = session_id
        self._open_browser = open_browser
        self._images_dir = state_dir / IMAGES_DIR_NAME
        self._supervisor_dir = state_dir / SUPERVISOR_DIR_NAME
        self._socket_path = state_dir / SOCKET_NAME
        self._database = state_dir / DATABASE_NAME
        self._lock = threading.Lock()
        self._transition = threading.Lock()
        #: Guards every spawn/reap of the child so a stop cannot race a start into an orphan.
        self._child_lock = threading.Lock()
        self._stop = threading.Event()
        self._listener: socket.socket | None = None
        self._unix: socket.socket | None = None
        #: Only the process that bound the CLI socket may remove it on the way out.
        self._owns_socket = False
        self._child: subprocess.Popen[bytes] | None = None
        self._endpoint = ""
        self._image: Image | None = None
        self._generation = 0
        self._snapshot: dict[str, object] = {
            "phase": "starting",
            "endpoint": "",
            "pid": os.getpid(),
            "active_image": None,
            "generation": 0,
            "active_upgrade": None,
            "stage": "starting",
        }

    # -- lifecycle ---------------------------------------------------------------------------

    def run(self) -> int:
        try:
            self._prepare()
        except _OwnerGone:
            # The owner was already gone before anything was bound or spawned.
            print("kernel supervisor: owner process is already gone", file=sys.stderr, flush=True)
            return 1
        except (SupervisorError, OSError, ValueError) as exc:
            self._fail(exc)
            return 1
        previous = {number: signal.signal(number, self._on_signal) for number in (signal.SIGTERM, signal.SIGINT)}
        try:
            self._serve_unix()
            self._announce()
            with self._transition:
                self._start_initial()
            self._announce_ready()
            while not self._stop.wait(0.25):
                if self._owner_gone():
                    # The process that owns this supervisor is gone: stop what runs and exit, so
                    # no child survives to hold the workspace and no fallback is ever spawned.
                    print("kernel supervisor: owner process exited; stopping", file=sys.stderr, flush=True)
                    return 1
                child = self._child
                if child is not None and child.poll() is not None and not self._transition.locked():
                    # A child that died while serving is visible, never silently replaced: the
                    # listener would otherwise accept connections nobody answers.
                    print(
                        f"kernel supervisor: kernel child exited with code {child.returncode}",
                        file=sys.stderr,
                        flush=True,
                    )
                    return 1
            return 0
        except _OwnerGone:
            print("kernel supervisor: owner process exited; stopping", file=sys.stderr, flush=True)
            return 1
        except _Shutdown:
            return 0
        except (SupervisorError, OSError, ValueError) as exc:
            self._fail(exc)
            return 1
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)
            self._shutdown()

    def _on_signal(self, _number: int, _frame: object) -> None:
        self._stop.set()

    def _prepare(self) -> None:
        if self._state_dir.is_relative_to(self._workspace):
            raise SupervisorError("state_dir_in_workspace", f"kernel state must live outside the workspace: {self._state_dir}")
        # An owner that already exited must never leave a supervisor behind holding the workspace.
        if self._owner_gone():
            raise _Shutdown
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._supervisor_dir.mkdir(parents=True, exist_ok=True)
        (self._supervisor_dir / UPGRADES_DIR_NAME).mkdir(parents=True, exist_ok=True)
        if self._socket_path.exists() and _socket_alive(self._socket_path):
            raise SupervisorError("supervisor_running", f"another supervisor already listens on {self._socket_path}")
        self._image = self._startup_image()
        try:
            self._listener = _bind_listener(self._host, self._port)
        except OSError as exc:
            raise SupervisorError("listen_failed", f"cannot bind {self._host}:{self._port}: {exc}") from exc
        address = self._listener.getsockname()
        self._endpoint = _endpoint_for(str(address[0]), int(address[1]))
        self._set_snapshot(endpoint=self._endpoint, active_image=self._image.digest)
        self._write_state("starting")

    def _announce(self) -> None:
        print(f"kernel endpoint: {self._endpoint}", flush=True)
        print(f"kernel bearer token: {self._api_token}", flush=True)
        if self._image is not None and self._image.bundle is not None:
            print(f"web client: {self._endpoint}/", flush=True)

    def _announce_ready(self) -> None:
        print(f"kernel ready: {self._endpoint} generation={self._generation}", flush=True)
        if self._open_browser and self._image is not None and self._image.bundle is not None:
            webbrowser.open(f"{self._endpoint}/")

    def _shutdown(self) -> None:
        self._stop.set()
        unix, self._unix = self._unix, None
        if unix is not None:
            try:
                unix.close()
            except OSError:
                pass
        # Killing the child first is what makes an in-flight drain/health call fail fast instead of
        # holding shutdown for its full timeout. No child can be spawned after the stop event, so
        # the transition thread either observes the stop or is already holding a child we reap here.
        self._stop_child()
        if self._transition.acquire(timeout=STOP_GRACE_SECONDS):
            try:
                self._stop_child()
            finally:
                self._transition.release()
        else:
            self._stop_child()
        try:
            if self._owns_socket:
                self._socket_path.unlink(missing_ok=True)
        except OSError:
            pass
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        self._set_snapshot(phase="stopped", stage="stopped", active_upgrade=None)
        self._write_state("stopped")

    def _fail(self, exc: BaseException) -> None:
        if isinstance(exc, SupervisorError):
            message = f"kernel supervisor: {exc.reason}: {exc.detail}" if exc.detail else f"kernel supervisor: {exc.reason}"
        else:
            message = f"kernel supervisor: {type(exc).__name__}: {exc}"
        print(message, file=sys.stderr, flush=True)

    def _require_running(self) -> None:
        if self._stop.is_set():
            raise _Shutdown
        if self._owner_gone():
            raise _OwnerGone

    def _owner_gone(self) -> bool:
        """Whether the process that owns this supervisor's lifetime has exited.

        The owner is the embedded TUI that spawned us: a hard death of it (SIGKILL, which runs no
        cleanup) would otherwise leave this supervisor and its child holding the workspace lock
        forever. The test is the kernel's own reparenting — once the owner dies this process's
        parent changes — so it needs no ``kill`` and PID reuse cannot fool it. A standalone
        supervisor names no owner and is never stopped this way.
        """
        owner = self._owner_pid
        return owner is not None and os.getppid() != owner

    # -- authority ---------------------------------------------------------------------------

    def _election(self) -> tuple[str | None, str | None, int | None]:
        """The recorded revision, its image pin, and the schema: the canonical election read."""
        if not self._database.is_file():
            return (None, None, None)
        try:
            revision_id, payload, schema = inspect_existing_workspace(self._database, str(self._workspace))
        except (ForeignDatabaseError, sqlite3.Error) as exc:
            raise SupervisorError("state_foreign", str(exc)) from exc
        digest: str | None = None
        if isinstance(payload, Mapping):
            image = payload.get("image")
            if isinstance(image, Mapping) and isinstance(image.get("digest"), str) and image["digest"]:
                digest = image["digest"]
        return (revision_id, digest, schema)

    def _startup_image(self) -> Image:
        """The elected image when state pins one, otherwise a snapshot of this installation."""
        _, digest, _ = self._election()
        if digest is None:
            return ensure_running_image(self._images_dir)
        try:
            return image_for_digest(self._images_dir, digest)
        except (FileNotFoundError, ValueError) as exc:
            raise SupervisorError(
                "elected_image_missing",
                f"the recorded revision is pinned to image {digest}, which is not materialized under "
                f"{self._images_dir}; restore that image (or the state directory) instead of starting "
                f"different bytes: {exc}",
            ) from exc

    def _has_sessions(self) -> bool:
        if not self._database.is_file():
            return False
        try:
            connection = open_read_only(self._database)
        except ForeignDatabaseError:
            return False
        try:
            row = connection.execute("SELECT COUNT(*) AS total FROM sessions WHERE workspace = ?", (str(self._workspace),)).fetchone()
            return bool(row is not None and int(row["total"]) > 0)
        except sqlite3.Error:
            return False
        finally:
            connection.close()

    # -- child lifecycle ---------------------------------------------------------------------

    def _start_child(
        self,
        image: Image,
        *,
        generation: int,
        upgrade_id: str | None,
        session_id: str | None,
    ) -> None:
        with self._child_lock:
            self._require_running()
            if self._child is not None and self._child.poll() is None:
                raise SupervisorError("child_already_running", "a kernel child is already running; stop it first")
            listener = self._listener
            if listener is None:
                raise SupervisorError("listener_missing", "the supervisor has no bound listener")
            descriptor = listener.fileno()
            # Every string value uses the ``--flag=value`` form: a generated token can begin with
            # ``-`` (token_urlsafe does so with p ≈ 1/64), and argparse would then read it as a flag
            # and exit 2 before the child ever starts. ``=`` binds the value regardless of its shape.
            arguments = [
                "serve",
                f"--workspace={self._workspace}",
                f"--state-dir={self._state_dir}",
                f"--listen-fd={descriptor}",
                "--fenced",
                f"--bearer-token={self._api_token}",
                f"--control-token={self._control_token}",
                f"--generation={generation}",
                f"--supervisor-pid={os.getpid()}",
            ]
            if upgrade_id is not None:
                arguments.append(f"--upgrade-id={upgrade_id}")
            if session_id is not None:
                arguments.append(f"--session-id={session_id}")
            for plugin_id in self._trusted:
                arguments.append(f"--trust-plugin={plugin_id}")
            # The working directory is inherited on purpose: a user's relative paths keep working,
            # and ``-P`` plus the image-only PYTHONPATH is what keeps imports pinned. Child stdout
            # is discarded so it can never corrupt the supervisor's exact stdout contract; it logs
            # to stderr, which is inherited.
            child = subprocess.Popen(
                launch_argv(*arguments),
                env=launch_environment(image),
                pass_fds=(descriptor,),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                close_fds=True,
            )
            self._child = child
            self._generation = generation
            self._image = image
            self._set_snapshot(generation=generation, active_image=image.digest, stage="starting")

    def _stop_child(self) -> None:
        with self._child_lock:
            child, self._child = self._child, None
        if child is None:
            return
        if child.poll() is not None:
            child.wait()
            return
        child.terminate()
        try:
            child.wait(timeout=STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            child.kill()
            try:
                child.wait(timeout=STOP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                # An unkillable process cannot be reaped; the next start refuses the workspace
                # rather than pretending the child is gone.
                print("kernel supervisor: child did not exit after SIGKILL", file=sys.stderr, flush=True)

    def _await_health(
        self,
        generation: int,
        *,
        timeout: float,
        require: str | None = None,
    ) -> Mapping[str, object]:
        """Poll the shared listener until the child of ``generation`` reports a ready stage.

        The deadline is wall-clock bounded and every tick observes process exit, so a crashing
        child is a named failure rather than a fixed sleep that later reports a timeout.
        """
        accepted = {require} if require is not None else {"validated", "adopted", "serving"}
        deadline = time.monotonic() + timeout
        while True:
            self._require_running()
            child = self._child
            if child is None:
                raise SupervisorError("child_missing", "no kernel child is running")
            code = child.poll()
            if code is not None:
                raise SupervisorError("child_exited", f"kernel child exited with code {code} before it was ready")
            try:
                status, body = _request(self._endpoint, "/api/health", token=self._api_token, timeout=1.0)
            except urllib.error.URLError, TimeoutError, ConnectionError, OSError:
                status, body = 0, {}
            if status == 200 and str(body.get("generation")) == str(generation):
                stage = str(body.get("stage", ""))
                if stage in accepted:
                    self._set_snapshot(stage=stage)
                    return body
            if time.monotonic() >= deadline:
                wanted = require or "validated"
                raise SupervisorError(
                    "health_timeout",
                    f"kernel child generation {generation} did not report stage {wanted} within {timeout:.0f}s",
                )
            time.sleep(POLL_SECONDS)

    def _post_control(self, path: str, body: Mapping[str, object], *, timeout: float = 60.0) -> tuple[int, Mapping[str, object]]:
        self._require_running()
        try:
            return _request(self._endpoint, path, token=self._control_token, body=body, timeout=timeout)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise SupervisorError("child_unreachable", f"{path} failed: {exc}") from exc

    def _resume_old_image(self, image: Image, *, session_id: str | None) -> bool:
        """Abort an upgrade: release the fence and consume every durable pause exactly once.

        A child of this image that is still up only needs ``admit`` (the same driver a restart
        uses, so a paused intent resumes rather than being replayed); one that is gone is restarted
        fenced and promoted, which is an ordinary restart of the still-elected image.
        """
        child = self._child
        if child is not None and child.poll() is None:
            try:
                status, body = _request(self._endpoint, "/api/health", token=self._api_token, timeout=2.0)
            except urllib.error.URLError, TimeoutError, ConnectionError, OSError:
                status, body = 0, {}
            if status == 200 and body.get("image_digest") == image.digest:
                try:
                    status, _ = self._post_control("/api/kernel/admit", {}, timeout=30.0)
                except SupervisorError:
                    return False
                if status == 200:
                    self._set_snapshot(phase="serving", stage="serving", active_upgrade=None)
                    self._write_state("serving")
                    return True
        try:
            return self._ensure_serving(image, session_id=session_id)
        except Exception:  # noqa: BLE001 - the caller reports the recovery failure by name
            return False

    def _adopt(self, session_id: str | None) -> Mapping[str, object]:
        status, body = self._post_control("/api/kernel/adopt", {"session_id": session_id}, timeout=DEFAULT_DRAIN_SECONDS)
        if status != 200 or body.get("adopted") is not True:
            raise SupervisorError(
                str(body.get("reason") or body.get("code") or "adopt_failed"),
                str(body.get("detail") or body.get("error") or f"HTTP {status}"),
            )
        return body

    def _promote(self) -> None:
        status, body = self._post_control("/api/kernel/promote", {}, timeout=DEFAULT_DRAIN_SECONDS)
        if status != 200 or body.get("serving") is not True:
            raise SupervisorError(
                str(body.get("reason") or body.get("code") or "promote_failed"),
                str(body.get("detail") or body.get("error") or f"HTTP {status}"),
            )

    def _preflight(self, image: Image, *, timeout: float = 300.0) -> Mapping[str, object]:
        """Run the candidate's own read-only preflight under the candidate image."""
        argv = launch_argv("preflight", "--workspace", str(self._workspace), "--state-dir", str(self._state_dir))
        try:
            completed = subprocess.run(
                argv,
                env=launch_environment(image),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SupervisorError("preflight_timeout", f"candidate preflight did not finish within {timeout:.0f}s") from exc
        except OSError as exc:
            raise SupervisorError("preflight_failed", str(exc)) from exc
        payload = _last_json_object(completed.stdout)
        if completed.returncode == 0 and payload is not None and payload.get("ok") is True:
            return payload
        if completed.returncode == 1 and payload is not None and payload.get("ok") is False:
            raise SupervisorError(str(payload.get("reason") or "preflight_refused"), str(payload.get("detail") or ""))
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        raise SupervisorError(
            "preflight_failed",
            detail[-1] if detail else f"candidate preflight exited with code {completed.returncode}",
        )

    # -- startup -----------------------------------------------------------------------------

    def _start_initial(self) -> None:
        image = self._image
        if image is None:
            raise SupervisorError("image_missing", "the supervisor has no image to run")
        adoption_required = self._preflight_adoption(image)
        if adoption_required and self._session_id is None and self._has_sessions():
            raise SupervisorError(
                "session_required",
                "this workspace has sessions, so adopting a new generation needs an explicit session id (`voidcode serve --session-id <id>`)",
            )
        self._start_child(image, generation=1, upgrade_id=None, session_id=self._session_id)
        self._await_health(1, timeout=DEFAULT_HEALTH_SECONDS)
        if adoption_required:
            self._adopt(self._session_id)
        self._promote()
        self._await_health(1, timeout=DEFAULT_HEALTH_SECONDS, require="serving")
        self._set_snapshot(phase="serving", stage="serving")
        self._write_state("serving")

    def _preflight_adoption(self, image: Image) -> bool:
        """Whether this start has to adopt a generation: the candidate's own read-only preflight.

        A workspace with no database at all is the child's normal first-adoption path, and a
        recorded revision that pins this very image at the current schema is a plain restore, so
        neither needs a preflight. Everything else is proven read-only before any downtime.
        """
        if not self._database.is_file():
            return False
        revision_id, digest, schema = self._election()
        if revision_id is not None and digest == image.digest and schema == SCHEMA_VERSION:
            return False
        try:
            report = self._preflight(image)
        except SupervisorError as exc:
            if exc.reason in {"db_missing", "no_adopted_revision"}:
                return False
            raise
        return report.get("adoption_required") is True

    # -- CLI socket --------------------------------------------------------------------------

    def _serve_unix(self) -> None:
        path = self._socket_path
        if path.exists():
            if _socket_alive(path):
                raise SupervisorError("supervisor_running", f"another supervisor already listens on {path}")
            path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(path))
            os.chmod(path, 0o600)
            server.listen(8)
        except OSError as exc:
            server.close()
            raise SupervisorError("socket_bind_failed", f"cannot bind {path}: {exc}") from exc
        self._owns_socket = True
        self._unix = server
        threading.Thread(target=self._accept, name="SupervisorSocket", daemon=True).start()

    def _accept(self) -> None:
        server = self._unix
        if server is None:
            return
        while not self._stop.is_set():
            try:
                connection, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(connection,), name="SupervisorClient", daemon=True).start()

    def _handle(self, connection: socket.socket) -> None:
        with connection:
            try:
                connection.settimeout(30.0)
                try:
                    request = _read_frame(connection)
                except _FrameError as exc:
                    _send_frame(connection, {"status": "error", "reason": str(exc.args[0]), "detail": "malformed supervisor request"})
                    return
                if request.get("op") == "stop":
                    _send_frame(connection, {"status": "stopping"})
                    self._stop.set()
                    return
                _send_frame(connection, self._dispatch(request))
            except OSError:
                return

    def _dispatch(self, request: Mapping[str, object]) -> Mapping[str, object]:
        op = request.get("op")
        if op == "status":
            return {"status": "ok", **self._status()}
        if op == "upgrade":
            return self._upgrade(request)
        return {"status": "error", "reason": "unknown_op", "detail": f"unsupported op {op!r}"}

    def _status(self) -> dict[str, object]:
        with self._lock:
            return {
                "phase": self._snapshot["phase"],
                "endpoint": self._snapshot["endpoint"],
                "pid": self._snapshot["pid"],
                "active_image": self._snapshot["active_image"],
                "generation": self._snapshot["generation"],
                "active_upgrade": self._snapshot["active_upgrade"],
                "stage": self._snapshot["stage"],
            }

    # -- upgrade -----------------------------------------------------------------------------

    def _upgrade(self, request: Mapping[str, object]) -> Mapping[str, object]:
        if not self._transition.acquire(blocking=False):
            return {
                "status": "blocked",
                "upgrade_id": None,
                "from_image": self._image.digest if self._image else None,
                "to_image": None,
                "from_revision_id": None,
                "to_revision_id": None,
                "readoption": False,
                "reason": "transition_in_progress",
                "detail": "another upgrade is already running",
                "record": None,
            }
        try:
            return self._upgrade_locked(request)
        finally:
            self._transition.release()

    def _upgrade_locked(self, request: Mapping[str, object]) -> dict[str, object]:
        upgrade_id = uuid.uuid4().hex
        started_at = int(time.time())
        result: dict[str, object] = {
            "status": "failed",
            "upgrade_id": upgrade_id,
            "from_image": None,
            "to_image": None,
            "from_revision_id": None,
            "to_revision_id": None,
            "readoption": False,
            "reason": "",
            "detail": "",
            "blockers": [],
            "record": None,
        }
        raw_candidate = request.get("candidate")
        session_id = request.get("session_id")
        timeout_seconds = _bounded_seconds(request.get("timeout_seconds"), DEFAULT_DRAIN_SECONDS)
        health_timeout = max(DEFAULT_HEALTH_SECONDS, timeout_seconds)
        if not isinstance(raw_candidate, str) or not raw_candidate:
            return _error_frame("candidate_missing", "candidate must be a path to an installation directory")
        if session_id is not None and (not isinstance(session_id, str) or not session_id):
            return _error_frame("session_invalid", "session_id must be a non-empty string or null")
        active = self._image
        candidate: Image | None = None
        adoption_required = False
        try:
            self._require_running()
            if active is None:
                raise SupervisorError("image_missing", "the supervisor has no image to run")
            from_revision, from_digest, _ = self._election()
            result["from_revision_id"] = from_revision
            result["from_image"] = from_digest or active.digest
            try:
                package_root = validate_candidate(Path(raw_candidate), active_package_root=active.package_root, workspace=self._workspace)
            except (OSError, ValueError) as exc:
                raise SupervisorError("candidate_invalid", str(exc)) from exc
            try:
                candidate = materialize(package_root, self._images_dir)
            except (OSError, ValueError) as exc:
                raise SupervisorError("materialize_failed", str(exc)) from exc
            result["to_image"] = candidate.digest
            if candidate.digest == active.digest:
                raise SupervisorError("candidate_identical", "the candidate has the running image's digest; nothing would change")
            report = self._preflight(candidate)
            adoption_required = report.get("adoption_required") is True
            if adoption_required and session_id is None and self._has_sessions():
                raise SupervisorError(
                    "session_required",
                    "a revision-changing upgrade needs an explicit session id because this workspace has sessions",
                )
        except _Shutdown:
            return self._finish(result, "failed", reason="supervisor_stopping", detail="the supervisor was asked to stop", started_at=started_at)
        except _OwnerGone:
            return self._finish(result, "failed", reason="owner_exited", detail="the owning process exited", started_at=started_at)
        except SupervisorError as exc:
            return self._finish(result, "refused", reason=exc.reason, detail=exc.detail, started_at=started_at)
        except Exception as exc:  # noqa: BLE001 - every preflight failure is a named refusal
            return self._finish(
                result,
                "refused",
                reason="preflight_failed",
                detail=f"{type(exc).__name__}: {exc}",
                started_at=started_at,
            )

        assert candidate is not None  # validated above
        next_generation = self._generation + 1
        upgrade_view = {
            "upgrade_id": upgrade_id,
            "stage": "draining",
            "from_image": result["from_image"],
            "to_image": candidate.digest,
        }
        self._set_snapshot(phase="upgrading", active_upgrade=upgrade_view)
        self._write_state("upgrading")
        try:
            # Two-phase drain: the fence goes up first, but the old child keeps serving (and its
            # follow streams stay open) until the drain actually succeeds. Every failure exit here
            # is before the restart, so the fence must be released and paused intents resumed.
            try:
                drained, body = self._post_control(
                    "/api/kernel/drain",
                    {
                        "timeout_seconds": timeout_seconds,
                        "require_quiescent": adoption_required,
                        "pause_at_batch_boundary": True,
                        "pause_intent": upgrade_id,
                    },
                    timeout=timeout_seconds + 30.0,
                )
            except SupervisorError as exc:
                # A transport failure is indistinguishable from "the fence may be up": treat it as
                # an abort, never as permission to stop the child.
                drained, body = 0, {"error": f"{exc.reason}: {exc.detail}"}
            if drained != 200 or body.get("drained") is not True:
                blocked = drained == 409
                raw_blockers = body.get("blockers")
                blockers = [dict(item) for item in raw_blockers if isinstance(item, Mapping)] if isinstance(raw_blockers, list) else []
                if blocked:
                    detail = str(body.get("detail") or "")
                else:
                    detail = str(body.get("detail") or body.get("error") or f"drain answered HTTP {drained}")
                resumed = self._resume_old_image(active, session_id=session_id)
                if not resumed:
                    detail = f"{detail}; the old image could not be resumed".lstrip("; ")
                result["blockers"] = blockers
                return self._finish(
                    result,
                    "blocked" if blocked and resumed else "failed",
                    reason="busy_work" if blocked else "drain_failed",
                    detail=detail,
                    started_at=started_at,
                )
            self._set_snapshot(active_upgrade={**upgrade_view, "stage": "restarting"})
            self._stop_child()
            self._require_running()
            self._start_child(candidate, generation=next_generation, upgrade_id=upgrade_id, session_id=session_id)
            self._await_health(next_generation, timeout=health_timeout)
            if adoption_required:
                self._set_snapshot(active_upgrade={**upgrade_view, "stage": "adopting"})
                adopted = self._adopt(session_id)
                result["readoption"] = True
                if isinstance(adopted.get("to_revision_id"), str):
                    result["to_revision_id"] = adopted["to_revision_id"]
            self._set_snapshot(active_upgrade={**upgrade_view, "stage": "promoting"})
            self._promote()
            self._await_health(next_generation, timeout=health_timeout, require="serving")
        except _Shutdown:
            return self._finish(result, "failed", reason="supervisor_stopping", detail="the supervisor was asked to stop", started_at=started_at)
        except _OwnerGone:
            # The owner is gone: stop what is running and spawn nothing, not even a rollback.
            return self._finish(result, "failed", reason="owner_exited", detail="the owning process exited", started_at=started_at)
        except Exception as exc:  # noqa: BLE001 - recovery must see every failure
            reason = exc.reason if isinstance(exc, SupervisorError) else type(exc).__name__
            detail = exc.detail if isinstance(exc, SupervisorError) else str(exc)
            try:
                committed = self._committed(candidate)
            except (SupervisorError, _OwnerGone) as read_error:
                if isinstance(read_error, _OwnerGone):
                    return self._finish(result, "failed", reason="owner_exited", detail="the owning process exited", started_at=started_at)
                return self._finish(result, "failed", reason=read_error.reason, detail=read_error.detail, started_at=started_at)
            if committed:
                result["readoption"] = True
            if self._stop.is_set():
                return self._finish(result, "failed", reason="supervisor_stopping", detail=detail, started_at=started_at)
            if self._owner_gone():
                return self._finish(result, "failed", reason="owner_exited", detail="the owning process exited", started_at=started_at)
            target = candidate if committed else active
            try:
                # A committed adoption means the candidate is the reader (its restart path is the
                # only correct one); before that, the old image stays elected and resumes its own
                # durable intents through admit.
                serving = self._ensure_serving(target, session_id=session_id) if committed else self._resume_old_image(target, session_id=session_id)
            except Exception as recovery:  # noqa: BLE001 - report the recovery failure, not a stack
                serving = False
                detail = f"{detail}; recovery failed: {type(recovery).__name__}: {recovery}"
            if not committed:
                status = "rolled_back" if serving else "failed"
            else:
                status = "upgraded" if serving else "failed"
            return self._finish(result, status, reason=reason, detail=detail, started_at=started_at)

        result["status"] = "upgraded"
        self._set_snapshot(phase="serving", stage="serving", active_upgrade=None)
        self._write_state("serving")
        try:
            _, to_revision, _ = self._election()
            if to_revision is not None and result["to_revision_id"] is None:
                result["to_revision_id"] = to_revision
        except SupervisorError:
            pass
        except _OwnerGone:
            return self._finish(result, "failed", reason="owner_exited", detail="the owning process exited", started_at=started_at)
        return self._finish(result, "upgraded", reason="", detail="", started_at=started_at)

    def _committed(self, candidate: Image) -> bool:
        """Whether the adoption transaction committed: the revision pin is the only authority."""
        _, digest, _ = self._election()
        return digest == candidate.digest

    def _ensure_serving(self, image: Image, *, session_id: str | None) -> bool:
        """Make ``image`` the serving child again, restarting it only when it is not already up."""
        child = self._child
        if child is not None and child.poll() is None:
            try:
                status, body = _request(self._endpoint, "/api/health", token=self._api_token, timeout=2.0)
            except urllib.error.URLError, TimeoutError, ConnectionError, OSError:
                status, body = 0, {}
            if status == 200 and body.get("image_digest") == image.digest:
                # The right image is still up: promote is the idempotent election check that also
                # clears the fence, so paused intents resume on this image instead of being replayed.
                try:
                    self._promote()
                    self._await_health(self._generation, timeout=DEFAULT_HEALTH_SECONDS, require="serving")
                except SupervisorError:
                    pass
                else:
                    self._set_snapshot(phase="serving", stage="serving", active_upgrade=None)
                    self._write_state("serving")
                    return True
            self._stop_child()
        generation = self._generation + 1
        self._start_child(image, generation=generation, upgrade_id=None, session_id=session_id)
        self._await_health(generation, timeout=DEFAULT_HEALTH_SECONDS)
        self._promote()
        self._await_health(generation, timeout=DEFAULT_HEALTH_SECONDS, require="serving")
        self._set_snapshot(phase="serving", stage="serving", active_upgrade=None)
        self._write_state("serving")
        return True

    def _finish(
        self,
        result: dict[str, object],
        status: str,
        *,
        reason: str,
        detail: str,
        started_at: int,
    ) -> dict[str, object]:
        """Record one transition outcome and return the exact socket/CLI shape for that status."""
        result["status"] = status
        result["reason"] = reason
        result["detail"] = detail
        record = self._supervisor_dir / UPGRADES_DIR_NAME / f"{result['upgrade_id']}.json"
        try:
            _write_json(
                record,
                {
                    "upgrade_id": result["upgrade_id"],
                    "status": status,
                    "reason": reason,
                    "detail": detail,
                    "blockers": result.get("blockers", []),
                    "from_image": result["from_image"],
                    "to_image": result["to_image"],
                    "from_revision_id": result["from_revision_id"],
                    "to_revision_id": result["to_revision_id"],
                    "readoption": result["readoption"],
                    "started_at": started_at,
                    "finished_at": int(time.time()),
                },
            )
            result["record"] = str(record)
        except OSError as exc:
            # The journal is observability: losing it never changes the transition's outcome.
            print(f"kernel supervisor: cannot write upgrade record: {exc}", file=sys.stderr, flush=True)
        child = self._child
        alive = child is not None and child.poll() is None
        phase = "serving" if alive else "failed"
        self._set_snapshot(phase=phase, stage=phase, active_upgrade=None)
        self._write_state(phase)
        return _frame(result, status)

    # -- observability -----------------------------------------------------------------------

    def _set_snapshot(self, **changes: object) -> None:
        with self._lock:
            self._snapshot.update(changes)

    def _write_state(self, phase: str) -> None:
        image = self._image
        with self._lock:
            active_upgrade = self._snapshot["active_upgrade"]
            generation = self._snapshot["generation"]
        try:
            _write_json(
                self._supervisor_dir / "state.json",
                {
                    "phase": phase,
                    "endpoint": self._endpoint,
                    "pid": os.getpid(),
                    "generation": generation,
                    "active_image": image.digest if image is not None else None,
                    "active_upgrade": active_upgrade,
                    "updated_at": int(time.time()),
                },
            )
        except OSError as exc:
            print(f"kernel supervisor: cannot write supervisor state: {exc}", file=sys.stderr, flush=True)


def _socket_alive(path: Path) -> bool:
    """Whether a live process still owns ``path``: a stale socket file is replaced, a live one kept."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.5)
        probe.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        probe.close()
