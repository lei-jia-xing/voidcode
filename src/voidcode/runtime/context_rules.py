from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast
from urllib.parse import unquote

RULE_FILE_NAME = "AGENTS.md"
MAX_RULE_FILES = 8
MAX_RULE_FILE_CHARS = 12_000


RULEBOOK_DIRECTORY = ".voidcode/rules"
RULEBOOK_LEGACY_FILE = "RULES.md"
MAX_RULEBOOK_FILES = 64
MAX_RULEBOOK_CONTENT_CHARS = 12_000
MAX_RULEBOOK_METADATA_CHARS = 240
MAX_RULEBOOK_PROMPT_CHARS = 12_000
MAX_RULE_URI_LINES = 2_000
MAX_RULE_URI_BYTES = 50 * 1024
RULEBOOK_SNAPSHOT_VERSION = 2
RULE_URI_PREFIX = "voidcode://rule/"
LEGACY_RULE_URI_PREFIX = "rule://"
_RULE_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")

RuleApplication = Literal["always_apply", "discoverable"]
RuleScope = Literal["workspace", "repo"]


class _ToolResultLike(Protocol):
    @property
    def data(self) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class RuntimeFileRuleContext:
    path: str
    content: str
    truncated: bool = False

    def metadata_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "source": "runtime_file_rules",
            "path": self.path,
        }
        if self.truncated:
            payload["truncated"] = True
        return payload


def runtime_file_rule_contexts(
    *,
    workspace: Path | None,
    tool_results: tuple[_ToolResultLike, ...],
    max_rule_files: int = MAX_RULE_FILES,
    max_rule_file_chars: int = MAX_RULE_FILE_CHARS,
    include_workspace_root: bool = True,
) -> tuple[RuntimeFileRuleContext, ...]:
    if workspace is None or max_rule_files < 1:
        return ()
    workspace_root = workspace.resolve(strict=False)
    rule_paths = _applicable_rule_paths(
        workspace_root=workspace_root,
        touched_paths=_touched_paths_from_tool_results(tool_results),
        max_rule_files=max_rule_files,
        include_workspace_root=include_workspace_root,
    )
    contexts: list[RuntimeFileRuleContext] = []
    for rule_path in rule_paths:
        try:
            content = rule_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        truncated = len(content) > max_rule_file_chars
        if truncated:
            content = content[:max_rule_file_chars].rstrip()
            content = f"{content}\n[Rule file truncated by runtime context policy]"
        contexts.append(
            RuntimeFileRuleContext(
                path=rule_path.relative_to(workspace_root).as_posix(),
                content=content.strip(),
                truncated=truncated,
            )
        )
    return tuple(contexts)


def _touched_paths_from_tool_results(tool_results: tuple[_ToolResultLike, ...]) -> tuple[str, ...]:
    paths: set[str] = set()

    def add(value: object) -> None:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                paths.add(stripped)

    for result in tool_results:
        add(result.data.get("path"))
        add(result.data.get("output_path"))
        raw_arguments = result.data.get("arguments")
        if isinstance(raw_arguments, dict):
            arguments = cast(dict[str, object], raw_arguments)
            add(arguments.get("path"))
        raw_matches = result.data.get("matches")
        if isinstance(raw_matches, list | tuple):
            for raw_match in raw_matches:
                if not isinstance(raw_match, dict):
                    continue
                match = cast(dict[str, object], raw_match)
                add(match.get("file"))
                add(match.get("path"))
        raw_changes = result.data.get("changes")
        if isinstance(raw_changes, list | tuple):
            for raw_change in raw_changes:
                if not isinstance(raw_change, dict):
                    continue
                change = cast(dict[str, object], raw_change)
                add(change.get("path"))
    return tuple(sorted(paths))


def _applicable_rule_paths(
    *,
    workspace_root: Path,
    touched_paths: tuple[str, ...],
    max_rule_files: int,
    include_workspace_root: bool,
) -> tuple[Path, ...]:
    ordered: list[Path] = []
    seen: set[Path] = set()

    root_rule_path = workspace_root / RULE_FILE_NAME
    if include_workspace_root and root_rule_path.is_file():
        seen.add(root_rule_path)
        ordered.append(root_rule_path)

    for raw_path in touched_paths:
        touched_path = _workspace_path(workspace_root=workspace_root, raw_path=raw_path)
        if touched_path is None:
            continue
        for rule_path in _candidate_rule_paths(workspace_root=workspace_root, path=touched_path):
            if rule_path in seen or not rule_path.is_file():
                continue
            seen.add(rule_path)
            ordered.append(rule_path)
    if len(ordered) <= max_rule_files:
        return tuple(ordered)
    return tuple(ordered[-max_rule_files:])


