from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from ...core.questions import PendingQuestionOption, PendingQuestionPrompt, QuestionResponse
from ...core.turns import ReportedCall
from ...security.json_values import json_wire_object, own_json_object
from ...tools.contracts import (
    AttachmentOutput,
    ConfirmedStop,
    EmptyOutput,
    OpaqueToolBody,
    OutputBounds,
    OutputReference,
    ProgressYield,
    QuestionAnswered,
    QuestionPrepared,
    TerminalYield,
    TerminalYieldFailure,
    TextOutput,
    ToolControl,
    ToolDiagnostics,
    ToolFailure,
    ToolOutput,
    ToolResult,
    ToolSuccess,
    UnconfirmedStop,
)
from ...tools.read import ReadResultBody


def _output_payload(output: ToolOutput) -> dict[str, object]:
    bounds = output.bounds
    payload: dict[str, object] = {
        "bounds": {
            "truncated": bounds.truncated,
            "partial": bounds.partial,
            "source": bounds.source,
            "fallback_reason": bounds.fallback_reason,
            "reference": None
            if bounds.reference is None
            else {
                "uri": bounds.reference.uri,
                "artifact": None if bounds.reference.artifact is None else json_wire_object(bounds.reference.artifact),
            },
        }
    }
    if isinstance(output, TextOutput):
        payload.update({"kind": "text", "text": output.text, "presentation": output.presentation})
    elif isinstance(output, AttachmentOutput):
        payload.update({"kind": "attachment", "mime": output.mime, "data_uri": output.data_uri, "presentation": output.presentation})
    elif isinstance(output, EmptyOutput):
        payload.update({"kind": "empty", "presentation": output.presentation})
    else:
        raise TypeError(f"unsupported typed output: {type(output).__name__}")
    return payload


def _control_payload(control: ToolControl | None) -> dict[str, object] | None:
    if control is None:
        return None
    if isinstance(control, QuestionPrepared):
        return {
            "kind": "question_prepared",
            "prompts": [
                {
                    "question": prompt.question,
                    "header": prompt.header,
                    "multiple": prompt.multiple,
                    "options": [{"label": option.label, "description": option.description} for option in prompt.options],
                }
                for prompt in control.prompts
            ],
        }
    if isinstance(control, QuestionAnswered):
        return {"kind": "question_answered", "responses": [{"header": item.header, "answers": list(item.answers)} for item in control.responses]}
    if isinstance(control, ProgressYield):
        return {"kind": "progress", "types": list(control.types), "result": control.result, "data": json_wire_object(control.data)}
    if isinstance(control, TerminalYield):
        return {"kind": "terminal", "summary": control.summary, "data": json_wire_object(control.data)}
    if isinstance(control, TerminalYieldFailure):
        return {"kind": "terminal_failure", "data": json_wire_object(control.data)}
    raise TypeError(f"unsupported typed control: {type(control).__name__}")


def report_payload(report: ReportedCall) -> dict[str, object]:
    """Versioned authoritative report shape; flat event fields remain presentation only."""
    result = report.result
    payload: dict[str, object] = {
        "version": 1,
        "tool_call_id": report.tool_call_id,
        "final_tool_name": report.final_tool_name,
        "arguments": json_wire_object(report.authorized_arguments),
        "result": None,
    }
    encoded_result: dict[str, object] = {
        "kind": "failure" if isinstance(result, ToolFailure) else "success",
        "tool_name": result.tool_name,
        "output": _output_payload(result.output),
        "body": None if result.body is None else json_wire_object(result.body.as_payload()),
        "control": _control_payload(result.control),
    }
    payload["result"] = encoded_result
    if isinstance(result, ToolFailure):
        encoded_result["error"] = result.error
        encoded_result["diagnostics"] = None if result.diagnostics is None else result.diagnostics.as_payload()
        encoded_result["execution"] = (
            None
            if result.execution is None
            else {
                "kind": "confirmed" if isinstance(result.execution, ConfirmedStop) else "unconfirmed",
                "cancellation_signalled": result.execution.cancellation_signalled,
            }
        )
        encoded_result["timeout_seconds"] = result.timeout_seconds
    return payload


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"persisted report {name} must be an object")
    return value


