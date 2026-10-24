from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

MAX_FORMAT_BYTES = 50 * 1024 * 1024
MAX_VIEW_BYTES = 50 * 1024
MAX_CELL_BYTES = 8 * 1024
MAX_ROWS = 200
MAX_CELLS = 200
MAX_OUTPUTS = 20
MAX_IMAGE_BYTES = 5 * 1024 * 1024
SQLITE_TIMEOUT = 0.5
PDF_TIMEOUT = 5
PDF_TEXT_BYTES = 10 * 1024 * 1024
PDF_PREVIEW_BYTES = 32 * 1024


def _bounded_text(value: str, budget: list[int], clipped: list[bool]) -> str:
    encoded = value.encode("utf-8")
    size = min(len(encoded), MAX_CELL_BYTES, budget[0])
    if size < len(encoded):
        clipped[0] = True
    preview = encoded[:size].decode("utf-8", errors="ignore")
    budget[0] -= len(preview.encode("utf-8"))
    return preview


def _join_strings(value: object, *, description: str) -> str:
    if isinstance(value, str):
        result = value
    elif isinstance(value, list) and all(isinstance(part, str) for part in value):
        if sum(len(part.encode("utf-8")) for part in value) > MAX_FORMAT_BYTES:
            raise ValueError(f"Notebook {description} exceeds the source file size limit")
        result = "".join(value)
    else:
        raise ValueError(f"invalid Notebook {description}")
    if len(result.encode("utf-8")) > MAX_FORMAT_BYTES:
        raise ValueError(f"Notebook {description} exceeds the source file size limit")
    return result


