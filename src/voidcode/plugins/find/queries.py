"""Files-plugin grep/read execution for ``find``."""

from __future__ import annotations

from voidcode.capabilities.files.find_queries import (
    FIND_GREP,
    FIND_READ,
    FindGrepRequest,
    FindGrepResult,
    FindReadRequest,
    FindReadResult,
)
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import PluginServices

from .models import FindArgs


class _FindCancelled(Exception):
    """Abort landed inside a nested grep/read call; the outer invoke reports it."""


# NOTE (query-composition fence): find asks the files plugin for its grep/read
# results instead of importing those tools. The files plugin answers by invoking
# the same GrepTool/ReadTool implementations the model uses, so behavior, path
# validation and effects stay owned there; disabling files removes the query and
# find fails visibly instead of reading the workspace itself. Intermediate pages
# reach no session model: grep matches feed a tool-free judge prompt plus local
# validation, and read pages become excerpts stored in the typed body, never in
# output.text — so attaching zero ReadObservation coverage is correct. Revisit
# the day excerpts enter output.text.


def _cancelled(tool_context: ToolContext) -> bool:
    return tool_context.abort_signal is not None and tool_context.abort_signal.cancelled


def _grep_request(args: FindArgs, *, pattern: str, limit: int, context: int, ignore_case: bool) -> FindGrepRequest:
    return FindGrepRequest(
        pattern=pattern,
        path=args.path,
        ignore_case=ignore_case,
        context=context,
        include=args.include,
        exclude=args.exclude,
        respect_gitignore=args.respect_gitignore,
        limit=limit,
    )


def _run_grep(
    args: FindArgs,
    *,
    pattern: str,
    limit: int,
    context: int,
    tool_context: ToolContext,
    services: PluginServices,
    ignore_case: bool = True,
) -> FindGrepResult:
    """Ask files for one grep result; unavailable/contract failures propagate."""
    if _cancelled(tool_context):
        raise _FindCancelled("grep search cancelled")
    result = FIND_GREP.ask(services, _grep_request(args, pattern=pattern, limit=limit, context=context, ignore_case=ignore_case))
    if _cancelled(tool_context):
        raise _FindCancelled("grep search cancelled")
    return result


def _read_segment(path: str, *, offset: int, limit: int, tool_context: ToolContext, services: PluginServices) -> FindReadResult:
    """Ask files for one text page; unavailable/contract failures propagate."""
    if _cancelled(tool_context):
        raise _FindCancelled("read cancelled")
    result = FIND_READ.ask(services, FindReadRequest(path=path, offset=offset, limit=limit))
    if _cancelled(tool_context):
        raise _FindCancelled("read cancelled")
    return result


def _render_excerpt(body: FindReadResult) -> str:
    return "\n".join(f"{record.line}: {record.text}" for record in body.lines)
