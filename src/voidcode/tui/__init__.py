"""Generic HTTP-backed TUI and client for the VoidCode core."""

from .client import CoreClientError, CoreHttpClient
from .local_client import CoreLocalClient
from .terminal_app import CoreTuiApp, run_tui
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
    "CoreClientError",
    "CoreHttpClient",
    "CoreLocalClient",
    "CoreTuiApp",
    "UnsupportedViewError",
    "render_plugin_view",
    "run_tui",
]
