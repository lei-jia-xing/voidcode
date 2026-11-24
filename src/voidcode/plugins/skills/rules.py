"""Workspace rule context: the AGENTS.md hierarchy and the ``.voidcode/rules`` rulebook.

Ported from the legacy runtime context rules (``runtime/context/rules.py``) onto the plugin
boundary with the same caps, hierarchy order, truncation math, and prompt text. The plugin
feeds this module the workspace-relative paths its tools touched instead of legacy tool
result bodies.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

RULE_FILE_NAME = "AGENTS.md"
LOCAL_RULE_FILE_NAME = "AGENTS.override.md"
RULEBOOK_DIRECTORY = ".voidcode/rules"
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RuleLimits:
    """One bound per rule-ingestion dimension; shared defaults, never caller guesses."""

    max_rule_files: int = 8
    max_rule_file_chars: int = 12_000
    max_total_rule_bytes: int = 36_000
    max_rulebook_files: int = 64
    max_rulebook_content_chars: int = 12_000
    max_rulebook_metadata_chars: int = 240
    max_rulebook_prompt_chars: int = 12_000


DEFAULT_RULE_LIMITS: Final = RuleLimits()

#: Rule files/rules that are ignored still produce a bounded, visible diagnostic.
MAX_RULE_DIAGNOSTICS = 8
MAX_RULE_DIAGNOSTIC_CHARS = 240

_RULE_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")

RuleApplication = Literal["always_apply", "discoverable"]
RuleScope = Literal["workspace", "repo"]


def _bounded_rule_message(message: object) -> str:
    compact = " ".join(str(message).split())
    if len(compact) <= MAX_RULE_DIAGNOSTIC_CHARS:
        return compact
    return compact[: MAX_RULE_DIAGNOSTIC_CHARS - 3].rstrip() + "..."


def _add_diagnostic(diagnostics: list[str], message: object) -> None:
    msg_str = _bounded_rule_message(message)
    logger.warning("Rules: %s", msg_str)
    if len(diagnostics) > MAX_RULE_DIAGNOSTICS:
        return
    if len(diagnostics) == MAX_RULE_DIAGNOSTICS:
        diagnostics.append("further rule diagnostics omitted")
        return
    diagnostics.append(msg_str)


class RuleDocumentError(ValueError):
    """One rulebook document is structurally invalid; the rest of the catalog stands."""


@dataclass(frozen=True, slots=True)
class RuntimeFileRuleContext:
    path: str
    content: str


@dataclass(frozen=True, slots=True)
class FileRuleResult:
    """Applicable file-rule contexts plus bounded diagnostics for ignored files."""

    contexts: tuple[RuntimeFileRuleContext, ...] = ()
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _RuleCandidate:
    path: Path
    trusted_root: Path


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
    limits: RuleLimits = DEFAULT_RULE_LIMITS,
    include_workspace_root: bool = True,
) -> tuple[RuntimeFileRuleContext, ...]:
    return runtime_file_rule_result(
        workspace=workspace,
        touched_paths=touched_paths,
        limits=limits,
        include_workspace_root=include_workspace_root,
    ).contexts


def runtime_file_rule_result(
    *,
    workspace: Path | None,
    touched_paths: tuple[str, ...] = (),
    limits: RuleLimits = DEFAULT_RULE_LIMITS,
    include_workspace_root: bool = True,
) -> FileRuleResult:
    if workspace is None or limits.max_rule_files < 1 or limits.max_total_rule_bytes < 0:
        return FileRuleResult()
    workspace_root = workspace.resolve(strict=False)
    project_root = _find_project_root(workspace_root)
    rule_paths = _applicable_rule_paths(
        workspace_root=workspace_root,
        project_root=project_root,
        touched_paths=touched_paths,
        max_rule_files=limits.max_rule_files,
        include_workspace_root=include_workspace_root,
    )
    diagnostics: list[str] = []
    if limits.max_total_rule_bytes == 0 and rule_paths:
        first_path = rule_paths[0].path
        return FileRuleResult(contexts=(RuntimeFileRuleContext(path=_display_path(first_path, workspace_root), content=""),))
    contexts: list[RuntimeFileRuleContext] = []
    remaining_bytes = limits.max_total_rule_bytes
    for index, candidate in enumerate(rule_paths):
        if remaining_bytes == 0:
            break
        rule_path = candidate.path
        rel_path = _display_path(rule_path, workspace_root)
        try:
            resolved_path = rule_path.resolve(strict=True)
        except OSError as exc:
            _add_diagnostic(diagnostics, f"{rel_path}: cannot resolve rule file ({exc}); skipped")
            continue
        trusted_root = candidate.trusted_root.resolve(strict=False)
        if not resolved_path.is_relative_to(trusted_root):
            _add_diagnostic(diagnostics, f"{rel_path}: resolves outside the trusted rule root; skipped")
            continue
        try:
            content = resolved_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            _add_diagnostic(diagnostics, f"{rel_path}: unreadable rule file ({exc}); skipped")
            continue
        file_truncated = len(content) > limits.max_rule_file_chars
        content = content[: limits.max_rule_file_chars]
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
        final_content = content.strip()
        used_bytes = len(final_content.encode("utf-8"))
        remaining_bytes -= used_bytes
        contexts.append(RuntimeFileRuleContext(path=rel_path, content=final_content))
        if budget_truncated or (remaining_bytes == 0 and index + 1 < len(rule_paths)):
            break
    return FileRuleResult(contexts=tuple(contexts), diagnostics=tuple(diagnostics))


def _display_path(path: Path, workspace_root: Path) -> str:
    try:
        return path.relative_to(workspace_root).as_posix()
    except ValueError:
        return Path(os.path.relpath(path, workspace_root)).as_posix()


def _applicable_rule_paths(
    *,
    workspace_root: Path,
    project_root: Path,
    touched_paths: tuple[str, ...],
    max_rule_files: int,
    include_workspace_root: bool,
) -> tuple[_RuleCandidate, ...]:
    baseline_rules: list[_RuleCandidate] = []
    seen: set[Path] = set()

    if include_workspace_root:
        for rule_path in _project_hierarchy_rule_paths(workspace_root=workspace_root):
            if rule_path not in seen:
                seen.add(rule_path)
                baseline_rules.append(_RuleCandidate(path=rule_path, trusted_root=project_root))

    dynamic_rules: list[_RuleCandidate] = []
    for raw_path in touched_paths:
        touched_path = _workspace_path(workspace_root=workspace_root, raw_path=raw_path)
        if touched_path is None:
            continue
        for rule_path in _candidate_rule_paths(workspace_root=workspace_root, path=touched_path):
            if rule_path in seen:
                continue
            seen.add(rule_path)
            dynamic_rules.append(_RuleCandidate(path=rule_path, trusted_root=workspace_root))

    # The hierarchy is ordered outermost -> workspace-local. When it exceeds the cap,
    # keep the nearest rules: dropping the outer project policy is safer than dropping
    # the workspace's own instructions. Reserve part of the cap for touched-path rules
    # whose directories are more specific than any baseline file.
    dynamic_reserve = min(len(dynamic_rules), max(0, max_rule_files // 2))
    baseline_budget = max(0, max_rule_files - dynamic_reserve)
    retained_baseline = baseline_rules[-baseline_budget:] if baseline_budget else []
    allowed_dynamic_count = max(0, max_rule_files - len(retained_baseline))
    retained_dynamic = dynamic_rules[-allowed_dynamic_count:] if allowed_dynamic_count else []
    return tuple(retained_baseline + retained_dynamic)


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


def render_rule_diagnostics(diagnostics: tuple[str, ...]) -> str:
    """Bounded visibility for rule files that could not be used."""
    if not diagnostics:
        return ""
    return "Runtime rule diagnostics (some rule files were ignored):\n" + "\n".join(f"- {message}" for message in diagnostics)


@dataclass(frozen=True, slots=True)
class RuleMetadata:
    name: str
    description: str
    application: RuleApplication
    scope: RuleScope
    precedence: int
    path: str

    def payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "application": self.application,
            "scope": self.scope,
            "precedence": self.precedence,
            "path": self.path,
        }


@dataclass(frozen=True, slots=True)
class RuleCatalogEntry:
    metadata: RuleMetadata
    content: str


@dataclass(frozen=True, slots=True)
class RuleCatalog:
    entries: tuple[RuleCatalogEntry, ...]
    diagnostics: tuple[str, ...] = ()

    @property
    def always_apply(self) -> tuple[RuleCatalogEntry, ...]:
        return tuple(entry for entry in self.entries if entry.metadata.application == "always_apply")

    @property
    def discoverable(self) -> tuple[RuleCatalogEntry, ...]:
        return tuple(entry for entry in self.entries if entry.metadata.application == "discoverable")


def _rule_sort_key(metadata: RuleMetadata) -> tuple[int, int, str]:
    return (0 if metadata.scope == "workspace" else 1, metadata.precedence, metadata.name)


def _parse_rule_document(path: Path, text: str) -> RuleCatalogEntry:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    fields: dict[str, str] = {}
    body = normalized
    lines = normalized.splitlines()
    if lines and lines[0].strip() == "---":
        end = next((index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
        if end is None:
            raise RuleDocumentError("frontmatter block is not closed with '---'")
        for line in lines[1:end]:
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()
        body = "\n".join(lines[end + 1 :])
    raw_name = fields.get("name") or path.stem
    name = raw_name.strip()
    if _RULE_NAME_PATTERN.fullmatch(name) is None or "/" in name or "\\" in name:
        raise RuleDocumentError(f"invalid rule name {raw_name!r}")
    application_value = fields.get("application")
    if application_value == "always_apply":
        application: RuleApplication = "always_apply"
    elif application_value == "discoverable":
        application = "discoverable"
    else:
        raise RuleDocumentError("frontmatter 'application' must be 'always_apply' or 'discoverable'")
    scope_value = fields.get("scope", "workspace")
    if scope_value not in {"workspace", "repo"}:
        raise RuleDocumentError("frontmatter 'scope' must be 'workspace' or 'repo'")
    scope = scope_value
    try:
        precedence = int(fields.get("precedence", "100" if application == "always_apply" else "0"))
    except ValueError as exc:
        raise RuleDocumentError("frontmatter 'precedence' must be an integer") from exc
    if not 0 <= precedence <= 1_000:
        raise RuleDocumentError("frontmatter 'precedence' must be between 0 and 1000")
    content = body.strip()
    if not content:
        raise RuleDocumentError("rule document has no body")
    limit = DEFAULT_RULE_LIMITS.max_rulebook_content_chars
    if len(content) > limit:
        marker = "\n[Rule content truncated by runtime context policy]"
        content = content[: max(0, limit - len(marker))].rstrip() + marker
    description = fields.get("description") or next((line.strip().lstrip("#").strip() for line in content.splitlines() if line.strip()), name)
    description = description[: DEFAULT_RULE_LIMITS.max_rulebook_metadata_chars].strip()
    metadata = RuleMetadata(name, description, application, scope, precedence, path.as_posix())
    return RuleCatalogEntry(metadata=metadata, content=content)


def _rule_files(*, workspace_root: Path, rule_roots: tuple[str, ...]) -> tuple[tuple[Path, ...], list[str]]:
    candidates: set[Path] = set()
    diagnostics: list[str] = []
    for raw_root in rule_roots:
        root = (workspace_root / raw_root).resolve(strict=False)
        try:
            root.relative_to(workspace_root)
        except ValueError:
            _add_diagnostic(diagnostics, f"rulebook root '{raw_root}' escapes the workspace; ignored")
            continue
        if not root.is_dir():
            continue
        try:
            found = list(root.rglob("*.md"))
        except OSError as exc:
            _add_diagnostic(diagnostics, f"rulebook root '{raw_root}' cannot be scanned ({exc}); ignored")
            continue
        for candidate in found:
            try:
                resolved = candidate.resolve(strict=True)
            except OSError as exc:
                _add_diagnostic(diagnostics, f"{candidate}: cannot resolve rulebook file ({exc}); skipped")
                continue
            if not resolved.is_relative_to(workspace_root):
                _add_diagnostic(diagnostics, f"{candidate}: resolves outside the workspace; skipped")
                continue
            if not resolved.is_file():
                continue
            candidates.add(resolved)
    ordered = sorted(candidates, key=lambda path: path.relative_to(workspace_root).as_posix())
    if len(ordered) > DEFAULT_RULE_LIMITS.max_rulebook_files:
        _add_diagnostic(
            diagnostics,
            f"rulebook file cap {DEFAULT_RULE_LIMITS.max_rulebook_files} reached; extra files were ignored",
        )
        ordered = ordered[: DEFAULT_RULE_LIMITS.max_rulebook_files]
    return tuple(ordered), diagnostics


def build_rule_catalog(workspace: Path | None, *, rule_roots: tuple[str, ...] = (RULEBOOK_DIRECTORY,)) -> RuleCatalog:
    if workspace is None:
        return RuleCatalog(())
    workspace_root = workspace.resolve(strict=False)
    parsed: dict[str, RuleCatalogEntry] = {}
    paths, diagnostics = _rule_files(workspace_root=workspace_root, rule_roots=rule_roots)
    for path in paths:
        rel_path = _display_path(path, workspace_root)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            _add_diagnostic(diagnostics, f"{rel_path}: unreadable rulebook file ({exc}); skipped")
            continue
        try:
            entry = _parse_rule_document(path, text)
        except RuleDocumentError as exc:
            _add_diagnostic(diagnostics, f"{rel_path}: {exc}; skipped")
            continue
        previous = parsed.get(entry.metadata.name)
        if previous is None or _rule_sort_key(entry.metadata) > _rule_sort_key(previous.metadata):
            parsed[entry.metadata.name] = entry
    entries = tuple(sorted(parsed.values(), key=lambda entry: _rule_sort_key(entry.metadata)))
    return RuleCatalog(entries, tuple(diagnostics))


def rulebook_prompt_context(catalog: RuleCatalog) -> str:
    parts: list[str] = []
    if catalog.always_apply:
        parts.append("Runtime rulebook always-apply rules are workspace guidance only; runtime policy remains authoritative.")
        for entry in catalog.always_apply:
            metadata = entry.metadata
            parts.append(f"\nRule {metadata.name} (scope={metadata.scope}, precedence={metadata.precedence}):\n{entry.content}")
    if catalog.discoverable:
        parts.append("\nDiscoverable runtime rules (read the listed files when needed):")
        for entry in catalog.discoverable:
            metadata = entry.metadata
            parts.append(
                f"- {metadata.name}: {metadata.description} (scope={metadata.scope}, precedence={metadata.precedence}, path={metadata.path})"
            )
    if catalog.diagnostics:
        parts.append("\nRuntime rulebook diagnostics (some rule documents were ignored or truncated):")
        parts.extend(f"- {message}" for message in catalog.diagnostics)
    text = "\n".join(parts)
    limit = DEFAULT_RULE_LIMITS.max_rulebook_prompt_chars
    if len(text) <= limit:
        return text
    marker = "\n[Rulebook prompt truncated by runtime context policy]"
    return text[: max(0, limit - len(marker))] + marker
