"""Extracted CoreTuiApp behavior: input actions.

Methods are applied to the app through RuntimeActions mixins; the facade stays in
terminal_app.py and owns composition, not these responsibilities.
"""

from __future__ import annotations

import logging
import shlex
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from voidcode.sdk.client import (
    MAX_PROMPT_BYTES,
    MAX_PROMPT_CHARS,
)
from voidcode.sdk.provider import ImageAttachment

from . import catalog, events
from .attachments import (
    MAX_PENDING_IMAGES,
    AttachmentError,
    PendingImage,
    format_bytes,
    load_image_file,
)
from .clipboard_image import ClipboardImageUnavailable, read_clipboard_image
from .composer import SHELL_ESCAPE_PREFIX, ComposerAction, ComposerOutcome
from .formatting import (
    normalize_chord as _normalize_chord,
)
from .formatting import (
    safe as _safe,
)
from .formatting import (
    slash_display as _slash_display,
)
from .formatting import (
    slash_public_path as _slash_public_path,
)
from .help_view import TUI_CONTROLS
from .keys import Key
from .rendering import QUIT_ARM_SECONDS
from .term import copy_to_clipboard

logger = logging.getLogger(__name__)
if TYPE_CHECKING:
    from .terminal_app import CoreTuiApp


#: A lone ESC byte is ambiguous until no further input follows.
_ESCAPE_FLUSH_SECONDS = 0.05
#: One typed image path is bounded like any other client-side text buffer.
_MAX_ATTACHMENT_PATH_CHARS = 4096


