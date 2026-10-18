"""Serve the kernel HTTP transport on loopback with a randomized bearer token."""

from __future__ import annotations

import secrets
from pathlib import Path


def generate_bearer_token() -> str:
    return secrets.token_urlsafe(32)


def serve_kernel(*, workspace: Path, state_dir: Path | None, trusted_plugins: frozenset[str], host: str, port: int, bearer_token: str | None) -> None:
    import uvicorn

    from .http import create_kernel_app

    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("kernel transport binds loopback only")
    token = bearer_token or generate_bearer_token()
    app = create_kernel_app(workspace=workspace, state_dir=state_dir, trusted_plugins=trusted_plugins, bearer_token=token)
    # The credential travels on this explicit startup channel only: stdout at
    # launch, never in events, errors, or workspace files.
    print(f"kernel bearer token: {token}", flush=True)
    uvicorn.run(app, host=host, port=port, lifespan="auto", log_level="warning")
