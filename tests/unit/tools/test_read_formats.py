from __future__ import annotations

import hashlib
import sqlite3
import struct
import zlib
from pathlib import Path

import pytest

from voidcode.core.tool_context import ToolContext
from voidcode.core.transcript import output_text
from voidcode.tools.contracts import ToolCall
from voidcode.tools.read import ReadResultBody, ReadTool
from voidcode.tools.read_formats import render_notebook, render_pdf_bytes, render_pdf_path, render_sqlite


def test_sqlite_view_is_real_read_only_and_paginates(tmp_path: Path) -> None:
    database = tmp_path / "real.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE records (name TEXT, count INTEGER)")
    connection.executemany("INSERT INTO records VALUES (?, ?)", [("héllo", 1), ("second", 2), ("third", 3)])
    connection.commit()
    connection.close()

    page = render_sqlite(database, table="records", query=None, offset=2, limit=1)

    assert page["type"] == "sqlite"
    assert page["columns"] == ["name", "count"]
    assert page["rows"] == [["second", 2]]
    assert page["next_offset"] == 3
    assert page["source_sha256"] == hashlib.sha256(database.read_bytes()).hexdigest()


def test_sqlite_view_rejects_mutation_and_attach_without_touching_sentinel(tmp_path: Path) -> None:
    database = tmp_path / "protected.db"
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE records (value TEXT)")
    connection.execute("INSERT INTO records VALUES ('safe')")
    connection.commit()
    connection.close()

    for query in (
        "DELETE FROM records",
        f"ATTACH DATABASE '{tmp_path / 'attached.db'}' AS external",
        "SELECT load_extension('not-a-real-extension')",
    ):
        with pytest.raises(ValueError, match="read-only query failed"):
            render_sqlite(database, table="records", query=query, offset=1, limit=10)

    check = sqlite3.connect(database)
    assert check.execute("SELECT value FROM records").fetchone() == ("safe",)
    check.close()
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert not (tmp_path / "attached.db").exists()


def test_sqlite_recursive_query_is_time_bounded(tmp_path: Path) -> None:
    database = tmp_path / "recursive.sqlite3"
    sqlite3.connect(database).close()

    with pytest.raises(ValueError, match="interrupted|read-only query failed"):
        render_sqlite(
            database,
            table=None,
            query="WITH RECURSIVE forever(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM forever) SELECT x FROM forever ORDER BY x DESC",
            offset=1,
            limit=1,
        )


def test_sqlite_query_respects_cancellation(tmp_path: Path) -> None:
    database = tmp_path / "cancel.sqlite3"
    sqlite3.connect(database).close()
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls > 1

    with pytest.raises(ValueError, match="SQLite query cancelled"):
        render_sqlite(
            database,
            table=None,
            query="WITH RECURSIVE forever(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM forever) SELECT x FROM forever ORDER BY x DESC",
            offset=1,
            limit=1,
            cancelled=cancelled,
        )


def _one_pixel_png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">2I5B", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )


