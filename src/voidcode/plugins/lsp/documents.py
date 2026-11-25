"""Document synchronization, document-state lookup, and request path scoping."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from voidcode.sdk.provider import ProviderAbortSignal
from voidcode.sdk.services import ProcessServices

from .contracts import ResolvedLspServerConfig
from .lsp_types import LspDocumentState
from .roots import discover_workspace_root
from .server_registry import LocalServerKey, OpenedLspDocument, RunningLspServer

#: Preferred LSP language ids per document extension. A server's configured
#: languages decide which candidate wins; the first configured language is only
#: the fallback when none of the document's candidates are configured.
_LANGUAGE_IDS_BY_EXTENSION: dict[str, tuple[str, ...]] = {
    ".py": ("python",),
    ".pyi": ("python",),
    ".js": ("javascript",),
    ".mjs": ("javascript",),
    ".cjs": ("javascript",),
    ".jsx": ("javascriptreact", "javascript"),
    ".ts": ("typescript",),
    ".mts": ("typescript",),
    ".cts": ("typescript",),
    ".tsx": ("typescriptreact", "typescript"),
    ".go": ("go",),
    ".rs": ("rust",),
    ".zig": ("zig",),
    ".zon": ("zig",),
    ".java": ("java",),
    ".kt": ("kotlin",),
    ".kts": ("kotlin",),
    ".cs": ("csharp",),
    ".csx": ("csharp",),
    ".rb": ("ruby",),
    ".php": ("php",),
    ".phtml": ("php",),
    ".lua": ("lua",),
    ".toml": ("toml",),
    ".c": ("c",),
    ".cc": ("cpp",),
    ".cpp": ("cpp",),
    ".cxx": ("cpp",),
    ".h": ("cpp", "c"),
    ".hh": ("cpp",),
    ".hpp": ("cpp",),
    ".hxx": ("cpp",),
    ".css": ("css",),
    ".scss": ("scss",),
    ".less": ("less",),
    ".html": ("html",),
    ".htm": ("html",),
    ".vue": ("vue",),
    ".svelte": ("svelte",),
    ".astro": ("astro",),
    ".md": ("markdown",),
    ".markdown": ("markdown",),
    ".mdx": ("mdx", "markdown"),
    ".json": ("json",),
    ".jsonc": ("jsonc", "json"),
    ".yaml": ("yaml", "dockercompose"),
    ".yml": ("yaml", "dockercompose"),
    ".sh": ("shell", "bash"),
    ".bash": ("bash", "shell"),
    ".zsh": ("zsh", "shell"),
    ".xml": ("xml",),
    ".xsd": ("xsd", "xml"),
    ".xsl": ("xsl", "xml"),
    ".xslt": ("xslt", "xml"),
    ".svg": ("svg", "xml"),
    ".tex": ("tex",),
    ".bib": ("bibtex",),
    ".cmake": ("cmake",),
    ".dockerfile": ("dockerfile",),
}


class NotificationSender(Protocol):
    def __call__(
        self,
        services: ProcessServices,
        running_server: RunningLspServer,
        *,
        method: str,
        params: dict[str, object],
        abort_signal: ProviderAbortSignal | None,
    ) -> None: ...


def language_id_for_path(path: Path, config: ResolvedLspServerConfig) -> str:
    suffix = path.suffix.lower()
    candidates = _LANGUAGE_IDS_BY_EXTENSION.get(suffix, ())
    for language in candidates:
        if language in config.languages:
            return language
    if len(config.languages) == 1:
        return config.languages[0]
    bare_suffix = suffix.removeprefix(".")
    if bare_suffix in config.languages:
        return bare_suffix
    if config.languages:
        return config.languages[0]
    return bare_suffix or "plaintext"


def document_state(
    running_servers: Mapping[LocalServerKey, RunningLspServer],
    *,
    session_id: str,
    file_path: Path,
    workspace: Path,
) -> LspDocumentState | None:
    path = file_path.resolve()
    uri = path.as_uri()
    expected_workspace = workspace.resolve()
    matches: list[LspDocumentState] = []
    for key, running_server in tuple(running_servers.items()):
        if key[0] != session_id:
            continue
        if not running_server.config.matches_path(path):
            continue
        resolved_root = discover_workspace_root(
            file_path=path,
            workspace_root=expected_workspace,
            root_markers=running_server.config.root_markers,
        )
        if running_server.workspace_root != resolved_root:
            continue
        document = running_server.documents.get(uri)
        if document is None:
            continue
        try:
            actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        if actual_hash != document.content_hash:
            continue
        matches.append(
            LspDocumentState(
                version=document.version,
                content_hash=document.content_hash,
                position_encoding=running_server.position_encoding,
            )
        )
    return matches[0] if len(matches) == 1 else None


def synchronize_document(
    services: ProcessServices,
    running_server: RunningLspServer,
    file_path: Path,
    *,
    notify: NotificationSender,
) -> LspDocumentState | None:
    if not running_server.document_sync_enabled:
        return None
    path = file_path.resolve()
    if not path.is_relative_to(running_server.workspace_root) or not running_server.config.matches_path(path):
        raise ValueError(f"LSP document is outside the configured server workspace: {path}")
    try:
        content_bytes = path.read_bytes()
        content = content_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"LSP document is unavailable as UTF-8 text: {path}") from exc

    content_hash = hashlib.sha256(content_bytes).hexdigest()
    uri = path.as_uri()
    opened = running_server.documents.get(uri)
    if opened is not None and opened.content_hash == content_hash:
        return LspDocumentState(
            version=opened.version,
            content_hash=opened.content_hash,
            position_encoding=running_server.position_encoding,
        )

    version = opened.version + 1 if opened is not None else 1
    if opened is None:
        language_id = language_id_for_path(path, running_server.config)
        notify(
            services,
            running_server,
            method="textDocument/didOpen",
            params={
                "textDocument": {
                    "uri": uri,
                    "languageId": language_id,
                    "version": version,
                    "text": content,
                }
            },
            abort_signal=None,
        )
    else:
        notify(
            services,
            running_server,
            method="textDocument/didChange",
            params={
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [{"text": content}],
            },
            abort_signal=None,
        )
    running_server.documents[uri] = OpenedLspDocument(version, content_hash)
    return LspDocumentState(
        version=version,
        content_hash=content_hash,
        position_encoding=running_server.position_encoding,
    )


def text_document_path(params: dict[str, object]) -> Path | None:
    document = params.get("textDocument")
    if not isinstance(document, dict) or not isinstance(document.get("uri"), str):
        return None
    return path_from_file_uri(document["uri"])


def candidate_uris(params: dict[str, object]) -> tuple[str, ...]:
    uris: list[str] = []
    for container_key in ("textDocument", "item"):
        raw_container = params.get(container_key)
        if not isinstance(raw_container, dict):
            continue
        uri = raw_container.get("uri")
        if isinstance(uri, str):
            uris.append(uri)
    return tuple(uris)


def request_file_path(params: dict[str, object]) -> Path | None:
    for uri in candidate_uris(params):
        candidate = path_from_file_uri(uri)
        if candidate is not None:
            return candidate.resolve()
    return None


def path_from_file_uri(uri: str) -> Path | None:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    raw_path = parsed.path
    if parsed.netloc and parsed.netloc != "localhost":
        raw_path = f"//{parsed.netloc}{parsed.path}"
        # Python 3.14 rejects remote file authorities on POSIX. Preserve the
        # UNC-style path expected by LSP clients while still decoding escapes.
        if os.name != "nt":
            return Path(unquote(raw_path))
    return Path(url2pathname(raw_path))


def workspace_root_for_request(
    *,
    workspace: Path,
    request_path: Path | None,
    server_config: ResolvedLspServerConfig,
) -> Path:
    workspace_root = workspace.resolve()
    if request_path is None:
        return workspace_root
    return discover_workspace_root(
        file_path=request_path,
        workspace_root=workspace_root,
        root_markers=server_config.root_markers,
    )