def _fields(value: object, name: str, expected: set[str]) -> Mapping[str, object]:
    raw = _mapping(value, name)
    if raw.keys() != expected:
        raise ValueError(f"persisted report {name} has an invalid field set")
    return raw


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"persisted report {name} must be a string")
    return value


def _optional_string(value: object, name: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"persisted report {name} must be a string or null")
    return value


def _parse_output(value: object) -> TextOutput | EmptyOutput | AttachmentOutput:
    raw = _mapping(value, "output")
    kind = _string(raw.get("kind"), "output kind")
    fields = {
        "text": {"kind", "text", "presentation", "bounds"},
        "empty": {"kind", "presentation", "bounds"},
        "attachment": {"kind", "mime", "data_uri", "presentation", "bounds"},
    }.get(kind)
    if fields is None:
        raise ValueError("persisted report output kind is unsupported")
    raw = _fields(raw, "output", fields)
    bounds_data = _fields(raw.get("bounds"), "output bounds", {"truncated", "partial", "source", "fallback_reason", "reference"})
    truncated = bounds_data.get("truncated")
    partial = bounds_data.get("partial")
    if not isinstance(truncated, bool) or not isinstance(partial, bool):
        raise ValueError("persisted report output bounds flags must be booleans")
    source = _optional_string(bounds_data.get("source"), "output source")
    fallback = _optional_string(bounds_data.get("fallback_reason"), "output fallback reason")
    reference_data = bounds_data.get("reference")
    reference = None
    if reference_data is not None:
        ref = _fields(reference_data, "output reference", {"uri", "artifact"})
        uri = _string(ref.get("uri"), "output reference URI")
        artifact_data = ref.get("artifact")
        artifact = None if artifact_data is None else own_json_object(_mapping(artifact_data, "artifact metadata"))
        reference = OutputReference(uri, artifact)
    bounds = OutputBounds(truncated, partial, reference, source, fallback)
    presentation = _optional_string(raw.get("presentation"), "output presentation")
    if kind == "text":
        return TextOutput(_string(raw.get("text"), "output text"), presentation, bounds)
    if kind == "empty":
        return EmptyOutput(presentation, bounds)
    return AttachmentOutput(_string(raw.get("mime"), "attachment MIME"), _string(raw.get("data_uri"), "attachment data URI"), presentation, bounds)


def _parse_control(value: object) -> ToolControl | None:
    if value is None:
        return None
    raw = _mapping(value, "control")
    kind = _string(raw.get("kind"), "control kind")
    if kind == "question_prepared":
        raw = _fields(value, "control", {"kind", "prompts"})
        prompts_raw = raw.get("prompts")
        if not isinstance(prompts_raw, list):
            raise ValueError("persisted prepared-question prompts must be an array")
        prompts = []
        for item in prompts_raw:
            prompt = _fields(item, "question prompt", {"question", "header", "options", "multiple"})
            options_raw = prompt.get("options")
            if not isinstance(options_raw, list):
                raise ValueError("persisted question options must be an array")
            options = []
            for item in options_raw:
                option = _fields(item, "question option", {"label", "description"})
                options.append(
                    PendingQuestionOption(
                        _string(option.get("label"), "question option label"), _string(option.get("description"), "question option description")
                    )
                )
            multiple = prompt.get("multiple")
            if not isinstance(multiple, bool):
                raise ValueError("persisted question multiple flag must be a boolean")
            prompts.append(
                PendingQuestionPrompt(
                    _string(prompt.get("question"), "question text"), _string(prompt.get("header"), "question header"), tuple(options), multiple
                )
            )
        return QuestionPrepared(tuple(prompts))
    if kind == "question_answered":
        raw = _fields(value, "control", {"kind", "responses"})
        responses_raw = raw.get("responses")
        if not isinstance(responses_raw, list):
            raise ValueError("persisted question responses must be an array")
        responses = []
        for item in responses_raw:
            response = _fields(item, "question response", {"header", "answers"})
            answers = response.get("answers")
            if not isinstance(answers, list) or not all(isinstance(answer, str) for answer in answers):
                raise ValueError("persisted question answers must be strings")
            responses.append(QuestionResponse(_string(response.get("header"), "question response header"), tuple(answers)))
        return QuestionAnswered(tuple(responses))
    if kind == "progress":
        raw = _fields(value, "control", {"kind", "types", "result", "data"})
        types = raw.get("types")
        data = raw.get("data")
        result = _optional_string(raw.get("result"), "progress result")
        if not isinstance(types, list) or not all(isinstance(item, str) for item in types):
            raise ValueError("persisted progress types must be strings")
        return ProgressYield(tuple(types), result, _mapping(data, "progress data"))
    if kind == "terminal":
        raw = _fields(value, "control", {"kind", "summary", "data"})
        return TerminalYield(_string(raw.get("summary"), "terminal summary"), _mapping(raw.get("data"), "terminal data"))
    if kind == "terminal_failure":
        raw = _fields(value, "control", {"kind", "data"})
        return TerminalYieldFailure(_mapping(raw.get("data"), "terminal failure data"))
    raise ValueError("persisted report control kind is unsupported")


