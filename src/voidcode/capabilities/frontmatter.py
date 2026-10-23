"""Bounded Markdown frontmatter using PyYAML, shared by agents and skills."""

import yaml

MAX_FRONTMATTER_CHARS = 64 * 1024


class FrontmatterError(ValueError):
    """Invalid Markdown framing or YAML metadata."""


def split_frontmatter(text: str, *, require_body: bool = False) -> tuple[str, str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise FrontmatterError("must start with a YAML frontmatter delimiter '---' line")
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            body = "".join(lines[index + 1 :]).strip()
            if require_body and not body:
                raise FrontmatterError("must not have an empty markdown body after the frontmatter block")
            return "".join(lines[1:index]), body
    raise FrontmatterError("must close the YAML frontmatter block with a '---' line")


class _Loader(yaml.SafeLoader):
    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[object, object]:
        seen: set[str] = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue
            key = self.construct_object(key_node, deep=False)
            line = key_node.start_mark.line + 1
            if not isinstance(key, str):
                raise FrontmatterError(
                    f"frontmatter key {key_node.value!r} on line {line} must be a string; YAML resolved it to {type(key).__name__}"
                )
            if key in seen:
                raise FrontmatterError(f"duplicate frontmatter key {key!r} on line {line}")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def load_frontmatter_mapping(raw: str) -> dict[str, object]:
    if len(raw) > MAX_FRONTMATTER_CHARS:
        raise FrontmatterError(f"frontmatter must not exceed {MAX_FRONTMATTER_CHARS} characters (got {len(raw)})")
    try:
        loaded = yaml.load(raw, Loader=_Loader)
    except yaml.MarkedYAMLError as exc:
        if exc.problem_mark is None:
            raise FrontmatterError(f"invalid YAML frontmatter: {exc}") from exc
        mark = exc.problem_mark
        context = f"{exc.context} " if exc.context else ""
        raise FrontmatterError(
            f"invalid YAML frontmatter on line {mark.line + 1}, column {mark.column + 1}: {context}{exc.problem or 'invalid YAML'}"
        ) from exc
    except yaml.YAMLError as exc:
        raise FrontmatterError(f"invalid YAML frontmatter: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise FrontmatterError(f"frontmatter must be a mapping, not {type(loaded).__name__}")
    return loaded