def render_sqlite(
    path: Path,
    *,
    table: str | None,
    query: str | None,
    offset: int,
    limit: int,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, object]:
    """Return a bounded SQLite observation; query is one read-only statement."""
    if offset < 1 or limit < 1:
        raise ValueError("SQLite offset and limit must be positive")
    if cancelled is not None and cancelled():
        raise ValueError("SQLite query cancelled")
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=SQLITE_TIMEOUT)
    except sqlite3.Error as exc:
        raise ValueError(f"cannot open SQLite database read-only: {exc}") from exc
    start = time.monotonic()
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VIEW_BYTES)
        connection.set_progress_handler(
            lambda: int((cancelled is not None and cancelled()) or time.monotonic() - start > SQLITE_TIMEOUT),
            1000,
        )
        schemas: list[tuple[str, str | None]] = []
        schema_bytes = 0
        for name, schema in connection.execute(
            "SELECT name, sql FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name LIMIT 101"
        ):
            schema_bytes += len(name.encode("utf-8")) + len((schema or "").encode("utf-8"))
            if len(schemas) >= 100 or schema_bytes > MAX_VIEW_BYTES:
                raise ValueError("SQLite schema exceeds the bounded view limit")
            schemas.append((name, schema))
        table_names = {name for name, _ in schemas}
        if table is not None and table not in table_names:
            raise ValueError(f"SQLite table not found: {table}")
        selected_table = table or (schemas[0][0] if schemas else None)
        if query is None:
            escaped = selected_table.replace('"', '""') if selected_table else ""
            sql = f'SELECT * FROM "{escaped}"' if selected_table else None
        else:
            sql = query.strip()
            if not sql:
                raise ValueError("SQLite query must not be empty")
            if len(sql.encode("utf-8")) > MAX_VIEW_BYTES:
                raise ValueError("SQLite query exceeds the maximum supported SQL size")
        columns: list[str] = []
        rows: list[list[object]] = []
        next_offset = None
        truncated = False
        tables = [{"name": name, "sql": schema} for name, schema in schemas]
        if sql is not None:

            def authorize(action: int, arg1: str | None, arg2: str | None, _db: str | None, _source: str | None) -> int:
                if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_RECURSIVE}:
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_FUNCTION:
                    function = (arg2 or arg1 or "").lower()
                    return sqlite3.SQLITE_DENY if function in {"load_extension", "writefile", "eval"} else sqlite3.SQLITE_OK
                return sqlite3.SQLITE_DENY

            connection.set_authorizer(authorize)
            cursor = connection.execute(sql)
            columns = [str(description[0]) for description in cursor.description or ()]
            selected_bytes = len(json.dumps({"tables": tables, "columns": columns, "rows": []}, ensure_ascii=False).encode("utf-8"))
            if selected_bytes > MAX_VIEW_BYTES:
                raise ValueError("SQLite schema and column names exceed the total view byte limit")
            row_count = 0
            for index, row in enumerate(cursor):
                row_count = index + 1
                if index < offset - 1:
                    continue
                if len(rows) >= min(limit, MAX_ROWS):
                    next_offset = offset + len(rows)
                    truncated = True
                    break
                safe_row: list[object] = []
                for value in row:
                    if isinstance(value, bytes):
                        if len(value) > MAX_CELL_BYTES:
                            raise ValueError("SQLite query returned a BLOB larger than the per-cell view limit")
                        safe_row.append({"type": "blob", "hex": value.hex()})
                    else:
                        if isinstance(value, str) and len(value.encode("utf-8")) > MAX_CELL_BYTES:
                            raise ValueError("SQLite query returned a text cell larger than the per-cell view limit")
                        safe_row.append(value)
                try:
                    row_bytes = len(json.dumps(safe_row, ensure_ascii=False, allow_nan=False).encode("utf-8")) + (2 if rows else 0)
                except ValueError as exc:
                    raise ValueError("SQLite query returned a non-finite number, which cannot be represented in JSON") from exc
                if row_bytes > MAX_VIEW_BYTES - selected_bytes:
                    if not rows:
                        raise ValueError("SQLite query row exceeds the total view byte limit")
                    next_offset = offset + len(rows)
                    truncated = True
                    break
                rows.append(safe_row)
                selected_bytes += row_bytes
            if offset > row_count and offset > 1:
                raise ValueError(f"SQLite query offset {offset} is out of range ({row_count} rows)")
        return {
            "type": "sqlite",
            "source": str(path),
            "source_sha256": _hash_file(path),
            "tables": tables,
            "table": selected_table,
            "columns": columns,
            "rows": rows,
            "offset": offset,
            "limit": min(limit, MAX_ROWS),
            "next_offset": next_offset,
            "truncated": truncated,
            "partial": truncated,
        }
    except sqlite3.Error as exc:
        if cancelled is not None and cancelled():
            raise ValueError("SQLite query cancelled") from exc
        raise ValueError(f"SQLite read-only query failed: {exc}") from exc
    finally:
        connection.close()


