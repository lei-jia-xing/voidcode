from __future__ import annotations

import base64
import hashlib
import os
import re
import sys
import time
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO, cast

from ..common.output_limits import (
    MODEL_FIELD_CHARS as MAX_MODEL_FIELD_CHARS,
)
from ..common.output_limits import (
    TOOL_OUTPUT_BYTES as MAX_TOOL_OUTPUT_BYTES,
)
from ..common.output_limits import (
    TOOL_OUTPUT_LINES as MAX_TOOL_OUTPUT_LINES,
)
from ..core.tool_context import (
    ArtifactBinary,
    ArtifactInvalid,
    ArtifactPage,
    ArtifactRead,
    ArtifactUnavailable,
    NextPage,
    PageEnd,
)
from .contracts import AttachmentOutput, OutputReference, TextOutput, ToolFailure, ToolOutput, ToolResult

MAX_BINARY_READ_BYTES = 50 * 1024 * 1024  # 50MiB
MAX_ARTIFACT_PAGE_BYTES = MAX_TOOL_OUTPUT_BYTES - 4096
MAX_INLINE_ATTACHMENT_BYTES = (MAX_TOOL_OUTPUT_BYTES - 128) * 3 // 4
RASTER_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"})
_TEXT_ARTIFACT_MIMES = frozenset({"application/json", "application/xml", "application/javascript"})
_STREAM_ARTIFACT_FIELDS = ("stdout_artifact", "stderr_artifact")


def _is_text_artifact_mime(mime: str) -> bool:
    return mime.startswith("text/") or mime.startswith("image/svg+xml") or mime in _TEXT_ARTIFACT_MIMES


_INLINE_BLOB_KEYS = frozenset({"data_uri", "dataUri", "base64", "blob"})
_ARTIFACT_REFERENCE_PREFIX = "voidcode://artifact/"
_ARTIFACT_PRODUCER = "voidcode.tool_output.v1"
_ARTIFACT_ID_PATTERN = re.compile(r"^artifact_[0-9a-f]{24}$")
_ARTIFACT_CACHE_DIR_NAME = "tool-output"


def _string_summary(value: str, *, include_preview: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "omitted": True,
        "byte_count": len(value.encode("utf-8")),
        "line_count": len(value.splitlines()),
    }
    if include_preview:
        preview = value[:MAX_MODEL_FIELD_CHARS]
        payload["preview"] = preview
        payload["omitted_chars"] = max(0, len(value) - len(preview))
    return payload


def _sanitize_mapping(value: Mapping[str, object]) -> dict[str, object]:
    """Return a bounded model-visible copy of one mapping."""
    return {str(item_key): _sanitize_value(item_value, key=str(item_key)) for item_key, item_value in value.items()}


def _sanitize_value(value: object, *, key: str | None = None) -> object:
    if isinstance(value, str):
        if key in _INLINE_BLOB_KEYS:
            return _string_summary(value, include_preview=False)
        if len(value) > MAX_MODEL_FIELD_CHARS:
            return _string_summary(value, include_preview=True)
        return value
    if isinstance(value, Mapping):
        return _sanitize_mapping(value)
    if isinstance(value, list | tuple):
        return [_sanitize_value(item, key=key) for item in value]
    return value


def sanitize_tool_arguments(arguments: Mapping[str, object]) -> dict[str, object]:
    return _sanitize_mapping(arguments)


def sanitize_tool_data(data: Mapping[str, object]) -> dict[str, object]:
    return _sanitize_mapping(data)


def sanitize_tool_result_data(data: Mapping[str, object]) -> dict[str, object]:
    sanitized = sanitize_tool_data(data)
    raw_arguments = data.get("arguments")
    if isinstance(raw_arguments, Mapping):
        sanitized["arguments"] = sanitize_tool_arguments(raw_arguments)
    return sanitized


def _utf8_prefix(text: str, *, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _preview_text(content: str, *, max_lines: int, max_bytes: int) -> str:
    lines = content.splitlines(keepends=True)
    line_limited = "".join(lines[:max_lines]) if len(lines) > max_lines else content
    return _utf8_prefix(line_limited, max_bytes=max_bytes)


def _safe_artifact_segment(value: str | None, *, fallback: str = "") -> str:
    raw = value if value else fallback
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in raw)
    return safe[:96]


def _normalization_payload(
    *,
    kind: str,
    artifact_id: str,
    original_byte_count: int,
    original_line_count: int,
    max_bytes: int,
    max_lines: int,
) -> dict[str, object]:
    return {
        "kind": kind,
        "artifact_id": artifact_id,
        "original_byte_count": original_byte_count,
        "original_line_count": original_line_count,
        "max_bytes": max_bytes,
        "max_lines": max_lines,
    }


def _user_cache_root() -> Path:
    if sys.platform == "win32":
        cache_home = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if cache_home:
            return Path(cache_home) / "voidcode"
        return Path.home() / "AppData" / "Local" / "voidcode"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "voidcode"
    cache_home = os.environ.get("XDG_CACHE_HOME")
    if cache_home:
        return Path(cache_home) / "voidcode"
    return Path.home() / ".cache" / "voidcode"


def _user_state_root() -> Path:
    if sys.platform == "win32":
        return _user_cache_root()
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    if xdg_state_home:
        return Path(xdg_state_home) / "voidcode"
    return Path.home() / ".local" / "state" / "voidcode"


def session_artifacts_durable_root() -> Path:
    """Return the private per-user root for session-owned durable artifacts."""
    state_root = _user_state_root()
    root = state_root / "artifacts"
    if root.is_symlink():
        raise PermissionError(f"session artifacts root must not be a symlink: {root}")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    resolved_root = root.resolve()
    if resolved_root.parent != state_root.resolve():
        raise PermissionError(f"session artifacts root escaped state directory: {root}")
    if sys.platform != "win32":
        try:
            resolved_root.chmod(0o700)
        except OSError as exc:
            raise PermissionError(f"failed to secure session artifacts directory {resolved_root}: {exc}") from exc
    return resolved_root


