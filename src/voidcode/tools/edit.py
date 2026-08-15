from __future__ import annotations

import difflib
import hashlib
import re
from pathlib import Path
from typing import ClassVar

from rapidfuzz.distance import Levenshtein

from ..formatter import (
    FormatterExecutionResult,
    FormatterExecutor,
    formatter_diagnostics,
    formatter_payload,
)
from ..hook.config import RuntimeHooksConfig
from ..runtime.edit_schema_policy import EditSchema, EditSchemaResolver, select_edit_schema
from ..security.path_policy import resolve_workspace_path
from ._post_edit_diagnostics import post_edit_lsp_diagnostics
from ._repair import (
    bounded_block_preview,
    bounded_candidate_diff,
    line_prefix_retry_guidance,
    looks_line_number_prefixed,
    raise_tool_diagnostic,
)
from ._syntax_validation import post_edit_syntax_diagnostics
from .contracts import ToolCall, ToolDefinition, ToolResult
from .guards import enforce_read_before_write, enforce_seen_lines
from .runtime_context import current_runtime_tool_context


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
        start = index + 1
    return ranges


def _detect_line_ending(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    return "\n"


def _convert_line_endings(text: str, ending: str) -> str:
    if ending == "\n":
        return _normalize_line_endings(text)
    return text.replace("\n", "\r\n")


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

    candidates: list[tuple[float, int, str, str]] = []
    normalized_old = WhitespaceNormalizedReplacer.normalize(old_string)
    dedented_old = IndentationFlexibleReplacer.remove_indentation(old_string)

    for start in range(len(lines) - window_size + 1):
        block = "\n".join(lines[start : start + window_size])
        ratio = difflib.SequenceMatcher(None, old_string.strip(), block.strip()).ratio()
        if ratio < 0.58:
            continue

        notes: list[str] = []
        if WhitespaceNormalizedReplacer.normalize(block) == normalized_old:
            notes.append("whitespace-only mismatch")
        if IndentationFlexibleReplacer.remove_indentation(block) == dedented_old:
            notes.append("indentation-only mismatch")
        if len(old_lines) >= 3:
            first_close = BlockAnchorReplacer._similar(lines[start].strip(), old_lines[0].strip())
            last_close = BlockAnchorReplacer._similar(lines[start + window_size - 1].strip(), old_lines[-1].strip())
            if first_close and last_close:
                notes.append("block anchors are close")
            elif first_close:
                notes.append("first block anchor is close; check the ending line")
            elif last_close:
                notes.append("last block anchor is close; check the starting line")
        if not notes:
            notes.append("near text match")

        candidates.append((ratio, start, ", ".join(notes), block))

    candidates.sort(key=lambda item: item[0], reverse=True)
    hints: list[str] = []
    for ratio, start, note, block in candidates[:limit]:
        hints.append(
            f"  - L{start + 1} ({round(ratio * 100)}% similar; {note})\n"
            f"{bounded_block_preview(lines, start, window_size)}\n"
            "    Diff (- oldString, + current):\n"
            f"{bounded_candidate_diff(old_string, block).replace('expected', 'oldString')}"
        )
    return hints


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


def _edit_mismatch_message(
    *,
    content: str,
    old_string: str,
    attempted_replacers: list[str],
) -> str:
    lines = [
        "Could not find oldString in the file.",
        "Replacers attempted:",
        *(f"  - {name}" for name in attempted_replacers),
    ]

    hints = _near_match_hints(content, old_string)
    if hints:
        lines.extend(
            [
                "Near-match hints:",
                *hints,
                "Tip: re-read the shown lines and retry with exact current text, including indentation.",
            ]
        )
    else:
        lines.append("No nearby text match found; re-read the file before retrying the edit.")

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


class SimpleReplacer:
    @staticmethod
    def find(content: str, old: str) -> list[str]:
        results: list[str] = []
        if not old:
            return results

        start = 0
        while True:
            idx = content.find(old, start)
            if idx == -1:
                break
            results.append(content[idx : idx + len(old)])
            start = idx + 1
        return results


class LineTrimmedReplacer:
    @staticmethod
    def find(content: str, old: str) -> list[str]:
        # Find blocks where each corresponding line, after trimming whitespace, matches
        results: list[str] = []
        original_lines = content.split("\n")
        search_lines = old.split("\n")

        if search_lines and search_lines[-1] == "":
            search_lines = search_lines[:-1]

        L = len(search_lines)
        if L == 0:
            return results

        for i in range(len(original_lines) - L + 1):
            good = True
            for j in range(L):
                if original_lines[i + j].strip() != search_lines[j].strip():
                    good = False
                    break
            if good:
                # reconstruct the exact block from the original content
                block = "\n".join(original_lines[i : i + L])
                results.append(block)
        return results


class BlockAnchorReplacer:
    _MAX_ANCHOR_LENGTH = 200
    _MAX_LINES_SCAN = 2000

    @staticmethod
    def _similar(a: str, b: str) -> bool:
        if abs(len(a) - len(b)) > max(1, max(len(a), len(b)) // 4):
            return False

        if len(a) > BlockAnchorReplacer._MAX_ANCHOR_LENGTH:
            a = a[: BlockAnchorReplacer._MAX_ANCHOR_LENGTH]
        if len(b) > BlockAnchorReplacer._MAX_ANCHOR_LENGTH:
            b = b[: BlockAnchorReplacer._MAX_ANCHOR_LENGTH]

        max_distance = max(1, max(len(a), len(b)) // 4)
        distance = Levenshtein.distance(a, b, score_cutoff=max_distance)
        return distance <= max_distance

    @staticmethod
    def find(content: str, old: str) -> list[str]:
        results: list[str] = []
        original_lines = content.split("\n")
        search_lines = old.split("\n")
        if len(search_lines) < 3:
            return results

        if search_lines and search_lines[-1] == "":
            search_lines = search_lines[:-1]

        first_anchor = search_lines[0].strip()
        last_anchor = search_lines[-1].strip()

        # Try to locate blocks bounded by anchors with similarity tolerance
        max_scan = min(len(original_lines), BlockAnchorReplacer._MAX_LINES_SCAN)
        for i in range(max_scan):
            if not BlockAnchorReplacer._similar(original_lines[i].strip(), first_anchor):
                continue
            # search a corresponding end line with similarity to last_anchor
            upper = min(len(original_lines), i + BlockAnchorReplacer._MAX_LINES_SCAN)
            for j in range(i + 2, upper):
                if BlockAnchorReplacer._similar(original_lines[j].strip(), last_anchor):
                    # extract the block from i to j inclusive
                    block = "\n".join(original_lines[i : j + 1])
                    results.append(block)
                    break
        return results


class WhitespaceNormalizedReplacer:
    @staticmethod
    def normalize(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def find(content: str, old: str) -> list[str]:
        results: list[str] = []
        normalized_old = WhitespaceNormalizedReplacer.normalize(old)
        lines = content.split("\n")
        find_lines = old.split("\n")

        for _i, line in enumerate(lines):
            if WhitespaceNormalizedReplacer.normalize(line) == normalized_old:
                results.append(line)

        if len(find_lines) > 1:
            for i in range(len(lines) - len(find_lines) + 1):
                block = "\n".join(lines[i : i + len(find_lines)])
                if WhitespaceNormalizedReplacer.normalize(block) == normalized_old:
                    results.append(block)

        return results


class IndentationFlexibleReplacer:
    @staticmethod
    def remove_indentation(text: str) -> str:
        lines = text.split("\n")
        non_empty = [line for line in lines if line.strip()]
        if not non_empty:
            return text

        min_indent: int | None = None
        for line in non_empty:
            match = re.match(r"^(\s*)", line)
            if match and match.group(1):
                indent = len(match.group(1))
                min_indent = indent if min_indent is None else min(min_indent, indent)

        if min_indent is None:
            return text

        dedented = [line[min_indent:] if len(line) >= min_indent else line for line in lines]
        return "\n".join(dedented)

    @staticmethod
    def find(content: str, old: str) -> list[str]:
        results: list[str] = []
        normalized_old = IndentationFlexibleReplacer.remove_indentation(old)
        content_lines = content.split("\n")
        find_lines = old.split("\n")
        if not find_lines:
            return results

        for i in range(len(content_lines) - len(find_lines) + 1):
            block = "\n".join(content_lines[i : i + len(find_lines)])
            if IndentationFlexibleReplacer.remove_indentation(block) == normalized_old:
                results.append(block)

        return results


def _replace(
    content: str,
    old_string: str,
    new_string: str,
    *,
    replace_all: bool = False,
    edit_schema: EditSchema = EditSchema.FLEXIBLE,
) -> tuple[str, int, tuple[tuple[int, int], ...]]:
    """Return (new content, replacement count, 1-based inclusive line ranges replaced)."""
    # Smart replacement pipeline using 9 replacers in order
    if old_string == new_string:
        raise_tool_diagnostic(
            message="No changes to apply: oldString and newString are identical.",
            error_kind="no_op",
            reason="identical_old_and_new",
            retry_guidance=("Provide a newString that differs from oldString, or use read to confirm the file already has the desired content."),
            details={"old_string": old_string, "new_string": new_string},
        )

    if not content:
        raise_tool_diagnostic(
            message="Content is empty; nothing to edit.",
            error_kind="no_op",
            reason="empty_content",
            retry_guidance=(
                "The target file is empty; use write to create content, or read the file again and retry the edit against the current text."
            ),
            details={"content_empty": True},
        )

    if replace_all:
        # For replaceAll we still need to verify at least one match exists via smart replacers
        pass

    if edit_schema is EditSchema.STRICT:
        # Strict profile: exact-match only. The simple replacer is the sole
        # strategy; any non-exact input fails with the ``ambiguous_match``
        # diagnostic below instead of falling through to fuzzy matching.
        replacers = [SimpleReplacer]
    else:
        replacers = [
            SimpleReplacer,
            LineTrimmedReplacer,
            BlockAnchorReplacer,
            WhitespaceNormalizedReplacer,
            IndentationFlexibleReplacer,
            # EscapeNormalizedReplacer to be added below after class definitions
            # MultiOccurrenceReplacer, TrimmedBoundaryReplacer, ContextAwareReplacer
        ]

    # The actual smart replacers will be defined later in file; to avoid forward reference issues,
    # we attempt to import them lazily from globals() after their definitions. If not yet defined,
    # we will skip them for the initial pass.
    smart_names = [
        "EscapeNormalizedReplacer",
        "MultiOccurrenceReplacer",
        "TrimmedBoundaryReplacer",
        "ContextAwareReplacer",
    ]
    # Build dynamic replacer list by checking their presence in globals
    if edit_schema is not EditSchema.STRICT:
        for name in smart_names:
            if name in globals():
                replacers.append(globals()[name])

    # Helper to perform a replacement given a matched substring
    # If replace_all is requested we attempt to replace all occurrences found by any replacer
    total_replacements = 0
    current = content

    # Try each replacer in order until we find at least one match
    attempted_replacers: list[str] = []
    for replacer in replacers:
        attempted_replacers.append(replacer.__name__)
        try:
            matches = replacer.find(current, old_string)
        except Exception:
            continue
        if not matches:
            continue

        # If replaceAll, replace all occurrences found by this replacer
        if replace_all:
            seen: set[str] = set()
            match_ranges: list[tuple[int, int]] = []
            for m in matches:
                if m in seen:
                    continue
                seen.add(m)
                if m:
                    match_ranges.extend(_occurrence_line_ranges(current, m))
                    count = current.count(m)
                    if count:
                        current = current.replace(m, new_string)
                        total_replacements += count
            return current, total_replacements, tuple(match_ranges)

        # If not replacing all, enforce single-match constraint per problem statement
        if len(matches) > 1:
            raise_tool_diagnostic(
                message="Multiple matches found. Use replaceAll to replace all occurrences.",
                error_kind="ambiguous_match",
                reason="ambiguous_match",
                retry_guidance=(
                    "Either set replaceAll=true to replace every occurrence, or re-read the file and "
                    "provide a longer, more specific oldString that uniquely identifies the intended "
                    "location."
                ),
                details={
                    "match_count": len(matches),
                    "matches": _match_locations(current, matches),
                    "replace_all_hint": True,
                },
            )
        # Exactly one match; replace that exact substring in the current content
        m = matches[0]
        if m:
            match_ranges = _occurrence_line_ranges(current, m)
            current = current.replace(m, new_string, 1)
            total_replacements += 1
            return current, total_replacements, tuple(match_ranges[:1])

    # If we reach here, no replacer found any match
    if edit_schema is EditSchema.STRICT:
        raise_tool_diagnostic(
            message=(
                "No exact match for oldString under strict edit matching. "
                "Fuzzy matching is disabled for this model, so the oldString must match the "
                "current file text exactly."
            ),
            error_kind="ambiguous_match",
            reason="strict_no_exact_match",
            retry_guidance=(
                "Use read on the target path, copy exact current file text without line-number prefixes, then retry edit with that exact oldString."
            ),
            details={
                "edit_schema": "strict",
                "match_count": 0,
                "old_string_line_count": len(old_string.splitlines()),
                "line_number_prefix_suspected": looks_line_number_prefixed(old_string),
            },
        )
    raise_tool_diagnostic(
        message=_edit_mismatch_message(content=current, old_string=old_string, attempted_replacers=attempted_replacers),
        error_kind="tool_input_mismatch",
        reason="old_string_not_found",
        retry_guidance=(
            "Use read on the target path, copy exact current file text without line-number prefixes, then retry edit with that exact oldString."
        ),
        details={
            "attempted_replacers": attempted_replacers,
            "old_string_line_count": len(old_string.splitlines()),
            "line_number_prefix_suspected": looks_line_number_prefixed(old_string),
        },
    )


class EscapeNormalizedReplacer:
    @staticmethod
    def find(content: str, old: str) -> list[str]:
        results: list[str] = []
        # interpret escapes in old
        try:
            old_unescaped = bytes(old, "utf-8").decode("unicode_escape")
        except Exception:
            old_unescaped = old
        variants = [old, old_unescaped]
        for v in variants:
            if not v:
                continue
            start = 0
            while True:
                idx = content.find(v, start)
                if idx == -1:
                    break
                results.append(content[idx : idx + len(v)])
                start = idx + 1
        return results


class MultiOccurrenceReplacer:
    @staticmethod
    def find(content: str, old: str) -> list[str]:
        results: list[str] = []
        if old == "":
            return results
        start = 0
        while True:
            idx = content.find(old, start)
            if idx == -1:
                break
            results.append(content[idx : idx + len(old)])
            start = idx + 1
        return results


class TrimmedBoundaryReplacer:
    @staticmethod
    def find(content: str, old: str) -> list[str]:
        results: list[str] = []
        old_lines = old.split("\n")
        if not old_lines:
            return results
        lines = content.split("\n")
        n = len(old_lines)
        for i in range(len(lines) - n + 1):
            block = "\n".join(lines[i : i + n])
            if block.strip() == old.strip():
                results.append(block)
        return results


class ContextAwareReplacer:
    @staticmethod
    def find(content: str, old: str) -> list[str]:
        results: list[str] = []
        old_lines = old.split("\n")
        if len(old_lines) < 3:
            return results
        lines = content.split("\n")
        n = len(old_lines)
        for i in range(len(lines) - n + 1):
            match = True
            for k in range(n):
                if lines[i + k].strip() != old_lines[k].strip():
                    match = False
                    break
            if match:
                results.append("\n".join(lines[i : i + n]))
        return results


class EditTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="edit",
        description=(
            "Edit a file by replacing text. Supports multiple replacement strategies "
            "for flexible matching. When constructing oldString from read output, "
            "omit any leading line-number prefix such as '42: ' and pass only the "
            "file's actual text."
        ),
        input_schema={
            "path": {
                "type": "string",
                "description": "The path to the file to edit (relative to workspace)",
            },
            "oldString": {
                "type": "string",
                "description": "The text to replace (must match exactly)",
            },
            "newString": {"type": "string", "description": "The text to replace it with"},
            "replaceAll": {
                "type": "boolean",
                "description": "Replace all occurrences of oldString (default: false)",
            },
            "expectedHash": {
                "type": "string",
                "description": (
                    "Required SHA-256 hash of the current file content, taken from data.content_hash "
                    "of a prior read result. Rejects stale edits when the file changed since that read."
                ),
            },
            "required": ["path", "oldString", "newString", "expectedHash"],
        },
        read_only=False,
        path_argument_keys=("path",),
    )

    def __init__(
        self,
        *,
        hooks_config: RuntimeHooksConfig | None = None,
        edit_schema_resolver: EditSchemaResolver | None = None,
    ) -> None:
        self._hooks_config = hooks_config
        self._edit_schema_resolver = edit_schema_resolver

    def _edit_schema_for_current_model(self) -> EditSchema:
        """Resolve the edit matching schema for the invoking model.

        The model comes from the active runtime tool invocation context. With a
        configured resolver the profile is model-specific; otherwise the pure
        policy is applied without effectiveness data, which defaults to
        ``EditSchema.FLEXIBLE`` (current behavior).
        """
        context = current_runtime_tool_context()
        model = context.model if context is not None else None
        if self._edit_schema_resolver is not None:
            return self._edit_schema_resolver(model)
        return select_edit_schema(model, None)

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
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

        resolution = resolve_workspace_path(
            workspace=workspace,
            raw_path=path_value,
            containment_error="edit only allows paths inside the workspace",
            allow_outside_workspace=True,
        )
        workspace_root = resolution.workspace_root
        candidate = resolution.candidate

        if not candidate.exists():
            raise ValueError(f"edit target does not exist: {path_value}")

        if not candidate.is_file():
            raise ValueError(f"edit target is not a file: {path_value}")

        display_path = str(candidate.resolve()) if resolution.is_external else candidate.relative_to(workspace_root).as_posix()
        enforce_read_before_write(
            tool_name=self.definition.name,
            workspace=workspace_root,
            raw_path=path_value,
            candidate=candidate,
            display_path=display_path,
            is_external=resolution.is_external,
        )

        expected_hash = call.arguments.get("expectedHash")
        if not isinstance(expected_hash, str):
            raise_tool_diagnostic(
                message="edit requires an expectedHash argument: the file must be read before it is edited.",
                error_kind="tool_input_mismatch",
                reason="missing_expected_hash",
                retry_guidance=("Use read on the target path, copy data.content_hash from the result, then retry the edit with that expectedHash."),
                details={"path": display_path, "raw_path": path_value},
            )

        content_old = read_utf8_text(candidate)
        actual_hash = hashlib.sha256(content_old.encode("utf-8")).hexdigest()
        if expected_hash != actual_hash:
            raise_tool_diagnostic(
                message="Edit rejected because the file changed since it was read (stale edit).",
                error_kind="stale_edit",
                reason="content_hash_mismatch",
                retry_guidance="Read the file again, use the returned data.content_hash, then retry the edit.",
                details={"expected_hash": expected_hash, "actual_hash": actual_hash, "path": display_path},
            )

        ending = _detect_line_ending(content_old)
        normalized_old = _normalize_line_endings(old_string)
        normalized_new = _normalize_line_endings(new_string)
        normalized_content = _normalize_line_endings(content_old)

        try:
            new_content, match_count, match_ranges = _replace(
                normalized_content,
                normalized_old,
                normalized_new,
                replace_all=replace_all,
                edit_schema=self._edit_schema_for_current_model(),
            )
        except ValueError:
            raise

        for start_line, end_line in match_ranges:
            enforce_seen_lines(
                tool_name=self.definition.name,
                workspace=workspace_root,
                raw_path=path_value,
                candidate=candidate,
                display_path=display_path,
                is_external=resolution.is_external,
                start_line=start_line,
                end_line=end_line,
            )

        new_content = _convert_line_endings(new_content, ending)

        candidate.write_bytes(new_content.encode("utf-8"))
        formatter_result: FormatterExecutionResult | None = None
        if self._hooks_config is not None:
            formatter_result = FormatterExecutor(self._hooks_config, workspace_root).run(candidate)
        final_content = read_utf8_text(candidate)
        diff, additions, deletions = summarize_diff(
            path=candidate,
            before=content_old,
            after=final_content,
        )
        diagnostics = formatter_diagnostics(formatter_result)

        output = "Edit applied successfully."
        if match_count > 1:
            output += f" ({match_count} occurrences replaced)"
        if diagnostics:
            output += f" Formatter warning: {diagnostics[0]['message']}"

        data: dict[str, object] = {
            "path": display_path,
            "additions": additions,
            "deletions": deletions,
            "match_count": match_count,
            "diff": diff,
        }
        if formatter_result is not None and formatter_result.status != "not_configured":
            data["formatter"] = formatter_payload(formatter_result)
        if diagnostics:
            data["diagnostics"] = diagnostics
        lsp_diagnostics = post_edit_lsp_diagnostics(
            workspace=workspace_root,
            paths=[display_path],
        )
        if lsp_diagnostics:
            current_diagnostics = data.get("diagnostics")
            existing = current_diagnostics if isinstance(current_diagnostics, list) else []
            data["diagnostics"] = [*existing, *lsp_diagnostics]
        syntax_diagnostics = post_edit_syntax_diagnostics(
            workspace=workspace_root,
            paths=[display_path],
        )
        if syntax_diagnostics:
            current_diagnostics = data.get("diagnostics")
            existing = current_diagnostics if isinstance(current_diagnostics, list) else []
            data["diagnostics"] = [*existing, *syntax_diagnostics]

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=output,
            data=data,
        )
