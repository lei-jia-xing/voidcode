import importlib
import tomllib
from types import ModuleType

from .._paths import REPO_ROOT


def test_import_voidcode_exposes_version() -> None:
    voidcode = importlib.import_module("voidcode")
    pyproject_data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    expected_version = pyproject_data["project"]["version"]

    assert isinstance(voidcode, ModuleType)
    version = getattr(voidcode, "__version__", None)

    assert version == expected_version
