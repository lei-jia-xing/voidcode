"""Slash-command and ``@path`` completion controller.

The controller owns every popup/caret decision and the single in-flight
workspace path query.  ``CoreTuiApp`` supplies a snapshot of the displayed
catalog plus an ``ensure_catalog`` callback, so the controller never reaches
into app fields and the async/stale/draft/caret contract stays in one place.
"""

from __future__ import annotations

import queue
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from voidcode.capabilities.files.path_queries import PathCompletionRequest, PathCompletionResult
from voidcode.sdk.client import SessionClient
from voidcode.sdk.command_help import command_argument_hints
from voidcode.sdk.commands import CommandDescriptor

from . import events, operations
from .composer import Composer
from .formatting import NoticeTone, notice_row, slash_public_path
from .help_view import TUI_CONTROLS
from .styles import Styles
from .work import WorkGroup

#: `@path` popup bounds. Normal completion stays prefix/depth-1; an explicit
#: `**` path segment requests recursive traversal up to this depth.
_PATH_COMPLETION_LIMIT = 50
_PATH_RECURSIVE_DEPTH = 6
_PATH_RECURSIVE_SEGMENT = "**"

#: Failure prefix per typed transport failure; the message is appended verbatim.
_FAILURE_PREFIXES: Mapping[str, str] = {
    "unavailable": "@path completion unavailable",
    "contract": "@path completion contract failure",
    "error": "@path completion failed",
}


@dataclass(frozen=True, slots=True)
class CompletionCatalog:
    """The currently displayed command catalog snapshot."""

    revision: str
    commands: tuple[CommandDescriptor, ...]


@dataclass(frozen=True, slots=True)
class _PathCompletionRound:
    """One requested query round; the newest serial owns the popup."""

    request_id: int
    request: PathCompletionRequest
    target_range: tuple[int, int]
    draft: str
    cursor: int
    token: events.AbortToken
    session_id: str
    generation: int


def recursive_path_query(query: str) -> tuple[str, str] | None:
    """Split an explicit ``DIR/**`` or ``DIR/**/FRAGMENT`` token.

    A bare ``**`` recurses from the workspace root. Only one trailing marker
    segment is recognized; anything else stays a literal prefix query, so the
    normal ``@path`` behavior is never reinterpreted.
    """
    marker = _PATH_RECURSIVE_SEGMENT
    if query == marker:
        base, fragment = "", ""
    elif query.startswith(f"{marker}/"):
        base, fragment = "", query[len(marker) + 1 :]
    elif f"/{marker}/" in query:
        base, fragment = query.split(f"/{marker}/", 1)
    elif query.endswith(f"/{marker}"):
        base, fragment = query[: -len(marker) - 1], ""
    else:
        return None
    if "/" in fragment:
        return None
    return base.strip("/"), fragment


