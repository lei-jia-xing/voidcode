import os
import tempfile

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("PYTHONUTF8", "1")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="voidcode-pytest-config-")
