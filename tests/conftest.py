from __future__ import annotations

import inspect
import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("PYTHONUTF8", "1")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="voidcode-pytest-config-")


@pytest.fixture(autouse=True)
def _isolated_xdg_runtime_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-test isolation for XDG state/cache/data directories.

    The runtime SQLite database (XDG_STATE_HOME), provider catalog cache
    (XDG_CACHE_HOME), and exported user data (XDG_DATA_HOME) all default to
    user-global locations. Without per-test override, tests would share these
    files and corrupt each other. XDG_CONFIG_HOME is intentionally session-
    scoped at module import time because runtime config loaders cache it.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / ".xdg-state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / ".xdg-cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ".xdg-data"))


_TEST_ROOT = Path(__file__).resolve().parent

_SLOW_TEST_FILES = {
    Path("unit/runtime/test_http_question_payload_fuzz.py"),
    Path("unit/runtime/test_runtime_service_extensions.py"),
}


def _relative_test_path(path: Path) -> Path | None:
    try:
        return path.resolve().relative_to(_TEST_ROOT)
    except ValueError:
        return None


def _source_contains(item: pytest.Item, *needles: str) -> bool:
    test_obj = getattr(item, "obj", None)
    if test_obj is None:
        return False
    try:
        source = inspect.getsource(test_obj)
    except (OSError, TypeError):
        return False
    return any(needle in source for needle in needles)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        test_path = _relative_test_path(Path(str(item.path)))
        if test_path is None:
            continue

        if test_path.parts[0] == "integration":
            item.add_marker(pytest.mark.integration)

        if test_path.parts[:3] == ("unit", "tools", "fuzz") or "fuzz" in test_path.name:
            item.add_marker(pytest.mark.fuzz)
            item.add_marker(pytest.mark.slow)

        if test_path in _SLOW_TEST_FILES:
            item.add_marker(pytest.mark.slow)

        if test_path == Path("unit/interface/test_cli_smoke.py") and _source_contains(
            item,
            "_run_module_cli(",
            "subprocess.run(",
        ):
            item.add_marker(pytest.mark.slow)

        if _source_contains(item, "time.sleep("):
            item.add_marker(pytest.mark.slow)
