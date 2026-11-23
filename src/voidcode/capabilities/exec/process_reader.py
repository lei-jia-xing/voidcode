"""Read the exec plugin's current, owner- and session-scoped process output."""

from __future__ import annotations

import codecs
from concurrent.futures import CancelledError
from typing import Literal
from urllib.parse import urlsplit

from voidcode.capabilities.resource_pages import MAX_RESOURCE_PAGE_BYTES
from voidcode.sdk.resources import (
    ResourcePage,
    ResourceProcessStatus,
    ResourceReadCaller,
    ResourceReader,
    ResourceReaderServices,
    ResourceReadRequest,
)
from voidcode.sdk.services import ProcessOutput
from voidcode.sdk.tools import OutputBounds, TextOutput

from .background_process import MAX_PROCESS_ROWS, _ProcessRecord, _read_records

_Stream = Literal["stdout", "stderr"]


def _check_cancelled(caller: ResourceReadCaller) -> None:
    if caller.abort_signal is not None and caller.abort_signal.cancelled:
        raise CancelledError("read cancelled")


def _parse_uri(request: ResourceReadRequest) -> tuple[str | None, _Stream | None]:
    """Return (process id, stream), or (None, None) for the inventory root."""
    if "?" in request.uri or "#" in request.uri:
        raise ValueError("proc URIs do not support query strings or fragments")
    parts = urlsplit(request.uri)
    if parts.scheme.lower() != "proc":
        raise ValueError("unsupported proc resource URI")
    if not parts.netloc:
        if parts.path in {"", "/"}:
            return None, None
        raise ValueError("proc resource URI requires an opaque process id")
    process_id = parts.netloc
    if any(character in process_id for character in ":@%/\\[]") or any(character.isspace() for character in process_id):
        raise ValueError("malformed proc process id")
    if parts.path in {"", "/"}:
        return process_id, "stdout"
    if parts.path == "/stderr":
        return process_id, "stderr"
    raise ValueError("unsupported proc resource path; use the root for stdout or /stderr for stderr")


def _inventory_page(
    request: ResourceReadRequest,
    caller: ResourceReadCaller,
    services: ResourceReaderServices,
) -> ResourcePage:
    if request.byte_offset is not None or request.byte_limit is not None:
        raise ValueError("proc inventory uses line paging, not byte offsets")
    statuses = {status.process_id: status for status in services.list_processes()}
    records, _ = _read_records(services)
    visible_records = {process_id: record for process_id, record in records.items() if record.origin_session_id == caller.session_id}

    rows: list[dict[str, object]] = []
    for _process_id, status in sorted(statuses.items()):
        rows.append(_active_row(status))
    for process_id, record in sorted(visible_records.items()):
        if process_id not in statuses:
            rows.append(_unavailable_row(record))
    rows.sort(key=lambda row: str(row["process_id"]))

    start = request.offset - 1
    limit = request.limit if request.limit is not None else MAX_PROCESS_ROWS
    page_rows = rows[start : start + limit]
    next_offset = request.offset + len(page_rows) if start + len(page_rows) < len(rows) else None
    lines = [_inventory_line(row) for row in page_rows]
    content = "Processes visible to this session:" + ("\n" + "\n".join(lines) if lines else "\n(none)")
    if next_offset is not None:
        content += f"\nNext offset: {next_offset}."
    return ResourcePage(
        output=TextOutput(content, bounds=OutputBounds(truncated=next_offset is not None, partial=next_offset is not None)),
        body={
            "type": "process_inventory",
            "path": request.uri,
            "offset": request.offset,
            "limit": limit,
            "count": len(page_rows),
            "total_count": len(rows),
            "next_offset": next_offset,
            "processes": page_rows,
        },
    )


def _active_row(status: ResourceProcessStatus) -> dict[str, object]:
    return {
        "process_id": status.process_id,
        "uri": f"proc://{status.process_id}",
        "status": "running" if status.running else "exited",
        "running": status.running,
        "exit_code": status.exit_code,
        "logs_available": True,
    }


def _unavailable_row(record: _ProcessRecord) -> dict[str, object]:
    return {
        "process_id": record.process_id,
        "uri": f"proc://{record.process_id}",
        "status": "unknown_not_supervised",
        "running": None,
        "logs_available": False,
        "last_recorded_state": record.state,
        "last_recorded_exit_code": record.exit_code,
    }


def _inventory_line(row: dict[str, object]) -> str:
    if row["status"] == "unknown_not_supervised":
        return (
            f"{row['uri']}: current liveness unknown; not supervised by this Core; "
            f"logs unavailable (last recorded state: {row['last_recorded_state']})."
        )
    line = f"{row['uri']}: {row['status']}"
    if row["exit_code"] is not None:
        line += f" (exit code {row['exit_code']})"
    return line


def _unavailable_page(request: ResourceReadRequest, process_id: str, stream: _Stream, record: _ProcessRecord) -> ResourcePage:
    content = (
        f"{request.uri}: this process is not supervised by this Core. Current liveness is unknown and "
        f"{stream} logs are unavailable. The stored state is historical only; control is not restored. "
        f"Last recorded state: {record.state}."
    )
    return ResourcePage(
        output=TextOutput(content),
        body={
            "type": "process_status",
            "path": request.uri,
            "process_id": process_id,
            "status": "unknown_not_supervised",
            "running": None,
            "last_recorded_state": record.state,
            "last_recorded_exit_code": record.exit_code,
            "logs_available": False,
        },
    )