class InputActions:
    def _handle_input_bytes(self: CoreTuiApp, chunk: bytes, *, arrived_at: float | None = None) -> bool:
        """Decode bytes and dispatch keys; empty input only flushes a due escape. Returns True if any key dispatched."""
        now = time.monotonic()
        stamp = arrived_at if arrived_at is not None else now
        handled = False
        if chunk and self._escape_flush_at and stamp >= self._escape_flush_at:
            # Backlogged chunk past the window: resolve the pending escape first
            # so it can never merge into unrelated later bytes.
            self._escape_flush_at = 0.0
            flushed = self.key_decoder.flush()
            handled = bool(flushed)
            if self._dispatch_keys(flushed):
                return True
            now = time.monotonic()
        if chunk:
            # A lone ESC is ambiguous until no further bytes follow: the standard
            # disambiguation window resolves it as Esc.
            self._escape_flush_at = stamp + _ESCAPE_FLUSH_SECONDS
            keys = list(self.key_decoder.feed(chunk))
        elif self._escape_flush_at and now >= self._escape_flush_at:
            self._escape_flush_at = 0.0
            keys = self.key_decoder.flush()
        else:
            keys = []
        if self._dispatch_keys(keys):
            return True
        return handled or bool(keys)

    def _dispatch_keys(self: CoreTuiApp, keys: list[Key]) -> bool:
        """Dispatch decoded keys; returns True when quitting (caller must stop)."""
        for key in keys:
            if key.name == "escape" and (self.pending_approval is not None or self.active_overlay is not None):
                # The decoder names the raw byte "escape"; the overlays speak
                # "esc". The composer keeps the decoder's own name.
                key = Key("esc")
            if key.name == "ctrl+c":
                if self._attachment_path is not None:
                    self._attachment_path = None
                    self.status_line = "Image attach cancelled"
                    continue
                now = time.monotonic()
                if now - self._quit_armed_at < QUIT_ARM_SECONDS:
                    self.running = False
                    return True
                self._quit_armed_at = now
                self.composer.clear()
                continue
            self._quit_armed_at = float("-inf")
            if (
                key.name == "ctrl+d"
                and not self.composer.value
                and self._attachment_path is None
                and not len(self._pending_images)
                and self.pending_approval is None
                and self.active_overlay is None
            ):
                self.running = False
                return True
            if self.pending_approval is not None:
                self._handle_approval_key(key)
            elif self.active_overlay is not None:
                self._handle_overlay_key(key)
            else:
                self._handle_composer_key(key)
        return False

    def _handle_composer_key(self: CoreTuiApp, key: Key) -> None:
        self.clear_loading_status()
        self._follower.abort_interaction()
        name = key.name
        if self._attachment_path is not None:
            self._handle_attachment_path_key(key)
            return
        chord = _normalize_chord(name)
        if chord in self._shortcut_bindings:
            # A catalog chord: never through the composer, never as slash text.
            self._dispatch_shortcut(self._shortcut_bindings[chord])
            return
        if chord in self._shortcut_conflicts:
            claimants = ", ".join(f"/{_slash_display(entry)}" for entry in self._shortcut_conflicts[chord])
            self.terminal.commit_rows([self.styles.fg("warning", f"Shortcut {name} is claimed by {claimants}; no action bound")])
            return
        if chord == "alt+i":
            self._begin_attachment_path()
            return
        if chord == "alt+v":
            self._attach_clipboard_image()
            return
        if chord == "alt+r":
            self._remove_last_image()
            return
        if not self.catalog_revision and "+" in chord and chord not in ("ctrl+o", "alt+up") and not self.composer.owns_key(chord):
            # A modified chord the composer does not own can only be a catalog
            # shortcut; with no catalog it must say so instead of doing nothing.
            catalog.get(self, retry=True)
            return
        if chord == "ctrl+o":
            self._show_truncated_output()
            return
        if chord == "alt+up":
            self._withdraw_last_followup()
            return
        if name == "enter" and not self.composer.completion:
            line = self.composer.value.strip()
            if len(self._pending_images) and not line:
                self.status_line = "Attached images are not sent alone — write a prompt; images kept"
                return
            if line and not line.startswith("/"):
                nchars = len(line)
                nbytes = len(line.encode("utf-8"))
                if nchars > MAX_PROMPT_CHARS or nbytes > MAX_PROMPT_BYTES:
                    self.status_line = (
                        f"Prompt too large ({nchars} chars, {nbytes} bytes; "
                        f"max {MAX_PROMPT_CHARS} chars, {MAX_PROMPT_BYTES} bytes) — draft kept, nothing sent"
                    )
                    return
        if name in ("enter", "tab") and self.composer.completion:
            selected = self.composer.completion[self.composer.completion_index]
            replacement = self._completion.path_replacement(selected)
            if replacement is not None:
                self.composer.accept_completion_with(replacement)
                self._sync_slash_completion()
                return
            paths = [f"/{_slash_public_path(entry)}" for entry in self.catalog_cache if entry.slash_visible]
            paths.extend(f"/tui {control.name}" for control in TUI_CONTROLS)
            is_group = selected not in paths
            if is_group or (name == "tab" and any(path.startswith(selected + " ") for path in paths)):
                self.composer.accept_completion_with(selected + " ")
                self._sync_slash_completion()
                return
        if name == "enter" and self.composer.completion and self.composer.value == self.composer.completion[self.composer.completion_index]:
            self.composer.set_completion(())
        elif name in ("enter", "tab", "up", "down", "escape") and (self.composer.completion or name in ("tab", "up", "down", "escape")):
            was_open = bool(self.composer.completion)
            outcome = self.composer.handle_key(key)
            if name not in ("up", "down") or not self.composer.completion:
                self._sync_slash_completion(dismissed=(was_open and name in ("enter", "tab")) or name == "escape")
            return self._outcome(outcome)
        outcome = self.composer.handle_key(key)
        self._sync_slash_completion()
        return self._outcome(outcome)

    def _outcome(self: CoreTuiApp, outcome: ComposerOutcome) -> None:
        if outcome.action == ComposerAction.CANCEL_TURN:
            self._cancel_active_run()
        elif outcome.action == ComposerAction.INTERRUPT:
            self.running = False
        elif outcome.action == ComposerAction.COPY and outcome.text is not None:
            try:
                copy_to_clipboard(outcome.text)
            except ValueError as exc:
                self.status_line = f"Copy failed: {exc} — draft kept"
            except OSError as exc:
                self.status_line = f"Copy failed: {exc}"
            else:
                nbytes = len(outcome.text.encode("utf-8"))
                self.status_line = f"Copied {nbytes} bytes to clipboard"
        elif outcome.action == ComposerAction.SUBMIT and outcome.text is not None:
            raw_line = outcome.text
            line = raw_line.strip()
            if not line:
                return
            if line.startswith("/"):
                if self._dispatch_slash_command(line) is False:
                    self.composer.set_text(outcome.text)
                return
            catalog.get(self, retry=True)
            if not self.catalog_revision:
                catalog.unavailable(self)
                self.composer.set_text(outcome.text)
                return
            client_syntax = self._client_syntax_command(raw_line)
            if client_syntax is not None:
                descriptor, argument = client_syntax
                self._dispatch_catalog_command(descriptor, argument)
                return
            if raw_line.startswith(SHELL_ESCAPE_PREFIX):
                self.composer.set_text(outcome.text)
                self.terminal.commit_rows(
                    [
                        self.styles.fg(
                            "error",
                            f"Shell escape `{SHELL_ESCAPE_PREFIX}` is unavailable: the user_shell plugin is not enabled",
                        )
                    ]
                )
                return
            images = self._pending_images.take()
            if images:
                self._dispatch_user_prompt(line, images=images)
            else:
                self._dispatch_user_prompt(line)

    def _begin_attachment_path(self: CoreTuiApp) -> None:
        """Open the one-line path prompt without touching the composer draft or caret."""
        if len(self._pending_images) >= MAX_PENDING_IMAGES:
            self._commit_attachment_failure(f"At most {MAX_PENDING_IMAGES} images can be attached to one prompt; remove one first")
            return
        self.composer.set_completion(())
        self.composer.completion_descriptions = {}
        self._completion.cancel()
        self._attachment_path = ""
        self.status_line = "Image path: type a path, Enter to attach, Esc to cancel — draft kept"

    def _handle_attachment_path_key(self: CoreTuiApp, key: Key) -> None:
        """Consume keys for the path prompt; the normal composer never sees them."""
        assert self._attachment_path is not None
        name = key.name
        if name in ("esc", "escape"):
            self._attachment_path = None
            self.status_line = "Image attach cancelled"
            return
        if name == "enter":
            path_text = self._attachment_path.strip()
            self._attachment_path = None
            if not path_text:
                self.status_line = "Image attach cancelled: no path entered"
                return
            self._attach_image_paths(self._attachment_paths(path_text))
            return
        if name == "backspace":
            self._attachment_path = self._attachment_path[:-1]
            return
        if name == "delete":
            return
        if name == "ctrl+u":
            self._attachment_path = ""
            return
        if name == "paste":
            self._append_attachment_path(key.text)
            return
        if key.text and not name.startswith(("alt+", "ctrl+")):
            self._append_attachment_path(key.text)

    def _append_attachment_path(self: CoreTuiApp, text: str) -> None:
        assert self._attachment_path is not None
        clean = text.replace("\x00", "").replace("\r", " ").replace("\n", " ")
        if not clean:
            return
        if len(self._attachment_path) + len(clean) > _MAX_ATTACHMENT_PATH_CHARS:
            self.status_line = f"Image path is too long (max {_MAX_ATTACHMENT_PATH_CHARS} characters)"
            return
        self._attachment_path += clean

    def _attachment_paths(self: CoreTuiApp, raw: str) -> tuple[str, ...]:
        """One existing path with spaces wins; otherwise honour shell-style quoting."""
        text = raw.strip()
        if not text:
            return ()
        try:
            if Path(text).expanduser().exists():
                return (text,)
        except OSError:
            pass
        try:
            paths = tuple(shlex.split(text))
        except ValueError:
            return (text,)
        return paths or (text,)

    def _attach_image_paths(self: CoreTuiApp, paths: Sequence[str]) -> None:
        """Validate and attach explicitly named paths; every refusal is visible."""
        attached = 0
        for path_text in paths:
            try:
                image = load_image_file(path_text)
            except AttachmentError as exc:
                self._commit_attachment_failure(f"Image attach failed: {exc}")
                continue
            if self._accept_pending_image(image):
                attached += 1
        if attached:
            label = f"Attached {image.label}" if attached == 1 else f"Attached {attached} images"
            self._announce_pending_images(label)

    def _attach_clipboard_image(self: CoreTuiApp) -> None:
        """Read one image off the desktop clipboard without blocking the UI thread."""
        if len(self._pending_images) >= MAX_PENDING_IMAGES:
            self._commit_attachment_failure(f"At most {MAX_PENDING_IMAGES} images can be attached to one prompt; remove one first")
            return
        self.status_line = "Reading image from clipboard…"

        def worker() -> None:
            try:
                image = read_clipboard_image()
            except ClipboardImageUnavailable as exc:
                self.event_queue.put(events.ClipboardImageEvent(error=f"Clipboard image unavailable: {exc}"))
            except AttachmentError as exc:
                self.event_queue.put(events.ClipboardImageEvent(error=f"Clipboard image rejected: {exc}"))
            except Exception as exc:  # noqa: BLE001 - the UI must surface every clipboard failure
                self.event_queue.put(events.ClipboardImageEvent(error=f"Clipboard image unavailable: {exc}"))
            else:
                self.event_queue.put(events.ClipboardImageEvent(image=image))

        self._work.submit("ClipboardImage", worker)

    def _accept_pending_image(self: CoreTuiApp, image: PendingImage) -> bool:
        try:
            self._pending_images.add(image)
        except AttachmentError as exc:
            self._commit_attachment_failure(f"Image attachment rejected: {exc}")
            return False
        return True

    def _announce_pending_images(self: CoreTuiApp, prefix: str) -> None:
        total = format_bytes(self._pending_images.total_bytes)
        self.status_line = f"{prefix} · {len(self._pending_images)}/{MAX_PENDING_IMAGES} images · {total} · Alt+R removes the last"

    def _restore_attachments(self: CoreTuiApp, attachments: Sequence[ImageAttachment]) -> None:
        """Put rejected/failed prompt images back once, without overwriting newer ones."""
        for attachment in attachments:
            try:
                self._pending_images.add(PendingImage.from_attachment(attachment))
            except AttachmentError as exc:
                self._commit_attachment_failure(f"Image attachment not restored: {exc}")

    def _remove_last_image(self: CoreTuiApp) -> None:
        try:
            removed = self._pending_images.remove_last()
        except AttachmentError as exc:
            self._commit_attachment_failure(str(exc))
            return
        remaining = len(self._pending_images)
        self.status_line = f"Removed {removed.label} · {remaining}/{MAX_PENDING_IMAGES} images attached"

    def _detach_images(self: CoreTuiApp, argument: str) -> None:
        text = argument.strip().lower()
        if not len(self._pending_images):
            self.status_line = "No pending images to remove"
            return
        if text in ("", "last"):
            self._remove_last_image()
            return
        if text == "all":
            removed = len(self._pending_images)
            self._pending_images.clear()
            self.status_line = f"Removed {removed} pending image(s)"
            return
        if text.isdigit() and int(text) > 0:
            try:
                removed = self._pending_images.remove_at(int(text) - 1)
            except AttachmentError as exc:
                self._commit_attachment_failure(str(exc))
                return
            remaining = len(self._pending_images)
            self.status_line = f"Removed {removed.label} · {remaining}/{MAX_PENDING_IMAGES} images attached"
            return
        self.status_line = "Usage: /tui detach [index|all]"

    def _commit_attachment_failure(self: CoreTuiApp, message: str, *, tone: str = "error") -> None:
        safe_message = _safe(message)
        self.terminal.commit_rows([self.styles.fg(tone, safe_message)])
        self.status_line = safe_message
