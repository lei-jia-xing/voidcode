"""Runtime path resolution following the XDG Base Directory specification.

VoidCode separates four kinds of filesystem state:

- **Runtime state** (mutable, must persist across restarts, not user-edited):
  the SQLite session database lives here. Resolved via ``$XDG_STATE_HOME``.
- **Cache** (regenerable, safe to discard): provider model catalog and other
  derived data. Resolved via ``$XDG_CACHE_HOME``.
- **User data** (persistent user assets, e.g. exported bundles): resolved via
  ``$XDG_DATA_HOME``. Currently unused by the runtime; reserved for future use.
- **User configuration** (hand-editable global config): resolved via
  ``$XDG_CONFIG_HOME``. Owned by ``runtime/config.py``.

Workspace-local artifacts (``.voidcode.json`` config files,
``.voidcode/agents/skills/tools/commands/`` directories) remain per-project
and are intentionally outside this module.

A single explicit override exists for the SQLite database path:
``$VOIDCODE_DB_PATH`` takes precedence over all XDG resolution. This is the
canonical way to point a runtime at a non-default database (testing,
multi-tenant deployment, custom installation).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

VOIDCODE_DIR_NAME = "voidcode"
SESSIONS_DB_FILENAME = "sessions.sqlite3"
PROVIDER_CATALOG_FILENAME = "provider-model-catalog.json"

DB_PATH_ENV = "VOIDCODE_DB_PATH"


def _envmap(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _running_on_windows() -> bool:
    return os.name == "nt"


def _xdg_dir(env: Mapping[str, str], var: str, posix_fallback: Path) -> Path:
    value = env.get(var)
    if value:
        return Path(value).expanduser() / VOIDCODE_DIR_NAME
    return posix_fallback / VOIDCODE_DIR_NAME


def state_home(env: Mapping[str, str] | None = None) -> Path:
    """Return ``<state-root>/voidcode`` for runtime state files (SQLite DB).

    POSIX default: ``~/.local/state/voidcode``.
    Windows default: ``%LOCALAPPDATA%\\voidcode\\state``.
    """
    e = _envmap(env)
    if _running_on_windows():
        local_appdata = e.get("LOCALAPPDATA")
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return base / VOIDCODE_DIR_NAME / "state"
    return _xdg_dir(e, "XDG_STATE_HOME", Path.home() / ".local" / "state")


def cache_home(env: Mapping[str, str] | None = None) -> Path:
    """Return ``<cache-root>/voidcode`` for regenerable cache files.

    POSIX default: ``~/.cache/voidcode``.
    Windows default: ``%LOCALAPPDATA%\\voidcode\\cache``.
    """
    e = _envmap(env)
    if _running_on_windows():
        local_appdata = e.get("LOCALAPPDATA")
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return base / VOIDCODE_DIR_NAME / "cache"
    return _xdg_dir(e, "XDG_CACHE_HOME", Path.home() / ".cache")


def data_home(env: Mapping[str, str] | None = None) -> Path:
    """Return ``<data-root>/voidcode`` for persistent user data.

    POSIX default: ``~/.local/share/voidcode``.
    Windows default: ``%APPDATA%\\voidcode``.
    """
    e = _envmap(env)
    if _running_on_windows():
        appdata = e.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / VOIDCODE_DIR_NAME
    return _xdg_dir(e, "XDG_DATA_HOME", Path.home() / ".local" / "share")


def sessions_db_path(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the canonical SQLite session database path.

    Resolution order:

    1. ``$VOIDCODE_DB_PATH`` (explicit override; absolute path)
    2. ``state_home()/sessions.sqlite3`` (XDG default)
    """
    e = _envmap(env)
    override = e.get(DB_PATH_ENV)
    if override:
        return Path(override).expanduser()
    return state_home(e) / SESSIONS_DB_FILENAME


def provider_catalog_cache_path(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the provider model catalog cache file path."""
    return cache_home(env) / PROVIDER_CATALOG_FILENAME


__all__ = [
    "DB_PATH_ENV",
    "PROVIDER_CATALOG_FILENAME",
    "SESSIONS_DB_FILENAME",
    "VOIDCODE_DIR_NAME",
    "cache_home",
    "data_home",
    "provider_catalog_cache_path",
    "sessions_db_path",
    "state_home",
]