def _decode_page(data: bytes, *, final: bool) -> tuple[Literal["utf-8", "hex"], str, int, int]:
    """Decode without replacement; return encoding, visible value, consumed bytes, pending bytes."""
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    try:
        text = decoder.decode(data, final=final)
    except UnicodeDecodeError:
        return "hex", data.hex(), len(data), 0
    pending = len(decoder.getstate()[0])
    consumed = len(data) - pending
    if data and not final and consumed == 0:
        # A too-small page cannot advance past this incomplete code point in text form.
        # Hex is exact and gives the caller a real byte cursor rather than a stuck link.
        return "hex", data.hex(), len(data), 0
    return "utf-8", text, consumed, pending


def _output_page(
    request: ResourceReadRequest,
    process_id: str,
    stream: _Stream,
    status: ResourceProcessStatus,
    output_page: ProcessOutput,
) -> ResourcePage:
    data = output_page.data
    page_offset = output_page.offset
    total_bytes = output_page.total_bytes
    page_eof = output_page.eof
    running = output_page.running
    if not page_eof and not data:
        raise ValueError("process output reader returned an empty page before the captured end")

    encoding, rendered, consumed, pending = _decode_page(data, final=page_eof and not running)
    next_byte_offset = page_offset + consumed if not page_eof else None
    pending_byte_offset = page_offset + consumed if pending else None
    current_end_offset = total_bytes
    if encoding == "hex":
        output_text = f"Output bytes (hex): {rendered}" if data else "Output bytes: (empty)"
        raw_content: str | None = None
        hex_content: str | None = rendered
    else:
        output_text = f"Output text (UTF-8):\n{rendered}" if rendered else "Output text: (empty)"
        raw_content = rendered
        hex_content = None

    content = (
        f"{request.uri} — {stream}; running={running}; exit_code={status.exit_code}; "
        f"read {len(data)} byte(s) at offset {page_offset} of {total_bytes}.\n"
        f"Captured-end page EOF: {page_eof}.\n{output_text}"
    )
    if next_byte_offset is not None:
        content += f"\nNext byte offset: {next_byte_offset}; continue with byte_offset={next_byte_offset}."
    elif page_eof and running:
        resume_offset = pending_byte_offset if pending_byte_offset is not None else current_end_offset
        content += (
            f"\nAt the current captured end (byte offset {current_end_offset}); the process is still running. "
            f"No further page is available yet. Re-read later with byte_offset={resume_offset}."
        )
    elif page_eof:
        content += "\nThis is the final captured end for the supervised process."

    partial = next_byte_offset is not None or pending > 0
    return ResourcePage(
        output=TextOutput(content, bounds=OutputBounds(truncated=next_byte_offset is not None, partial=partial)),
        body={
            "type": "process_output",
            "path": request.uri,
            "process_id": process_id,
            "stream": stream,
            "running": running,
            "exit_code": status.exit_code,
            "offset": page_offset,
            "offset_unit": "raw_bytes",
            "byte_count": len(data),
            "displayed_byte_count": consumed,
            "total_bytes": total_bytes,
            "current_end_byte_offset": current_end_offset,
            "page_eof": page_eof,
            "next_byte_offset": next_byte_offset,
            "pending_byte_offset": pending_byte_offset,
            "encoding": encoding,
            "raw_content": raw_content,
            "hex_content": hex_content,
        },
    )


def read_process_resource(
    request: ResourceReadRequest,
    caller: ResourceReadCaller,
    services: ResourceReaderServices,
) -> ResourcePage | None:
    """Read the scoped process inventory or one stdout/stderr snapshot page."""
    _check_cancelled(caller)
    process_id, stream = _parse_uri(request)
    if process_id is None:
        return _inventory_page(request, caller, services)
    if stream is None:
        raise RuntimeError("process URI did not select an output stream")
    if request.offset != 1 or request.limit is not None:
        raise ValueError("process output uses byte_offset/byte_limit, not line offsets")

    statuses = services.list_processes()
    status = next((candidate for candidate in statuses if candidate.process_id == process_id), None)
    records, _ = _read_records(services)
    record = records.get(process_id)
    if status is None:
        if record is None or record.origin_session_id != caller.session_id:
            return None
        return _unavailable_page(request, process_id, stream, record)

    _check_cancelled(caller)
    byte_offset = request.byte_offset if request.byte_offset is not None else 0
    limit = request.byte_limit if request.byte_limit is not None else MAX_RESOURCE_PAGE_BYTES
    output_page = services.read_process_output(
        process_id,
        stream=stream,
        offset=byte_offset,
        limit=limit,
    )
    _check_cancelled(caller)
    latest_status = next((candidate for candidate in services.list_processes() if candidate.process_id == process_id), status)
    return _output_page(request, process_id, stream, latest_status, output_page)


PROC_READER = ResourceReader("proc", read_process_resource)