def _suffix_for_mime(mime: str) -> str:
    if mime == "image/png":
        return ".png"
    if mime in ("image/jpeg", "image/jpg"):
        return ".jpg"
    if mime == "image/webp":
        return ".webp"
    if mime == "image/gif":
        return ".gif"
    if mime == "application/pdf":
        return ".pdf"
    if mime == "application/json":
        return ".json"
    return ".bin"


def tool_output_artifact_temp_root() -> Path:
    """Return the private per-user root for tool output artifacts."""

    root = _user_cache_root() / _ARTIFACT_CACHE_DIR_NAME
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def _artifact_id(
    *,
    session_id: str | None,
    tool_call_id: str | None,
    tool_name: str,
    content_hash: str,
) -> str:
    digest = hashlib.sha256(f"{session_id or ''}\0{tool_call_id or ''}\0{tool_name}\0{content_hash}".encode()).hexdigest()[:24]
    return f"artifact_{digest}"


def create_tool_output_artifact(
    *,
    session_id: str | None,
    tool_call_id: str | None,
    tool_name: str,
    content: str | bytes = "",
    source_path: Path | str | None = None,
    stream: BinaryIO | None = None,
    kind: str = "content",
    mime: str | None = None,
    source: str | None = None,
) -> dict[str, object]:
    # Boundary: returned as a plain ``dict[str, object]`` because the runtime-owned
    # ``BackgroundProcessState.stdout_artifact``/``stderr_artifact`` fields are typed
    # that way, so callers that need ``artifact_id`` recover it with a cast.
    hasher = hashlib.sha256()
    byte_count = 0
    line_count = 0
    is_text = False

    content_type = mime or (
        "text/plain; charset=utf-8" if isinstance(content, str) and not source_path and not stream else "application/octet-stream"
    )
    suffix = ".txt" if _is_text_artifact_mime(content_type) else _suffix_for_mime(content_type)
    if _is_text_artifact_mime(content_type):
        is_text = True

    # Prepare a private target directory without following a pre-existing session symlink.
    if session_id is not None:
        if not session_id:
            raise ValueError("session_id must be non-empty or None")
        session_segment = _safe_artifact_segment(session_id)
        if session_segment != session_id:
            raise ValueError("session_id contains unsupported artifact path characters")
        durable_root = session_artifacts_durable_root()
        target_dir = durable_root / session_segment
        if target_dir.is_symlink():
            raise PermissionError(f"session artifact directory must not be a symlink: {target_dir}")
        target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        target_dir = target_dir.resolve()
        if target_dir.parent != durable_root or target_dir.name != session_segment:
            raise PermissionError(f"session artifact directory escaped durable root: {target_dir}")
    else:
        target_dir = tool_output_artifact_temp_root() / "unbound"
        target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if sys.platform != "win32" and session_id is not None:
        try:
            target_dir.chmod(0o700)
        except OSError as exc:
            raise PermissionError(f"failed to secure session artifacts directory {target_dir}: {exc}") from exc

    call_segment = _safe_artifact_segment(tool_call_id, fallback="unknown-tool-call")
    tool_segment = _safe_artifact_segment(tool_name)

    if source_path is not None or stream is not None:
        # Streaming path: stream in 64KiB chunks to a temporary file, compute hash incrementally
        temp_file = target_dir / f".tmp-{call_segment}-{tool_segment}-{os.getpid()}_{time.time_ns()}{suffix}"
        fd = os.open(temp_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as out_fh:
                if source_path is not None:
                    src_p = Path(source_path)
                    with src_p.open("rb") as in_fh:
                        for chunk in iter(lambda: in_fh.read(65536), b""):
                            hasher.update(chunk)
                            byte_count += len(chunk)
                            if is_text:
                                line_count += chunk.count(b"\n")
                            out_fh.write(chunk)
                elif stream is not None:
                    for chunk in iter(lambda: stream.read(65536), b""):
                        hasher.update(chunk)
                        byte_count += len(chunk)
                        if is_text:
                            line_count += chunk.count(b"\n")
                        out_fh.write(chunk)
        except Exception:
            temp_file.unlink(missing_ok=True)
            raise

        content_hash = hasher.hexdigest()
        artifact_id = _artifact_id(
            session_id=session_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            content_hash=content_hash,
        )
        final_path = target_dir / f"{call_segment}-{tool_segment}-{artifact_id}{suffix}"
        temp_file.replace(final_path)
        if sys.platform != "win32" and session_id:
            try:
                final_path.chmod(0o600)
            except OSError as exc:
                raise PermissionError(f"failed to secure artifact file {final_path}: {exc}") from exc
    else:
        if isinstance(content, str):
            encoded = content.encode("utf-8")
            is_text = True
            line_count = len(content.splitlines())
            content_type = mime or "text/plain; charset=utf-8"
            suffix = ".txt"
        else:
            encoded = content
            is_text = False
            line_count = 0
            content_type = mime or "application/octet-stream"
            suffix = _suffix_for_mime(content_type)
        hasher.update(encoded)
        content_hash = hasher.hexdigest()
        byte_count = len(encoded)
        artifact_id = _artifact_id(
            session_id=session_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            content_hash=content_hash,
        )
        final_path = target_dir / f"{call_segment}-{tool_segment}-{artifact_id}{suffix}"
        fd = os.open(final_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        if is_text:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out_fh:
                _ = out_fh.write(cast(str, content))
        else:
            with os.fdopen(fd, "wb") as out_fh:
                _ = out_fh.write(encoded)
        if sys.platform != "win32" and session_id:
            try:
                final_path.chmod(0o600)
            except OSError as exc:
                raise PermissionError(f"failed to secure artifact file {final_path}: {exc}") from exc

    payload: dict[str, object] = {
        "artifact_id": artifact_id,
        "producer": _ARTIFACT_PRODUCER,
        "session_id": session_id,
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "kind": kind,
        "byte_count": byte_count,
        "line_count": line_count,
        "sha256": content_hash,
        "content_type": content_type,
        "mime": content_type,
        "created_at": int(time.time() * 1000),
        "path": str(final_path),
        "uri": f"{_ARTIFACT_REFERENCE_PREFIX}{artifact_id}",
        "status": "available",
    }
    if source is not None:
        payload["source"] = source
    return payload


def _artifact_missing_payload(artifact: Mapping[str, object]) -> dict[str, object]:
    return {
        "artifact_id": artifact.get("artifact_id"),
        "status": "missing",
        "artifact_missing": True,
        "path": artifact.get("path"),
    }


def _artifact_invalid_payload(artifact: Mapping[str, object]) -> dict[str, object]:
    return {
        "artifact_id": artifact.get("artifact_id"),
        "status": "invalid",
        "artifact_missing": True,
    }


def _artifact_path_from_metadata(artifact: Mapping[str, object]) -> Path | None:
    producer = artifact.get("producer")
    if producer != _ARTIFACT_PRODUCER:
        return None
    artifact_id = artifact.get("artifact_id")
    if not isinstance(artifact_id, str) or _ARTIFACT_ID_PATTERN.fullmatch(artifact_id) is None:
        return None
    session_id = artifact.get("session_id")
    if isinstance(session_id, str) and session_id:
        session_segment = _safe_artifact_segment(session_id)
        if session_segment != session_id:
            return None
        try:
            durable_root = session_artifacts_durable_root()
        except OSError, PermissionError:
            return None
        raw_session_dir = durable_root / session_segment
        if raw_session_dir.is_symlink():
            return None
        session_dir = raw_session_dir.resolve()
        if session_dir.parent != durable_root or session_dir.name != session_segment or not session_dir.is_relative_to(durable_root):
            return None
        raw_path = artifact.get("path")
        if isinstance(raw_path, str) and raw_path != "":
            try:
                raw_candidate = Path(raw_path)
                candidate = raw_candidate.resolve()
                if (
                    not raw_candidate.is_symlink()
                    and candidate.parent == session_dir
                    and candidate.is_relative_to(session_dir)
                    and f"-{artifact_id}" in candidate.name
                ):
                    return candidate
            except OSError:
                pass
        if session_dir.is_dir():
            for child in session_dir.iterdir():
                if child.is_symlink():
                    continue
                try:
                    resolved = child.resolve()
                    if resolved.is_file() and resolved.parent == session_dir and f"-{artifact_id}" in resolved.name:
                        return resolved
                except OSError:
                    continue
        return None

    if session_id is not None:
        # Invalid typed session ID must NOT fall back to unbound cache.
        return None

    # Unbound / sessionless path (only allowed when session_id is strictly None).
    temp_root = tool_output_artifact_temp_root().resolve()
    unbound_dir = (temp_root / "unbound").resolve()
    if unbound_dir.parent != temp_root or not unbound_dir.is_relative_to(temp_root):
        return None
    raw_path = artifact.get("path")
    if isinstance(raw_path, str) and raw_path != "":
        try:
            raw_candidate = Path(raw_path)
            candidate = raw_candidate.resolve()
            if not raw_candidate.is_symlink() and candidate.parent == unbound_dir and f"-{artifact_id}" in candidate.name:
                return candidate
        except OSError:
            pass
    if unbound_dir.is_dir():
        for child in unbound_dir.iterdir():
            if child.is_symlink():
                continue
            try:
                resolved = child.resolve()
                if resolved.is_file() and resolved.parent == unbound_dir and f"-{artifact_id}" in resolved.name:
                    return resolved
            except OSError:
                continue
    return None


def _line_count(path: Path) -> int:
    count = 0
    with path.open(encoding="utf-8") as artifact_file:
        for _line in artifact_file:
            count += 1
    return count


def _event_payload(event: object) -> Mapping[str, object] | None:
    if isinstance(event, Mapping):
        payload = event.get("payload")
    else:
        payload = getattr(event, "payload", None)
    if not isinstance(payload, Mapping):
        return None
    return payload


def _walk_candidate_artifact(val: Mapping[str, object]) -> Iterator[Mapping[str, object]]:
    yield val
    nested = val.get("artifact")
    if isinstance(nested, Mapping):
        yield nested
    for k in ("error_artifact", "output_artifact", "body_artifact", "source_artifact", *_STREAM_ARTIFACT_FIELDS):
        sub = val.get(k)
        if isinstance(sub, Mapping):
            yield sub
            sub_nested = sub.get("artifact")
            if isinstance(sub_nested, Mapping):
                yield sub_nested
    sub_list = val.get("artifacts")
    if isinstance(sub_list, list | tuple):
        for item in sub_list:
            if isinstance(item, Mapping):
                yield item
                item_nested = item.get("artifact")
                if isinstance(item_nested, Mapping):
                    yield item_nested


def iter_event_artifact_references(payload: Mapping[str, object]) -> Iterator[Mapping[str, object]]:
    """Yield exact canonical refs first, then their bounded public projections."""
    reported = payload.get("reported_call")
    if isinstance(reported, Mapping):
        result = reported.get("result")
        if isinstance(result, Mapping):
            output = result.get("output")
            if isinstance(output, Mapping):
                bounds = output.get("bounds")
                if isinstance(bounds, Mapping):
                    reference = bounds.get("reference")
                    if isinstance(reference, Mapping):
                        artifact = reference.get("artifact")
                        if isinstance(artifact, Mapping):
                            yield from _walk_candidate_artifact(artifact)
            body = result.get("body")
            if isinstance(body, Mapping):
                for key in ("artifact", "error_artifact", "output_artifact", "body_artifact", "source_artifact", *_STREAM_ARTIFACT_FIELDS):
                    artifact = body.get(key)
                    if isinstance(artifact, Mapping):
                        yield from _walk_candidate_artifact(artifact)
                artifacts = body.get("artifacts")
                if isinstance(artifacts, list | tuple):
                    for item in artifacts:
                        if isinstance(item, Mapping):
                            yield from _walk_candidate_artifact(item)
    for key in ("artifact", "error_artifact", "output_artifact", "body_artifact", "source_artifact", *_STREAM_ARTIFACT_FIELDS):
        artifact = payload.get(key)
        if isinstance(artifact, Mapping):
            yield from _walk_candidate_artifact(artifact)
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, list | tuple):
        for item in artifacts:
            if isinstance(item, Mapping):
                yield from _walk_candidate_artifact(item)


def resolve_tool_output_artifact(
    events: object,
    *,
    artifact_id: str | None = None,
    tool_call_id: str | None = None,
) -> dict[str, object] | None:
    """Resolve runtime-generated artifact metadata from event payloads by id or tool call."""

    if artifact_id is None and tool_call_id is None:
        raise ValueError("artifact_id or tool_call_id is required")
    if not isinstance(events, list | tuple):
        return None
    for event in events:
        payload = _event_payload(event)
        if payload is None:
            continue
        for artifact_mapping in iter_event_artifact_references(payload):
            if artifact_id is not None and artifact_mapping.get("artifact_id") != artifact_id:
                continue
            if tool_call_id is not None and artifact_mapping.get("tool_call_id") != tool_call_id:
                continue
            artifact_dict = dict(artifact_mapping)
            if _artifact_path_from_metadata(artifact_dict) is None:
                continue
            return artifact_dict
    return None


def _artifact_integrity(path: Path, artifact: Mapping[str, object]) -> tuple[int, str, int] | ArtifactInvalid | ArtifactUnavailable:
    expected_bytes = artifact.get("byte_count")
    expected_sha = artifact.get("sha256")
    if type(expected_bytes) is not int or expected_bytes < 0:
        return ArtifactInvalid("artifact byte count metadata is invalid")
    if not isinstance(expected_sha, str) or re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None:
        return ArtifactInvalid("artifact checksum metadata is invalid")
    try:
        size = path.stat().st_size
        if size > MAX_BINARY_READ_BYTES:
            return ArtifactInvalid(f"artifact exceeds maximum supported binary size ({MAX_BINARY_READ_BYTES} bytes)")
        if size != expected_bytes:
            return ArtifactInvalid("artifact byte count mismatch")
        hasher = hashlib.sha256()
        byte_count = 0
        newline_count = 0
        last_byte = b""
        with path.open("rb") as artifact_file:
            for chunk in iter(lambda: artifact_file.read(65536), b""):
                hasher.update(chunk)
                byte_count += len(chunk)
                newline_count += chunk.count(b"\n")
                last_byte = chunk[-1:]
        actual_sha = hasher.hexdigest()
    except OSError as exc:
        return ArtifactUnavailable(f"failed to verify artifact: {exc}")
    if byte_count != expected_bytes:
        return ArtifactInvalid("artifact byte count mismatch")
    if actual_sha != expected_sha:
        return ArtifactInvalid("artifact content has been tampered with or corrupted")
    line_count = newline_count + int(byte_count > 0 and last_byte != b"\n")
    return byte_count, actual_sha, line_count


def _binary_artifact(
    path: Path,
    artifact: Mapping[str, object],
    *,
    artifact_id: str,
    mime: str,
    source: str | None,
) -> ArtifactBinary | ArtifactInvalid | ArtifactUnavailable:
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        return ArtifactUnavailable(f"failed to read artifact bytes: {exc}")
    actual_sha = hashlib.sha256(raw_bytes).hexdigest()
    expected_sha = artifact.get("sha256")
    expected_bytes = artifact.get("byte_count")
    if len(raw_bytes) > MAX_BINARY_READ_BYTES:
        return ArtifactInvalid(f"artifact exceeds maximum supported binary size ({MAX_BINARY_READ_BYTES} bytes)")
    if type(expected_bytes) is not int or len(raw_bytes) != expected_bytes:
        return ArtifactInvalid("artifact byte count mismatch")
    if not isinstance(expected_sha, str) or actual_sha != expected_sha:
        return ArtifactInvalid("artifact content has been tampered with or corrupted")
    data_uri = None
    if len(raw_bytes) <= MAX_INLINE_ATTACHMENT_BYTES and (mime in RASTER_IMAGE_MIMES or mime == "application/pdf"):
        data_uri = f"data:{mime};base64,{base64.b64encode(raw_bytes).decode('ascii')}"
    return ArtifactBinary(
        artifact_id=artifact_id,
        mime=mime,
        byte_count=len(raw_bytes),
        sha256=actual_sha,
        data=raw_bytes,
        data_uri=data_uri,
        source=source,
        artifact=artifact,
    )


def _read_utf8_byte_page(path: Path, *, offset: int, limit: int, file_size: int) -> tuple[str, int | None]:
    if offset > file_size:
        raise ValueError("byte_offset is beyond the end of the artifact")
    with path.open("rb") as artifact_file:
        if offset:
            artifact_file.seek(offset)
            first = artifact_file.read(1)
            if first and first[0] & 0xC0 == 0x80:
                raise ValueError("byte_offset must be at a UTF-8 character boundary")
        artifact_file.seek(offset)
        sample = artifact_file.read(limit + 4)
    end = min(limit, len(sample))
    while end:
        try:
            text = sample[:end].decode("utf-8")
            break
        except UnicodeDecodeError as exc:
            if exc.reason != "unexpected end of data" or exc.end != end:
                raise
            end = exc.start
    else:
        text = ""
    if end == 0 and offset < file_size:
        raise ValueError("byte_limit cannot fit the next UTF-8 character")
    next_offset = offset + end if offset + end < file_size else None
    return text, next_offset


def _read_text_line_page(
    path: Path,
    *,
    offset: int,
    limit: int,
    line_count: int,
) -> tuple[bytes, int | None, int | None]:
    selected = bytearray()
    line_index = 0
    position = 0
    try:
        with path.open("rb") as artifact_file:
            while line_index < offset:
                chunk = artifact_file.readline(65536)
                if not chunk:
                    return b"", None, None
                position += len(chunk)
                while not chunk.endswith(b"\n"):
                    chunk = artifact_file.readline(65536)
                    if not chunk:
                        break
                    position += len(chunk)
                line_index += 1

            selected_lines = 0
            while selected_lines < limit and line_index < line_count:
                line_start = position
                line = bytearray()
                while True:
                    chunk = artifact_file.readline(MAX_ARTIFACT_PAGE_BYTES - len(line) + 1)
                    if not chunk:
                        break
                    line.extend(chunk)
                    position += len(chunk)
                    if len(line) > MAX_ARTIFACT_PAGE_BYTES:
                        if selected:
                            return bytes(selected), line_index, None
                        return b"", None, line_start
                    if chunk.endswith(b"\n"):
                        break
                if not line:
                    break
                if len(selected) + len(line) > MAX_ARTIFACT_PAGE_BYTES:
                    return bytes(selected), line_index, None
                selected.extend(line)
                selected_lines += 1
                line_index += 1
    except OSError as exc:
        raise RuntimeError(f"failed to read artifact text: {exc}") from exc
    next_line = line_index if line_index < line_count else None
    return bytes(selected), next_line, None


def read_tool_output_artifact(
    artifact: Mapping[str, object],
    *,
    offset: int = 0,
    limit: int = 2000,
    byte_offset: int | None = None,
    byte_limit: int | None = None,
) -> ArtifactRead:
    """Read an integrity-checked bounded line or UTF-8 byte page."""

    if type(offset) is not int or offset < 0 or type(limit) is not int or limit < 0:
        return ArtifactInvalid("artifact page offsets and limits must be non-negative integers")
    if byte_offset is not None and (type(byte_offset) is not int or byte_offset < 0):
        return ArtifactInvalid("byte_offset must be a non-negative integer")
    if byte_offset is None and byte_limit is not None:
        return ArtifactInvalid("byte_limit requires byte_offset")
    if byte_offset is not None and byte_limit is not None and (type(byte_limit) is not int or not 4 <= byte_limit <= MAX_ARTIFACT_PAGE_BYTES):
        return ArtifactInvalid(f"byte_limit must be between 4 and {MAX_ARTIFACT_PAGE_BYTES}")

    path = _artifact_path_from_metadata(artifact)
    if path is None:
        return ArtifactInvalid("artifact metadata is invalid")
    if path.is_symlink():
        return ArtifactInvalid("artifact path must not be a symlink")
    if not path.is_file():
        return ArtifactUnavailable("artifact is missing from storage")

    mime_value = artifact.get("mime", artifact.get("content_type"))
    if not isinstance(mime_value, str) or not mime_value:
        return ArtifactInvalid("artifact MIME metadata is invalid")
    content_type = artifact.get("content_type")
    if content_type is not None and content_type != mime_value:
        return ArtifactInvalid("artifact MIME metadata disagrees")
    effective_mime = mime_value
    artifact_id = artifact.get("artifact_id")
    assert isinstance(artifact_id, str)
    source_value = artifact.get("source")
    source = source_value if isinstance(source_value, str) else None

    if limit == 0 and byte_offset is None:
        verified = _artifact_integrity(path, artifact)
        if isinstance(verified, ArtifactInvalid | ArtifactUnavailable):
            return verified
        byte_count, actual_sha, _ = verified
        return ArtifactBinary(artifact_id, effective_mime, byte_count, actual_sha, source=source, artifact=artifact)

    is_binary = not _is_text_artifact_mime(effective_mime)
    if not is_binary:
        try:
            with path.open("rb") as artifact_file:
                is_binary = b"\x00" in artifact_file.read(1024)
        except OSError as exc:
            return ArtifactUnavailable(f"failed to inspect artifact: {exc}")
    if is_binary:
        return _binary_artifact(path, artifact, artifact_id=artifact_id, mime=effective_mime, source=source)

    verified = _artifact_integrity(path, artifact)
    if isinstance(verified, ArtifactInvalid | ArtifactUnavailable):
        return verified
    byte_count, actual_sha, total_lines = verified
    bounded_limit = min(limit, MAX_TOOL_OUTPUT_LINES)
    actual_byte_limit = MAX_ARTIFACT_PAGE_BYTES if byte_limit is None else byte_limit
    try:
        if byte_offset is not None:
            text, next_byte_offset = _read_utf8_byte_page(
                path,
                offset=byte_offset,
                limit=actual_byte_limit,
                file_size=byte_count,
            )
            page = ArtifactPage(
                artifact_id,
                text,
                total_lines,
                0,
                bounded_limit,
                PageEnd(),
                byte_offset=byte_offset,
                byte_limit=actual_byte_limit,
                next_byte_offset=next_byte_offset,
                sha256=actual_sha,
                byte_count=byte_count,
                mime=effective_mime,
                source=source,
                artifact=artifact,
            )
            return page
        raw_text, next_line, long_line_offset = _read_text_line_page(
            path,
            offset=offset,
            limit=bounded_limit,
            line_count=total_lines,
        )
        if long_line_offset is not None:
            text, next_byte_offset = _read_utf8_byte_page(
                path,
                offset=long_line_offset,
                limit=actual_byte_limit,
                file_size=byte_count,
            )
            return ArtifactPage(
                artifact_id,
                text,
                total_lines,
                offset,
                bounded_limit,
                PageEnd(),
                byte_offset=long_line_offset,
                byte_limit=actual_byte_limit,
                next_byte_offset=next_byte_offset,
                sha256=actual_sha,
                byte_count=byte_count,
                mime=effective_mime,
                source=source,
                artifact=artifact,
            )
        text = raw_text.decode("utf-8")
    except UnicodeDecodeError:
        return _binary_artifact(path, artifact, artifact_id=artifact_id, mime=effective_mime, source=source)
    except ValueError as exc:
        return ArtifactInvalid(str(exc))
    except RuntimeError as exc:
        return ArtifactUnavailable(str(exc))

    return ArtifactPage(
        artifact_id,
        text,
        total_lines,
        offset,
        bounded_limit,
        PageEnd() if next_line is None else NextPage(next_line),
        sha256=actual_sha,
        byte_count=byte_count,
        mime=effective_mime,
        source=source,
        artifact=artifact,
    )


def search_tool_output_artifact(
    artifact: Mapping[str, object],
    *,
    pattern: str,
    case_sensitive: bool = False,
    limit: int = 100,
) -> dict[str, object]:
    """Search a temp-backed tool output artifact and return bounded matching lines."""

    path = _artifact_path_from_metadata(artifact)
    if path is None:
        return _artifact_invalid_payload(artifact)
    if not path.is_file():
        return _artifact_missing_payload(artifact)
    verified = _artifact_integrity(path, artifact)
    if isinstance(verified, ArtifactInvalid):
        return _artifact_invalid_payload(artifact)
    if isinstance(verified, ArtifactUnavailable):
        return _artifact_missing_payload(artifact)

    mime_value = artifact.get("mime", artifact.get("content_type"))
    if not isinstance(mime_value, str) or not mime_value:
        return _artifact_invalid_payload(artifact)
    mime = mime_value
    if not _is_text_artifact_mime(mime):
        return {
            "artifact_id": artifact.get("artifact_id"),
            "status": "available",
            "artifact_missing": False,
            "pattern": pattern,
            "case_sensitive": case_sensitive,
            "match_count": 0,
            "matches": [],
            "binary": True,
        }

    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        return {
            "artifact_id": artifact.get("artifact_id"),
            "status": "invalid",
            "artifact_missing": False,
            "error": str(exc),
            "match_count": 0,
            "matches": [],
        }
    matches: list[dict[str, object]] = []
    try:
        with path.open(encoding="utf-8") as artifact_file:
            for line_number, line in enumerate(artifact_file, start=1):
                line_text = line.rstrip("\n")
                if regex.search(line_text):
                    matches.append({"line_number": line_number, "line": line_text})
                    if len(matches) >= max(0, limit):
                        break
    except UnicodeDecodeError:
        return {
            "artifact_id": artifact.get("artifact_id"),
            "status": "available",
            "artifact_missing": False,
            "pattern": pattern,
            "match_count": 0,
            "matches": [],
            "binary": True,
        }
    return {
        "artifact_id": artifact.get("artifact_id"),
        "status": "available",
        "artifact_missing": False,
        "pattern": pattern,
        "match_count": len(matches),
        "matches": matches,
    }


def cap_tool_result_output(
    result: ToolResult,
    *,
    session_id: str | None = None,
    tool_call_id: str | None = None,
    max_lines: int = MAX_TOOL_OUTPUT_LINES,
    max_bytes: int = MAX_TOOL_OUTPUT_BYTES,
) -> ToolResult:
    """Cap text/attachment output and error independently, retaining full content in session-owned artifacts."""

    def spill_text(content: str, *, kind: str, reference: OutputReference | None = None) -> tuple[str, OutputReference] | None:
        byte_count = len(content.encode("utf-8"))
        line_count = len(content.splitlines())
        if byte_count <= max_bytes and line_count <= max_lines:
            return None
        if reference is None or not reference.uri.startswith(_ARTIFACT_REFERENCE_PREFIX):
            artifact = create_tool_output_artifact(
                session_id=session_id,
                tool_call_id=tool_call_id,
                tool_name=result.tool_name,
                content=content,
                kind=kind,
            )
            artifact_id = cast(str, artifact["artifact_id"])
            uri = f"{_ARTIFACT_REFERENCE_PREFIX}{artifact_id}"
            reference = OutputReference(uri=uri, artifact=artifact)
        label = "error" if kind == "error" else "output"
        uri = reference.uri

        def hint(omitted_bytes: int, omitted_lines: int, prefix: str) -> str:
            return (
                f"{prefix}[Tool {label} truncated: omitted {omitted_bytes} bytes and {omitted_lines} lines. "
                f'Artifact ID={uri.rsplit("/", 1)[-1]}. Read the full {label} with read(path="{uri}").]'
            )

        preview = _preview_text(content, max_lines=max_lines, max_bytes=max_bytes)
        prefix = "\n" if preview and not preview.endswith(("\n", "\r")) else ""
        hint_fits = (
            len(hint(byte_count, line_count, prefix).encode("utf-8")) <= max_bytes
            and len(hint(byte_count, line_count, prefix).splitlines()) <= max_lines
        )
        if hint_fits:
            # Reserve the hint in the same budget; its omitted counts can change its size.
            for _ in range(max_lines + 1):
                prefix = "\n" if preview and not preview.endswith(("\n", "\r")) else ""
                omitted_bytes = max(0, byte_count - len(preview.encode("utf-8")))
                omitted_lines = max(0, line_count - len(preview.splitlines()))
                suffix = hint(omitted_bytes, omitted_lines, prefix)
                if len((preview + suffix).encode("utf-8")) <= max_bytes and len((preview + suffix).splitlines()) <= max_lines:
                    break
                if len(preview.encode("utf-8")) + len(suffix.encode("utf-8")) > max_bytes:
                    preview = _utf8_prefix(preview, max_bytes=max(0, max_bytes - len(suffix.encode("utf-8"))))
                else:
                    preview_lines = preview.splitlines(keepends=True)
                    preview = "".join(preview_lines[:-1]) if preview_lines else ""
            prefix = "\n" if preview and not preview.endswith(("\n", "\r")) else ""
            omitted_bytes = max(0, byte_count - len(preview.encode("utf-8")))
            omitted_lines = max(0, line_count - len(preview.splitlines()))
            text = preview + hint(omitted_bytes, omitted_lines, prefix)
        else:
            text = preview
        source_metadata = dict(reference.artifact or {})
        nested_artifact = source_metadata.get("artifact")
        metadata = dict(nested_artifact) if isinstance(nested_artifact, Mapping) else dict(source_metadata)
        metadata["artifact"] = dict(nested_artifact) if isinstance(nested_artifact, Mapping) else dict(source_metadata)
        artifact_id = metadata.get("artifact_id", uri.rsplit("/", 1)[-1])
        retry_guidance = f'Read the full {label} with read(path="{uri}").'
        metadata.update(
            {
                "normalization": _normalization_payload(
                    kind="tool_error_truncated" if kind == "error" else "tool_output_truncated",
                    artifact_id=cast(str, artifact_id),
                    original_byte_count=byte_count,
                    original_line_count=line_count,
                    max_bytes=max_bytes,
                    max_lines=max_lines,
                ),
                "diagnostics": [
                    {
                        "source": "tool_output",
                        "severity": "warning",
                        "reason": "tool_error_truncated" if kind == "error" else "tool_output_truncated",
                        "message": f"Tool {label} was truncated before being sent to the model.",
                        "retry_guidance": retry_guidance,
                    }
                ],
                "retry_guidance": retry_guidance,
                "artifact_id": artifact_id,
                "artifact_status": "available",
                "output_path": metadata.get("path"),
                "tool_output_max_bytes": max_bytes,
                "tool_output_max_lines": max_lines,
            }
        )
        if kind == "error":
            metadata["original_error_byte_count"] = byte_count
            metadata["original_error_line_count"] = line_count
        else:
            metadata["original_byte_count"] = byte_count
            metadata["original_line_count"] = line_count
        return text, OutputReference(uri=uri, artifact=metadata)

    def spill_attachment(output: AttachmentOutput) -> tuple[AttachmentOutput, OutputReference | None] | None:
        raw_bytes: bytes | None = None
        mime = output.mime
        if output.data_uri is not None and output.data_uri.startswith("data:"):
            try:
                header, encoded_data = output.data_uri.split(",", 1)
                raw_bytes = base64.b64decode(encoded_data, validate=True)
                if ";" in header:
                    mime = header.split(";", 1)[0].removeprefix("data:")
            except Exception:
                raw_bytes = None
        invalid_data_uri = output.data_uri is not None and raw_bytes is None
        reference = output.bounds.reference
        presentation = output.presentation
        if invalid_data_uri:
            presentation = (
                "[Invalid inline attachment data URI; using its artifact reference.]"
                if reference is not None
                else "[Attachment unavailable: invalid data URI.]"
            )
        presentation_reference = None
        presentation_too_large = presentation is not None and (
            len(presentation.encode("utf-8")) > max_bytes or len(presentation.splitlines()) > max_lines
        )
        if presentation_too_large:
            capped_presentation = spill_text(presentation, kind="content")
            if capped_presentation is not None:
                presentation, presentation_reference = capped_presentation
        changed = invalid_data_uri or presentation_too_large

        def keep_presentation_artifact(media_reference: OutputReference | None) -> OutputReference | None:
            if presentation_reference is None:
                return media_reference
            if media_reference is None or media_reference.artifact is None:
                return presentation_reference if media_reference is None else media_reference
            metadata = dict(media_reference.artifact)
            media_artifact = metadata.get("artifact")
            if not isinstance(media_artifact, Mapping):
                media_artifact = dict(metadata)
                metadata["artifact"] = media_artifact
            presentation_metadata = dict(presentation_reference.artifact or {})
            presentation_artifact = presentation_metadata.get("artifact", presentation_metadata)
            metadata.setdefault("source_artifact", media_artifact)
            metadata["presentation_artifact"] = presentation_artifact
            existing_artifacts = metadata.get("artifacts")
            artifacts = list(existing_artifacts) if isinstance(existing_artifacts, list | tuple) else []
            for item in (media_artifact, presentation_artifact):
                if isinstance(item, Mapping) and item not in artifacts:
                    artifacts.append(item)
            metadata["artifacts"] = artifacts
            return OutputReference(uri=media_reference.uri, artifact=metadata)

        inline_ok = raw_bytes is not None and len(raw_bytes) <= min(max_bytes, MAX_INLINE_ATTACHMENT_BYTES) and mime in RASTER_IMAGE_MIMES
        data_uri = output.data_uri if inline_ok else None
        if output.data_uri is not None and not inline_ok:
            changed = True
        if presentation is not None and (len(presentation.encode("utf-8")) > max_bytes or len(presentation.splitlines()) > max_lines):
            presentation = _preview_text(presentation, max_lines=max_lines, max_bytes=max_bytes)
        reference = keep_presentation_artifact(reference)
        if reference is not None and reference.artifact is not None:
            if not changed:
                return None
            return (
                AttachmentOutput(
                    mime=mime,
                    data_uri=data_uri,
                    presentation=presentation,
                    bounds=replace(output.bounds, truncated=True, partial=True, reference=reference),
                ),
                reference,
            )
        if raw_bytes is None or inline_ok:
            if not changed:
                return None
            return (
                AttachmentOutput(
                    mime=mime,
                    data_uri=data_uri,
                    presentation=presentation,
                    bounds=replace(
                        output.bounds,
                        truncated=True,
                        partial=True,
                        reference=reference,
                        fallback_reason=("invalid_attachment_data_uri" if invalid_data_uri and reference is None else output.bounds.fallback_reason),
                    ),
                ),
                reference,
            )
        artifact = create_tool_output_artifact(
            session_id=session_id,
            tool_call_id=tool_call_id,
            tool_name=result.tool_name,
            content=raw_bytes,
            kind="attachment",
            mime=mime,
            source=output.bounds.source,
        )
        artifact_id = cast(str, artifact["artifact_id"])
        uri = f"{_ARTIFACT_REFERENCE_PREFIX}{artifact_id}"
        label = "Image" if mime in RASTER_IMAGE_MIMES else "PDF" if mime == "application/pdf" else "Attachment"
        preview = presentation or (f'[{label} ({mime}, {len(raw_bytes)} bytes). Artifact ID={artifact_id}. Read full data with read(path="{uri}").]')
        if len(preview.encode("utf-8")) > max_bytes or len(preview.splitlines()) > max_lines:
            preview = _preview_text(preview, max_lines=max_lines, max_bytes=max_bytes)
        reference = keep_presentation_artifact(OutputReference(uri=uri, artifact=artifact))
        return (
            AttachmentOutput(
                mime=mime,
                data_uri=None,
                presentation=preview,
                bounds=replace(output.bounds, truncated=True, partial=True, reference=reference),
            ),
            reference,
        )

    capped_error = spill_text(result.error, kind="error") if isinstance(result, ToolFailure) else None
    existing_ref = result.output.bounds.reference
    capped_txt = spill_text(result.output.text, kind="content", reference=existing_ref) if isinstance(result.output, TextOutput) else None
    capped_output: tuple[ToolOutput, OutputReference | None] | None = None
    if isinstance(result.output, TextOutput):
        if capped_txt is not None:
            txt, ref = capped_txt
            metadata = dict(ref.artifact or {})
            body = result.body
            payload = body if isinstance(body, Mapping) else getattr(body, "structured_content", None)
            if isinstance(payload, Mapping):
                body_artifacts: list[Mapping[str, object]] = []
                for key in ("source_artifact", "body_artifact", "output_artifact", "result_artifact", *_STREAM_ARTIFACT_FIELDS):
                    candidate = payload.get(key)
                    if isinstance(candidate, Mapping) and candidate not in body_artifacts:
                        body_artifacts.append(candidate)
                listed = payload.get("artifacts")
                if isinstance(listed, list | tuple):
                    for candidate in listed:
                        if isinstance(candidate, Mapping) and candidate not in body_artifacts:
                            body_artifacts.append(candidate)
                if body_artifacts:
                    metadata.setdefault("source_artifact", body_artifacts[0])
                    metadata["body_artifact"] = body_artifacts[0]
                    previous = metadata.get("artifacts")
                    artifacts = list(previous) if isinstance(previous, list | tuple) else []
                    for candidate in (*body_artifacts, metadata.get("artifact")):
                        if isinstance(candidate, Mapping) and candidate not in artifacts:
                            artifacts.append(candidate)
                    metadata["artifacts"] = artifacts
                    ref = OutputReference(uri=ref.uri, artifact=metadata)
            new_bounds = replace(result.output.bounds, truncated=True, partial=True, reference=ref)
            capped_output = (replace(result.output, text=txt, bounds=new_bounds), ref)
        elif result.output.presentation is not None and (
            len(result.output.presentation.encode("utf-8")) > max_bytes or len(result.output.presentation.splitlines()) > max_lines
        ):
            capped_presentation = spill_text(
                result.output.presentation,
                kind="content",
                reference=existing_ref,
            )
            if capped_presentation is not None:
                presentation, ref = capped_presentation
                capped_output = (
                    replace(
                        result.output,
                        presentation=presentation,
                        bounds=replace(result.output.bounds, truncated=True, partial=True, reference=ref),
                    ),
                    ref,
                )
    elif isinstance(result.output, AttachmentOutput):
        capped_output = spill_attachment(result.output)

    if capped_error is None and capped_output is None:
        return result
    if isinstance(result, ToolFailure):
        final_error = capped_error[0] if capped_error is not None else result.error
        final_output = capped_output[0] if capped_output is not None else result.output
        if capped_error is not None:
            primary_ref = capped_error[1]
            output_ref = capped_output[1] if capped_output is not None and capped_output[1] is not None else result.output.bounds.reference
            error_meta = dict(primary_ref.artifact or {})
            error_artifact = error_meta.get("artifact")
            output_meta = dict(output_ref.artifact or {}) if output_ref is not None else {}
            output_artifact = output_meta.get("artifact", output_meta) if output_meta else None
            body = result.body
            payload = body if isinstance(body, Mapping) else getattr(body, "structured_content", None)
            body_artifacts: list[Mapping[str, object]] = []
            if isinstance(payload, Mapping):
                for key in ("source_artifact", "body_artifact", "output_artifact", "result_artifact", *_STREAM_ARTIFACT_FIELDS):
                    candidate = payload.get(key)
                    if isinstance(candidate, Mapping) and candidate not in body_artifacts:
                        body_artifacts.append(candidate)
                listed = payload.get("artifacts")
                if isinstance(listed, list | tuple):
                    body_artifacts.extend(item for item in listed if isinstance(item, Mapping) and item not in body_artifacts)
            if output_artifact is None and body_artifacts:
                output_artifact = body_artifacts[0]
            if isinstance(error_artifact, Mapping):
                error_meta["error_artifact"] = error_artifact
            if isinstance(output_artifact, Mapping):
                error_meta["output_artifact"] = output_artifact
            source_artifact = output_meta.get("source_artifact")
            if not isinstance(source_artifact, Mapping):
                source_artifact = output_artifact
            if isinstance(source_artifact, Mapping):
                error_meta["source_artifact"] = source_artifact
            body_artifact = output_meta.get("body_artifact")
            if not isinstance(body_artifact, Mapping) and body_artifacts:
                body_artifact = body_artifacts[0]
            if isinstance(body_artifact, Mapping):
                error_meta["body_artifact"] = body_artifact
            artifacts: list[Mapping[str, object]] = []
            for raw_artifacts in (output_meta.get("artifacts"), body_artifacts):
                if isinstance(raw_artifacts, list | tuple):
                    artifacts.extend(item for item in raw_artifacts if isinstance(item, Mapping) and item not in artifacts)
            for item in (source_artifact, body_artifact, output_artifact, error_artifact):
                if isinstance(item, Mapping) and item not in artifacts:
                    artifacts.append(item)
            if artifacts:
                error_meta["artifacts"] = artifacts
            primary_ref = OutputReference(uri=primary_ref.uri, artifact=error_meta)
        else:
            primary_ref = capped_output[1] if capped_output is not None else None
        return replace(
            result,
            error=final_error,
            output=replace(
                final_output,
                bounds=replace(
                    final_output.bounds,
                    truncated=final_output.bounds.truncated or capped_error is not None,
                    partial=final_output.bounds.partial or capped_error is not None,
                    reference=primary_ref,
                ),
            ),
        )
    assert capped_output is not None
    return replace(result, output=capped_output[0])


__all__ = [
    "MAX_BINARY_READ_BYTES",
    "MAX_MODEL_FIELD_CHARS",
    "MAX_TOOL_OUTPUT_BYTES",
    "MAX_TOOL_OUTPUT_LINES",
    "RASTER_IMAGE_MIMES",
    "cap_tool_result_output",
    "create_tool_output_artifact",
    "iter_event_artifact_references",
    "read_tool_output_artifact",
    "resolve_tool_output_artifact",
    "sanitize_tool_arguments",
    "sanitize_tool_data",
    "sanitize_tool_result_data",
    "search_tool_output_artifact",
    "session_artifacts_durable_root",
    "tool_output_artifact_temp_root",
]