def render_notebook(
    path: Path,
    *,
    offset: int,
    limit: int,
    cell: int | None = None,
    output_offset: int = 1,
    output_limit: int = MAX_OUTPUTS,
) -> dict[str, object]:
    if path.stat().st_size > MAX_FORMAT_BYTES:
        raise ValueError(f"Notebook exceeds the maximum supported size ({MAX_FORMAT_BYTES} bytes)")
    if output_offset < 1 or output_limit < 1:
        raise ValueError("Notebook output_offset and output_limit must be positive")
    with path.open("rb") as source:
        raw = source.read(MAX_FORMAT_BYTES + 1)
    if len(raw) > MAX_FORMAT_BYTES:
        raise ValueError(f"Notebook exceeds the maximum supported size ({MAX_FORMAT_BYTES} bytes)")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"invalid Jupyter notebook JSON: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("cells"), list):
        raise ValueError("invalid Jupyter notebook: expected a cells array")
    cells = document["cells"]
    if len(cells) > MAX_CELLS:
        raise ValueError(f"Notebook exceeds the maximum supported cell count ({MAX_CELLS})")
    if cell is not None:
        if cell < 0 or cell >= len(cells):
            raise ValueError(f"Notebook cell index out of range: {cell}")
        indexes = [cell]
        offset = cell + 1
    else:
        if output_offset != 1:
            raise ValueError("Notebook output pagination requires a single cell selector")
        if offset > max(1, len(cells)):
            raise ValueError(f"Notebook cell offset {offset} is out of range ({len(cells)} cells)")
        indexes = list(range(offset - 1, min(len(cells), offset - 1 + min(limit, MAX_CELLS))))

    budget = [MAX_VIEW_BYTES]
    rendered: list[dict[str, object]] = []
    images: list[dict[str, object]] = []
    output_count = 0
    image_bytes = 0
    next_output_offset: int | None = None
    outputs_truncated = False
    clipped_text = [False]
    raw_reference_required = False
    for index in indexes:
        item = cells[index]
        if not isinstance(item, dict) or item.get("cell_type") not in {"code", "markdown", "raw"}:
            raise ValueError(f"invalid Jupyter notebook cell at index {index}")
        source = item.get("source", "")
        if isinstance(source, list):
            source = _join_strings(source, description=f"cell {index} source")
        if not isinstance(source, str):
            raise ValueError(f"invalid source in Jupyter notebook cell {index}")
        cell_view: dict[str, object] = {
            "index": index,
            "type": item["cell_type"],
            "source": _bounded_text(source, budget, clipped_text),
        }
        if item["cell_type"] == "code":
            count = item.get("execution_count")
            if count is not None and (not isinstance(count, int) or isinstance(count, bool)):
                raise ValueError(f"invalid execution_count in Jupyter notebook cell {index}")
            cell_view["execution_count"] = count
            outputs = item.get("outputs", [])
            if not isinstance(outputs, list):
                raise ValueError(f"invalid outputs in Jupyter notebook cell {index}")
            if output_offset > max(1, len(outputs)):
                raise ValueError(f"Notebook output offset {output_offset} is out of range ({len(outputs)} outputs)")
            selected_outputs = outputs[output_offset - 1 : output_offset - 1 + min(output_limit, MAX_OUTPUTS)]
            output_views: list[dict[str, object]] = []
            for output in selected_outputs:
                output_count += 1
                if output_count > MAX_OUTPUTS:
                    raise ValueError(f"Notebook exceeds the maximum supported output count ({MAX_OUTPUTS})")
                if not isinstance(output, dict):
                    raise ValueError(f"invalid output in Jupyter notebook cell {index}")
                output_type = output.get("output_type")
                if not isinstance(output_type, str):
                    raise ValueError(f"invalid output type in Jupyter notebook cell {index}")
                data = output.get("data", {})
                if not isinstance(data, dict):
                    raise ValueError(f"invalid output data in Jupyter notebook cell {index}")
                if len(data) > MAX_OUTPUTS:
                    raise ValueError(f"Notebook output has more than {MAX_OUTPUTS} data types")
                view: dict[str, object] = {
                    "output_type": output_type,
                    "data_types": sorted(str(key) for key in data),
                }
                supported_data_types = {
                    "text/plain",
                    "text/markdown",
                    "text/html",
                    "application/json",
                    "image/png",
                    "image/jpeg",
                    "image/webp",
                }
                unsupported_data_types = sorted(str(key) for key in data if key not in supported_data_types)
                if unsupported_data_types:
                    view["unsupported_data_types"] = unsupported_data_types
                    raw_reference_required = True
                if output_type == "stream" and "text" in output:
                    view["text"] = _bounded_text(_join_strings(output["text"], description="stream output"), budget, clipped_text)
                for mime in ("text/plain", "text/markdown", "text/html", "application/json"):
                    if mime not in data:
                        continue
                    value = data[mime]
                    if mime in {"text/plain", "text/markdown", "text/html"}:
                        text = _join_strings(value, description=f"{mime} output")
                    elif isinstance(value, str):
                        text = value
                    else:
                        text = json.dumps(value, ensure_ascii=False)
                    view[mime] = _bounded_text(text, budget, clipped_text)
                if "traceback" in output:
                    view["traceback"] = _bounded_text(_join_strings(output["traceback"], description="traceback"), budget, clipped_text)
                for mime in ("image/png", "image/jpeg", "image/webp"):
                    if mime not in data:
                        continue
                    encoded_value = data[mime]
                    if isinstance(encoded_value, list):
                        if any(not isinstance(part, str) for part in encoded_value):
                            raise ValueError(f"invalid {mime} image in notebook cell {index}")
                        if sum(len(part) for part in encoded_value) > MAX_IMAGE_BYTES * 4 // 3 + 8:
                            raise ValueError(f"Notebook {mime} image exceeds {MAX_IMAGE_BYTES} bytes")
                        encoded_value = "".join(encoded_value)
                    if not isinstance(encoded_value, str):
                        raise ValueError(f"invalid {mime} image in notebook cell {index}")
                    if len(encoded_value) > MAX_IMAGE_BYTES * 4 // 3 + 8:
                        raise ValueError(f"Notebook {mime} image exceeds {MAX_IMAGE_BYTES} bytes")
                    try:
                        image = base64.b64decode(encoded_value, validate=True)
                    except ValueError as exc:
                        raise ValueError(f"invalid base64 {mime} image in notebook cell {index}") from exc
                    image_bytes += len(image)
                    if image_bytes > MAX_IMAGE_BYTES:
                        raise ValueError(f"Notebook images exceed the {MAX_IMAGE_BYTES}-byte total limit")
                    if not _valid_image(image, mime):
                        raise ValueError(f"Notebook image does not contain valid {mime} data")
                    image_info: dict[str, object] = {
                        "cell": index,
                        "mime": mime,
                        "byte_count": len(image),
                        "sha256": hashlib.sha256(image).hexdigest(),
                        "data": image,
                    }
                    images.append(image_info)
                    view["image"] = image_info
                output_views.append(view)
            output_end = output_offset - 1 + len(output_views)
            cell_next_output = output_end + 1 if output_end < len(outputs) else None
            outputs_truncated |= cell_next_output is not None
            if cell is not None:
                next_output_offset = cell_next_output
            cell_view["outputs"] = output_views
            cell_view["output_offset"] = output_offset
            cell_view["next_output_offset"] = cell_next_output
            cell_view["outputs_truncated"] = cell_next_output is not None
        rendered.append(cell_view)
    next_offset = None if cell is not None or offset - 1 + len(rendered) >= len(cells) else offset + len(rendered)
    truncated = next_offset is not None or outputs_truncated or clipped_text[0]
    return {
        "type": "notebook",
        "source_bytes": raw,
        "source": str(path),
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "cell_count": len(cells),
        "offset": offset,
        "limit": min(limit, MAX_CELLS),
        "next_offset": next_offset,
        "next_output_offset": next_output_offset,
        "cells": rendered,
        "images": images,
        "truncated_text": clipped_text[0],
        "truncated": truncated,
        "partial": truncated,
        "raw_reference_required": raw_reference_required,
    }


