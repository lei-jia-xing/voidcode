"""Catalog lifecycle for the TUI: acquire, cache, surface failures, recover.

State lives on ``CoreTuiApp``; these functions own the one cache/retry policy.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from voidcode.sdk.commands import CommandDescriptor

from .formatting import safe, shortcut_bindings

if TYPE_CHECKING:
    from .terminal_app import CoreTuiApp

logger = logging.getLogger(__name__)


def cache(app: CoreTuiApp, commands: tuple[CommandDescriptor, ...], revision: str) -> None:
    """Store one catalog revision and rebuild its cached shortcut bindings."""
    app.catalog_cache = commands
    app.catalog_revision = revision
    app._catalog_error = None
    app._shortcut_bindings, app._shortcut_conflicts = shortcut_bindings(commands)
    if app.composer.completion:
        app._sync_slash_completion()


def unavailable(app: CoreTuiApp, detail: str | None = None) -> None:
    """One visible, retryable status for every catalog-dependent action."""
    if detail is not None:
        app._catalog_error = detail
    if app._catalog_error is None:
        app._catalog_error = "unknown error"
    app.status_line = f"Catalog unavailable: {safe(app._catalog_error)} (retry)"


def load(app: CoreTuiApp) -> bool:
    """Fetch the command catalog; failures are visible, never raised into the loop."""
    try:
        catalog = app.client.command_catalog()
    except Exception as exc:
        logger.warning("command catalog unavailable", exc_info=True)
        unavailable(app, safe(exc) or type(exc).__name__)
        return False
    cache(app, catalog.commands, catalog.revision_id)
    return True


def get(app: CoreTuiApp, *, retry: bool = False) -> tuple[CommandDescriptor, ...]:
    """Cached catalog, or one visible failure when none can be acquired.

    Automatic UI paths pass ``retry=False`` so a failed load is reported
    without hammering the server on every keystroke; explicit actions pass
    ``retry=True`` because the user asked for this action now.
    """
    if app.catalog_revision:
        return app.catalog_cache
    if app._catalog_error is not None and not retry:
        return ()
    load(app)
    return app.catalog_cache


def displayed_revision(app: CoreTuiApp) -> str:
    """Revision rendered overlays capture; catalog failure is visible, not blank-silent."""
    get(app)
    if not app.catalog_revision:
        unavailable(app)
    return app.catalog_revision
