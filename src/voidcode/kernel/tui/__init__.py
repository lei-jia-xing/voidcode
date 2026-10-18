"""Generic HTTP-backed TUI and client for the VoidCode kernel."""

from .client import KernelClientError, KernelHttpClient
from .terminal_app import KernelTuiApp, run_tui
from .views import (
    InteractiveChoices,
    InteractiveConfirmation,
    InteractiveForm,
    UnsupportedViewError,
    render_plugin_view,
)

__all__ = [
    "InteractiveChoices",
    "InteractiveConfirmation",
    "InteractiveForm",
    "KernelClientError",
    "KernelHttpClient",
    "KernelTuiApp",
    "UnsupportedViewError",
    "render_plugin_view",
    "run_tui",
]