def _workspace_path(*, workspace_root: Path, raw_path: str) -> Path | None:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(workspace_root)
    except ValueError:
        return None
    return resolved


def _candidate_rule_paths(*, workspace_root: Path, path: Path) -> tuple[Path, ...]:
    start = path if path.is_dir() else path.parent
    directories: list[Path] = []
    current = start
    while True:
        try:
            current.relative_to(workspace_root)
        except ValueError:
            break
        directories.append(current)
        if current == workspace_root:
            break
        current = current.parent
    return tuple(directory / RULE_FILE_NAME for directory in reversed(directories))


@dataclass(frozen=True, slots=True)
class RuleMetadata:
    name: str
    description: str
    application: RuleApplication
    scope: RuleScope
    precedence: int
    path: str
    content_hash: str

    def payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "application": self.application,
            "scope": self.scope,
            "precedence": self.precedence,
            "uri": f"{RULE_URI_PREFIX}{self.name}",
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True)
class RuleCatalogEntry:
    metadata: RuleMetadata
    content: str


@dataclass(frozen=True, slots=True)
class RulebookSnapshot:
    entries: tuple[RuleMetadata, ...]
    snapshot_hash: str
    snapshot_version: int = RULEBOOK_SNAPSHOT_VERSION


@dataclass(frozen=True, slots=True)
class RuleCatalog:
    entries: tuple[RuleCatalogEntry, ...]
    snapshot: RulebookSnapshot

    @property
    def always_apply(self) -> tuple[RuleCatalogEntry, ...]:
        return tuple(entry for entry in self.entries if entry.metadata.application == "always_apply")

    @property
    def discoverable(self) -> tuple[RuleCatalogEntry, ...]:
        return tuple(entry for entry in self.entries if entry.metadata.application == "discoverable")

    def resolve(self, name: str) -> RuleCatalogEntry:
        for entry in self.entries:
            if entry.metadata.name == name:
                return entry
        raise ValueError(f"unknown rule in runtime catalog: {name}")


def _canonical_rule_bytes(content: str) -> bytes:
    return content.replace("\r\n", "\n").replace("\r", "\n").strip().encode("utf-8")


