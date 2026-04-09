import importlib
import sys
from pathlib import Path
from types import ModuleType

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))


def test_import_voidcode_exposes_version() -> None:
    voidcode = importlib.import_module("voidcode")

    assert isinstance(voidcode, ModuleType)
    version = getattr(voidcode, "__version__", None)

    assert version == "0.1.0"
