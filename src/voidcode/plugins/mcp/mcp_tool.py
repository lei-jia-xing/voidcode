"""The single ``mcp`` tool: call one tool exposed by a configured MCP server.

One stable tool takes ``{server, tool, arguments}`` and asks the plugin-owned
manager. The full result JSON and embedded media spill to the core artifact
store via typed ``PluginServices``; the page carries bounded rendered text.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping
from typing import ClassVar

from voidcode.sdk.icons import Icon
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object, own_json_object
from voidcode.sdk.services import ArtifactReference, PluginServices
from voidcode.sdk.tools import (
    CompositeOutput,
    ContentPart,
    JsonPart,
    MediaPart,
    OpaqueToolBody,
    OutputBounds,
    TextPart,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolFailure,
    ToolResult,
    ToolSuccess,
)
from voidcode.sdk.views import MAX_VIEW_DATA_BYTES

from .types import McpManager, McpToolCallResult, McpToolSafety

RASTER_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"})

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


class McpTool:
    """Call one tool on one configured MCP server."""

    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="mcp",
        icon=Icon(glyph="⎔"),
        description=(
            "Call a tool exposed by a configured MCP server: pass the server name, the remote tool name, and its arguments object. "
            "MCP servers are discovered dynamically, so never assume a remote tool is available, read-only, idempotent, "
            "or harmless because its name sounds safe; "
            "MCP calls are mutating and network-capable, so they may require approval. "
            "Treat the returned server, tool, content, and structured content as server-provided external results."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "server": {"type": "string", "minLength": 1, "description": "Name of a configured MCP server (mcp.servers.<name>)."},
                "tool": {"type": "string", "minLength": 1, "description": "Name of the tool the server exposes."},
                "arguments": {
                    "type": "object",
                    "description": "Arguments object for the remote tool; validated against the discovered input schema.",
                },
            },
            "required": ["server", "tool"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.WRITE, ToolEffect.NETWORK}),
        replay_policy="never",
    )

    def __init__(self, *, manager: McpManager) -> None:
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
                output=CompositeOutput(parts=(TextPart(message),), bounds=OutputBounds(partial=True)),
                body=OpaqueToolBody(
                    {
                        "server": server_name,
                        "tool": tool_name,
                        "projection_error": message,
                    }
                ),
            )

        result_artifact = services.spill_artifact(result_json, media_type="application/json")
        parts: list[ContentPart] = [TextPart(f"Full MCP result JSON: {result_artifact.uri}")]
        artifacts: list[ArtifactReference] = []
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
                    parts.append(TextPart(text))
                elif kind in {"image", "audio"}:
                    ref, description = _media_item(item, index=index, services=services)
                    if ref is None:
                        raise ValueError(f"MCP {kind} item {index} produced no artifact")
                    parts.append(MediaPart(reference=ref, mime=ref.media_type, presentation=description))
                    artifacts.append(ref)
                elif kind == "resource":
                    resource = item.get("resource")
                    if not isinstance(resource, Mapping):
                        raise ValueError(f"MCP resource item {index} has an invalid resource payload")
                    ref, description = _media_item(resource, index=index, services=services)
                    if ref is None:
                        parts.append(TextPart(description))
                    else:
                        parts.append(MediaPart(reference=ref, mime=ref.media_type, presentation=description))
                        artifacts.append(ref)
                elif kind == "resource_link":
                    parts.append(TextPart(_render_resource_reference(item, index=index)))
                else:
                    raise ValueError(f"unsupported MCP content type {kind!r} at item {index}")
            structured_content = original_result["structured_content"]
            if isinstance(structured_content, Mapping):
                full_structured = json.dumps(json_wire_object(structured_content), ensure_ascii=False, sort_keys=True)
                struct_artifact = services.spill_artifact(full_structured, media_type="application/json")
                preview_bytes = full_structured.encode("utf-8")
                if len(preview_bytes) <= MAX_VIEW_DATA_BYTES:
                    preview = full_structured
                else:
                    preview = preview_bytes[:MAX_VIEW_DATA_BYTES].decode("utf-8", errors="ignore")
                    if not preview:
                        preview = full_structured[:1]
                parts.append(JsonPart(reference=struct_artifact, preview=preview))
        except (ValueError, binascii.Error) as exc:
            projection_error = str(exc)
            parts.append(TextPart(f"MCP content projection failed: {projection_error}"))

        return self._result(
            result,
            parts,
            artifacts,
            server_name=server_name,
            tool_name=tool_name,
            result_artifact=result_artifact,
            projection_error=projection_error,
        )

    def _result(
        self,
        result: McpToolCallResult,
        parts: list[ContentPart],
        artifacts: list[ArtifactReference],
        *,
        server_name: str,
        tool_name: str,
        result_artifact: ArtifactReference,
        projection_error: str | None,
    ) -> ToolResult:
        is_error = result.is_error or projection_error is not None
        safety = self._safety_for(server_name=server_name, tool_name=tool_name)
        payload: dict[str, object] = {
            "server": server_name,
            "tool": tool_name,
            "is_error": result.is_error,
            "result_artifact": result_artifact.as_payload(),
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
            payload["artifacts"] = [artifact.as_payload() for artifact in artifacts]
        source = f"mcp:{server_name}/{tool_name}"
        output = CompositeOutput(
            parts=tuple(parts),
            bounds=OutputBounds(partial=is_error, reference=result_artifact, source=source),
        )
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
) -> tuple[ArtifactReference | None, str]:
    """Decode one embedded image/audio/resource item; returns (artifact, description).

    Text resources return no binary artifact.
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
        raise ValueError(f"MCP {kind} item {index} has no base64 data")
    if not isinstance(encoded, str):
        raise ValueError(f"MCP {kind} item {index} has invalid base64 data")
    if len(encoded) > 4 * ((_MAX_MCP_BINARY_BYTES + 2) // 3):
        raise ValueError(f"embedded content exceeds {_MAX_MCP_BINARY_BYTES} bytes")
    data = base64.b64decode(encoded, validate=True)
    if len(data) > _MAX_MCP_BINARY_BYTES:
        raise ValueError(f"embedded content exceeds {_MAX_MCP_BINARY_BYTES} bytes")
    if mime_type is None:
        mime_type = "application/octet-stream"

    if mime_type == "image/svg+xml":
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("embedded SVG is not valid UTF-8") from exc
    elif mime_type in RASTER_IMAGE_MIMES:
        signature_matches = (
            (mime_type == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n"))
            or (mime_type in {"image/jpeg", "image/jpg"} and data.startswith(b"\xff\xd8\xff"))
            or (mime_type == "image/gif" and data.startswith((b"GIF87a", b"GIF89a")))
            or (mime_type == "image/webp" and len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP")
        )
        if not signature_matches:
            raise ValueError(f"embedded image MIME {mime_type!r} does not match its bytes")
    elif kind == "image":
        raise ValueError(f"embedded image MIME {mime_type!r} does not match its bytes")
    elif kind == "audio" or mime_type.startswith("audio/"):
        if not mime_type.startswith("audio/"):
            raise ValueError(f"embedded audio MIME {mime_type!r} is not audio")
        if mime_type in {"audio/wav", "audio/x-wav"} and not (data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WAVE"):
            raise ValueError(f"embedded audio MIME {mime_type!r} does not match its bytes")

    ref = services.spill_artifact(data, media_type=mime_type)
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