def render_pdf_bytes(
    data: bytes,
    *,
    page: int | None = None,
    offset: int = 1,
    limit: int = 2000,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, object]:
    if len(data) > MAX_FORMAT_BYTES:
        raise ValueError(f"PDF exceeds the maximum supported size ({MAX_FORMAT_BYTES} bytes)")
    if page is not None and page < 1:
        raise ValueError("PDF page must be a positive one-based page number")
    if not data.startswith(b"%PDF-"):
        raise ValueError("invalid PDF signature")
    with tempfile.TemporaryDirectory(prefix="voidcode-read-pdf-") as directory:
        source = Path(directory) / "source.pdf"
        source.write_bytes(data)
        return _extract_pdf(
            source,
            source_hash=hashlib.sha256(data).hexdigest(),
            page=page,
            offset=offset,
            limit=limit,
            cancelled=cancelled,
        )


def render_pdf_path(
    path: Path,
    *,
    page: int | None = None,
    offset: int = 1,
    limit: int = 2000,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, object]:
    if path.stat().st_size > MAX_FORMAT_BYTES:
        raise ValueError(f"PDF exceeds the maximum supported size ({MAX_FORMAT_BYTES} bytes)")
    if page is not None and page < 1:
        raise ValueError("PDF page must be a positive one-based page number")
    with path.open("rb") as source:
        data = source.read(MAX_FORMAT_BYTES + 1)
    if len(data) > MAX_FORMAT_BYTES:
        raise ValueError(f"PDF exceeds the maximum supported size ({MAX_FORMAT_BYTES} bytes)")
    result = render_pdf_bytes(data, page=page, offset=offset, limit=limit, cancelled=cancelled)
    result["source"] = str(path)
    result["source_bytes"] = data
    return result


