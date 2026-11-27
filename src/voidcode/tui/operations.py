"""Async operation bodies for the TUI: one request, one typed response event.

The app owns submission and state; these functions only perform one client or
config operation and build the frame the event pump will apply.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Literal

from voidcode.capabilities.files.path_queries import PATH_COMPLETION, PathCompletionRequest, PathCompletionResult
from voidcode.config import CONFIG_FILE_NAME, ModelField, update_model_section
from voidcode.sdk.atomic_file import replace_file
from voidcode.sdk.client import SessionClient
from voidcode.sdk.services import QueryContractError, QueryUnavailable
from voidcode.sdk.views import PluginViewDescriptor

from .events import (
    AbortToken,
    CommandInputReadyEvent,
    FollowupsLoadedEvent,
    FollowupsLoadFailedEvent,
    FollowupWithdrawFailedEvent,
    FollowupWithdrawnEvent,
    LiveInputReadyEvent,
    LiveViewsFailedEvent,
    LiveViewsLoadedEvent,
    ModelSwitchDoneEvent,
    PathCompletionEvent,
    SessionCreatedEvent,
    SessionCreateFailedEvent,
    SessionsFailedEvent,
    SessionsLoadedEvent,
    ViewsFailedEvent,
    ViewsLoadedEvent,
)


class _ClientQueryTransport:
    def __init__(self, client: SessionClient, session_id: str) -> None:
        self._client = client
        self._session_id = session_id

    def query(self, name: str, arguments: Mapping[str, object] | None = None) -> Mapping[str, object] | None:
        if name != PATH_COMPLETION.name:
            raise QueryContractError(f"path completion transport cannot answer {name!r}")
        return self._client.complete_paths(self._session_id, dict(arguments or {}))


def complete_paths(
    client: SessionClient,
    *,
    session_id: str,
    generation: int,
    request_id: int,
    request: PathCompletionRequest,
    token: AbortToken,
    target_range: tuple[int, int],
    draft: str,
    cursor: int,
) -> PathCompletionEvent:
    """Ask the server-side files query; never fall back to local directory reads."""
    result: PathCompletionResult | None = None
    failure: Literal["unavailable", "contract", "error"] | None = None
    error: str | None = None
    try:
        transport = _ClientQueryTransport(client, session_id)
        result = PATH_COMPLETION.ask(transport, request)
        if not result.entries and request.mode == "prefix" and request.query:
            # Fuzzy is an opt-in rescue, never a replacement: prefix matches
            # always win, and only an empty prefix round searches by subsequence.
            fuzzy = PATH_COMPLETION.ask(transport, replace(request, mode="fuzzy"))
            if fuzzy.entries:
                result = fuzzy
    except QueryUnavailable as exc:
        failure, error = "unavailable", str(exc)
    except QueryContractError as exc:
        failure, error = "contract", str(exc)
    except Exception as exc:
        failure, error = "error", str(exc)
    return PathCompletionEvent(
        token=token,
        request_id=request_id,
        target_range=target_range,
        draft=draft,
        cursor=cursor,
        result=result,
        failure=failure,
        error=error,
        session_id=session_id,
        generation=generation,
    )


def list_sessions(client: SessionClient, *, session_id: str, generation: int) -> SessionsLoadedEvent | SessionsFailedEvent:
    try:
        sessions = tuple(client.list_sessions())
    except Exception as exc:
        return SessionsFailedEvent(error=str(exc), session_id=session_id, generation=generation)
    return SessionsLoadedEvent(sessions=sessions, session_id=session_id, generation=generation)


def create_session(client: SessionClient, *, session_id: str, generation: int) -> SessionCreatedEvent | SessionCreateFailedEvent:
    try:
        created = client.create_session()
    except Exception as exc:
        return SessionCreateFailedEvent(error=str(exc), session_id=session_id, generation=generation)
    return SessionCreatedEvent(new_session_id=created.session_id, session_id=session_id, generation=generation)


def load_views(client: SessionClient, *, session_id: str, generation: int, name_filter: str) -> ViewsLoadedEvent | ViewsFailedEvent:
    try:
        snapshot = client.views(session_id)
    except Exception as exc:
        return ViewsFailedEvent(error=str(exc), session_id=session_id, generation=generation)
    return ViewsLoadedEvent(snapshot=snapshot, name_filter=name_filter, session_id=session_id, generation=generation)


def load_live_views(client: SessionClient, *, session_id: str, generation: int) -> LiveViewsLoadedEvent | LiveViewsFailedEvent:
    try:
        snapshot = client.views(session_id)
    except Exception as exc:
        return LiveViewsFailedEvent(error=str(exc), session_id=session_id, generation=generation)
    return LiveViewsLoadedEvent(revision_id=snapshot.revision_id, views=snapshot.views, session_id=session_id, generation=generation)


def load_followups(client: SessionClient, *, session_id: str, generation: int) -> FollowupsLoadedEvent | FollowupsLoadFailedEvent:
    try:
        receipt = client.list_followups(session_id)
    except Exception as exc:
        return FollowupsLoadFailedEvent(error=str(exc), session_id=session_id, generation=generation)
    return FollowupsLoadedEvent(followups=receipt.followups, session_id=session_id, generation=generation)


def withdraw_followup(
    client: SessionClient,
    *,
    session_id: str,
    continuation_id: str,
    expected_revision: str,
    generation: int,
) -> FollowupWithdrawnEvent | FollowupWithdrawFailedEvent:
    try:
        receipt = client.withdraw_followup(session_id, continuation_id, expected_revision=expected_revision)
    except Exception as exc:
        return FollowupWithdrawFailedEvent(error=str(exc), session_id=session_id, generation=generation)
    return FollowupWithdrawnEvent(receipt=receipt, session_id=session_id, generation=generation)


def _rollback_model_config(config_path: Path, previous_bytes: bytes | None, written_bytes: bytes | None) -> str | None:
    """Restore ``voidcode.toml`` after a rejected ``core.reload``, without clobbering concurrent writes."""
    if written_bytes is None:
        return None
    try:
        if previous_bytes is None:
            if config_path.is_file() and not config_path.is_symlink() and config_path.read_bytes() == written_bytes:
                config_path.unlink(missing_ok=True)
            return None
        replace_file(config_path, previous_bytes, expected_content=written_bytes)
        return None
    except OSError as exc:
        return f" (config rollback failed: {exc})"


def switch_model(
    client: SessionClient,
    *,
    workspace: Path,
    field: ModelField,
    value: str | int,
    revision: str,
    session_id: str,
    generation: int,
) -> ModelSwitchDoneEvent:
    config_path = workspace / CONFIG_FILE_NAME
    if config_path.is_symlink():
        return ModelSwitchDoneEvent(
            ok=False, stage="config", error=f"not a regular file: {CONFIG_FILE_NAME}", session_id=session_id, generation=generation
        )
    previous_bytes = config_path.read_bytes() if config_path.is_file() else None
    try:
        update_model_section(workspace, field, value)
        written_bytes = config_path.read_bytes() if config_path.is_file() and not config_path.is_symlink() else None
    except Exception as exc:
        return ModelSwitchDoneEvent(ok=False, stage="config", error=str(exc), session_id=session_id, generation=generation)
    try:
        result = client.invoke_command(session_id, "core.reload", {}, expected_revision=revision)
    except Exception as exc:
        rollback_note = _rollback_model_config(config_path, previous_bytes, written_bytes) or ""
        return ModelSwitchDoneEvent(
            ok=False, stage="reload", error=f"Reload failed: {exc}{rollback_note}", session_id=session_id, generation=generation
        )
    if result.status == "ok":
        data = result.data if isinstance(result.data, Mapping) else {}
        to_revision = str(data.get("to_revision_id", "")) or revision
        return ModelSwitchDoneEvent(ok=True, revision=to_revision, session_id=session_id, generation=generation)
    rollback_note = _rollback_model_config(config_path, previous_bytes, written_bytes) or ""
    return ModelSwitchDoneEvent(
        ok=False,
        stage="reload",
        error=f"Reload {result.status}: {result.error}{rollback_note}",
        session_id=session_id,
        generation=generation,
    )


def command_input(
    client: SessionClient,
    *,
    owner: str,
    name: str,
    revision: str,
    arguments: Mapping[str, object],
    token: AbortToken,
    session_id: str,
    generation: int,
) -> CommandInputReadyEvent:
    view: PluginViewDescriptor | None = None
    error: str | None = None
    refresh_after_ms: int | None = None
    try:
        snapshot = client.command_input(session_id, name, revision, arguments=dict(arguments))
        if snapshot.revision_id != revision:
            raise ValueError("command input revision changed; refresh the catalog")
        presentation = snapshot.presentation
        view = PluginViewDescriptor(owner=owner, name=name, title=presentation.title, view_type=presentation.view_type, data=presentation.data)
        refresh_after_ms = presentation.refresh_after_ms
    except Exception as exc:
        error = str(exc)
    return CommandInputReadyEvent(
        token=token,
        revision_id=revision,
        view=view,
        error=error,
        refresh_after_ms=refresh_after_ms,
        action=name,
        arguments=arguments,
        session_id=session_id,
        generation=generation,
    )


def live_input(
    client: SessionClient,
    *,
    action: str,
    revision: str,
    arguments: Mapping[str, object],
    token: AbortToken,
    session_id: str,
    generation: int,
) -> LiveInputReadyEvent:
    view: PluginViewDescriptor | None = None
    error: str | None = None
    refresh_after_ms: int | None = None
    try:
        snapshot = client.command_input(session_id, action, revision, arguments=dict(arguments))
        if snapshot.revision_id != revision:
            raise ValueError("command input revision changed; refresh the catalog")
        presentation = snapshot.presentation
        view = PluginViewDescriptor(owner="live", name=action, title=presentation.title, view_type=presentation.view_type, data=presentation.data)
        refresh_after_ms = presentation.refresh_after_ms
    except Exception as exc:
        error = str(exc)
    return LiveInputReadyEvent(
        token=token,
        revision_id=revision,
        action=action,
        arguments=arguments,
        view=view,
        error=error,
        refresh_after_ms=refresh_after_ms,
        session_id=session_id,
        generation=generation,
    )
