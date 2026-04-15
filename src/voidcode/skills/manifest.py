from __future__ import annotations

FRONTMATTER_DELIMITER = "---"
SUPPORTED_FRONTMATTER_KEYS = frozenset({"name", "description"})


def parse_skill_frontmatter(contents: str) -> dict[str, str]:
    lines = contents.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER_DELIMITER:
        raise ValueError("skill file must begin with a simplified frontmatter block")

    parsed: dict[str, str] = {}
    for index, raw_line in enumerate(lines[1:], start=2):
        line = raw_line.strip()
        if line == FRONTMATTER_DELIMITER:
            break
        if not line:
            continue
        key, separator, value = raw_line.partition(":")
        if separator != ":":
            raise ValueError(f"skill frontmatter line {index} must use 'key: value' syntax")
        normalized_key = key.strip()
        if normalized_key not in SUPPORTED_FRONTMATTER_KEYS:
            raise ValueError(f"unsupported skill frontmatter key: {normalized_key}")
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError(f"skill frontmatter field '{normalized_key}' must not be empty")
        parsed[normalized_key] = normalized_value
    else:
        raise ValueError("skill frontmatter must terminate with a closing '---' line")

    missing_keys = SUPPORTED_FRONTMATTER_KEYS.difference(parsed)
    if missing_keys:
        missing = ", ".join(sorted(missing_keys))
        raise ValueError(f"skill frontmatter missing required fields: {missing}")
    return parsed


def parse_skill_body(contents: str) -> str:
    lines = contents.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER_DELIMITER:
        raise ValueError("skill file must begin with a simplified frontmatter block")

    for index, raw_line in enumerate(lines[1:], start=2):
        if raw_line.strip() == FRONTMATTER_DELIMITER:
            return "\n".join(lines[index:]).strip()

    raise ValueError("skill frontmatter must terminate with a closing '---' line")
