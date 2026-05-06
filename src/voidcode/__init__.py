from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("voidcode")
except PackageNotFoundError:  # pragma: no cover - editable install fallback
    __version__ = "0.0.0+unknown"