def _extract_pdf(
    path: Path,
    *,
    source_hash: str,
    page: int | None,
    offset: int,
    limit: int,
    cancelled: Callable[[], bool] | None,
) -> dict[str, object]:
    if offset < 1 or limit < 1:
        raise ValueError("PDF offset and limit must be positive")
    if cancelled is not None and cancelled():
        raise ValueError("PDF text extraction cancelled")
    with tempfile.TemporaryDirectory(prefix="voidcode-read-pdf-text-") as directory:
        output = Path(directory) / "text.txt"
        command = ["pdftotext", "-layout", "-enc", "UTF-8"]
        if page is not None:
            command.extend(["-f", str(page), "-l", str(page)])
        command.extend([str(path), str(output)])
        try:
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError as exc:
            raise ValueError("PDF text view requires the Linux Poppler `pdftotext` utility") from exc
        try:
            deadline = time.monotonic() + PDF_TIMEOUT
            while process.poll() is None:
                if cancelled is not None and cancelled():
                    raise ValueError("PDF text extraction cancelled")
                if time.monotonic() > deadline:
                    raise ValueError("PDF text extraction timed out")
                if output.exists() and output.stat().st_size > PDF_TEXT_BYTES:
                    raise ValueError(f"PDF extracted text exceeds {PDF_TEXT_BYTES} bytes; select a page")
                time.sleep(0.01)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
        if process.returncode:
            raise ValueError("pdftotext failed: invalid, encrypted, or unreadable PDF")
        if not output.exists():
            raise ValueError("pdftotext did not produce output")
        with output.open("rb") as extracted:
            raw_text = extracted.read(PDF_TEXT_BYTES + 1)
        if len(raw_text) > PDF_TEXT_BYTES:
            raise ValueError(f"PDF extracted text exceeds {PDF_TEXT_BYTES} bytes; select a page")
        full_text = raw_text.decode("utf-8", errors="strict")
        if not full_text.strip():
            raise ValueError("PDF has no extractable text layer; OCR is not performed")
        lines = full_text.splitlines()
        if offset > len(lines):
            raise ValueError(f"PDF text offset {offset} is beyond {len(lines)} extracted lines")
        selected: list[str] = []
        used_bytes = 0
        preview_truncated = False
        for line in lines[offset - 1 : offset - 1 + min(limit, MAX_ROWS)]:
            encoded = line.encode("utf-8")
            available = max(0, PDF_PREVIEW_BYTES - used_bytes - (1 if selected else 0))
            if len(encoded) > available:
                prefix = encoded[:available].decode("utf-8", errors="ignore")
                if prefix:
                    selected.append(prefix)
                preview_truncated = True
                break
            selected.append(line)
            used_bytes += len(encoded) + (1 if len(selected) > 1 else 0)
        next_offset = offset + len(selected) if offset - 1 + len(selected) < len(lines) else None
        return {
            "type": "pdf",
            "source_sha256": source_hash,
            "page": page,
            "line_count": len(lines),
            "offset": offset,
            "limit": min(limit, MAX_ROWS),
            "next_offset": next_offset,
            "raw_content": "\n".join(selected),
            "full_text": full_text,
            "truncated_text": preview_truncated,
            "truncated": next_offset is not None or preview_truncated,
            "partial": next_offset is not None or preview_truncated,
        }


def _hash_file(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _valid_image(data: bytes, mime: str) -> bool:
    if mime == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24 and data[12:16] == b"IHDR"
    if mime == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9")
    return mime == "image/webp" and len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
