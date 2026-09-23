"""Shared YAML frontmatter syntax for markdown-backed assets.

Custom agent manifests, slash commands, and workspace skills all read a
``---``-delimited YAML block from a markdown file. This module owns the
*syntax* of that block and nothing else: where the block starts and ends, how
it decodes into a mapping, and the shared input bounds (safe loading, size
limit, duplicate-key rejection, string keys).

Each domain keeps owning its own field whitelist, required fields, value
types, and authority checks. Domain modules must not re-implement the block
loop; they call :func:`split_frontmatter` and :func:`load_frontmatter_mapping`.

Location: shared syntax, deliberately outside ``runtime/`` (no governance),
``graph/`` (no step semantics), and ``tools/`` (no tool behavior).
"""

from __future__ import annotations

from typing import Final

import yaml

FRONTMATTER_DELIMITER: Final[str] = "---"

# Frontmatter is metadata, not content. Bound it so a malformed or hostile
# markdown file cannot make the loader assemble an unbounded YAML document.
MAX_FRONTMATTER_CHARS: Final[int] = 64 * 1024

_MERGE_KEY_TAG: Final[str] = "tag:yaml.org,2002:merge"


class FrontmatterError(ValueError):
    """Shared frontmatter syntax error.

    Domains wrap or re-raise it: the agent registry and skill manifest wrap it
    with their own file-scoped message, and their domain-specific exception
    types (for example ``SkillManifestParseError``) keep working.
    """


def _located(source: str | None, message: str) -> str:
    return f"{source}: {message}" if source else message


def split_frontmatter(
    text: str,
    *,
    require_delimiter: bool = True,
    require_closing: bool = True,
    require_body: bool = False,
    strip_body: bool = True,
    source: str | None = None,
) -> tuple[str | None, str]:
    """Split markdown ``text`` into ``(raw_frontmatter, markdown_body)``.

    The block framing rules differ per domain, so they are options instead of
    three copies of the loop:

    ``require_delimiter``
        ``True`` (agent manifests, skills): the file must open with a ``---``
        line. ``False`` (slash commands): plain markdown is accepted and
        ``(None, text)`` is returned.
    ``require_closing``
        ``True`` (agent manifests, skills): an unterminated block is an error.
        ``False`` (slash commands): an unterminated block is not frontmatter at
        all, so ``(None, text)`` is returned.
    ``require_body``
        ``True`` (agent manifests): the markdown body after the block is
        required to be non-empty.
    ``strip_body``
        ``True`` (agent manifests, skills): surrounding whitespace is removed
        from the returned body. ``False`` (slash commands): everything after
        the closing delimiter line is returned verbatim, so a command template
        keeps its exact trailing whitespace.

    ``source`` is an optional location prefix used only when the caller does
    not add its own: pass the file path when the error is not wrapped with
    one, and pass ``None`` when the caller prefixes the path itself (agent
    manifests and skill manifests do).

    A delimiter is recognized when a line's stripped content is exactly
    ``---``, so trailing whitespace and CRLF endings are tolerated and a
    ``---``-prefixed value inside the block no longer truncates it early.
    """

    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != FRONTMATTER_DELIMITER:
        if require_delimiter:
            raise FrontmatterError(_located(source, "must start with a YAML frontmatter delimiter '---' line"))
        return None, text

    for index, raw_line in enumerate(lines[1:], start=1):
        if raw_line.strip() != FRONTMATTER_DELIMITER:
            continue
        body = "".join(lines[index + 1 :])
        if require_body and not body.strip():
            raise FrontmatterError(_located(source, "must not have an empty markdown body after the frontmatter block"))
        return "".join(lines[1:index]), body.strip() if strip_body else body

    if require_closing:
        raise FrontmatterError(_located(source, "must close the YAML frontmatter block with a '---' line"))
    return None, text


def load_frontmatter_mapping(raw: str | None, *, source: str | None = None) -> dict[str, object]:
    """Decode a raw frontmatter block into a string-keyed mapping.

    ``raw`` is the first element of :func:`split_frontmatter`; ``None`` (no
    frontmatter block) and an empty block both decode to ``{}``.

    The block is loaded with a :class:`yaml.SafeLoader` subclass, never the
    default loader. Compared to hand-rolled parsing, standard YAML semantics
    apply: implicit typing (``yes``/``no``/``on``/``off`` are booleans,
    numeric and date-looking scalars are numbers and dates), quoting, flow
    sequences/mappings, block scalars, and ``#`` comments in unquoted values.
    Domains stay responsible for accepting or rejecting those types.
    """

    if raw is None:
        return {}
    if len(raw) > MAX_FRONTMATTER_CHARS:
        raise FrontmatterError(
            _located(
                source,
                f"frontmatter must not exceed {MAX_FRONTMATTER_CHARS} characters (got {len(raw)})",
            )
        )
    try:
        loaded = yaml.load(raw, Loader=_FrontmatterLoader)
    except _FrontmatterSyntaxError as exc:
        raise FrontmatterError(_located(source, exc.message)) from exc
    except yaml.YAMLError as exc:
        raise FrontmatterError(_located(source, _yaml_error_message(exc))) from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise FrontmatterError(_located(source, f"frontmatter must be a mapping, not {type(loaded).__name__}"))
    return loaded


class _FrontmatterSyntaxError(Exception):
    """Internal carrier so loader-level failures keep line information."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _check_mapping_keys(loader: yaml.SafeLoader, node: yaml.MappingNode) -> None:
    seen: list[str] = []
    for key_node, _value_node in node.value:
        if key_node.tag == _MERGE_KEY_TAG:
            continue
        line = key_node.start_mark.line + 1
        key = loader.construct_object(key_node, deep=False)
        if not isinstance(key, str):
            raise _FrontmatterSyntaxError(
                f"frontmatter key {key_node.value!r} on line {line} must be a string; YAML resolved it to {type(key).__name__}"
            )
        if key in seen:
            raise _FrontmatterSyntaxError(f"duplicate frontmatter key {key!r} on line {line}")
        seen.append(key)


class _FrontmatterLoader(yaml.SafeLoader):
    """``SafeLoader`` that rejects duplicate and non-string mapping keys."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[object, object]:
        if isinstance(node, yaml.MappingNode):
            _check_mapping_keys(self, node)
        return super().construct_mapping(node, deep=deep)


def _yaml_error_message(exc: yaml.YAMLError) -> str:
    if isinstance(exc, yaml.MarkedYAMLError) and exc.problem_mark is not None:
        line = exc.problem_mark.line + 1
        column = exc.problem_mark.column + 1
        problem = exc.problem or "invalid YAML"
        context = f"{exc.context} " if exc.context else ""
        return f"invalid YAML frontmatter on line {line}, column {column}: {context}{problem}"
    return f"invalid YAML frontmatter: {exc}"


__all__ = [
    "FRONTMATTER_DELIMITER",
    "MAX_FRONTMATTER_CHARS",
    "FrontmatterError",
    "load_frontmatter_mapping",
    "split_frontmatter",
]
