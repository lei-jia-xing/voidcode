"""Composer image ingress: path, clipboard, queue, rendering, and limits."""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path
from typing import Any

import pytest

from voidcode.sdk.client import (
    ClientCapabilities,
    CommandCatalog,
    FollowupWithdrawReceipt,
    RunResult,
    RunSpec,
)
from voidcode.sdk.provider import ImageAttachment, ProviderMessage
from voidcode.tui import events, rendering
from voidcode.tui.attachments import (
    MAX_IMAGE_BYTES,
    MAX_PENDING_IMAGES,
    AttachmentError,
    PendingAttachments,
    PendingImage,
)
from voidcode.tui.clipboard_image import ClipboardImageUnavailable, _Backend, read_clipboard_image
from voidcode.tui.keys import Key
from voidcode.tui.styles import Styles
from voidcode.tui.terminal_app import CoreTuiApp

_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=")
_JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-body"


class _Terminal:
    width = 100
    content_width = 100
    height = 24
    eof = False
    color_system = None

    def query_default_background_rgb(self) -> None:
        return None

    def __init__(self) -> None:
        self.committed: list[str] = []
        self.frames: list[Any] = []

    def commit_rows(self, rows: list[str]) -> None:
        self.committed.extend(rows)

    def paint_frame(self, frame: Any) -> None:
        self.frames.append(frame)


class _Client:
    capabilities = ClientCapabilities()

    def __init__(self, *, run_result: RunResult | None = None) -> None:
        self.runs: list[RunSpec] = []
        self.run_result = run_result

    def command_catalog(self) -> CommandCatalog:
        return CommandCatalog(revision_id="rev-1", commands=())

    def run(self, session_id: str, spec: RunSpec) -> RunResult:
        self.runs.append(spec)
        return self.run_result or RunResult(session_id=session_id, run_id="r1", revision_id="rev-1", status="completed")


def _app() -> tuple[CoreTuiApp, _Terminal, _Client]:
    terminal = _Terminal()
    client = _Client()
    app = CoreTuiApp(client, session_id="s1", terminal=terminal)  # type: ignore[arg-type]
    app.catalog_cache = ()
    app.status_line = ""
    return app, terminal, client


def _image(path: Path, raw: bytes = _PNG) -> Path:
    path.write_bytes(raw)
    return path


