"""Extracted CoreTuiApp behavior: session actions.

Methods are applied to the app through RuntimeActions mixins; the facade stays in
terminal_app.py and owns composition, not these responsibilities.
"""

from __future__ import annotations

import logging
import queue
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING

from voidcode.sdk.client import (
    CommandCatalog,
    SessionRow,
)
from voidcode.sdk.commands import CommandDescriptor
from voidcode.sdk.views import (
    ChoiceOption,
    ChoicesViewData,
    ConfirmationViewData,
    FormViewData,
    MarkdownViewData,
    PluginViewDescriptor,
    ViewsSnapshot,
    is_read_only_load,
    parse_view_data,
)
from voidcode.tui.gates import (
    GateState,
)

from . import catalog, events, operations
from .artifact_viewer import (
    InteractiveArtifactViewer,
)
from .formatting import (
    safe as _safe,
)
from .formatting import (
    slash_public_path as _slash_public_path,
)
from .views import (
    InteractiveChoices,
    InteractiveConfirmation,
    InteractiveForm,
    InteractiveMarkdown,
    render_plugin_view,
)

logger = logging.getLogger(__name__)
if TYPE_CHECKING:
    from .terminal_app import CoreTuiApp


class SessionActions:
    def _switch_session(self: CoreTuiApp, session_id: str) -> None:
        """Rebind the follower to another session.

        Each session keeps its own committed cursor, so A -> B -> A replays
        nothing that was already settled in A; in-flight results from the
        previous session are dropped by the generation/session guard.
        """
        if isinstance(self.active_overlay, InteractiveArtifactViewer):
            self.terminal.commit_rows([self.styles.fg("warning", f"Artifact viewer closed on session switch: {self.active_overlay.source.uri}")])
        self._artifact_ack_deadline = None
        self._follower.new_epoch()
        self._completion.reset_session()
        while True:
            try:
                self.event_queue.get_nowait()
            except queue.Empty:
                break
        self.session_id = session_id
        self._close_live_input()
        self._overlays.reset_session()
        self.composer.load_history(())
        self._pending_images.clear()
        self._attachment_path = None
        self._gates = GateState()
        self._active_tools.clear()
        self._queued_followups = ()
        self._followup_withdrawing = False
        self._live_views = ()
        self._live_views_inflight = False
        self._live_views_refresh_at = 0.0
        self._commands_in_flight = 0
        self._clear_stream()
        self.latest_input_tokens = None
        self.cumulative_input_tokens = 0
        self.cumulative_output_tokens = 0
        self._latest_tool_artifacts = None
        # A new stream is about to start: its first frame reconciles.
        self._stream_ready = False
        self.status_line = ""
        self._maybe_welcome()
        self._start_event_stream()

    def _show_sessions(self: CoreTuiApp) -> None:
        """List sessions off the UI thread; stale results never replace a newer binding."""
        session_id, generation = self.session_id, self._generation
        self.set_loading_status("Loading sessions...")
        client = self.client

        self._work.submit(
            "CoreSessions",
            lambda: self.event_queue.put(operations.list_sessions(client, session_id=session_id, generation=generation)),
        )

    def _present_sessions(self: CoreTuiApp, sessions: list[SessionRow]) -> None:
        if not sessions:
            self.terminal.commit_rows([self.styles.fg("warning", "No active sessions.")])
            return
        options = tuple(
            ChoiceOption(
                id=row.session_id,
                label=(
                    f"{row.session_id[:8]} · turn {row.turn} · {row.summary or row.status}"
                    + (" [current]" if row.session_id == self.session_id else "")
                ),
            )
            for row in sessions
        )
        data = ChoicesViewData(options=options, action="tui.switch_session", argument_name="session_id")
        overlay = InteractiveChoices(data, revision_id=catalog.displayed_revision(self), title="Active Sessions")
        for idx, row in enumerate(sessions):
            if row.session_id == self.session_id:
                overlay.selected_index = idx
                break
        self._overlays.show(overlay)

    def _create_session(self: CoreTuiApp) -> None:
        """Create a session off the UI thread, then switch only if still on the same binding."""
        session_id, generation = self.session_id, self._generation
        self.status_line = "Creating session..."
        client = self.client

        self._work.submit(
            "CoreCreateSession",
            lambda: self.event_queue.put(operations.create_session(client, session_id=session_id, generation=generation)),
        )

    def _show_views(self: CoreTuiApp, name_filter: str = "") -> None:
        """Read plugin views off the UI thread; only explicit requests present them.

        Passive views are committed inline. Interactive choices/forms/view-
        confirmations are snapshots, not pending durable requests, so this is
        the one path allowed to mount one; true approval and confirmation gates
        are restored separately from their durable events.
        """
        self._follower.abort_interaction()
        session_id, generation = self.session_id, self._generation
        self.set_loading_status("Loading views...")
        client = self.client

        self._work.submit(
            "CoreViews",
            lambda: self.event_queue.put(operations.load_views(client, session_id=session_id, generation=generation, name_filter=name_filter)),
        )

    def _present_views(self: CoreTuiApp, snapshot: ViewsSnapshot, name_filter: str) -> None:
        views_revision = snapshot.revision_id
        descriptors: list[PluginViewDescriptor] = list(snapshot.views)
        if name_filter:
            wanted = name_filter.strip().lower()
            descriptors = [v for v in descriptors if wanted in v.name.lower() or wanted in v.title.lower()]
        if not descriptors:
            self.terminal.commit_rows([self.styles.fg("warning", "No plugin views available for this session.")])
            return
        for v in descriptors:
            if self._mount_interactive_view(v, views_revision):
                return
            # Non-interactive view: render rows and commit
            rows = render_plugin_view(v, self.terminal.width, self.styles)
            self.terminal.commit_rows(rows)

    def _mount_interactive_view(self: CoreTuiApp, view: PluginViewDescriptor, revision: str) -> bool:
        """Mount an explicitly requested view with its captured revision."""
        data = parse_view_data(view.view_type, view.data)
        if isinstance(data, MarkdownViewData):
            if not data.actions:
                return False
            self._overlays.show(InteractiveMarkdown(data, revision_id=revision, title=view.title))
        elif isinstance(data, ChoicesViewData):
            self._overlays.show(InteractiveChoices(data, revision_id=revision, title=view.title))
        elif isinstance(data, FormViewData):
            form = InteractiveForm(data, action_schema=self._action_schema(data.action), revision_id=revision, title=view.title)
            self._maybe_load_form_data(form)
            self._overlays.show(form)
        elif isinstance(data, ConfirmationViewData):
            self._overlays.show(InteractiveConfirmation(data, revision_id=revision))
        else:
            return False
        return True

    def _maybe_load_form_data(self: CoreTuiApp, form: InteractiveForm) -> None:
        """Run the declared load_action off the UI thread, exactly when it is read-only."""
        if not form.load_action:
            return
        load_action = str(form.load_action)
        commands = catalog.get(self, retry=True)
        if not self.catalog_revision:
            catalog.unavailable(self)
            form.load_error = f"Catalog unavailable: {_safe(self._catalog_error or 'unknown error')} (retry)"
            return
        catalog_snapshot = CommandCatalog(revision_id=self.catalog_revision, commands=commands)
        if not is_read_only_load(form.view, catalog_snapshot):
            form.load_error = f"load_action '{load_action}' must declare exactly the read effect"
            return

        session_id, generation = self.session_id, self._generation
        target = form

        def worker() -> None:
            data: Mapping[str, object] | None = None
            error: str | None = None
            try:
                res = self.client.invoke_command(
                    session_id,
                    load_action,
                    expected_revision=target.revision_id,
                )
                if isinstance(res.data, Mapping):
                    data = res.data
                elif res.error:
                    error = str(res.error)
            except Exception as exc:
                error = str(exc)
            self.event_queue.put(events.FormLoadEvent(form=target, data=data, error=error, session_id=session_id, generation=generation))

        self._work.submit("CoreFormLoad", worker)

    def _prepare_command_input_async(self: CoreTuiApp, descriptor: CommandDescriptor, *, arguments: Mapping[str, object] | None = None) -> None:
        """Read optional plugin input off the UI thread without invoking its action."""
        token = self._follower.begin_interaction()
        session_id, generation, revision = self.session_id, self._generation, self.catalog_revision
        owned_arguments = dict(arguments or {})
        # Hidden actions (Alt+A monitor) show their human description, never a
        # raw /dotted id a user could mistake for a slash command.
        shown = _safe(descriptor.description) if not descriptor.slash_visible else "/" + _slash_public_path(descriptor)
        self.set_loading_status(f"Loading {shown}...")

        self._work.submit(
            "CoreCommandInput",
            lambda: self.event_queue.put(
                operations.command_input(
                    self.client,
                    owner=descriptor.owner,
                    name=descriptor.name,
                    revision=revision,
                    arguments=owned_arguments,
                    token=token,
                    session_id=session_id,
                    generation=generation,
                )
            ),
        )

    def _refresh_live_input_async(self: CoreTuiApp) -> None:
        """Re-read the open live view's source once; stale replies never render."""
        live = self._live_input
        if live is None:
            return
        token = self._follower.begin_live()
        session_id, generation = self.session_id, self._generation

        self._work.submit(
            "CoreLiveInput",
            lambda: self.event_queue.put(
                operations.live_input(
                    self.client,
                    action=live.name,
                    revision=live.revision,
                    arguments=dict(live.arguments),
                    token=token,
                    session_id=session_id,
                    generation=generation,
                )
            ),
        )

    def _arm_live_input(
        self: CoreTuiApp,
        *,
        name: str,
        arguments: Mapping[str, object],
        revision: str,
        refresh_after_ms: int | None,
        source: events.InteractiveOverlay | None,
    ) -> None:
        """Enable polling only from explicit refresh metadata on the open view."""
        if refresh_after_ms is None:
            self._live_input = None
            self._live_refresh_at = 0.0
            return
        self._live_input = events.LiveInput(name=name, arguments=arguments, revision=revision, source=source, refresh_after_ms=refresh_after_ms)
        self._live_refresh_at = time.monotonic() + refresh_after_ms / 1000.0

    def _mount_live_view(
        self: CoreTuiApp, view: PluginViewDescriptor, revision: str, *, name: str, arguments: Mapping[str, object], refresh_after_ms: int | None
    ) -> bool:
        """Mount one live view and arm its refresh; empty choices stay open."""
        if not self._mount_interactive_view(view, revision):
            return False
        self._overlays.clear_suspended()
        self._arm_live_input(name=name, arguments=arguments, revision=revision, refresh_after_ms=refresh_after_ms, source=self.active_overlay)
        return True
