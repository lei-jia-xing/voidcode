"""Structured read views must expose omitted content and usable continuation."""

import json

import pytest

from voidcode.capabilities.files.read_formats import render_notebook


@pytest.mark.parametrize("cell", [None, 0])
def test_notebook_omitted_outputs_are_partial_and_recoverable(tmp_path, cell):
    path = tmp_path / "notebook.ipynb"
    path.write_text(
        json.dumps(
            {
                "cells": [
                    {
                        "cell_type": "code",
                        "source": "print('first')",
                        "outputs": [
                            {"output_type": "stream", "text": "first"},
                            {"output_type": "stream", "text": "second"},
                        ],
                    },
                    {"cell_type": "markdown", "source": "later cell"},
                ]
            }
        ),
        encoding="utf-8",
    )
    first = render_notebook(path, offset=1, limit=10, cell=cell, output_limit=1)
    assert first["partial"] is True
    assert first["truncated"] is True
    assert first["next_offset"] is None
    code = first["cells"][0]
    assert code["outputs_truncated"] is True
    assert code["next_output_offset"] == 2
    assert code["outputs"][0]["text"] == "first"
    assert first["next_output_offset"] == (2 if cell == 0 else None)

    second = render_notebook(path, offset=1, limit=10, cell=code["index"], output_offset=code["next_output_offset"], output_limit=1)
    assert second["cells"][0]["outputs"][0]["text"] == "second"
    assert second["partial"] is False
    assert second["next_output_offset"] is None


def test_pdf_preview_does_not_use_negative_slice_when_budget_is_full(monkeypatch):
    from pathlib import Path

    from voidcode.capabilities.files import read_formats

    full_text = "a" * read_formats.PDF_PREVIEW_BYTES + "\n" + "b" * 50000

    class Process:
        returncode = 0

        def __init__(self, command, **kwargs):
            Path(command[-1]).write_text(full_text, encoding="utf-8")

        def poll(self):
            return 0

        def wait(self):
            return 0

    monkeypatch.setattr(read_formats.subprocess, "Popen", Process)
    result = read_formats.render_pdf_bytes(b"%PDF-test")
    assert result["raw_content"] == "a" * read_formats.PDF_PREVIEW_BYTES
    assert result["partial"] is True
    assert result["next_offset"] == 2
    assert result["full_text"] == full_text


@pytest.mark.parametrize("failure", ["cancel", "timeout", "oversize", "callback_error", "interrupt"])
def test_pdf_extractor_is_killed_and_reaped_on_early_exit(monkeypatch, failure):
    from pathlib import Path

    from voidcode.capabilities.files import read_formats

    class Process:
        returncode = None
        killed = False
        reaped = False

        def poll(self):
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9

        def wait(self):
            self.reaped = True
            return self.returncode

    process = Process()

    def spawn(command, **kwargs):
        if failure == "oversize":
            Path(command[-1]).write_bytes(b"x")
        return process

    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        if calls == 1:
            return False
        if failure == "callback_error":
            raise RuntimeError("callback failed")
        if failure == "interrupt":
            raise KeyboardInterrupt
        return failure == "cancel"

    monkeypatch.setattr(read_formats.subprocess, "Popen", spawn)
    if failure == "timeout":
        monkeypatch.setattr(read_formats, "PDF_TIMEOUT", -1)
    if failure == "oversize":
        monkeypatch.setattr(read_formats, "PDF_TEXT_BYTES", 0)
    expected_error = {"callback_error": RuntimeError, "interrupt": KeyboardInterrupt}.get(failure, ValueError)
    with pytest.raises(expected_error):
        read_formats.render_pdf_bytes(b"%PDF-test", cancelled=cancelled)
    assert process.killed
    assert process.reaped
