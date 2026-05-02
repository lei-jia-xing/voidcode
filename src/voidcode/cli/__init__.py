from __future__ import annotations

import sys
from types import ModuleType

from . import app as _app
from .app import (
    ProviderReadinessResult,
    VoidCodeRuntime,
    build_parser,
    load_runtime_config,
    main,
    print,
    root_cli,
    serve,
    web,
)

__all__ = [
    "ProviderReadinessResult",
    "VoidCodeRuntime",
    "build_parser",
    "load_runtime_config",
    "main",
    "print",
    "root_cli",
    "serve",
    "web",
]


class _CliModule(ModuleType):
    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if hasattr(_app, name):
            setattr(_app, name, value)


sys.modules[__name__].__class__ = _CliModule
