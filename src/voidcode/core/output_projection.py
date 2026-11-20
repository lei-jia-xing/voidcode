"""Tool-result publication and model projection.

A tool result crosses two representations: the complete canonical JSON is published to
the artifact store before any event refers to it, and the event/model projection carries
only a bounded page plus a reference to that complete object. This collaborator owns
that split (`persist`), the report-time compaction (`prepare_report`), and the bounded
artifact reader both the core and the coverage projector use.
"""

from __future__ import annotations

import json
from dataclasses import replace

from ..sdk.codec import encode_tool_result
from ..sdk.services import ArtifactPage, ArtifactReference
from ..sdk.tools import (
    TOOL_OUTPUT_BYTES,
    CompositeOutput,
    ContentPart,
    JsonPart,
    TablePart,
    TextOutput,
    TextPart,
    ToolFailure,
    ToolResult,
)
from .artifacts import ArtifactNotFoundError, ArtifactStore
from .coverage import ProjectionRequiresReference, project_tool_result


class ResultProjector:
    """Publish complete tool results and derive the bounded report projection."""

    def __init__(self, artifacts: ArtifactStore) -> None:
        self._artifacts = artifacts

    def page(self, digest: str, offset: int, limit: int) -> ArtifactPage:
        """Read one bounded artifact page; the reader shape `project_tool_result` consumes."""
        try:
            data, total, eof, media_type = self._artifacts.read(digest, offset=offset, limit=limit)
        except ArtifactNotFoundError as exc:
            raise FileNotFoundError(f"artifact is not stored: {digest}") from exc
        return ArtifactPage(ArtifactReference(digest, total, media_type), offset, data, eof)

    def read(self, sha256: str, *, offset: int = 0, limit: int = 65536) -> ArtifactPage:
        """Read immutable workspace artifact bytes, never a caller-supplied path."""
        return self.page(sha256, offset, limit)

    def persist(self, result: ToolResult, *, force: bool) -> tuple[ToolResult, int]:
        """Publish canonical JSON before any compact event can refer to it; never hash a self-reference."""
        result = replace(result, result_reference=None)
        encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))
        pending: list[bytes] = []
        size = 0
        spill_id: str | None = None
        try:
            for piece in encoder.iterencode(encode_tool_result(result)):
                data = piece.encode("utf-8")
                size += len(data)
                if spill_id is None and (force or size > TOOL_OUTPUT_BYTES):
                    spill_id, spill = self._artifacts.start_spill("application/vnd.voidcode.tool-result+json", owner="core")
                    for prior in pending:
                        spill.append(prior)
                    pending.clear()
                if spill_id is None:
                    pending.append(data)
                else:
                    self._artifacts.spill(spill_id).append(data)
            if spill_id is None:
                return result, size
            digest, total, media_type = self._artifacts.finish(spill_id)
        except BaseException:
            if spill_id is not None:
                self._artifacts.discard(spill_id)
            raise
        return replace(result, result_reference=ArtifactReference(digest, total, media_type)), size

    def prepare_report(self, result: ToolResult) -> tuple[ToolResult, int, bool]:
        """Publish the complete result and return the bounded event representation plus its size."""
        stored, size = self.persist(
            result,
            force=(isinstance(result.output, (TextOutput, CompositeOutput)) and result.body is not None),
        )
        try:
            projected, _, clipped = project_tool_result(stored, max_bytes=TOOL_OUTPUT_BYTES, artifact_reader=self.page, include_images=False)
        except ProjectionRequiresReference:
            stored, size = self.persist(result, force=True)
            projected, _, clipped = project_tool_result(stored, max_bytes=TOOL_OUTPUT_BYTES, artifact_reader=self.page, include_images=False)
        if size <= TOOL_OUTPUT_BYTES:
            return stored, size, clipped
        # The event carries a bounded representation, not another copy of a large body/capture.
        # The complete original JSON is already durable at result_reference.
        output = stored.output
        projection = json.loads(projected)
        if isinstance(output, TextOutput):
            reference = output.bounds.reference
            if reference is None and len(output.text.encode("utf-8")) > TOOL_OUTPUT_BYTES:
                spill_id, spill = self._artifacts.start_spill("text/plain; charset=utf-8", owner="core")
                try:
                    for start in range(0, len(output.text), 65536):
                        spill.append(output.text[start : start + 65536].encode("utf-8"))
                    digest, total, media_type = self._artifacts.finish(spill_id)
                except BaseException:
                    self._artifacts.discard(spill_id)
                    raise
                reference = ArtifactReference(digest, total, media_type)
            text = projection.get("text", "")
            output = TextOutput(
                text if isinstance(text, str) else "",
                bounds=replace(output.bounds, reference=reference, partial=output.bounds.partial or clipped),
            )
        elif isinstance(output, CompositeOutput):
            compact_parts, compact_truncated = self._compact_composite_parts(stored)
            presentation = projection.get("presentation")
            output = CompositeOutput(
                parts=tuple(compact_parts),
                presentation=presentation if isinstance(presentation, str) else None,
                bounds=replace(output.bounds, partial=output.bounds.partial or clipped or compact_truncated),
            )
            clipped = clipped or compact_truncated
        else:
            presentation = projection.get("presentation", projection.get("text"))
            output = replace(
                output,
                presentation=presentation if isinstance(presentation, str) else None,
                bounds=replace(output.bounds, partial=output.bounds.partial or clipped),
            )
        compact = replace(stored, body=None, output=output, steering_context=None)
        if isinstance(compact, ToolFailure):
            compact = replace(compact, error=str(projection.get("error", compact.error)), diagnostics=None)
        return compact, size, clipped

    def _spill_composite_text(self, text: str) -> ArtifactReference:
        spill_id, spill = self._artifacts.start_spill("text/plain; charset=utf-8", owner="core")
        try:
            for start in range(0, len(text), 65536):
                spill.append(text[start : start + 65536].encode("utf-8"))
            digest, total, media_type = self._artifacts.finish(spill_id)
        except BaseException:
            self._artifacts.discard(spill_id)
            raise
        return ArtifactReference(digest, total, media_type)

    def _spill_composite_table(self, columns: tuple[str, ...], rows: tuple[tuple[str, ...], ...], total_rows: int | None) -> ArtifactReference:
        document = {"columns": list(columns), "rows": [list(row) for row in rows], "total_rows": total_rows if total_rows is not None else len(rows)}
        payload = json.dumps(document, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")).encode("utf-8")
        spill_id, spill = self._artifacts.start_spill("application/json", owner="core")
        try:
            for start in range(0, len(payload), 65536):
                spill.append(payload[start : start + 65536])
            digest, total, media_type = self._artifacts.finish(spill_id)
        except BaseException:
            self._artifacts.discard(spill_id)
            raise
        return ArtifactReference(digest, total, media_type)

    def _compact_composite_parts(self, stored: ToolResult) -> tuple[list[ContentPart], bool]:
        output = stored.output
        if not isinstance(output, CompositeOutput):
            raise ValueError("composite compaction requires a composite output")
        parts: list[ContentPart] = list(output.parts)
        truncated = False

        def _candidate_size(candidate: list[ContentPart]) -> int:
            candidate_output = CompositeOutput(parts=tuple(candidate), presentation=None, bounds=output.bounds)
            candidate_result = replace(stored, body=None, output=candidate_output, steering_context=None)
            if isinstance(candidate_result, ToolFailure):
                candidate_result = replace(candidate_result, diagnostics=None)
            payload = encode_tool_result(candidate_result)
            return len(json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")).encode("utf-8"))

        while _candidate_size(parts) > TOOL_OUTPUT_BYTES:
            best_index: int | None = None
            best_size = 0
            for index, part in enumerate(parts):
                if isinstance(part, TablePart) and part.rows:
                    size = sum(len(cell.encode("utf-8")) for row in part.rows for cell in row)
                    size += sum(len(column.encode("utf-8")) for column in part.columns)
                elif isinstance(part, TextPart) and part.text:
                    size = len(part.text.encode("utf-8"))
                elif isinstance(part, JsonPart) and len(part.preview) > 1:
                    size = len(part.preview.encode("utf-8"))
                else:
                    continue
                if size > best_size:
                    best_size = size
                    best_index = index
            if best_index is None:
                break
            part = parts[best_index]
            if isinstance(part, TablePart):
                if part.reference is None:
                    reference = self._spill_composite_table(part.columns, part.rows, part.total_rows)
                    total = part.total_rows if part.total_rows is not None else len(part.rows)
                    keep = len(part.rows) // 2
                    parts[best_index] = TablePart(
                        columns=part.columns,
                        rows=part.rows[:keep],
                        total_rows=total,
                        reference=reference,
                        presentation=part.presentation,
                    )
                else:
                    keep = len(part.rows) // 2
                    parts[best_index] = replace(part, rows=part.rows[:keep])
                truncated = True
            elif isinstance(part, TextPart):
                if part.reference is None:
                    reference = self._spill_composite_text(part.text)
                    keep = len(part.text) // 2
                    parts[best_index] = TextPart(text=part.text[:keep], reference=reference)
                else:
                    keep = len(part.text) // 2
                    parts[best_index] = TextPart(text=part.text[:keep], reference=part.reference)
                truncated = True
            elif isinstance(part, JsonPart):
                keep = max(1, len(part.preview) // 2)
                parts[best_index] = JsonPart(reference=part.reference, preview=part.preview[:keep], media_type=part.media_type)
                truncated = True
        return parts, truncated
