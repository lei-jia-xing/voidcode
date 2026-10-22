from __future__ import annotations

import difflib
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object, own_json_object
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import TextOutput, ToolCall, ToolDefinition, ToolEffect, ToolResult, ToolSuccess

from .apply_patch import execute_patch
from .guards import CharProvenanceTracker, enforce_read_before_write, enforce_seen_lines
from .repair import (
    ToolDiagnosticError,
    bounded_block_preview,
    bounded_candidate_diff,
    line_prefix_retry_guidance,
    looks_line_number_prefixed,
    raise_tool_diagnostic,
)


@dataclass(frozen=True, slots=True)
class EditResultBody:
    path: str
    additions: int
    deletions: int
    match_count: int
    diff: str
    diagnostics: tuple[Mapping[str, object], ...] = ()
    applied: int | None = None
    edits: tuple[tuple[int, EditResultBody], ...] | None = None
    content_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "diagnostics", tuple(own_json_object(item) for item in self.diagnostics))

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "path": self.path,
            "additions": self.additions,
            "deletions": self.deletions,
            "match_count": self.match_count,
            "diff": self.diff,
        }
        if self.applied is not None:
            payload["applied"] = self.applied
        if self.edits is not None:
            payload["edits"] = [{"index": idx, "result": res.as_payload()} for idx, res in self.edits]
        if self.content_hash is not None:
            payload["content_hash"] = self.content_hash
        if self.diagnostics:
            payload["diagnostics"] = [json_wire_object(item) for item in self.diagnostics]
        return payload


