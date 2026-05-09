from __future__ import annotations

from pathlib import Path
from typing import cast

from .runtime_context import RuntimeLspToolFacade, current_runtime_tool_context


def post_edit_lsp_diagnostics(*, workspace: Path, paths: list[str]) -> list[dict[str, object]]:
    context = current_runtime_tool_context()
    if context is None:
        return []
    lsp: RuntimeLspToolFacade | None = context.lsp
    if lsp is None:
        return []

    diagnostics: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw_path in paths:
        if raw_path in seen:
            continue
        seen.add(raw_path)
        try:
            payload = lsp.request_diagnostics(
                file_path=raw_path,
                workspace=str(workspace.resolve()),
            )
        except Exception as exc:
            diagnostics.append(
                {
                    "path": raw_path,
                    "source": "lsp",
                    "severity": "warning",
                    "message": f"Automatic diagnostics failed: {exc}",
                }
            )
            continue

        response = payload.get("lsp_response")
        response_dict = cast(dict[str, object], response) if isinstance(response, dict) else None
        result = response_dict.get("result") if response_dict is not None else None
        if not isinstance(result, dict):
            continue
        result_dict = cast(dict[str, object], result)
        items = result_dict.get("items")
        if not isinstance(items, list):
            continue
        for item in cast(list[object], items):
            if not isinstance(item, dict):
                continue
            diagnostic = cast(dict[str, object], item)
            start = diagnostic.get("range")
            start_dict = cast(dict[str, object], start) if isinstance(start, dict) else None
            start_position = start_dict.get("start") if start_dict is not None else None
            start_position_dict = (
                cast(dict[str, object], start_position)
                if isinstance(start_position, dict)
                else None
            )
            diagnostics.append(
                {
                    "path": raw_path,
                    "source": "lsp",
                    "severity": diagnostic.get("severity"),
                    "message": diagnostic.get("message"),
                    "code": diagnostic.get("code"),
                    "line": (
                        cast(int, start_position_dict.get("line")) + 1
                        if start_position_dict is not None
                        and isinstance(start_position_dict.get("line"), int)
                        else None
                    ),
                    "character": (
                        cast(int, start_position_dict.get("character")) + 1
                        if start_position_dict is not None
                        and isinstance(start_position_dict.get("character"), int)
                        else None
                    ),
                }
            )
    return diagnostics


__all__ = ["post_edit_lsp_diagnostics"]
