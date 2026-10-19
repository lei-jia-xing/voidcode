"""Bounded YAML frontmatter parsing for markdown-backed plugin resources.

Ported from the legacy shared helper (``src/voidcode/common/frontmatter.py``) with the same
framing rules, size bound and mapping checks: a plugin artifact is deployed on its own, so it
carries its own copy instead of importing a shared module.

Framing differences between callers are options, not three copies of the loop:
``require_delimiter`` (skills and agent manifests must open with ``---``), ``require_closing``
(an unterminated block is an error), ``require_body`` (the markdown body must be non-empty) and
``strip_body`` (whitespace handling).
"""

from __future__ import annotations

from typing import Final

import yaml

FRONTMATTER_DELIMITER: Final[str] = "---"
#: Frontmatter is metadata, not content: bound it so a malformed or hostile
#: file cannot make the loader assemble an unbounded YAML document.
MAX_FRONTMATTER_CHARS: Final[int] = 64 * 1024

_MERGE_KEY_TAG: Final[str] = "tag:yaml.org,2002:merge"


class FrontmatterError(ValueError):
    """Frontmatter syntax error; callers wrap it with their own file-scoped message."""


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

    A delimiter is recognized when a line's stripped content is exactly ``---``, so trailing
    whitespace and CRLF endings are tolerated and a ``---``-prefixed value inside the block
    does not truncate it early.
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

    ``None`` (no frontmatter block) and an empty block both decode to ``{}``. The block is
    loaded with a ``yaml.SafeLoader`` subclass, never the default loader.
    """
    if raw is None:
        return {}
    if len(raw) > MAX_FRONTMATTER_CHARS:
        raise FrontmatterError(_located(source, f"frontmatter must not exceed {MAX_FRONTMATTER_CHARS} characters (got {len(raw)})"))
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