def _normalize_line_endings(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _occurrence_line_ranges(content: str, needle: str) -> list[tuple[int, int]]:
    """1-based inclusive line ranges of every occurrence of ``needle`` in ``content``."""
    ranges: list[tuple[int, int]] = []
    start = 0
    while True:
        index = content.find(needle, start)
        if index == -1:
            break
        start_line = content.count("\n", 0, index) + 1
        end_line = content.count("\n", 0, index + len(needle)) + 1
        ranges.append((start_line, end_line))
        start = index + len(needle)
    return ranges


def _detect_line_ending(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    return "\n"


def _convert_line_endings(text: str, ending: str) -> str:
    normalized = _normalize_line_endings(text)
    return normalized if ending == "\n" else normalized.replace("\n", "\r\n")


def _near_match_hints(content: str, old_string: str, *, limit: int = 2) -> list[str]:
    old_lines = old_string.split("\n")
    if old_lines and old_lines[-1] == "":
        old_lines = old_lines[:-1]
    if not old_lines:
        return []

    lines = content.split("\n")
    window_size = min(len(old_lines), len(lines))
    if window_size == 0:
        return []
    candidates: list[tuple[float, int, str]] = []
    for start in range(len(lines) - window_size + 1):
        block = "\n".join(lines[start : start + window_size])
        ratio = difflib.SequenceMatcher(None, old_string.strip(), block.strip()).ratio()
        if ratio >= 0.58:
            candidates.append((ratio, start, block))

    candidates.sort(key=lambda item: item[0], reverse=True)
    return [
        f"  - L{start + 1} ({round(ratio * 100)}% similar; inspect only)\n"
        f"{bounded_block_preview(lines, start, window_size)}\n"
        "    Diff (- oldString, + current):\n"
        f"{bounded_candidate_diff(old_string, block).replace('expected', 'oldString')}"
        for ratio, start, block in candidates[:limit]
    ]


def _match_locations(content: str, matches: list[str]) -> list[dict[str, object]]:
    """Map each distinct match to 1-based line numbers and a short preview."""
    locations: list[dict[str, object]] = []
    seen: set[str] = set()
    for match in matches:
        if match in seen:
            continue
        seen.add(match)
        line_numbers: list[int] = []
        start = 0
        while True:
            index = content.find(match, start)
            if index == -1:
                break
            line_numbers.append(content.count("\n", 0, index) + 1)
            start = index + 1
        preview_lines = match.split("\n")[:3]
        locations.append(
            {
                "line_numbers": line_numbers,
                "preview": "\n".join(preview_lines)[:200],
            }
        )
    return locations


def _edit_mismatch_message(*, content: str, old_string: str) -> str:
    lines = ["Exact oldString was not found in the current file text."]
    hints = _near_match_hints(content, old_string)
    if hints:
        lines.extend(
            [
                "Advisory near-match suggestions (not applied):",
                *hints,
                "Re-read the shown lines and retry with exact current text, including indentation.",
            ]
        )
    else:
        lines.append("No similar text found; re-read the file before retrying.")

    if looks_line_number_prefixed(old_string):
        lines.append(line_prefix_retry_guidance())
    return "\n".join(lines)


def _trim_diff(diff: str) -> str:
    lines = diff.split("\n")
    content_lines = [
        line
        for line in lines
        if (line.startswith("+") or line.startswith("-") or line.startswith(" ")) and not line.startswith("---") and not line.startswith("+++")
    ]

    if not content_lines:
        return diff

    min_indent: int | None = None
    for line in content_lines:
        content = line[1:]
        if content.strip():
            match = re.match(r"^(\s*)", content)
            if match and match.group(1):
                indent = len(match.group(1))
                min_indent = indent if min_indent is None else min(min_indent, indent)

    if min_indent is None or min_indent == 0:
        return diff

    trimmed_lines: list[str] = []
    for line in lines:
        if (line.startswith("+") or line.startswith("-") or line.startswith(" ")) and not line.startswith("---") and not line.startswith("+++"):
            prefix = line[0]
            content = line[1:]
            trimmed_lines.append(prefix + content[min_indent:] if len(content) > min_indent else line)
        else:
            trimmed_lines.append(line)

    return "\n".join(trimmed_lines)


def read_utf8_text(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return handle.read()
    except UnicodeDecodeError as exc:
        raise ValueError("edit only supports UTF-8 text files") from exc


def summarize_diff(*, path: Path, before: str, after: str) -> tuple[str, int, int]:
    def _ensure_newlines(lines: list[str]) -> list[str]:
        return [line + "\n" for line in lines]

    diff = _trim_diff(
        "".join(
            difflib.unified_diff(
                _ensure_newlines(before.splitlines()) if before else [],
                _ensure_newlines(after.splitlines()) if after else [],
                fromfile=str(path),
                tofile=str(path),
            )
        )
    )

    additions = sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
    deletions = sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
    return diff, additions, deletions


def _replace(
    content: str,
    old_string: str,
    new_string: str,
    *,
    replace_all: bool = False,
    tracker: CharProvenanceTracker | None = None,
    context: ToolContext | None = None,
    resolved_path: str | None = None,
    tool_name: str = "edit",
    display_path: str = "",
    raw_path: str = "",
    is_external: bool = False,
) -> tuple[str, int, tuple[tuple[int, int], ...]]:
    if old_string == new_string:
        raise_tool_diagnostic(
            message="No changes to apply: oldString and newString are identical.",
            error_kind="no_op",
            reason="identical_old_and_new",
            retry_guidance="Provide a newString that differs from oldString, or use read to confirm the file already has the desired content.",
            details={"old_string": old_string, "new_string": new_string},
        )
    if not content:
        raise_tool_diagnostic(
            message="Content is empty; nothing to edit.",
            error_kind="no_op",
            reason="empty_content",
            retry_guidance="The target file is empty; use write to create content, or read it again before retrying.",
            details={"content_empty": True},
        )

    match_count = content.count(old_string) if old_string else 0
    if match_count == 0:
        raise_tool_diagnostic(
            message=_edit_mismatch_message(content=content, old_string=old_string),
            error_kind="tool_input_mismatch",
            reason="old_string_not_found",
            retry_guidance="Read the target path and retry with exact current file text, without line-number prefixes.",
            details={
                "old_string_line_count": len(old_string.splitlines()),
                "line_number_prefix_suspected": looks_line_number_prefixed(old_string),
            },
        )
    if not replace_all and match_count > 1:
        raise_tool_diagnostic(
            message="Multiple exact matches found. Use replaceAll to replace every occurrence.",
            error_kind="ambiguous_match",
            reason="ambiguous_match",
            retry_guidance="Set replaceAll=true or provide longer exact text that uniquely identifies the intended location.",
            details={
                "match_count": match_count,
                "matches": _match_locations(content, [old_string] * match_count),
                "replace_all_hint": True,
            },
        )

    match_ranges = tuple(_occurrence_line_ranges(content, old_string))
    if not replace_all:
        match_ranges = match_ranges[:1]
    if tracker is None:
        return content.replace(old_string, new_string, -1 if replace_all else 1), match_count if replace_all else 1, match_ranges

    current = content
    total_replacements = 0
    start_search = 0
    while True:
        idx = current.find(old_string, start_search)
        if idx == -1:
            break
        if context is not None and resolved_path is not None:
            tracker.check_seen_lines(
                idx,
                idx + len(old_string),
                context=context,
                resolved_path=resolved_path,
                tool_name=tool_name,
                display_path=display_path,
                raw_path=raw_path,
                is_external=is_external,
            )
        tracker.apply_replacement(idx, idx + len(old_string), new_string)
        current = tracker.content
        total_replacements += 1
        if not replace_all:
            break
        start_search = idx + len(new_string)
    return current, total_replacements, match_ranges


class EditTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="edit",
        description=(
            "Edit files by replacing text (single replacement or ordered batch) or applying a patch. "
            "OldString replacements require exact current file text; advisory near-match suggestions are never applied. "
            "When constructing oldString from read output, omit any leading line-number prefix such as '42: ' and pass only the file's actual text."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative or absolute path to the file to edit."},
                "oldString": {"type": "string", "description": "The exact text to replace (single replacement mode)."},
                "newString": {"type": "string", "description": "The replacement text (single replacement mode)."},
                "replaceAll": {"type": "boolean", "description": "Replace all occurrences of oldString (default: false)."},
                "expectedHash": {
                    "type": "string",
                    "description": (
                        "Required SHA-256 hash of the target file content from prior read output. "
                        "Rejects stale edits when the file changed since that read."
                    ),
                },
                "edits": {
                    "type": "array",
                    "minItems": 1,
                    "description": "Ordered replacements; each oldString must match the current file state at that step.",
                    "items": {
                        "type": "object",
                        "required": ["oldString", "newString"],
                        "properties": {
                            "oldString": {"type": "string", "description": "Exact text to replace."},
                            "newString": {"type": "string", "description": "Replacement text."},
                            "replaceAll": {"type": "boolean", "description": "Replace every match; defaults to false."},
                        },
                    },
                },
                "patch": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Patch text using unified diff or marker patch format (*** Begin Patch / *** End Patch).",
                },
                "expectedHashes": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": (
                        "Map of relative file paths to their SHA-256 content hashes from prior read output. "
                        "Required for existing files modified or deleted by the patch."
                    ),
                },
            },
            "oneOf": [
                {
                    "required": ["path", "oldString", "newString", "expectedHash"],
                    "not": {"anyOf": [{"required": ["edits"]}, {"required": ["patch"]}]},
                },
                {
                    "required": ["path", "edits", "expectedHash"],
                    "not": {"anyOf": [{"required": ["oldString"]}, {"required": ["newString"]}, {"required": ["patch"]}]},
                },
                {
                    "required": ["patch"],
                    "not": {"anyOf": [{"required": ["path"]}, {"required": ["oldString"]}, {"required": ["newString"]}, {"required": ["edits"]}]},
                },
            ],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.WRITE}),
        path_argument_keys=("path",),
    )

    def __init__(self) -> None:
        pass

    def invoke(
        self,
        call: ToolCall,
        *,
        context: ToolContext,
        services: PluginServices,  # noqa: ARG002 - required by the frozen Tool ABI
    ) -> ToolResult:
        workspace = context.require_workspace()
        has_patch = "patch" in call.arguments
        has_edits = "edits" in call.arguments
        has_single = "oldString" in call.arguments or "newString" in call.arguments
        modes_present = sum([has_patch, has_edits, has_single])
        if modes_present > 1:
            raise ValueError(
                "edit arguments are mutually exclusive: provide either single replacement "
                "(path, oldString, newString, expectedHash), edits batch (path, edits, expectedHash), "
                "or patch (patch, expectedHashes)."
            )
        if modes_present == 0:
            raise ValueError(
                "edit invocation requires either single replacement arguments (path, oldString, newString, expectedHash), "
                "an edits batch (path, edits, expectedHash), or a patch (patch)."
            )

        if has_patch:
            return execute_patch(
                call.arguments,
                context=context,
                tool_name=self.definition.name,
            )

        if has_edits:
            return self._invoke_edits_batch(call, context=context, workspace=workspace)

        return self._invoke_single(call, context=context, workspace=workspace)

    def _invoke_edits_batch(
        self,
        call: ToolCall,
        *,
        context: ToolContext,
        workspace: Path,
    ) -> ToolSuccess[EditResultBody]:
        path_value = call.arguments.get("path")
        if not isinstance(path_value, str) or not path_value.strip():
            raise ValueError("edit requires a non-empty string 'path' argument")

        raw_edits = call.arguments.get("edits")
        if not isinstance(raw_edits, (list, tuple)) or len(raw_edits) == 0:
            raise ValueError("Validation error: edits must contain at least one edit")

        workspace_root = workspace.resolve()
        requested_path = Path(path_value)
        candidate = (requested_path if requested_path.is_absolute() else workspace_root / requested_path).resolve()
        is_external = not candidate.is_relative_to(workspace_root)
        if not candidate.exists():
            raise ValueError(f"edit target does not exist: {path_value}")
        if not candidate.is_file():
            raise ValueError(f"edit target is not a file: {path_value}")

        display_path = candidate.as_posix() if is_external else candidate.relative_to(workspace_root).as_posix()
        resolved_path = candidate.as_posix()

        enforce_read_before_write(
            context=context,
            tool_name=self.definition.name,
            raw_path=path_value,
            candidate=candidate,
            display_path=display_path,
        )

        expected_hash = call.arguments.get("expectedHash")
        if not isinstance(expected_hash, str):
            raise_tool_diagnostic(
                message="edit requires an expectedHash argument: the file must be read before it is edited.",
                error_kind="tool_input_mismatch",
                reason="missing_expected_hash",
                retry_guidance=(
                    "Use read on the target path, copy its SHA-256 content hash from the output, then retry the edit with that expectedHash."
                ),
                details={"path": display_path, "raw_path": path_value},
            )

        content_before = read_utf8_text(candidate)
        actual_hash = hashlib.sha256(content_before.encode("utf-8")).hexdigest()
        if expected_hash != actual_hash:
            raise_tool_diagnostic(
                message="Edit rejected because the file changed since it was read (stale edit).",
                error_kind="stale_edit",
                reason="content_hash_mismatch",
                retry_guidance="Read the file again, use the SHA-256 content hash shown in read output, then retry the edit.",
                details={"expected_hash": expected_hash, "actual_hash": actual_hash, "path": display_path},
            )

        # Parse and validate edit items
        parsed_edits: list[tuple[str, str, bool]] = []
        for idx, item in enumerate(raw_edits, start=1):
            if not isinstance(item, Mapping):
                raise ValueError(f"edit item #{idx} must be an object")
            old_s = item.get("oldString")
            new_s = item.get("newString")
            rep_all = item.get("replaceAll", False)
            if not isinstance(old_s, str) or not isinstance(new_s, str):
                raise ValueError(f"edit item #{idx} requires string oldString and newString")
            if not isinstance(rep_all, bool):
                raise ValueError(f"edit item #{idx} replaceAll must be a boolean")
            parsed_edits.append((old_s, new_s, rep_all))

        ending = _detect_line_ending(content_before)

        # Preflight all edits against exact current text entirely in memory.
        tracker = CharProvenanceTracker(content_before)
        details: list[tuple[int, EditResultBody]] = []
        applied = 0

        for idx, (old_str, new_str, rep_all) in enumerate(parsed_edits, start=1):
            before_step = tracker.content

            try:
                next_content, match_count, match_ranges = _replace(
                    tracker.content,
                    old_str,
                    new_str,
                    replace_all=rep_all,
                    tracker=tracker,
                    context=context,
                    resolved_path=resolved_path,
                    tool_name=self.definition.name,
                    display_path=display_path,
                    raw_path=path_value,
                    is_external=is_external,
                )
            except ValueError as exc:
                if isinstance(exc, ToolDiagnosticError) and exc.error_details.get("reason") == "unseen_range":
                    raise exc
                message = (
                    f"edit failed at edit #{idx} of {len(parsed_edits)} for {display_path}.\n"
                    f"Prepared edits before failure: {idx - 1}; no file changes were written.\n"
                    "Retry guidance: correct the failing edit and retry the complete batch against the unchanged file.\n"
                    f"Underlying edit diagnostic:\n{exc}"
                )
                cause_details: dict[str, object] = {}
                if isinstance(exc, ToolDiagnosticError):
                    cause_details = {
                        "error_kind": exc.error_kind,
                        "error_details": exc.error_details,
                        "retry_guidance": exc.retry_guidance,
                    }
                raise ToolDiagnosticError(
                    message=message,
                    error_kind="tool_input_mismatch",
                    retry_guidance=("No batch edits were written. Correct the failing edit and retry the complete batch with the current file hash."),
                    error_details={
                        "reason": "edit_failed",
                        "path": display_path,
                        "failed_edit_index": idx,
                        "prepared_edits": idx - 1,
                        "applied_edits": 0,
                        "remaining_edits": len(parsed_edits) - idx + 1,
                        "cause": cause_details,
                    },
                ) from exc

            diff_step, add_step, del_step = summarize_diff(
                path=candidate,
                before=before_step,
                after=next_content,
            )
            step_body = EditResultBody(
                path=display_path,
                additions=add_step,
                deletions=del_step,
                match_count=match_count,
                diff=diff_step,
            )
            applied += 1
            details.append((idx, step_body))

        # All preflight edits passed in memory! Now write once to disk.
        final_text = _convert_line_endings(tracker.content, ending)
        candidate.write_bytes(final_text.encode("utf-8"))
        final_disk_bytes = candidate.read_bytes()
        final_disk_hash = hashlib.sha256(final_disk_bytes).hexdigest()
        final_content = read_utf8_text(candidate)
        diff, additions, deletions = summarize_diff(
            path=candidate,
            before=content_before,
            after=final_content,
        )

        content = f"Applied {applied} edits to {display_path}"

        return ToolSuccess(
            self.definition.name,
            output=TextOutput(content),
            body=EditResultBody(
                path=display_path,
                additions=additions,
                deletions=deletions,
                match_count=applied,
                diff=diff,
                applied=applied,
                edits=tuple(details),
                content_hash=final_disk_hash,
            ),
        )

    def _invoke_single(
        self,
        call: ToolCall,
        *,
        context: ToolContext,
        workspace: Path,
    ) -> ToolSuccess[EditResultBody]:
        path_value = call.arguments.get("path")
        if not isinstance(path_value, str):
            raise ValueError("edit requires a string path argument")

        old_string = call.arguments.get("oldString")
        if not isinstance(old_string, str):
            raise ValueError("edit requires a string oldString argument")

        new_string = call.arguments.get("newString")
        if not isinstance(new_string, str):
            raise ValueError("edit requires a string newString argument")

        replace_all = call.arguments.get("replaceAll", False)
        if not isinstance(replace_all, bool):
            raise ValueError("edit replaceAll must be a boolean")

        workspace_root = workspace.resolve()
        requested_path = Path(path_value)
        candidate = (requested_path if requested_path.is_absolute() else workspace_root / requested_path).resolve()
        is_external = not candidate.is_relative_to(workspace_root)

        if not candidate.exists():
            raise ValueError(f"edit target does not exist: {path_value}")

        if not candidate.is_file():
            raise ValueError(f"edit target is not a file: {path_value}")

        display_path = candidate.as_posix() if is_external else candidate.relative_to(workspace_root).as_posix()
        enforce_read_before_write(
            context=context,
            tool_name=self.definition.name,
            raw_path=path_value,
            candidate=candidate,
            display_path=display_path,
        )

        expected_hash = call.arguments.get("expectedHash")
        if not isinstance(expected_hash, str):
            raise_tool_diagnostic(
                message="edit requires an expectedHash argument: the file must be read before it is edited.",
                error_kind="tool_input_mismatch",
                reason="missing_expected_hash",
                retry_guidance=(
                    "Use read on the target path, copy its SHA-256 content hash from the output, then retry the edit with that expectedHash."
                ),
                details={"path": display_path, "raw_path": path_value},
            )

        content_old = read_utf8_text(candidate)
        actual_hash = hashlib.sha256(content_old.encode("utf-8")).hexdigest()
        if expected_hash != actual_hash:
            raise_tool_diagnostic(
                message="Edit rejected because the file changed since it was read (stale edit).",
                error_kind="stale_edit",
                reason="content_hash_mismatch",
                retry_guidance="Read the file again, use the SHA-256 content hash shown in read output, then retry the edit.",
                details={"expected_hash": expected_hash, "actual_hash": actual_hash, "path": display_path},
            )

        ending = _detect_line_ending(content_old)

        new_content, match_count, match_ranges = _replace(
            content_old,
            old_string,
            new_string,
            replace_all=replace_all,
        )

        for start_line, end_line in match_ranges:
            enforce_seen_lines(
                context=context,
                tool_name=self.definition.name,
                raw_path=path_value,
                candidate=candidate,
                display_path=display_path,
                start_line=start_line,
                end_line=end_line,
            )

        new_content = _convert_line_endings(new_content, ending)

        candidate.write_bytes(new_content.encode("utf-8"))
        final_disk_bytes = candidate.read_bytes()
        final_disk_hash = hashlib.sha256(final_disk_bytes).hexdigest()
        final_content = read_utf8_text(candidate)
        diff, additions, deletions = summarize_diff(
            path=candidate,
            before=content_old,
            after=final_content,
        )

        output = "Edit applied successfully."
        if match_count > 1:
            output += f" ({match_count} occurrences replaced)"

        return ToolSuccess(
            self.definition.name,
            output=TextOutput(output),
            body=EditResultBody(
                path=display_path,
                additions=additions,
                deletions=deletions,
                match_count=match_count,
                diff=diff,
                content_hash=final_disk_hash,
            ),
        )