def test_notebook_view_preserves_cell_source_and_decodes_png(tmp_path: Path) -> None:
    import base64
    import json

    image = _one_pixel_png()
    notebook = tmp_path / "sample.ipynb"
    notebook.write_text(
        json.dumps(
            {
                "cells": [
                    {
                        "cell_type": "code",
                        "source": ["  print('hi')\n", "\tvalue = 2"],
                        "execution_count": 1,
                        "outputs": [{"output_type": "display_data", "data": {"text/plain": ["ok\n"], "image/png": base64.b64encode(image).decode()}}],
                    },
                    {"cell_type": "markdown", "source": ["next\n"]},
                ],
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        ),
        encoding="utf-8",
    )

    page = render_notebook(notebook, offset=1, limit=1)

    assert page["cells"][0]["source"] == "  print('hi')\n\tvalue = 2"
    assert page["cells"][0]["execution_count"] == 1
    assert page["next_offset"] == 2
    decoded = page["images"][0]
    assert decoded["data"] == image
    assert decoded["sha256"] == hashlib.sha256(image).hexdigest()


def test_notebook_output_pagination_and_full_source_reference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / ".cache"))

    notebook = tmp_path / "output.ipynb"
    notebook.write_text(
        json.dumps(
            {
                "cells": [
                    {
                        "cell_type": "code",
                        "source": "print('large')",
                        "execution_count": 1,
                        "outputs": [
                            {"output_type": "stream", "name": "stdout", "text": "first"},
                            {"output_type": "stream", "name": "stdout", "text": "second"},
                        ],
                    },
                    {"cell_type": "markdown", "source": "x" * 9000},
                ],
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        ),
        encoding="utf-8",
    )

    output_page = render_notebook(notebook, offset=1, limit=1, cell=0, output_offset=1, output_limit=1)
    assert output_page["next_output_offset"] == 2
    assert output_page["cells"][0]["outputs"][0]["text"] == "first"
    next_output = render_notebook(notebook, offset=1, limit=1, cell=0, output_offset=2, output_limit=1)
    assert next_output["cells"][0]["outputs"][0]["text"] == "second"

    result = ReadTool().invoke(
        ToolCall(tool_name="read", arguments={"path": "output.ipynb", "offset": 2, "limit": 1}),
        context=ToolContext(workspace=tmp_path),
    )
    body = result.body.as_payload()
    assert body["truncated_text"] is True
    assert body["source_reference"].startswith("voidcode://artifact/")
    assert body["source_reference"] in (output_text(result.output) or "")


def test_read_tool_emits_artifact_reference_for_notebook_png(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import base64
    import json

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / ".cache"))
    notebook = tmp_path / "image.ipynb"
    notebook.write_text(
        json.dumps(
            {
                "cells": [
                    {
                        "cell_type": "code",
                        "source": "display(image)",
                        "execution_count": 1,
                        "outputs": [
                            {
                                "output_type": "display_data",
                                "data": {"image/png": base64.b64encode(_one_pixel_png()).decode("ascii")},
                            }
                        ],
                    }
                ],
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        ),
        encoding="utf-8",
    )

    result = ReadTool().invoke(
        ToolCall(tool_name="read", arguments={"path": "image.ipynb"}),
        context=ToolContext(workspace=tmp_path, invocation_id="image-call"),
    )
    body = result.body.as_payload()
    reference = body["cells"][0]["outputs"][0]["image"]["reference"]
    assert reference.startswith("voidcode://artifact/")
    assert reference in (output_text(result.output) or "")


def _simple_pdf(text: str, second_page: str = "second-page") -> bytes:
    def content(value: str) -> bytes:
        return f"BT /F1 12 Tf 72 720 Td ({value}) Tj ET".encode("ascii")

    first, second = content(text), content(second_page)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 7 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(first)).encode() + b" >>\nstream\n" + first + b"\nendstream",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 7 0 R >> >> /Contents 6 0 R >>",
        b"<< /Length " + str(len(second)).encode() + b" >>\nstream\n" + second + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, 1):
        offsets.append(len(out))
        out.extend(f"{index} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = len(out)
    out.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        out.extend(f"{offset:010d} 00000 n \n".encode())
    out.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(out)


def test_pdf_native_extraction_reads_valid_pdf_text(tmp_path: Path) -> None:
    pdf = tmp_path / "known.pdf"
    pdf.write_bytes(_simple_pdf("known-text"))

    try:
        first = render_pdf_path(pdf, page=1, offset=1, limit=10)
        second = render_pdf_path(pdf, page=2, offset=1, limit=10)
    except ValueError as exc:
        if "requires the Linux Poppler" in str(exc):
            pytest.skip("pdftotext is not installed")
        raise

    assert first["type"] == "pdf"
    assert "known-text" in first["raw_content"]
    assert "second-page" in second["raw_content"]
    assert "known-text" not in second["raw_content"]
    assert first["source_sha256"] == hashlib.sha256(pdf.read_bytes()).hexdigest()


def test_pdf_rejects_non_pdf_bytes() -> None:
    with pytest.raises(ValueError):
        render_pdf_bytes(b"not a PDF document")


def _fake_pdftotext(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    utility = binary_dir / "pdftotext"
    utility.write_text(f"#!/bin/sh\n{script}", encoding="utf-8")
    utility.chmod(0o700)
    monkeypatch.setenv("PATH", str(binary_dir))


def test_pdf_rejects_final_output_over_the_byte_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from voidcode.tools import read_formats

    _fake_pdftotext(tmp_path, monkeypatch, "for output do :; done\nprintf 'four' > \"$output\"\n")
    monkeypatch.setattr(read_formats, "PDF_TEXT_BYTES", 3)
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    with pytest.raises(ValueError, match="exceeds 3 bytes"):
        render_pdf_path(pdf)


def test_pdf_extraction_kills_native_process_on_cancellation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_pdftotext(tmp_path, monkeypatch, "exec /bin/sleep 30\n")
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    checks = 0

    def cancelled() -> bool:
        nonlocal checks
        checks += 1
        return checks > 1

    with pytest.raises(ValueError, match="PDF text extraction cancelled"):
        render_pdf_path(pdf, cancelled=cancelled)


def test_raw_pdf_output_exposes_its_artifact_reference_and_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / ".cache"))
    pdf = tmp_path / "report.pdf"
    contents = _simple_pdf("visible text")
    pdf.write_bytes(contents)

    result = ReadTool().invoke(
        ToolCall(tool_name="read", arguments={"path": "report.pdf", "view": "raw"}),
        context=ToolContext(workspace=tmp_path, invocation_id="raw-pdf"),
    )

    presentation = output_text(result.output) or ""
    assert "voidcode://artifact/" in presentation
    assert "application/pdf" in presentation
    assert f"{len(contents)} bytes" in presentation
    assert hashlib.sha256(contents).hexdigest() in presentation


