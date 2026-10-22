"""Workspace rule context: the AGENTS.md hierarchy and the ``.voidcode/rules`` rulebook.

Ported from the legacy runtime context rules (``runtime/context/rules.py``) onto the plugin
boundary with the same caps, hierarchy order, truncation math, and prompt text. The plugin
feeds this module the workspace-relative paths its tools touched instead of legacy tool
result bodies.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from voidcode.sdk.workspace_queries import RULE_URI_PREFIX, RulePage

RULE_FILE_NAME = "AGENTS.md"
LOCAL_RULE_FILE_NAME = "AGENTS.override.md"
MAX_RULE_FILES = 8
MAX_RULE_FILE_CHARS = 12_000
MAX_TOTAL_RULE_BYTES = 36_000

RULEBOOK_DIRECTORY = ".voidcode/rules"
MAX_RULEBOOK_FILES = 64
MAX_RULEBOOK_CONTENT_CHARS = 12_000
MAX_RULEBOOK_METADATA_CHARS = 240
MAX_RULEBOOK_PROMPT_CHARS = 12_000
#: Rule URI prefix, shared verbatim with the files plugin's ``voidcode://rule/`` reader.

_RULE_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")

RuleApplication = Literal["always_apply", "discoverable"]
RuleScope = Literal["workspace", "repo"]


@dataclass(frozen=True, slots=True)
class RuntimeFileRuleContext:
    path: str
    content: str


def _rule_file_in_directory(directory: Path) -> Path | None:
    local_override = directory / LOCAL_RULE_FILE_NAME
    if local_override.is_file():
        return local_override
    standard = directory / RULE_FILE_NAME
    if standard.is_file():
        return standard
    return None


def _find_project_root(workspace_root: Path) -> Path:
    current = workspace_root
    while True:
        git_marker = current / ".git"
        if git_marker.exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return workspace_root


def _project_hierarchy_rule_paths(*, workspace_root: Path) -> tuple[Path, ...]:
    project_root = _find_project_root(workspace_root)
    directories: list[Path] = []
    current = workspace_root
    while True:
        directories.append(current)
        if current == project_root:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    directories.reverse()

    rule_paths: list[Path] = []
    for directory in directories:
        rule_path = _rule_file_in_directory(directory)
        if rule_path is not None:
            rule_paths.append(rule_path)
    return tuple(rule_paths)


def runtime_file_rule_contexts(
    *,
    workspace: Path | None,
    touched_paths: tuple[str, ...] = (),
    max_rule_files: int = MAX_RULE_FILES,
    max_rule_file_chars: int = MAX_RULE_FILE_CHARS,
    max_total_rule_bytes: int = MAX_TOTAL_RULE_BYTES,
    include_workspace_root: bool = True,
) -> tuple[RuntimeFileRuleContext, ...]:
    if workspace is None or max_rule_files < 1 or max_total_rule_bytes < 0:
        return ()
    workspace_root = workspace.resolve(strict=False)
    rule_paths = _applicable_rule_paths(
        workspace_root=workspace_root,
        touched_paths=touched_paths,
        max_rule_files=max_rule_files,
        include_workspace_root=include_workspace_root,
    )
    if max_total_rule_bytes == 0 and rule_paths:
        first_path = rule_paths[0]
        try:
            rel_path = first_path.relative_to(workspace_root).as_posix()
        except ValueError:
            rel_path = Path(os.path.relpath(first_path, workspace_root)).as_posix()
        return (RuntimeFileRuleContext(path=rel_path, content=""),)
    contexts: list[RuntimeFileRuleContext] = []
    remaining_bytes = max_total_rule_bytes
    for index, rule_path in enumerate(rule_paths):
        if remaining_bytes == 0:
            break
        try:
            content = rule_path.read_text(encoding="utf-8")
        except OSError, UnicodeDecodeError:
            continue
        file_truncated = len(content) > max_rule_file_chars
        content = content[:max_rule_file_chars]
        encoded = content.encode("utf-8")
        marker = b"\n[Rule file truncated by runtime context policy]"
        budget_truncated = len(encoded) > remaining_bytes
        if len(encoded) > remaining_bytes:
            marker_bytes = marker if remaining_bytes >= len(marker) else b""
            prefix = encoded[: remaining_bytes - len(marker_bytes)]
            content = prefix.decode("utf-8", errors="ignore") + marker_bytes.decode("utf-8")
        elif file_truncated:
            marker_bytes = marker if remaining_bytes - len(encoded) >= len(marker) else b""
            content += marker_bytes.decode("utf-8")
        try:
            rel_path = rule_path.relative_to(workspace_root).as_posix()
        except ValueError:
            rel_path = Path(os.path.relpath(rule_path, workspace_root)).as_posix()
        final_content = content.strip()
        used_bytes = len(final_content.encode("utf-8"))
        remaining_bytes -= used_bytes
        contexts.append(RuntimeFileRuleContext(path=rel_path, content=final_content))
        if budget_truncated or (remaining_bytes == 0 and index + 1 < len(rule_paths)):
            break
    return tuple(contexts)


def _applicable_rule_paths(
    *,
    workspace_root: Path,
    touched_paths: tuple[str, ...],
    max_rule_files: int,
    include_workspace_root: bool,
) -> tuple[Path, ...]:
    baseline_rules: list[Path] = []
    seen: set[Path] = set()

    if include_workspace_root:
        for rule_path in _project_hierarchy_rule_paths(workspace_root=workspace_root):
            if rule_path not in seen:
                seen.add(rule_path)
                baseline_rules.append(rule_path)

    dynamic_rules: list[Path] = []
    for raw_path in touched_paths:
        touched_path = _workspace_path(workspace_root=workspace_root, raw_path=raw_path)
        if touched_path is None:
            continue
        for rule_path in _candidate_rule_paths(workspace_root=workspace_root, path=touched_path):
            if rule_path in seen:
                continue
            seen.add(rule_path)
            dynamic_rules.append(rule_path)

    if len(baseline_rules) >= max_rule_files:
        return tuple(baseline_rules[:max_rule_files])

    allowed_dynamic_count = max_rule_files - len(baseline_rules)
    retained_dynamic = dynamic_rules[-allowed_dynamic_count:] if len(dynamic_rules) > allowed_dynamic_count else dynamic_rules
    return tuple(baseline_rules + retained_dynamic)


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
    candidates: list[Path] = []
    for directory in reversed(directories):
        rule_path = _rule_file_in_directory(directory)
        if rule_path is not None:
            candidates.append(rule_path)
    return tuple(candidates)


def render_file_rule_context(context: RuntimeFileRuleContext) -> str:
    """The legacy provider framing of one file-rule injection."""
    return f"Runtime file rules are active for touched workspace paths.\nRule file: {context.path}\n{context.content}".strip()


def render_file_rules(contexts: tuple[RuntimeFileRuleContext, ...]) -> str:
    rendered = [text for context in contexts if (text := render_file_rule_context(context))]
    return "\n\n".join(rendered)


@dataclass(frozen=True, slots=True)
class RuleMetadata:
    name: str
    description: str
    application: RuleApplication
    scope: RuleScope
    precedence: int
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
class RuleCatalog:
    entries: tuple[RuleCatalogEntry, ...]

    @property
    def always_apply(self) -> tuple[RuleCatalogEntry, ...]:
        return tuple(entry for entry in self.entries if entry.metadata.application == "always_apply")

    @property
    def discoverable(self) -> tuple[RuleCatalogEntry, ...]:
        return tuple(entry for entry in self.entries if entry.metadata.application == "discoverable")


def _canonical_rule_bytes(content: str) -> bytes:
    return content.replace("\r\n", "\n").replace("\r", "\n").strip().encode("utf-8")


def _rule_sort_key(metadata: RuleMetadata) -> tuple[int, int, str]:
    return (0 if metadata.scope == "workspace" else 1, metadata.precedence, metadata.name)


def _parse_rule_document(path: Path, text: str) -> RuleCatalogEntry | None:
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
    raw_name = fields.get("name") or path.stem
    name = raw_name.strip()
    if _RULE_NAME_PATTERN.fullmatch(name) is None or "/" in name or "\\" in name:
        return None
    application_value = fields.get("application")
    if application_value == "always_apply":
        application: RuleApplication = "always_apply"
    elif application_value == "discoverable":
        application = "discoverable"
    else:
        return None
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
    metadata = RuleMetadata(name, description, application, scope, precedence, hashlib.sha256(_canonical_rule_bytes(content)).hexdigest())
    return RuleCatalogEntry(metadata=metadata, content=content)


def _rule_files(*, workspace_root: Path, rule_roots: tuple[str, ...]) -> tuple[Path, ...]:
    candidates: set[Path] = set()
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
        return RuleCatalog(())
    workspace_root = workspace.resolve(strict=False)
    parsed: dict[str, RuleCatalogEntry] = {}
    for path in _rule_files(workspace_root=workspace_root, rule_roots=rule_roots):
        try:
            entry = _parse_rule_document(path, path.read_text(encoding="utf-8"))
        except OSError, UnicodeDecodeError:
            continue
        if entry is None:
            continue
        previous = parsed.get(entry.metadata.name)
        if previous is None or _rule_sort_key(entry.metadata) > _rule_sort_key(previous.metadata):
            parsed[entry.metadata.name] = entry
    entries = tuple(sorted(parsed.values(), key=lambda entry: _rule_sort_key(entry.metadata)))
    return RuleCatalog(entries)


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


def read_rule_page(workspace: Path | None, uri: str, *, offset: int = 1, limit: int = 2000) -> RulePage | None:
    """Read one line page of the rulebook rule named by a ``voidcode://rule/<name>`` URI.

    The same catalog the context hook publishes (`build_rule_catalog`), so a URI the model was
    shown always resolves to the same text; ``None`` means no such rule exists in this workspace.
    """
    name = uri[len(RULE_URI_PREFIX) :] if uri.startswith(RULE_URI_PREFIX) else uri
    if _RULE_NAME_PATTERN.fullmatch(name.strip()) is None:
        return None
    catalog = build_rule_catalog(workspace)
    entry = next((item for item in catalog.entries if item.metadata.name == name.strip()), None)
    if entry is None:
        return None
    lines = entry.content.splitlines()
    start = max(1, offset) - 1
    window = lines[start : start + max(1, limit)]
    end = start + len(window)
    return RulePage(
        rule=entry.metadata.name,
        content_hash=entry.metadata.content_hash,
        text="\n".join(window),
        next_offset=end + 1 if end < len(lines) else None,
    )