def _rule_snapshot_hash(entries: tuple[RuleMetadata, ...]) -> str:
    payload = {
        "snapshot_version": RULEBOOK_SNAPSHOT_VERSION,
        "entries": [
            {
                "name": entry.name,
                "application": entry.application,
                "scope": entry.scope,
                "precedence": entry.precedence,
                "content_hash": entry.content_hash,
            }
            for entry in entries
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def rulebook_snapshot_payload(snapshot: RulebookSnapshot) -> dict[str, object]:
    return {
        "snapshot_version": snapshot.snapshot_version,
        "entries": [entry.payload() for entry in snapshot.entries],
        "snapshot_hash": snapshot.snapshot_hash,
    }


def rulebook_snapshot_from_payload(payload: object) -> RulebookSnapshot:
    if not isinstance(payload, dict):
        raise ValueError("persisted rulebook_snapshot must be an object")
    version = payload.get("snapshot_version")
    if version != RULEBOOK_SNAPSHOT_VERSION:
        raise ValueError(f"persisted rulebook_snapshot version must be {RULEBOOK_SNAPSHOT_VERSION}")
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError("persisted rulebook_snapshot entries must be a list")
    entries: list[RuleMetadata] = []
    for raw in cast(list[object], raw_entries):
        if not isinstance(raw, dict):
            raise ValueError("persisted rulebook_snapshot entries must be objects")
        item = cast(dict[str, object], raw)
        fields = ("name", "description", "application", "scope", "precedence", "uri", "content_hash")
        if any(field not in item for field in fields):
            raise ValueError("persisted rulebook_snapshot entry is incomplete")
        name, description, application, scope, content_hash = (
            item.get(key) for key in ("name", "description", "application", "scope", "content_hash")
        )
        precedence = item.get("precedence")
        uri = item.get("uri")
        if not all(isinstance(value, str) for value in (name, description, application, scope, content_hash, uri)):
            raise ValueError("persisted rulebook_snapshot entry fields have invalid types")
        valid_application = application in {"always_apply", "discoverable"}
        valid_scope = scope in {"workspace", "repo"}
        if not isinstance(precedence, int) or isinstance(precedence, bool) or not valid_application or not valid_scope:
            raise ValueError("persisted rulebook_snapshot entry has invalid metadata")
        if uri != f"{RULE_URI_PREFIX}{name}" or not _RULE_NAME_PATTERN.fullmatch(cast(str, name)):
            raise ValueError("persisted rulebook_snapshot entry has invalid rule URI")
        entries.append(
            RuleMetadata(
                cast(str, name),
                cast(str, description),
                cast(RuleApplication, application),
                cast(RuleScope, scope),
                precedence,
                "",
                cast(str, content_hash),
            )
        )
    normalized = tuple(sorted(entries, key=_rule_sort_key))
    expected_hash = _rule_snapshot_hash(normalized)
    snapshot_hash = payload.get("snapshot_hash")
    if snapshot_hash != expected_hash:
        raise ValueError("persisted rulebook_snapshot hash does not match its payload")
    return RulebookSnapshot(entries=normalized, snapshot_hash=cast(str, snapshot_hash))


def _rule_sort_key(metadata: RuleMetadata) -> tuple[int, int, str]:
    return (0 if metadata.scope == "workspace" else 1, metadata.precedence, metadata.name)


def _parse_rule_document(path: Path, text: str, *, workspace_root: Path) -> RuleCatalogEntry | None:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    fields: dict[str, str] = {}
    body = normalized
    lines = normalized.splitlines()
    if lines and lines[0].strip() == "---":
        end = next((index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
        if end is None:
            return None
        for line in lines[1:end]:
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()
        body = "\n".join(lines[end + 1 :])
    relative = path.relative_to(workspace_root).as_posix()
    raw_name = fields.get("name") or path.stem
    name = raw_name.strip().lower()
    if _RULE_NAME_PATTERN.fullmatch(name) is None or "/" in name or "\\" in name:
        return None
    application_value = fields.get("application", fields.get("apply", ""))
    if application_value in {"always", "always_apply", "sticky"}:
        application: RuleApplication = "always_apply"
    elif application_value in {"discoverable", "on_demand", "on-demand"}:
        application = "discoverable"
    else:
        application = "always_apply" if "/always/" in f"/{relative}" or path.name == RULEBOOK_LEGACY_FILE else "discoverable"
    scope_value = fields.get("scope", "workspace")
    if scope_value not in {"workspace", "repo"}:
        return None
    scope = scope_value
    try:
        precedence = int(fields.get("precedence", "100" if application == "always_apply" else "0"))
    except ValueError:
        return None
    if not 0 <= precedence <= 1_000:
        return None
    content = body.strip()
    if not content or len(content) > MAX_RULEBOOK_CONTENT_CHARS:
        content = content[:MAX_RULEBOOK_CONTENT_CHARS].rstrip()
    if not content:
        return None
    description = fields.get("description") or next((line.strip().lstrip("#").strip() for line in content.splitlines() if line.strip()), name)
    description = description[:MAX_RULEBOOK_METADATA_CHARS].strip()
    metadata = RuleMetadata(name, description, application, scope, precedence, relative, hashlib.sha256(_canonical_rule_bytes(content)).hexdigest())
    return RuleCatalogEntry(metadata=metadata, content=content)


def _rule_files(*, workspace_root: Path, rule_roots: tuple[str, ...]) -> tuple[Path, ...]:
    candidates: set[Path] = set()
    legacy = workspace_root / RULEBOOK_LEGACY_FILE
    if legacy.is_file():
        candidates.add(legacy)
    for raw_root in rule_roots:
        root = (workspace_root / raw_root).resolve(strict=False)
        try:
            root.relative_to(workspace_root)
        except ValueError:
            continue
        if not root.is_dir():
            continue
        for candidate in root.rglob("*.md"):
            if not candidate.is_file():
                continue
            resolved = candidate.resolve(strict=False)
            try:
                resolved.relative_to(workspace_root)
            except ValueError:
                continue
            candidates.add(resolved)
    return tuple(sorted(candidates, key=lambda path: path.relative_to(workspace_root).as_posix())[:MAX_RULEBOOK_FILES])


def build_rule_catalog(workspace: Path | None, *, rule_roots: tuple[str, ...] = (RULEBOOK_DIRECTORY,)) -> RuleCatalog:
    if workspace is None:
        empty = RulebookSnapshot((), _rule_snapshot_hash(()))
        return RuleCatalog((), empty)
    workspace_root = workspace.resolve(strict=False)
    parsed: dict[str, RuleCatalogEntry] = {}
    for path in _rule_files(workspace_root=workspace_root, rule_roots=rule_roots):
        try:
            entry = _parse_rule_document(path, path.read_text(encoding="utf-8"), workspace_root=workspace_root)
        except (OSError, UnicodeDecodeError):
            continue
        if entry is None:
            continue
        previous = parsed.get(entry.metadata.name)
        if previous is None or _rule_sort_key(entry.metadata) > _rule_sort_key(previous.metadata):
            parsed[entry.metadata.name] = entry
    entries = tuple(sorted(parsed.values(), key=lambda entry: _rule_sort_key(entry.metadata)))
    snapshot = RulebookSnapshot(tuple(entry.metadata for entry in entries), _rule_snapshot_hash(tuple(entry.metadata for entry in entries)))
    return RuleCatalog(entries, snapshot)


def rulebook_prompt_context(catalog: RuleCatalog) -> str:
    parts: list[str] = []
    if catalog.always_apply:
        parts.append("Runtime rulebook always-apply rules are workspace guidance only; runtime policy remains authoritative.")
        for entry in catalog.always_apply:
            metadata = entry.metadata
            parts.append(f"\nRule {metadata.name} (scope={metadata.scope}, precedence={metadata.precedence}):\n{entry.content}")
    if catalog.discoverable:
        parts.append(f"\nDiscoverable runtime rules (metadata only; read {RULE_URI_PREFIX}<name> when needed):")
        for entry in catalog.discoverable:
            metadata = entry.metadata
            parts.append(
                f"- {metadata.name}: {metadata.description} (scope={metadata.scope}, "
                f"precedence={metadata.precedence}, hash={metadata.content_hash}, uri={RULE_URI_PREFIX}{metadata.name})"
            )
    return "\n".join(parts)[:MAX_RULEBOOK_PROMPT_CHARS]


def rule_uri_name(path: str) -> str:
    if path.startswith(LEGACY_RULE_URI_PREFIX):
        raise ValueError(f"unsupported legacy rule URI: {path}; use {RULE_URI_PREFIX}<name>")
    if not path.startswith(RULE_URI_PREFIX):
        raise ValueError(f"unsupported rule URI: {path}")
    raw_name = path[len(RULE_URI_PREFIX) :]
    decoded = unquote(raw_name)
    if decoded != raw_name or "%" in raw_name or "/" in decoded or "\\" in decoded or ".." in decoded or "?" in decoded or "#" in decoded:
        raise ValueError("invalid rule URI: rule names must be a single safe slug")
    if _RULE_NAME_PATTERN.fullmatch(decoded) is None:
        raise ValueError("invalid rule URI: rule names must be a single safe slug")
    return decoded


def read_rule_uri(path: str, *, workspace: Path, offset: int = 1, limit: int = MAX_RULE_URI_LINES) -> dict[str, object]:
    name = rule_uri_name(path)
    if offset < 1 or limit < 1:
        raise ValueError("rule URI offset and limit must be positive")
    bounded_limit = min(limit, MAX_RULE_URI_LINES)
    entry = build_rule_catalog(workspace).resolve(name)
    lines = entry.content.splitlines()
    selected = lines[offset - 1 : offset - 1 + bounded_limit]
    raw_content = "\n".join(selected)
    encoded = raw_content.encode("utf-8")
    truncated_bytes = len(encoded) > MAX_RULE_URI_BYTES
    if truncated_bytes:
        raw_content = encoded[:MAX_RULE_URI_BYTES].decode("utf-8", errors="ignore")
    next_offset = offset + len(selected) if offset - 1 + len(selected) < len(lines) else None
    return {
        "path": path,
        "type": "rule",
        "rule": name,
        "scope": entry.metadata.scope,
        "precedence": entry.metadata.precedence,
        "application": entry.metadata.application,
        "content_hash": entry.metadata.content_hash,
        "offset": offset,
        "limit": bounded_limit,
        "next_offset": next_offset,
        "truncated": next_offset is not None or truncated_bytes,
        "partial": next_offset is not None or truncated_bytes,
        "raw_content": raw_content,
    }


__all__ = [
    "RuleCatalog",
    "RuleCatalogEntry",
    "RuleMetadata",
    "RulebookSnapshot",
    "RULEBOOK_DIRECTORY",
    "build_rule_catalog",
    "read_rule_uri",
    "rule_uri_name",
    "rulebook_prompt_context",
    "RULE_URI_PREFIX",
    "rulebook_snapshot_from_payload",
    "rulebook_snapshot_payload",
    "runtime_file_rule_contexts",
    "RuntimeFileRuleContext",
]