def parse_report_payload(value: object) -> ReportedCall:
    raw = _fields(value, "envelope", {"version", "tool_call_id", "final_tool_name", "arguments", "result"})
    version = raw.get("version")
    if type(version) is not int or version != 1:
        raise ValueError("persisted report version is unsupported")
    call_id = _string(raw.get("tool_call_id"), "call identity")
    tool_name = _string(raw.get("final_tool_name"), "authorized tool name")
    arguments = own_json_object(_mapping(raw.get("arguments"), "authorized arguments"))
    result_data = _mapping(raw.get("result"), "result")
    kind = _string(result_data.get("kind"), "result kind")
    if _string(result_data.get("tool_name"), "result tool name") != tool_name:
        raise ValueError("persisted report tool names disagree")
    if kind == "success":
        result_data = _fields(result_data, "result", {"kind", "tool_name", "output", "body", "control"})
    elif kind == "failure":
        result_data = _fields(
            result_data,
            "result",
            {"kind", "tool_name", "output", "body", "control", "error", "diagnostics", "execution", "timeout_seconds"},
        )
    else:
        raise ValueError("persisted report result kind is unsupported")
    body_data = result_data.get("body")
    if body_data is None:
        body = None
    else:
        raw_body = _mapping(body_data, "body")
        body = ReadResultBody.from_payload(raw_body) if tool_name == "read" and raw_body.get("type") == "file" else OpaqueToolBody(raw_body)
    output = _parse_output(result_data.get("output"))
    control = _parse_control(result_data.get("control"))
    if kind == "success":
        if isinstance(control, TerminalYieldFailure):
            raise ValueError("persisted successful report cannot contain failure control")
        result = cast(ToolResult, ToolSuccess(tool_name, output, body, control))
    else:
        if control is not None and not isinstance(control, TerminalYieldFailure):
            raise ValueError("persisted failed report contains a success control")
        error = _string(result_data.get("error"), "failure error")
        diagnostics_raw = result_data.get("diagnostics")
        diagnostics = None if diagnostics_raw is None else ToolDiagnostics.from_payload(dict(_mapping(diagnostics_raw, "diagnostics")))
        execution_raw = result_data.get("execution")
        execution = None
        if execution_raw is not None:
            execution_data = _fields(execution_raw, "execution observation", {"kind", "cancellation_signalled"})
            cancelled = execution_data.get("cancellation_signalled")
            if not isinstance(cancelled, bool):
                raise ValueError("persisted cancellation observation must be boolean")
            execution_kind = _string(execution_data.get("kind"), "execution observation kind")
            execution = (
                ConfirmedStop(cancelled) if execution_kind == "confirmed" else UnconfirmedStop(cancelled) if execution_kind == "unconfirmed" else None
            )
            if execution is None:
                raise ValueError("persisted execution observation kind is unsupported")
        timeout = result_data.get("timeout_seconds")
        if timeout is not None and (not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 0):
            raise ValueError("persisted timeout must be a non-negative integer or null")
        result = cast(
            ToolResult,
            ToolFailure(
                tool_name,
                error,
                output,
                body,
                control if isinstance(control, TerminalYieldFailure) else None,
                diagnostics,
                execution,
                timeout,
            ),
        )
    return ReportedCall(call_id, tool_name, arguments, result)
