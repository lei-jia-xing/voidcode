"""Pilot: shell_exec declares its result-body schema; peers validate without imports."""

from __future__ import annotations

from dataclasses import replace

import jsonschema
import pytest

from voidcode.capabilities.exec.shell_exec import SHELL_EXEC_RESULT_SCHEMA, ShellExecResultBody, ShellExecTool
from voidcode.sdk.codec import decode_tool_result, encode_tool_result
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.result_schema import ToolBodyValidationError, resolve_output_schema, try_validate_tool_body
from voidcode.sdk.services import ArtifactReference
from voidcode.sdk.tools import EmptyOutput, OpaqueToolBody, ToolFailure, ToolSuccess

_DIGEST = "ab" * 32


def _body(**overrides: object) -> ShellExecResultBody:
    base = ShellExecResultBody(
        command="echo ok",
        exit_code=0,
        stdout=ArtifactReference(_DIGEST, 3),
        stderr=ArtifactReference(_DIGEST, 0),
        timeout=120,
        truncated=False,
        cwd="/work",
    )
    return base if not overrides else replace(base, **overrides)  # type: ignore[arg-type]


def _peer_roundtrip(body: ShellExecResultBody) -> ToolSuccess[OpaqueToolBody]:
    """What an unrelated consumer holds: decoded wire JSON, never the owner's class."""
    decoded = decode_tool_result(encode_tool_result(ToolSuccess("shell_exec", EmptyOutput(), body)))
    assert isinstance(decoded, ToolSuccess)
    return decoded


def test_pilot_schema_is_declared_and_valid() -> None:
    assert resolve_output_schema(ShellExecTool.definition) is not None
    jsonschema.Draft202012Validator.check_schema(json_wire_object(SHELL_EXEC_RESULT_SCHEMA))


def test_peer_validates_good_return() -> None:
    validated = try_validate_tool_body(ShellExecTool.definition, _peer_roundtrip(_body()))
    assert validated is not None
    assert validated["command"] == "echo ok"
    assert validated["stdout"] == {"sha256": _DIGEST, "bytes": 3, "media_type": "application/octet-stream"}


def test_peer_rejects_tampered_return() -> None:
    decoded = _peer_roundtrip(_body())
    assert decoded.body is not None
    tampered = dict(decoded.body.as_payload())
    del tampered["stdout"]
    with pytest.raises(ToolBodyValidationError) as caught:
        try_validate_tool_body(ShellExecTool.definition, ToolSuccess("shell_exec", EmptyOutput(), OpaqueToolBody(tampered)))
    assert caught.value.tool_name == "shell_exec"
    assert "stdout" in caught.value.message


def test_failure_bodies_never_validate() -> None:
    failure = ToolFailure(tool_name="shell_exec", error="nope", body=_body())
    with pytest.raises(ToolBodyValidationError, match="failure results"):
        try_validate_tool_body(ShellExecTool.definition, failure)


@pytest.mark.parametrize(
    "overrides",
    [
        {"timed_out": True},
        {"cancelled": True, "reason": "user_stop"},
        {"cancelled": True, "reason": None},
        {"output_limit_exceeded": True, "truncated": True},
        {"exit_code": None},
        {"cwd": None},
    ],
)
def test_schema_covers_every_capture_branch(overrides: dict[str, object]) -> None:
    assert try_validate_tool_body(ShellExecTool.definition, _peer_roundtrip(_body(**overrides))) is not None
