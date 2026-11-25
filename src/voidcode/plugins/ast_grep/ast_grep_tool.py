"""Stable facade for the ast_grep plugin.

The implementation is split into cohesive modules: ``args``, ``targets``,
``process``, ``language``, ``rendering``, ``search_tool``, and
``replace_tool``. Modules depend only downward, and this facade is the single
import surface for plugin registration, tests, and historical callers.

No orchestration lives here. Public names are listed in ``__all__``; private
names remain importable because existing suites exercise helpers directly.
Monkeypatching a helper or constant should target its owning module.
"""

from __future__ import annotations

# ruff: noqa: F401 - compatibility re-exports are the point of this facade.
from .args import _DEFAULT_LIMIT, _SHA256_TEXT, AstGrepArgs, AstGrepReplaceArgs
from .language import (
    _below_search_root,
    _directory_globs,
    _file_matches_filters,
    _filter_matches,
    _ignore_flags,
    _infer_language,
    _match_resolved_path,
    _native_jsonl,
    _resolved_language,
    _surface_relative_path,
)
from .process import (
    _APPLY_CAPTURE_BYTES,
    _DIAGNOSTIC_CHARS,
    _MATCH_CAPTURE_BYTES,
    _OUTLINE_CAPTURE_BYTES,
    _extract_matches,
    _parse_applied_count,
    _parse_outline_language,
    _parse_stream_output,
    _raise_on_process_failure,
    _run_ast_grep,
    _short,
    _spill_output,
    _stop_failure,
    _truncated_or_failed,
    _warning_lines,
)
from .rendering import (
    _MATCH_PROJECTION_BUDGET_BYTES,
    _match_body_entry,
    _match_result_output,
    _render_single_match,
    _search_budget,
    _select_matches_for_budget,
    _warnings_block,
    _with_match_counts,
)
from .replace_tool import AstGrepReplaceTool
from .search_tool import AstGrepTool
from .targets import (
    _file_digest,
    _invalid_path,
    _load_replace_target,
    _raise_stale_edit,
    _ReplaceTarget,
    _resolve_replace_candidate,
    _resolve_search_candidate,
)

__all__ = [
    "AstGrepArgs",
    "AstGrepReplaceArgs",
    "AstGrepReplaceTool",
    "AstGrepTool",
]