def test_read_tool_keeps_derived_formats_opaque_and_raw_notebook_editable(tmp_path: Path) -> None:
    import json

    notebook = tmp_path / "minimal.ipynb"
    notebook.write_text(
        json.dumps({"cells": [{"cell_type": "markdown", "source": "original"}], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}),
        encoding="utf-8",
    )
    tool = ReadTool()
    derived = tool.invoke(ToolCall(tool_name="read", arguments={"path": "minimal.ipynb"}), context=ToolContext(workspace=tmp_path))
    assert derived.body is not None
    assert derived.body.as_payload()["type"] == "notebook"
    assert not isinstance(derived.body, ReadResultBody)

    raw = tool.invoke(
        ToolCall(tool_name="read", arguments={"path": "minimal.ipynb", "view": "raw"}),
        context=ToolContext(workspace=tmp_path),
    )
    assert isinstance(raw.body, ReadResultBody)
    assert raw.body.whole_file
    assert "original" in (output_text(raw.output) or "")


def test_read_tool_shows_sqlite_rows_in_opaque_body(tmp_path: Path) -> None:
    database = tmp_path / "live.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE people (name TEXT)")
    connection.execute("INSERT INTO people VALUES ('Ada Lovelace')")
    connection.commit()
    connection.close()

    result = ReadTool().invoke(
        ToolCall(tool_name="read", arguments={"path": "live.sqlite"}),
        context=ToolContext(workspace=tmp_path),
    )

    assert result.body is not None
    body = result.body.as_payload()
    assert body["type"] == "sqlite"
    assert body["rows"] == [["Ada Lovelace"]]
    assert "Ada Lovelace" in (output_text(result.output) or "")


def test_read_tool_exports_oversized_json_view_with_artifact_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / ".cache"))
    database = tmp_path / "expanded.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE records (value TEXT)")
    connection.executemany("INSERT INTO records VALUES (?)", [("\n" * 900,) for _ in range(50)])
    connection.commit()
    connection.close()

    result = ReadTool().invoke(
        ToolCall(tool_name="read", arguments={"path": "expanded.sqlite"}),
        context=ToolContext(workspace=tmp_path),
    )

    body = result.body.as_payload()
    assert body["output_truncated"] is True
    assert body["output_reference"].startswith("voidcode://artifact/")
    assert body["output_reference"] in (output_text(result.output) or "")
    assert body["output_artifact"]["mime"] == "application/json"