def _settle(app: CoreTuiApp, *, timeout: float = 5.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app._drain_events()
        if app._work.pending == 0 and app.event_queue.empty():
            return
        time.sleep(0.005)
    raise AssertionError("app background work never settled")


def _frame_text(terminal: _Terminal) -> str:
    return "\n".join(terminal.frames[-1].rows)


def test_path_prompt_attaches_multiple_without_touching_draft_or_caret(tmp_path: Path) -> None:
    app, terminal, _ = _app()
    one = _image(tmp_path / "one.png")
    two = _image(tmp_path / "two.png", _PNG + b"two")
    app.composer.set_text("describe these")
    app.composer.handle_key(Key("left"))
    draft = app.composer.value
    caret = app.composer.cursor

    app._handle_composer_key(Key("alt+i"))
    assert app._attachment_path == ""
    app._handle_composer_key(Key("paste", text=f'"{one}" "{two}"'))
    app._handle_composer_key(Key("enter"))

    assert [item.label for item in app._pending_images.items] == ["one.png", "two.png"]
    assert app.composer.value == draft
    assert app.composer.cursor == caret

    app._render_frame()
    rendered = _frame_text(terminal)
    assert "one.png" in rendered and "two.png" in rendered
    assert "Alt+R remove last" in rendered
    assert "/tui detach 2" in rendered

    app._handle_composer_key(Key("alt+r"))
    assert [item.label for item in app._pending_images.items] == ["one.png"]
    assert app.composer.value == draft and app.composer.cursor == caret

    assert app._dispatch_slash_command("/tui detach 1") is True
    assert app._pending_images.items == ()
    assert app.composer.value == draft and app.composer.cursor == caret


def test_slash_attach_multiple_and_detach_all(tmp_path: Path) -> None:
    app, _, _ = _app()
    one = _image(tmp_path / "one.png")
    two = _image(tmp_path / "two.png", _PNG + b"two")
    assert app._dispatch_slash_command(f'/tui attach "{one}" "{two}"') is True
    assert [item.label for item in app._pending_images.items] == ["one.png", "two.png"]
    assert app._dispatch_slash_command("/tui detach all") is True
    assert app._pending_images.items == ()
    assert "Removed 2 pending image(s)" in app.status_line


def test_path_prompt_render_includes_typed_path_and_keeps_draft() -> None:
    app, terminal, _ = _app()
    app.composer.set_text("keep")
    app._begin_attachment_path()
    app._handle_composer_key(Key("paste", text="chart.png"))
    app._render_frame()
    rendered = _frame_text(terminal)
    assert "Image path:" in rendered
    assert "chart.png" in rendered
    assert "draft kept" in rendered
    assert app.composer.value == "keep"
    assert terminal.frames[-1].cursor is not None


def test_invalid_and_oversize_paths_fail_visibly(tmp_path: Path) -> None:
    app, terminal, _ = _app()
    text_file = tmp_path / "note.txt"
    text_file.write_text("hello", encoding="utf-8")
    wrong_magic = _image(tmp_path / "fake.png", _JPEG)
    oversize = tmp_path / "large.png"
    oversize.write_bytes(_PNG + b"\0" * (MAX_IMAGE_BYTES - len(_PNG) + 1))

    app._attach_image_paths((str(tmp_path / "missing.png"), str(text_file), str(wrong_magic), str(oversize)))

    committed = "\n".join(terminal.committed)
    assert "cannot open" in committed
    assert "unsupported image extension" in committed
    assert "declared image/png but contents are image/jpeg" in committed
    assert "per-image limit" in committed
    assert app._pending_images.items == ()


def test_count_and_duplicate_limits_are_visible(tmp_path: Path) -> None:
    app, terminal, _ = _app()
    paths = [_image(tmp_path / f"image-{index}.png", _PNG + bytes([index])) for index in range(MAX_PENDING_IMAGES + 1)]
    app._attach_image_paths(tuple(str(path) for path in paths[:MAX_PENDING_IMAGES]))
    assert len(app._pending_images) == MAX_PENDING_IMAGES
    app._attach_image_paths((str(paths[MAX_PENDING_IMAGES]),))
    assert "at most" in "\n".join(terminal.committed)
    assert len(app._pending_images) == MAX_PENDING_IMAGES
    app._attach_image_paths((str(paths[0]),))
    assert "already attached" in "\n".join(terminal.committed)


def test_total_limit_is_enforced_before_the_attachment_enters_the_prompt() -> None:
    pending = PendingAttachments()
    pending.add(PendingImage(ImageAttachment("image/png", "YQ=="), "one", 1_500_000))
    with pytest.raises(AttachmentError, match="per-prompt limit"):
        pending.add(PendingImage(ImageAttachment("image/png", "Yg=="), "two", 1_500_000))
    with pytest.raises(AttachmentError, match="per-image limit"):
        pending.add(PendingImage(ImageAttachment("image/png", "Yw=="), "three", MAX_IMAGE_BYTES + 1))


def test_send_carries_images_and_glyph_badge_renders(tmp_path: Path) -> None:
    app, _, client = _app()
    path = _image(tmp_path / "chart.png")
    app._attach_image_paths((str(path),))
    app.composer.set_text("what is this?")
    app._handle_composer_key(Key("enter"))
    _settle(app)

    assert len(client.runs) == 1
    spec = client.runs[0]
    assert spec.prompt == "what is this?"
    assert len(spec.images) == 1
    assert spec.images[0].media_type == "image/png"
    assert base64.b64decode(spec.images[0].data) == _PNG
    assert app._pending_images.items == ()
    assert app.composer.value == ""

    rows = rendering.content_rows(ProviderMessage("user", spec.prompt, images=spec.images), 80, Styles(None))
    assert any("[Image: image/png]" in row for row in rows)


def test_image_only_prompt_is_refused_with_attachments_kept(tmp_path: Path) -> None:
    app, _, client = _app()
    app._attach_image_paths((str(_image(tmp_path / "chart.png")),))
    app._handle_composer_key(Key("enter"))
    assert client.runs == []
    assert len(app._pending_images) == 1
    assert "not sent alone" in app.status_line


def test_clipboard_unavailable_is_visible_and_draft_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    import voidcode.tui.terminal_app as terminal_app_module

    app, terminal, client = _app()
    app.composer.set_text("keep drafting")
    caret = app.composer.cursor

    def unavailable() -> PendingImage:
        raise ClipboardImageUnavailable("no supported clipboard reader found (wl-paste or xclip)")

    monkeypatch.setattr(terminal_app_module, "read_clipboard_image", unavailable)
    app._handle_composer_key(Key("alt+v"))
    _settle(app)

    assert "Clipboard image unavailable" in "\n".join(terminal.committed)
    assert app._pending_images.items == ()
    assert client.runs == []
    assert app.composer.value == "keep drafting"
    assert app.composer.cursor == caret


def test_clipboard_image_success_attaches_and_announces(monkeypatch: pytest.MonkeyPatch) -> None:
    import voidcode.tui.terminal_app as terminal_app_module

    app, _, _ = _app()
    image = PendingImage(ImageAttachment("image/png", base64.b64encode(_PNG).decode("ascii")), "clipboard image", len(_PNG))
    monkeypatch.setattr(terminal_app_module, "read_clipboard_image", lambda: image)
    app._handle_composer_key(Key("alt+v"))
    _settle(app)

    assert [item.label for item in app._pending_images.items] == ["clipboard image"]
    assert "Attached clipboard image" in app.status_line


def test_queued_followup_carries_images_and_withdrawal_names_the_limit() -> None:
    queued_terminal = _Terminal()
    queued_client = _Client(run_result=RunResult(session_id="s1", run_id="r1", revision_id="rev-1", status="queued"))
    app = CoreTuiApp(queued_client, session_id="s1", terminal=queued_terminal)  # type: ignore[arg-type]
    app.catalog_cache = ()
    app.status_line = ""
    app._run_active = True
    image = PendingImage(ImageAttachment("image/png", base64.b64encode(_PNG).decode("ascii")), "chart.png", len(_PNG))
    assert app._accept_pending_image(image)
    app.composer.set_text("follow up with this")
    app._handle_composer_key(Key("enter"))
    _settle(app)

    assert len(queued_client.runs) == 1
    assert len(queued_client.runs[0].images) == 1
    assert app._pending_images.items == ()

    receipt = FollowupWithdrawReceipt(
        session_id="s1",
        continuation_id="cid-1",
        revision_id="rev-1",
        prompt="follow up with this",
        image_count=1,
    )
    app.event_queue.put(events.FollowupWithdrawnEvent(receipt=receipt))
    app._drain_events()
    assert app.composer.value == "follow up with this"
    assert "attached images cannot be re-edited" in app.status_line
    assert app._pending_images.items == ()


def test_failed_queued_followup_restores_prompt_and_images() -> None:
    terminal = _Terminal()
    failed_client = _Client(run_result=RunResult(session_id="s1", run_id="r1", revision_id="rev-1", status="failed", error="queue exploded"))
    app = CoreTuiApp(failed_client, session_id="s1", terminal=terminal)  # type: ignore[arg-type]
    app.catalog_cache = ()
    app.status_line = ""
    app._run_active = True
    image = PendingImage(ImageAttachment("image/png", base64.b64encode(_PNG).decode("ascii")), "chart.png", len(_PNG))
    assert app._accept_pending_image(image)
    app.composer.set_text("retry me")
    app._handle_composer_key(Key("enter"))
    _settle(app)

    assert app.composer.value == "retry me"
    assert [item.label for item in app._pending_images.items] == ["restored image"]
    assert app.status_line == "Error: queue exploded"


def test_clipboard_reader_never_reads_plain_text_as_a_path(monkeypatch: pytest.MonkeyPatch) -> None:
    import voidcode.tui.clipboard_image as clipboard

    calls: list[tuple[str, ...]] = []

    def command(argv: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 0, stdout=b"text/plain\nUTF8_STRING\n", stderr=b"")

    monkeypatch.setattr(clipboard, "_backends", lambda: (_Backend("fake", wayland=False),))
    monkeypatch.setattr(clipboard, "_command", command)
    with pytest.raises(ClipboardImageUnavailable, match="no image target"):
        read_clipboard_image()
    assert all("text/plain" not in argv for argv in calls[1:])


def test_clipboard_reader_accepts_one_uri_list_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import voidcode.tui.clipboard_image as clipboard

    path = _image(tmp_path / "from-files.png")

    def command(argv: Any) -> subprocess.CompletedProcess[bytes]:
        if "TARGETS" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=b"text/uri-list\n", stderr=b"")
        return subprocess.CompletedProcess(argv, 0, stdout=path.as_uri().encode("utf-8"), stderr=b"")

    monkeypatch.setattr(clipboard, "_backends", lambda: (_Backend("fake", wayland=False),))
    monkeypatch.setattr(clipboard, "_command", command)
    image = read_clipboard_image()
    assert image.label == "from-files.png"
    assert base64.b64decode(image.attachment.data) == _PNG


def test_clipboard_reader_reports_missing_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    import voidcode.tui.clipboard_image as clipboard

    monkeypatch.setattr(clipboard, "_backends", lambda: ())
    with pytest.raises(ClipboardImageUnavailable, match="no supported clipboard reader"):
        read_clipboard_image()