class CompletionController:
    """Popup state for slash names, built-in ``/tui`` controls, and ``@path`` queries."""

    def __init__(
        self,
        *,
        composer: Composer,
        styles: Styles,
        work: WorkGroup,
        event_queue: queue.Queue[events.TuiFrame],
        client: SessionClient,
    ) -> None:
        self._composer = composer
        self._styles = styles
        self._work = work
        self._event_queue = event_queue
        self._client = client
        self._serial = 0
        self._active: int | None = None
        self._pending: _PathCompletionRound | None = None
        self._token: events.AbortToken | None = None
        self._failure_reported: str | None = None
        self._replacements: dict[str, str] = {}

    def path_replacement(self, candidate: str) -> str | None:
        """The accepted-text replacement for one popup candidate, if it is a path."""
        return self._replacements.get(candidate)

    def cancel(self) -> None:
        """Supersede every pending/active path round; their replies become stale."""
        self._serial += 1
        pending = self._pending
        self._pending = None
        if pending is not None:
            pending.token.abort()
        if self._token is not None:
            self._token.abort()
            self._token = None

    def reset_session(self) -> None:
        """Drop the in-flight query and failure dedup when the session changes."""
        self.cancel()
        self._active = None
        self._failure_reported = None

    def sync(
        self,
        *,
        dismissed: bool = False,
        catalog: CompletionCatalog,
        ensure_catalog: Callable[[], CompletionCatalog],
        session_id: str,
        generation: int,
    ) -> None:
        """Refresh the slash-name popup or queue the workspace path query; preserve selection."""
        if dismissed:
            self.cancel()
            self._close_slash()
            return
        path_token = self._path_completion_token()
        if path_token is not None:
            start, end, query = path_token
            self._request_path_completion(query, (start, end), session_id=session_id, generation=generation)
            return
        self.cancel()
        slash_token = self._slash_token()
        if slash_token is None:
            self._close_slash()
            return
        slash_idx, token_end, prefix = slash_token
        if not catalog.revision:
            # Completion is the first visible hint that commands are unavailable;
            # report once (no forced retry) and keep the typed draft untouched.
            catalog = ensure_catalog()
        candidates = self._slash_candidates(catalog.commands, prefix)
        if not candidates:
            self._close_slash()
            return
        previous = self._composer.completion
        index = self._composer.completion_index
        selected = previous[index] if previous and 0 <= index < len(previous) else None
        self._composer.set_completion(
            candidates,
            index=candidates.index(selected) if selected in candidates else 0,
            target_range=(slash_idx, token_end),
        )
        self._replacements = {}
        descriptions, groups = self._slash_descriptions(catalog.commands, candidates)
        self._composer.completion_descriptions = descriptions
        self._composer.completion_groups = groups

    def apply(self, frame: events.PathCompletionEvent, rows: list[str]) -> None:
        """Apply only the newest reply for the unchanged draft; never clear the draft."""
        if frame.request_id == self._active:
            self._active = None
            self._token = None
        stale = (
            frame.token.aborted or frame.request_id != self._serial or frame.draft != self._composer.value or frame.cursor != self._composer.cursor
        )
        if not stale:
            if frame.failure is not None or frame.result is None:
                message = frame.error or "workspace path query failed"
                prefix = _FAILURE_PREFIXES[frame.failure or "error"]
                if self._failure_reported != f"{prefix}: {message}":
                    self._failure_reported = f"{prefix}: {message}"
                    rows.append(notice_row(f"{prefix}: {message}", "warning" if frame.failure == "unavailable" else "error", self._styles))
                self._close_slash()
            else:
                self._failure_reported = None
                self._render_path_completion(frame.result, frame.target_range)
        self._start_pending()

    @staticmethod
    def path_request(query: str) -> PathCompletionRequest:
        """Translate the draft token into the typed query, including recursion."""
        recursive = recursive_path_query(query)
        if recursive is None:
            return PathCompletionRequest(query=query, limit=_PATH_COMPLETION_LIMIT)
        base, fragment = recursive
        if fragment:
            joined = f"{base}/{fragment}" if base else fragment
        else:
            joined = f"{base}/" if base else ""
        return PathCompletionRequest(query=joined, depth=_PATH_RECURSIVE_DEPTH, limit=_PATH_COMPLETION_LIMIT)

    def _path_completion_token(self) -> tuple[int, int, str] | None:
        """Locate the path token under the caret; never a slash-command token."""
        text = self._composer.value
        cursor = self._composer.cursor

        at_idx = text[:cursor].rfind("@")
        if at_idx >= 0 and (at_idx == 0 or text[at_idx - 1] in " \t\n([{\"':"):
            token_mid = text[at_idx:cursor]
            if not any(c in " \t\n" for c in token_mid):
                token_end = cursor
                while token_end < len(text) and text[token_end] not in " \t\n)]}\"':":
                    token_end += 1
                return at_idx, token_end, text[at_idx + 1 : cursor]

        slash_idx = text[:cursor].rfind("/")
        if slash_idx > 0 and text[slash_idx - 1] not in " \t\n":
            path_start = cursor
            while path_start > 0 and text[path_start - 1] not in " \t\n([{\"':":
                path_start -= 1
            path_token = text[path_start:cursor]
            if "/" in path_token and "\n" not in path_token:
                token_end = cursor
                while token_end < len(text) and text[token_end] not in " \t\n)]}\"':":
                    token_end += 1
                return path_start, token_end, path_token
        return None

    def _slash_token(self) -> tuple[int, int, str] | None:
        """The slash-name token at the caret plus its replace range."""
        text = self._composer.value
        cursor = self._composer.cursor
        slash_idx = text[:cursor].rfind("/")
        if slash_idx < 0:
            return None
        if slash_idx > 0 and text[slash_idx - 1] not in " \t\n":
            return None
        prefix = text[slash_idx + 1 : cursor]
        if "\n" in prefix:
            return None
        token_end = cursor
        while token_end < len(text) and (text[token_end].isalnum() or text[token_end] in "_-"):
            token_end += 1
        return slash_idx, token_end, prefix

    @staticmethod
    def _slash_candidates(commands: tuple[CommandDescriptor, ...], prefix: str) -> list[str]:
        """Slash-command names matching the draft token after ``/``."""
        names = [slash_public_path(entry) for entry in commands if entry.slash_visible]
        names.extend(f"tui {control.name}" for control in TUI_CONTROLS)
        parent, _, partial = prefix.rpartition(" ")
        base = f"{parent} " if parent else ""
        candidates = {base + name[len(base) :].split(" ", 1)[0] for name in names if name.startswith(base) and name[len(base) :].startswith(partial)}
        return sorted(f"/{name}" for name in candidates if name and name != parent)

    @staticmethod
    def _slash_descriptions(
        commands: tuple[CommandDescriptor, ...],
        candidates: list[str],
    ) -> tuple[dict[str, str], set[str]]:
        """Hints for visible commands plus one summary per namespace root."""
        visible_paths = [f"/{slash_public_path(entry)}" for entry in commands if entry.slash_visible]
        descriptions: dict[str, str] = {}
        for entry in commands:
            if not entry.slash_visible:
                continue
            path = f"/{slash_public_path(entry)}"
            hints = " ".join(command_argument_hints(entry))
            descriptions[path] = f"{hints} — {entry.description}" if hints else entry.description
        groups = {candidate for candidate in candidates if candidate == "/tui" or any(path.startswith(candidate + " ") for path in visible_paths)}
        for candidate in groups:
            if candidate in descriptions:
                continue
            children = sorted({path[len(candidate) + 1 :].split(" ", 1)[0] for path in visible_paths if path.startswith(candidate + " ")})
            if children:
                descriptions[candidate] = f"Subcommands: {', '.join(children)}"
        return descriptions, groups

    def _request_path_completion(self, query: str, target_range: tuple[int, int], *, session_id: str, generation: int) -> None:
        """Queue the newest workspace query without touching the draft or caret."""
        self._serial += 1
        request_id = self._serial
        if self._pending is not None:
            self._pending.token.abort()
        if self._token is not None:
            self._token.abort()
        try:
            request = self.path_request(query)
        except ValueError as exc:
            # The query string itself is user input; an overlong token simply
            # never completes, and the failure is reported once, not per key.
            message = f"@path completion unavailable: {exc}"
            self._pending = None
            if self._failure_reported != message:
                self._failure_reported = message
                self._notice(message, "warning", session_id=session_id, generation=generation)
            self._close_slash()
            return
        self._pending = _PathCompletionRound(
            request_id=request_id,
            request=request,
            target_range=target_range,
            draft=self._composer.value,
            cursor=self._composer.cursor,
            token=events.AbortToken(),
            session_id=session_id,
            generation=generation,
        )
        # A new prefix already invalidates the old popup; the reply owns the view.
        self._close_slash()
        self._start_pending()

    def _start_pending(self) -> None:
        if self._active is not None:
            return
        round_ = self._pending
        if round_ is None:
            return
        self._pending = None
        self._active = round_.request_id
        self._token = round_.token
        self._work.submit(
            "PathCompletion",
            lambda: self._event_queue.put(
                operations.complete_paths(
                    self._client,
                    session_id=round_.session_id,
                    generation=round_.generation,
                    request_id=round_.request_id,
                    request=round_.request,
                    token=round_.token,
                    target_range=round_.target_range,
                    draft=round_.draft,
                    cursor=round_.cursor,
                )
            ),
        )

    def _render_path_completion(self, result: PathCompletionResult, target_range: tuple[int, int]) -> None:
        previous = self._composer.completion
        index = self._composer.completion_index
        selected = previous[index] if previous and 0 <= index < len(previous) else None
        candidates = [entry.path for entry in result.entries]
        descriptions: dict[str, str] = {entry.path: entry.kind for entry in result.entries}
        replacements: dict[str, str] = {entry.path: entry.path if entry.kind == "directory" else f"{entry.path} " for entry in result.entries}
        self._composer.completion_descriptions = descriptions
        self._composer.completion_groups = set()
        self._composer.set_completion(
            candidates,
            index=candidates.index(selected) if selected in candidates else 0,
            target_range=target_range,
        )
        self._replacements = replacements if candidates else {}

    def _close_slash(self) -> None:
        self._composer.set_completion(())
        self._composer.completion_descriptions = {}
        self._replacements = {}

    def _notice(self, text: str, tone: NoticeTone, *, session_id: str, generation: int) -> None:
        self._event_queue.put(events.NoticeEvent(text=text, tone=tone, session_id=session_id, generation=generation))


__all__ = ["CompletionCatalog", "CompletionController", "recursive_path_query"]
