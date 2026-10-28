"""Generic RPC-backed TUI and client for the VoidCode core."""

from .client import CoreRpcClient
from .local_client import CoreLocalClient
from .terminal_app import CoreTuiApp, run_embedded_tui, run_tui
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
    "CoreRpcClient",
    "CoreLocalClient",
    "CoreTuiApp",
    "UnsupportedViewError",
    "render_plugin_view",
    "run_embedded_tui",
    "run_tui",
]
