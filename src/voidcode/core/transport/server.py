"""Serve the core HTTP transport on loopback, supervised or standalone.

A supervised child serves the listener the supervisor owns (passed as an inherited file
descriptor), validates its own generation before constructing a core, stays fenced until the
supervisor promotes it, and exits when its supervisor is gone. A standalone server binds its own
port directly.
"""

from __future__ import annotations

import os
import secrets
import socket
import threading
import time
from pathlib import Path
from typing import Any


def generate_bearer_token() -> str:
    return secrets.token_urlsafe(32)


def serve_core(
    *,
    workspace: Path,
    state_dir: Path | None,
    host: str,
    port: int,
    bearer_token: str | None,
    listen_fd: int | None = None,
    fenced: bool = False,
    control_token: str | None = None,
    generation: int = 1,
    upgrade_id: str | None = None,
    supervisor_pid: int | None = None,
    quiet: bool = False,
) -> None:
    import uvicorn

    from ..images import installed_identity, running_identity
    from .http import create_core_app, create_core_app_with_factory
    from .static import frontend_distribution

    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("core transport binds loopback only")
    token = bearer_token or generate_bearer_token()
    frontend_dist = frontend_distribution()
    identity = running_identity() or installed_identity()
    if listen_fd is None and not fenced:
        app = create_core_app(
            workspace=workspace,
            state_dir=state_dir,
            bearer_token=token,
            frontend_dist=frontend_dist,
        )
    else:
        from ..bootstrap import open_core, state_directory
        from ..engine import Core
        from ..preflight import LAUNCH_PROTOCOL_VERSION, PreflightRefused, verify_preflight
        from ..storage.store import SCHEMA_VERSION

        resolved_state = state_dir if state_dir is not None else state_directory(workspace)

        def child_factory() -> Core | None:
            import json as _json
            import sys as _sys

            try:
                report = verify_preflight(
                    database=resolved_state / "kernel-v1.sqlite3",
                    workspace=str(workspace),
                    state_dir=resolved_state,
                    candidate_package_root=identity.package_root,
                    candidate_launch_protocol=LAUNCH_PROTOCOL_VERSION,
                    image_digest=identity.digest,
                    web_bundle=identity.bundle,
                    target_schema=SCHEMA_VERSION,
                )
            except PreflightRefused as exc:
                if exc.reason in {"db_missing", "no_adopted_revision"}:
                    return open_core(workspace, state_dir=resolved_state)
                print(
                    _json.dumps({"ok": False, "reason": exc.reason, "detail": exc.detail}, ensure_ascii=False),
                    file=_sys.stderr,
                    flush=True,
                )
                raise SystemExit(f"preflight refused: {exc.reason}: {exc.detail}") from None
            if report.adoption_required:
                return None
            return open_core(workspace, state_dir=resolved_state)

        app = create_core_app_with_factory(
            core_factory=child_factory,
            bearer_token=token,
            frontend_dist=frontend_dist,
            fenced=fenced,
            control_token=control_token,
            image=identity,
            generation=generation,
            upgrade_id=upgrade_id,
            workspace=workspace,
            state_dir=resolved_state,
        )
    if not quiet:
        # The credential travels on this explicit startup channel only: stdout at
        # launch, never in events, errors, or workspace files.
        print(f"core bearer token: {token}", flush=True)
        if frontend_dist is not None:
            print(f"web client: http://{host}:{port}/", flush=True)
    if listen_fd is None:
        uvicorn.run(app, host=host, port=port, lifespan="auto", log_level="warning")
        return
    listener = socket.socket(fileno=listen_fd)
    config = uvicorn.Config(app, log_level="warning", timeout_graceful_shutdown=10)
    server = uvicorn.Server(config)
    if supervisor_pid is not None:
        _watch_supervisor(server, expected_pid=supervisor_pid)
    server.run(sockets=[listener])


def _watch_supervisor(server: Any, *, expected_pid: int) -> None:
    """Exit when the supervisor is gone, without signaling any pid.

    A dead supervisor reparents this process, so the watcher compares ``os.getppid()`` against
    the pid it was started with: no ``kill`` call ever runs, which makes PID reuse harmless.
    On mismatch the server is asked to shut down gracefully (lifespan closes the core and
    releases the workspace lease); a bounded fallback exits the process if shutdown hangs.
    """
    if os.getppid() != expected_pid:
        raise SystemExit(f"supervisor process {expected_pid} is already gone")

    def watch() -> None:
        while os.getppid() == expected_pid:
            time.sleep(1.0)
        try:
            server.should_exit = True
        except Exception:
            pass
        time.sleep(25.0)
        os._exit(3)

    thread = threading.Thread(target=watch, name="SupervisorWatcher", daemon=True)
    thread.start()
