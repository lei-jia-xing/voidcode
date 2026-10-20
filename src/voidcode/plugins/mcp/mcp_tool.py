"""The single ``mcp`` tool: call one tool exposed by a configured MCP server.

Ported from ``voidcode.tools.mcp``. The per-server ``mcp/<server>/<tool>``
adapters are gone: one stable manifest-declared tool takes
``{server, tool, arguments}`` and asks the plugin-owned manager. Artifact spill
goes through the plugin-local artifact boundary (``artifacts.py``) instead of
the runtime tool-output store.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping

from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object, own_json_object
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import (
    AttachmentOutput,
    EmptyOutput,
    OpaqueToolBody,
    OutputBounds,
    OutputReference,
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolFailure,
    ToolResult,
    ToolSuccess,
)

from .artifacts import (
    MAX_ARTIFACT_PAGE_BYTES,
    MAX_INLINE_ATTACHMENT_BYTES,
    RASTER_IMAGE_MIMES,
    artifact_uri,
    create_artifact,
)
from .types import McpManager, McpToolCallResult, McpToolSafety

_MAX_MCP_BINARY_BYTES = 50 * 1024 * 1024
_MIME_TOKEN = r"[!#$%&'*+.^_`|~0-9A-Za-z-]+"
_MIME_TYPE_PATTERN = re.compile(rf"{_MIME_TOKEN}/{_MIME_TOKEN}\Z")
_MIME_PARAMETER_PATTERN = re.compile(_MIME_TOKEN + r"\s*=\s*(?:" + _MIME_TOKEN + r'|"(?:[^"\\\x00-\x1f\x7f]|\\.)*")\Z')


def _mime_type(value: object, *, required: bool) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("MCP content MIME type is missing or invalid")
    parts: list[str] = []
    start = 0
    in_quotes = escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
        elif in_quotes and character == "\\":
            escaped = True
        elif character == '"':
            in_quotes = not in_quotes
        elif character == ";" and not in_quotes:
            parts.append(value[start:index])
            start = index + 1
    if in_quotes or escaped:
        raise ValueError(f"MCP content MIME type {value!r} is invalid")
    parts.append(value[start:])
    mime_type = parts[0].strip()
    if not _MIME_TYPE_PATTERN.fullmatch(mime_type) or any(_MIME_PARAMETER_PATTERN.fullmatch(parameter.strip()) is None for parameter in parts[1:]):
        raise ValueError(f"MCP content MIME type {value!r} is invalid")
    return mime_type.lower()


def _call_arguments(raw: Mapping[str, object]) -> tuple[str, str, dict[str, object]]:
    server_name = raw.get("server")
    tool_name = raw.get("tool")
    if not isinstance(server_name, str) or not server_name:
        raise ValueError("mcp requires a non-empty 'server' argument")
    if not isinstance(tool_name, str) or not tool_name:
        raise ValueError("mcp requires a non-empty 'tool' argument")
    raw_arguments = raw.get("arguments")
    arguments = {} if raw_arguments is None else raw_arguments
    if not isinstance(arguments, Mapping):
        raise ValueError("mcp 'arguments' must be an object")
    return server_name, tool_name, json_wire_object(arguments)


def _spill_artifact(
    services: PluginServices,
    *,
    kind: str,
    content: str | bytes,
    mime: str,
    source: str,
) -> dict[str, object] | None:
    """Spill a payload through the kernel artifact service; None keeps it inline.

    ``None`` only when the handler's services object exposes no spill method: the
    payload stays inline (bounded) and the output says truncation happened.
    """
    artifact = create_artifact(services, kind=kind, content=content, mime=mime, source=source)
    uri = artifact_uri(artifact)
    if artifact is None or uri is None:
        return None
    payload = dict(artifact)
    payload["uri"] = uri
    return payload


def _bounded_inline_text(value: str, *, limit: int) -> tuple[str, bool]:
    """Bound one inline string the way an artifact page bounds a spilled one."""
    if len(value) <= limit:
        return value, False
    return value[:limit], True


class McpTool:
    """Call one tool on one configured MCP server."""

    def __init__(self, *, definition: ToolDefinition, manager: McpManager) -> None:
        self.definition = definition
        self._manager = manager

    def invoke(self, call: ToolCall, *, context: ToolContext, services: PluginServices) -> ToolResult:
        workspace = context.require_workspace()
        server_name, tool_name, arguments = _call_arguments(call.arguments)
        result = self._manager.call_tool(
            server_name=server_name,
            tool_name=tool_name,
            arguments=arguments,
            workspace=workspace,
        )
        try:
            if type(result.is_error) is not bool:
                raise ValueError("MCP result is_error must be a boolean")
            if not isinstance(result.content, list):
                raise ValueError("MCP result content must be an array")
            if result.structured_content is not None and not isinstance(result.structured_content, Mapping):
                raise ValueError("MCP structured content must be an object")
            original_result = own_json_object(
                {
                    "content": result.content,
                    "structured_content": result.structured_content,
                    "is_error": result.is_error,
                }
            )
            result_json = json.dumps(
                json_wire_object(original_result),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            message = f"MCP result is not valid JSON: {exc}"
            return ToolFailure(
                tool_name=self.definition.name,
                error=message,
                output=TextOutput(message, bounds=OutputBounds(partial=True)),
                body=OpaqueToolBody(
                    {
                        "server": server_name,
                        "tool": tool_name,
                        "projection_error": message,
                    }
                ),
            )

        source = f"mcp:{server_name}/{tool_name}"
        result_artifact = _spill_artifact(services, kind="mcp-result", content=result_json, mime="application/json", source=source)
        rendered: list[str] = []
        artifacts: list[dict[str, object]] = []
        projection_error: str | None = None
        try:
            content_items = original_result["content"]
            if not isinstance(content_items, tuple):
                raise ValueError("MCP content must be an array")
            for index, raw_item in enumerate(content_items):
                if not isinstance(raw_item, Mapping):
                    raise ValueError(f"MCP content item {index} must be an object")
                item = dict(raw_item)
                kind = item.get("type")
                if kind == "text":
                    text = item.get("text")
                    if not isinstance(text, str):
                        raise ValueError(f"MCP text item {index} has an invalid text payload")
                    rendered.append(text)
                elif kind in {"image", "audio"}:
                    ref, description = _media_item(item, index=index, services=services, source=source)
                    rendered.append(description)
                    if ref is not None:
                        artifacts.append(ref)
                elif kind in {"resource", "embedded_resource"}:
                    resource = item.get("resource")
                    if not isinstance(resource, Mapping):
                        if kind == "embedded_resource":
                            raise ValueError(f"MCP {kind} item {index} has an invalid resource payload")
                        resource = item
                    ref, description = _media_item(resource, index=index, services=services, source=source)
                    rendered.append(description)
                    if ref is not None:
                        artifacts.append(ref)
                elif kind == "resource_link":
                    rendered.append(_render_resource_reference(item, index=index))
                else:
                    raise ValueError(f"unsupported MCP content type {kind!r} at item {index}")
            structured_content = original_result["structured_content"]
            if isinstance(structured_content, Mapping):
                rendered.append("Structured content:\n" + json.dumps(json_wire_object(structured_content), ensure_ascii=False, sort_keys=True))
        except (ValueError, binascii.Error) as exc:
            projection_error = str(exc)
            rendered.append(f"MCP content projection failed: {projection_error}")

        return self._result(
            result,
            rendered,
            artifacts,
            server_name=server_name,
            tool_name=tool_name,
            original_result=original_result,
            result_artifact=result_artifact,
            result_json=result_json,
            projection_error=projection_error,
        )

    def _result(
        self,
        result: McpToolCallResult,
        rendered: list[str],
        artifacts: list[dict[str, object]],
        *,
        server_name: str,
        tool_name: str,
        original_result: Mapping[str, object],
        result_artifact: dict[str, object] | None,
        result_json: str,
        projection_error: str | None,
    ) -> ToolResult:
        result_uri = None if result_artifact is None else result_artifact.get("uri")
        if result_artifact is not None and (not isinstance(result_uri, str) or not result_uri):
            raise RuntimeError("MCP result JSON artifact has no URI")
        referenced_artifacts: list[tuple[str, dict[str, object]]] = []
        for artifact in artifacts:
            uri = artifact.get("uri")
            if isinstance(uri, str):
                referenced_artifacts.append((uri, artifact))
        references = [uri for uri, _ in referenced_artifacts]
        json_truncated = False
        result_reference: OutputReference | None = None
        if isinstance(result_uri, str):
            rendered.insert(0, f"Full MCP result JSON: {result_uri}")
            result_reference = OutputReference(result_uri, result_artifact)
        else:
            # With no spill method to reference a store, the full result JSON is
            # inlined and bounded instead.
            inline_json, json_truncated = _bounded_inline_text(result_json, limit=MAX_ARTIFACT_PAGE_BYTES)
            suffix = " (truncated: no artifact storage for the full JSON)" if json_truncated else ""
            rendered.insert(0, f"Full MCP result JSON (inline){suffix}:\n{inline_json}")
        if references:
            rendered.insert(1, "Embedded output artifacts: " + ", ".join(references))
        content = "\n\n".join(part for part in rendered if part)
        is_error = result.is_error or projection_error is not None
        safety = self._safety_for(server_name=server_name, tool_name=tool_name)
        payload: dict[str, object] = {
            "server": server_name,
            "tool": tool_name,
            **json_wire_object(original_result),
            "result_artifact": result_artifact,
            "safety": {
                "read_only": safety.read_only,
                "destructive": safety.destructive,
                "idempotent": safety.idempotent,
                "open_world": safety.open_world,
                "source": safety.source,
            },
        }
        if projection_error is not None:
            payload["projection_error"] = projection_error
        if artifacts:
            payload["artifacts"] = ([result_artifact] if result_artifact is not None else []) + artifacts
        image_artifacts = [artifact for artifact in artifacts if artifact.get("kind") == "mcp-image"]
        image_artifact = image_artifacts[0] if len(image_artifacts) == 1 else None
        artifact_size = image_artifact.get("byte_count") if image_artifact is not None else None
        artifact_uri_value = image_artifact.get("uri") if image_artifact is not None else None
        artifact_mime = image_artifact.get("mime") if image_artifact is not None else None
        first_reference = OutputReference(*referenced_artifacts[0]) if referenced_artifacts else None
        image_reference = (
            OutputReference(artifact_uri_value, image_artifact) if image_artifact is not None and isinstance(artifact_uri_value, str) else None
        )
        bounds = OutputBounds(truncated=json_truncated, partial=is_error, reference=image_reference or first_reference or result_reference)
        if (
            image_artifact is not None
            and type(artifact_size) is int
            and artifact_size <= MAX_INLINE_ATTACHMENT_BYTES
            and isinstance(artifact_mime, str)
            and image_reference is not None
        ):
            output = AttachmentOutput(
                artifact_mime,
                data_uri=None,
                presentation=content,
                bounds=OutputBounds(truncated=json_truncated, partial=is_error, reference=image_reference),
            )
        elif content:
            output = TextOutput(content, bounds=OutputBounds(truncated=json_truncated, partial=is_error, reference=result_reference))
        else:
            output = EmptyOutput(bounds=bounds)
        if is_error:
            error = (
                f"MCP content projection failed: {projection_error}"
                if projection_error is not None
                else f"MCP tool {server_name}/{tool_name} reported an error"
            )
            return ToolFailure(
                tool_name=self.definition.name,
                error=error,
                output=output,
                body=OpaqueToolBody(payload),
            )
        return ToolSuccess(
            tool_name=self.definition.name,
            output=output,
            body=OpaqueToolBody(payload),
        )

    def _safety_for(self, *, server_name: str, tool_name: str) -> McpToolSafety:
        """Advisory safety hints for the called tool; default-deny when unknown.

        Hints are for inspection only: local policy and the declared effects
        decide governance, never a remote annotation.
        """
        descriptor = self._manager.descriptor_for(server_name=server_name, tool_name=tool_name)
        return McpToolSafety() if descriptor is None else descriptor.safety


def _media_item(
    item: Mapping[str, object],
    *,
    index: int,
    services: PluginServices,
    source: str,
) -> tuple[dict[str, object] | None, str]:
    """Decode one embedded image/audio/resource item; returns (artifact, description).

    ``None`` means the bytes could not be spilled, so nothing references them.
    """
    kind = item.get("type")
    if kind is None and ("text" in item or "blob" in item):
        kind = "resource"
    if kind not in {"image", "audio", "resource"}:
        raise ValueError(f"unsupported MCP media type {kind!r} at item {index}")
    mime_type = _mime_type(item.get("mimeType"), required=kind in {"image", "audio"})
    uri = item.get("uri")
    if uri is not None and (not isinstance(uri, str) or not uri):
        raise ValueError(f"MCP resource item {index} has an invalid URI")
    if kind == "resource" and not isinstance(uri, str):
        raise ValueError(f"MCP resource item {index} has no URI")
    description = f"[MCP {kind} item {index}"
    if mime_type is not None:
        description += f", MIME {mime_type}"
    if isinstance(uri, str):
        description += f", URI {uri}"

    text = item.get("text")
    if kind == "resource" and text is not None:
        if not isinstance(text, str):
            raise ValueError("resource text payload is invalid")
        return None, description + f"]\n{text}"

    encoded = item.get("blob") if kind == "resource" else item.get("data")
    if encoded is None:
        if kind == "resource":
            return None, description + "; remote resource is not fetched]"
        raise ValueError(f"MCP {kind} item {index} has no base64 data")
    if not isinstance(encoded, str):
        raise ValueError(f"MCP {kind} item {index} has invalid base64 data")
    if len(encoded) > 4 * ((_MAX_MCP_BINARY_BYTES + 2) // 3):
        raise ValueError(f"embedded content exceeds {_MAX_MCP_BINARY_BYTES} bytes")
    data = base64.b64decode(encoded, validate=True)
    if len(data) > _MAX_MCP_BINARY_BYTES:
        raise ValueError(f"embedded content exceeds {_MAX_MCP_BINARY_BYTES} bytes")
    if mime_type is None:
        raise ValueError(f"MCP resource blob {index} has no MIME type")

    artifact_kind = "mcp-resource"
    if mime_type == "image/svg+xml":
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("embedded SVG is not valid UTF-8") from exc
        artifact_kind = "mcp-svg"
    elif mime_type in RASTER_IMAGE_MIMES:
        signature_matches = (
            (mime_type == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n"))
            or (mime_type in {"image/jpeg", "image/jpg"} and data.startswith(b"\xff\xd8\xff"))
            or (mime_type == "image/gif" and data.startswith((b"GIF87a", b"GIF89a")))
            or (mime_type == "image/webp" and len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP")
        )
        if not signature_matches:
            raise ValueError(f"embedded image MIME {mime_type!r} does not match its bytes")
        artifact_kind = "mcp-image"
    elif kind == "image":
        raise ValueError(f"embedded image MIME {mime_type!r} does not match its bytes")
    elif kind == "audio" or mime_type.startswith("audio/"):
        if not mime_type.startswith("audio/"):
            raise ValueError(f"embedded audio MIME {mime_type!r} is not audio")
        if mime_type in {"audio/wav", "audio/x-wav"} and not (data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WAVE"):
            raise ValueError(f"embedded audio MIME {mime_type!r} does not match its bytes")
        artifact_kind = "mcp-audio"
    elif kind == "audio":
        raise ValueError(f"embedded audio MIME {mime_type!r} is not audio")

    spilled = _spill_artifact(services, kind=artifact_kind, content=data, mime=mime_type, source=source)
    if spilled is None:
        # Without a spill method the bytes cannot be referenced, so they stay out
        # of the transcript.
        return None, description + f"; {len(data)} embedded bytes omitted: no artifact storage]"
    ref = {**spilled, "kind": artifact_kind, "mime": mime_type, "byte_count": len(data)}
    return ref, description + f"; {len(data)} embedded bytes saved as output artifact]"


def _render_resource_reference(item: Mapping[str, object], *, index: int) -> str:
    uri = item.get("uri")
    if not isinstance(uri, str) or not uri:
        raise ValueError(f"MCP resource link {index} has no URI")
    fields = [f"URI={uri!r}"]
    for key in ("title", "name", "description"):
        value = item.get(key)
        if value is not None:
            if not isinstance(value, str):
                raise ValueError(f"MCP resource link {index} has invalid {key}")
            fields.append(f"{key}={value!r}")
    mime_type = _mime_type(item.get("mimeType"), required=False)
    if mime_type is not None:
        fields.append(f"MIME={mime_type!r}")
    text = item.get("text")
    if text is not None and not isinstance(text, str):
        raise ValueError(f"MCP resource link {index} has invalid text")
    reference = f"[MCP resource reference {index}: {', '.join(fields)}; remote resources are not fetched]"
    return reference + (f"\n{text}" if isinstance(text, str) and text else "")
