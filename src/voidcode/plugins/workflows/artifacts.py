"""Whole-artifact reads and bounded UTF-8 result paging."""

from __future__ import annotations

from voidcode.sdk.services import ArtifactServices

from .constants import RESULT_PAGE_BYTES
from .state import WorkflowNode, WorkflowRun


def _read_full_artifact(services: ArtifactServices, sha256: str) -> str:
    """Read one artifact whole: bounded pages joined as bytes, decoded once as strict UTF-8."""
    chunks: list[bytes] = []
    offset = 0
    while True:
        page = services.read_artifact(sha256, offset=offset, limit=65536)
        if not page.data and not page.eof:
            raise ValueError(f"artifact {sha256} read made no progress at offset {offset}")
        chunks.append(bytes(page.data))
        if page.eof:
            break
        offset += len(page.data)
    return b"".join(chunks).decode("utf-8")


def _run_result_page(services: ArtifactServices, run: WorkflowRun, offset: int) -> tuple[str, int]:
    if run.result is not None:
        raw = run.result.encode("utf-8")
        return _utf8_page(raw, offset), len(raw)
    if run.result_sha256 is None:
        return "", 0
    text = _read_artifact_page(services, run.result_sha256, offset)
    total = run.result_bytes if run.result_bytes is not None else offset + len(text.encode("utf-8"))
    return text, total


def _result_page(services: ArtifactServices, node: WorkflowNode, offset: int) -> tuple[str, int]:
    if node.output is not None:
        raw = node.output.encode("utf-8")
        return _utf8_page(raw, offset), len(raw)
    if node.output_sha256 is None:
        return "", 0
    text = _read_artifact_page(services, node.output_sha256, offset)
    total = node.output_bytes if node.output_bytes is not None else offset + len(text.encode("utf-8"))
    return text, total


def _read_artifact_page(services: ArtifactServices, sha256: str, offset: int) -> str:
    """One bounded artifact page, cut only on UTF-8 character boundaries (strict decode)."""
    page = services.read_artifact(sha256, offset=offset, limit=RESULT_PAGE_BYTES + 3)
    window = bytes(page.data)
    if window and window[0] & 0xC0 == 0x80:
        raise ValueError(f"byte offset {offset} splits a UTF-8 character; use a next_offset from a previous page")
    cut = min(RESULT_PAGE_BYTES, len(window))
    walked = 0
    while cut > 0 and walked < 3 and cut < len(window) and window[cut] & 0xC0 == 0x80:
        cut -= 1
        walked += 1
    return window[:cut].decode("utf-8")


def _utf8_page(raw: bytes, offset: int) -> str:
    if offset >= len(raw):
        return ""
    if offset > 0 and raw[offset] & 0xC0 == 0x80:
        raise ValueError(f"byte offset {offset} splits a UTF-8 character; use a next_offset from a previous page")
    cut = min(offset + RESULT_PAGE_BYTES, len(raw))
    while cut < len(raw) and raw[cut] & 0xC0 == 0x80:
        cut -= 1
    return raw[offset:cut].decode("utf-8")
