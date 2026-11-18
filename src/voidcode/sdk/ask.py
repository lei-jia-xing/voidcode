"""Model-initiated persistent user ask: questions, answers, and their validator.

Single-valued questions reuse ``FormField`` verbatim; multi-choice is the one
new shape (``AskMultiSelect``). ``validate_ask_answer`` is the single shared
validator used by both clients (inline errors) and Core (answer admission),
so the two can never drift.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from .json_values import json_wire_object, own_json_object
from .services import ArtifactReference
from .suspend import SuspendPark
from .views import ChoiceOption, FormField, FormFieldType, validate_view_data

AskAnswerStatus = Literal["answered", "cancelled", "expired"]

_SQLITE_INT_MAX = 9_223_372_036_854_775_807
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

#: Strict numeric literals: no silent truncation, no scientific-integer coercion
#: (same shapes as sdk/views.py:454-455).
_INTEGER_PATTERN = re.compile(r"^[+-]?\d+$")
_NUMBER_PATTERN = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


@dataclass(frozen=True, slots=True)
class AskMultiSelect:
    """The ONE new field shape: multi-choice. Everything single-valued reuses FormField."""

    name: str
    label: str
    options: tuple[ChoiceOption, ...]
    required: bool = True
    min_selections: int = 0
    max_selections: int | None = None
    context: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("ask multi-select requires a non-empty name")
        if not isinstance(self.label, str) or not self.label:
            raise ValueError("ask multi-select requires a non-empty label")
        options = tuple(self.options)
        object.__setattr__(self, "options", options)
        if not options:
            raise ValueError("ask multi-select requires at least one option")
        if any(not isinstance(option, ChoiceOption) for option in options):
            raise ValueError("ask multi-select options must be ChoiceOption entries")
        ids = [option.id for option in options]
        if any(not isinstance(identifier, str) or not identifier for identifier in ids) or len(set(ids)) != len(ids):
            raise ValueError("ask multi-select options require unique non-empty ids")
        if any(not isinstance(option.label, str) for option in options):
            raise ValueError("ask multi-select option labels must be text")
        if type(self.required) is not bool:
            raise ValueError("ask multi-select required must be a boolean")
        if type(self.min_selections) is not int or self.min_selections < 0:
            raise ValueError("ask multi-select min_selections must be a non-negative integer")
        if self.max_selections is not None and (type(self.max_selections) is not int or self.max_selections < self.min_selections):
            raise ValueError("ask multi-select max_selections must be an integer at or above min_selections, or null")
        if self.context is not None and not isinstance(self.context, str):
            raise ValueError("ask multi-select context must be text or null")

    def as_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "label": self.label,
            "options": [{"id": option.id, "label": option.label} for option in self.options],
            "required": self.required,
            "min_selections": self.min_selections,
            "max_selections": self.max_selections,
            "context": self.context,
        }

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> AskMultiSelect:
        if not isinstance(payload, Mapping):
            raise ValueError("ask multi-select payload must be an object")
        allowed = {"name", "label", "options", "required", "min_selections", "max_selections", "context"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError("ask multi-select contains unknown field(s): " + ", ".join(unknown))
        if "name" not in payload or "label" not in payload or "options" not in payload:
            raise ValueError("ask multi-select is missing required field(s)")
        name = payload["name"]
        label = payload["label"]
        if not isinstance(name, str) or not name:
            raise ValueError("ask multi-select requires a non-empty name")
        if not isinstance(label, str) or not label:
            raise ValueError("ask multi-select requires a non-empty label")
        raw_options = payload["options"]
        if not isinstance(raw_options, (list, tuple)) or not raw_options:
            raise ValueError("ask multi-select requires at least one option")
        options: list[ChoiceOption] = []
        for raw in raw_options:
            if not isinstance(raw, Mapping) or set(raw) != {"id", "label"}:
                raise ValueError("ask multi-select options must be {id, label} objects")
            option_id, option_label = raw["id"], raw["label"]
            if not isinstance(option_id, str) or not option_id or not isinstance(option_label, str):
                raise ValueError("ask multi-select options require unique non-empty ids")
            options.append(ChoiceOption(id=option_id, label=option_label))
        required = payload.get("required", True)
        min_selections = payload.get("min_selections", 0)
        max_selections = payload.get("max_selections")
        context = payload.get("context")
        if type(required) is not bool:
            raise ValueError("ask multi-select required must be a boolean")
        if type(min_selections) is not int or min_selections < 0:
            raise ValueError("ask multi-select min_selections must be a non-negative integer")
        if max_selections is not None and (type(max_selections) is not int or max_selections < min_selections):
            raise ValueError("ask multi-select max_selections must be an integer at or above min_selections, or null")
        if context is not None and not isinstance(context, str):
            raise ValueError("ask multi-select context must be text or null")
        return AskMultiSelect(
            name=name,
            label=label,
            options=tuple(options),
            required=required,
            min_selections=min_selections,
            max_selections=max_selections,
            context=context,
        )


type AskQuestion = FormField | AskMultiSelect

AskContextKind = Literal["notice", "markdown", "fields", "diff", "status", "table", "json", "media"]

_ASK_CONTEXT_KINDS = ("notice", "markdown", "fields", "diff", "status", "table", "json", "media")


@dataclass(frozen=True, slots=True)
class AskContextBlock:
    """One context block reusing an existing view DTO (validated by validate_view_data,
    sdk/views.py:320-332, hence MAX_VIEW_DATA_BYTES-bounded, sdk/views.py:311). No new renderer."""

    view_type: AskContextKind
    data: Mapping[str, object]

    def __post_init__(self) -> None:
        if self.view_type not in _ASK_CONTEXT_KINDS:
            raise ValueError(f"ask context block requires one of: {', '.join(_ASK_CONTEXT_KINDS)}")
        validated = validate_view_data(self.view_type, self.data)
        object.__setattr__(self, "data", validated)

    def as_payload(self) -> dict[str, object]:
        return {"view_type": self.view_type, "data": json_wire_object(self.data)}


def _validated_timeout(timeout_ms: object) -> int | None:
    if timeout_ms is not None and (type(timeout_ms) is not int or not 1 <= timeout_ms <= _SQLITE_INT_MAX):
        raise ValueError("ask timeout_ms must be a positive SQLite INTEGER or null")
    return timeout_ms


def _validated_questions(questions: tuple[AskQuestion, ...]) -> tuple[AskQuestion, ...]:
    owned = tuple(questions)
    if not owned:
        raise ValueError("ask requires at least one question")
    for question in owned:
        if not isinstance(question, (FormField, AskMultiSelect)):
            raise ValueError("ask questions must be FormField or AskMultiSelect entries")
    names = [question.name for question in owned]
    if any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
        raise ValueError("ask question names must be unique and non-empty")
    return owned


def _validated_media(media: tuple[ArtifactReference, ...]) -> tuple[ArtifactReference, ...]:
    owned = tuple(media)
    for reference in owned:
        if not isinstance(reference, ArtifactReference):
            raise ValueError("ask media must hold ArtifactReference entries")
        if not isinstance(reference.sha256, str) or _SHA256_PATTERN.fullmatch(reference.sha256) is None:
            raise ValueError("ask media requires lowercase sha256 digests")
        if type(reference.bytes) is not int or reference.bytes < 0:
            raise ValueError("ask media byte counts must be non-negative integers")
        if not isinstance(reference.media_type, str) or not reference.media_type:
            raise ValueError("ask media requires a media type")
    return owned


def _question_as_payload(question: AskQuestion) -> dict[str, object]:
    if isinstance(question, AskMultiSelect):
        return {"kind": "multi", **question.as_payload()}
    options = question.options
    return {
        "kind": "field",
        "name": question.name,
        "label": question.label,
        "field_type": question.field_type,
        "required": question.required,
        "options": None if options is None else [{"id": option.id, "label": option.label} for option in options],
    }


def _question_from_payload(payload: Mapping[str, object]) -> AskQuestion:
    if not isinstance(payload, Mapping):
        raise ValueError("ask question payload must be an object")
    kind = payload.get("kind")
    if kind == "multi":
        rest = {key: value for key, value in payload.items() if key != "kind"}
        return AskMultiSelect.from_payload(rest)
    if kind == "field":
        allowed = {"kind", "name", "label", "field_type", "required", "options"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError("ask question contains unknown field(s): " + ", ".join(unknown))
        if "name" not in payload or "label" not in payload:
            raise ValueError("ask question is missing required field(s)")
        name = payload["name"]
        label = payload["label"]
        if not isinstance(name, str) or not name:
            raise ValueError("ask question requires a non-empty name")
        if not isinstance(label, str):
            raise ValueError("ask question label must be text")
        field_type = payload.get("field_type", "text")
        if field_type not in ("text", "multiline", "integer", "number", "boolean"):
            raise ValueError("ask question has an unknown field_type")
        required = payload.get("required", True)
        if type(required) is not bool:
            raise ValueError("ask question required must be a boolean")
        raw_options = payload.get("options")
        options: tuple[ChoiceOption, ...] | None
        if raw_options is None:
            options = None
        elif isinstance(raw_options, (list, tuple)):
            parsed: list[ChoiceOption] = []
            for raw in raw_options:
                if not isinstance(raw, Mapping) or set(raw) != {"id", "label"}:
                    raise ValueError("ask question options must be {id, label} objects")
                option_id, option_label = raw["id"], raw["label"]
                if not isinstance(option_id, str) or not isinstance(option_label, str):
                    raise ValueError("ask question options must be {id, label} objects")
                parsed.append(ChoiceOption(id=option_id, label=option_label))
            options = tuple(parsed)
        else:
            raise ValueError("ask question options must be a list of options or null")
        return FormField(name=name, label=label, field_type=cast(FormFieldType, field_type), required=required, options=options)
    raise ValueError("ask question requires kind 'field' or 'multi'")


def _media_from_payload(payload: object) -> ArtifactReference:
    if not isinstance(payload, Mapping) or set(payload) != {"sha256", "bytes", "media_type"}:
        raise ValueError("ask media must be {sha256, bytes, media_type} objects")
    sha256 = payload["sha256"]
    total = payload["bytes"]
    media_type = payload["media_type"]
    if not isinstance(sha256, str) or type(total) is not int or not isinstance(media_type, str):
        raise ValueError("ask media fields have the wrong types")
    return ArtifactReference(sha256=sha256, bytes=total, media_type=media_type)


def _context_from_payload(payload: object) -> AskContextBlock:
    if not isinstance(payload, Mapping) or set(payload) != {"view_type", "data"}:
        raise ValueError("ask context blocks must be {view_type, data} objects")
    view_type = payload["view_type"]
    data = payload["data"]
    if view_type not in _ASK_CONTEXT_KINDS:
        raise ValueError(f"ask context block requires one of: {', '.join(_ASK_CONTEXT_KINDS)}")
    if not isinstance(data, Mapping):
        raise ValueError("ask context block data must be an object")
    return AskContextBlock(view_type=view_type, data=data)


@dataclass(frozen=True, slots=True)
class AskRequestDraft:
    """What the model/plugin stages. No ids (Core assigns), no permissions, no defaults-as-answers."""

    title: str
    questions: tuple[AskQuestion, ...]
    context: tuple[AskContextBlock, ...] = ()
    media: tuple[ArtifactReference, ...] = ()
    timeout_ms: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("ask draft requires a non-empty title")
        object.__setattr__(self, "questions", _validated_questions(self.questions))
        context = tuple(self.context)
        if any(not isinstance(block, AskContextBlock) for block in context):
            raise ValueError("ask context must hold AskContextBlock entries")
        object.__setattr__(self, "context", context)
        object.__setattr__(self, "media", _validated_media(self.media))
        _validated_timeout(self.timeout_ms)

    def as_payload(self) -> dict[str, object]:
        return {
            "title": self.title,
            "questions": [_question_as_payload(question) for question in self.questions],
            "context": [block.as_payload() for block in self.context],
            "media": [reference.as_payload() for reference in self.media],
            "timeout_ms": self.timeout_ms,
        }

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> AskRequestDraft:
        if not isinstance(payload, Mapping):
            raise ValueError("ask draft payload must be an object")
        allowed = {"title", "questions", "context", "media", "timeout_ms"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError("ask draft contains unknown field(s): " + ", ".join(unknown))
        if "title" not in payload or "questions" not in payload:
            raise ValueError("ask draft is missing required field(s)")
        title = payload["title"]
        if not isinstance(title, str) or not title.strip():
            raise ValueError("ask draft requires a non-empty title")
        raw_questions = payload["questions"]
        if not isinstance(raw_questions, (list, tuple)):
            raise ValueError("ask draft questions must be a list of questions")
        questions = tuple(_question_from_payload(raw) for raw in raw_questions)
        raw_context = payload.get("context", [])
        if not isinstance(raw_context, (list, tuple)):
            raise ValueError("ask draft context must be a list of context blocks")
        context = tuple(_context_from_payload(raw) for raw in raw_context)
        raw_media = payload.get("media", [])
        if not isinstance(raw_media, (list, tuple)):
            raise ValueError("ask draft media must be a list of artifact references")
        media = tuple(_media_from_payload(raw) for raw in raw_media)
        timeout_ms = _validated_timeout(payload.get("timeout_ms"))
        return AskRequestDraft(title=title, questions=questions, context=context, media=media, timeout_ms=timeout_ms)


@dataclass(frozen=True, slots=True)
class AskRequest:
    request_id: str
    session_id: str
    revision_id: str
    owner: str
    title: str
    questions: tuple[AskQuestion, ...]
    context: tuple[AskContextBlock, ...]
    media: tuple[ArtifactReference, ...]
    timeout_ms: int | None

    def __post_init__(self) -> None:
        for name in ("request_id", "session_id", "revision_id", "owner"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"ask request requires a non-empty {name}")
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("ask request requires a non-empty title")
        object.__setattr__(self, "questions", _validated_questions(self.questions))
        context = tuple(self.context)
        if any(not isinstance(block, AskContextBlock) for block in context):
            raise ValueError("ask context must hold AskContextBlock entries")
        object.__setattr__(self, "context", context)
        object.__setattr__(self, "media", _validated_media(self.media))
        _validated_timeout(self.timeout_ms)

    def as_payload(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "revision_id": self.revision_id,
            "owner": self.owner,
            "title": self.title,
            "questions": [_question_as_payload(question) for question in self.questions],
            "context": [block.as_payload() for block in self.context],
            "media": [reference.as_payload() for reference in self.media],
            "timeout_ms": self.timeout_ms,
        }

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> AskRequest:
        if not isinstance(payload, Mapping):
            raise ValueError("ask request payload must be an object")
        allowed = {"request_id", "session_id", "revision_id", "owner", "title", "questions", "context", "media", "timeout_ms"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError("ask request contains unknown field(s): " + ", ".join(unknown))
        missing = sorted(allowed - set(payload))
        if missing:
            raise ValueError("ask request is missing field(s): " + ", ".join(missing))
        ids: dict[str, str] = {}
        for name in ("request_id", "session_id", "revision_id", "owner"):
            value = payload[name]
            if not isinstance(value, str) or not value:
                raise ValueError(f"ask request requires a non-empty {name}")
            ids[name] = value
        title = payload["title"]
        if not isinstance(title, str) or not title.strip():
            raise ValueError("ask request requires a non-empty title")
        raw_questions = payload["questions"]
        if not isinstance(raw_questions, (list, tuple)):
            raise ValueError("ask request questions must be a list of questions")
        questions = tuple(_question_from_payload(raw) for raw in raw_questions)
        raw_context = payload["context"]
        if not isinstance(raw_context, (list, tuple)):
            raise ValueError("ask request context must be a list of context blocks")
        context = tuple(_context_from_payload(raw) for raw in raw_context)
        raw_media = payload["media"]
        if not isinstance(raw_media, (list, tuple)):
            raise ValueError("ask request media must be a list of artifact references")
        media = tuple(_media_from_payload(raw) for raw in raw_media)
        timeout_ms = _validated_timeout(payload["timeout_ms"])
        return AskRequest(
            request_id=ids["request_id"],
            session_id=ids["session_id"],
            revision_id=ids["revision_id"],
            owner=ids["owner"],
            title=title,
            questions=questions,
            context=context,
            media=media,
            timeout_ms=timeout_ms,
        )


@dataclass(frozen=True, slots=True)
class AskAnswer:
    request_id: str
    values: Mapping[str, object]
    status: AskAnswerStatus
    terminal_sequence: int

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id:
            raise ValueError("ask answer requires a non-empty request_id")
        if not isinstance(self.values, Mapping):
            raise ValueError("ask answer values must be an object")
        object.__setattr__(self, "values", own_json_object(self.values))
        if self.status not in ("answered", "cancelled", "expired"):
            raise ValueError(f"unknown ask answer status {self.status!r}")
        if type(self.terminal_sequence) is not int or self.terminal_sequence < 1:
            raise ValueError("ask answer terminal sequence must be a positive integer")

    def as_payload(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "values": json_wire_object(self.values),
            "status": self.status,
            "terminal_sequence": self.terminal_sequence,
        }


@dataclass(frozen=True, slots=True)
class AskResultBody:
    """Typed tool-result body for the adopted ask success (validated by consumers via
    try_validate_tool_body against the ask tool's output_schema)."""

    answers: Mapping[str, object]
    status: Literal["answered"] = "answered"

    def __post_init__(self) -> None:
        if not isinstance(self.answers, Mapping):
            raise ValueError("ask result body answers must be an object")
        object.__setattr__(self, "answers", own_json_object(self.answers))
        if self.status != "answered":
            raise ValueError("ask result body status is always 'answered'")

    def as_payload(self) -> dict[str, object]:
        return {"answers": json_wire_object(self.answers), "status": self.status}


class AskError(ValueError):
    """An ask is unknown, not pending, already answered, or already parked.

    Already-parked is the one-park-per-cursor rule (D1): a second suspend
    while the session holds a live park is refused visibly rather than
    queued; sequential asks are the supported shape.
    """


class AskAlreadyParked(AskError):
    """A second suspend was attempted while the session holds a live park.

    One-park-per-cursor rule (D1): the second ask fails visibly rather than
    queueing; sequential asks are the supported shape.
    """


class AskValidationError(ValueError):
    """One ask answer failed validation, carrying the failing field plus its reason."""

    def __init__(self, field: str, reason: str) -> None:
        if not isinstance(field, str):
            raise ValueError("ask validation error requires a field name")
        if not isinstance(reason, str) or not reason:
            raise ValueError("ask validation error requires a reason")
        self._field = field
        self._reason = reason
        super().__init__(f"ask answer invalid for field {field!r}: {reason}")

    @property
    def field(self) -> str:
        return self._field

    @property
    def reason(self) -> str:
        return self._reason


def _validate_field(
    field: FormField,
    name: str,
    label: str,
    raw: object,
    present: bool,
    validated: dict[str, object],
) -> None:
    if field.options is not None and raw not in (None, ""):
        if not isinstance(raw, str) or raw not in {option.id for option in field.options}:
            raise AskValidationError(field=name, reason=f"Field '{label}' must be one of the available choices.")
    if field.field_type == "boolean":
        if raw is True or raw is False:
            validated[name] = raw
            return
        if present and raw is not None and not isinstance(raw, bool) and not (isinstance(raw, str) and not raw.strip()):
            raise AskValidationError(field=name, reason=f"Field '{label}' must be a boolean.")
        if field.required:
            raise AskValidationError(field=name, reason=f"Field '{label}' is required.")
        return
    if field.field_type == "integer" and isinstance(raw, int) and not isinstance(raw, bool):
        validated[name] = raw
        return
    if field.field_type == "number" and isinstance(raw, (int, float)) and not isinstance(raw, bool):
        if not math.isfinite(float(raw)):
            raise AskValidationError(field=name, reason=f"Field '{label}' must be a finite number.")
        validated[name] = raw
        return
    if present and raw is not None and not isinstance(raw, str):
        raise AskValidationError(field=name, reason=f"Field '{label}' must be text.")
    text = "" if raw is None else str(raw)
    if not text.strip():
        if field.required:
            raise AskValidationError(field=name, reason=f"Field '{label}' is required.")
        return
    candidate = text.strip()
    if field.field_type == "integer":
        if not _INTEGER_PATTERN.match(candidate):
            raise AskValidationError(field=name, reason=f"Field '{label}' must be an integer.")
        validated[name] = int(candidate)
    elif field.field_type == "number":
        if not _NUMBER_PATTERN.match(candidate):
            raise AskValidationError(field=name, reason=f"Field '{label}' must be a number.")
        value = float(candidate)
        if not math.isfinite(value):
            raise AskValidationError(field=name, reason=f"Field '{label}' must be a finite number.")
        validated[name] = value
    else:
        validated[name] = text


def _validate_multi(
    question: AskMultiSelect,
    name: str,
    label: str,
    raw: object,
    present: bool,
    validated: dict[str, object],
) -> None:
    if not present or raw is None:
        if question.required:
            raise AskValidationError(field=name, reason=f"Field '{label}' is required.")
        return
    if not isinstance(raw, (list, tuple)):
        raise AskValidationError(field=name, reason=f"Field '{label}' must be a list of choice ids.")
    option_ids = {option.id for option in question.options}
    selections = list(raw)
    for selection in selections:
        if not isinstance(selection, str) or selection not in option_ids:
            raise AskValidationError(field=name, reason=f"Field '{label}' must be one of the available choices.")
    if len(set(selections)) != len(selections):
        raise AskValidationError(field=name, reason=f"Field '{label}' must not repeat choices.")
    if not selections:
        if question.required:
            raise AskValidationError(field=name, reason=f"Field '{label}' is required.")
        return
    if len(selections) < question.min_selections:
        raise AskValidationError(field=name, reason=f"Field '{label}' must select at least {question.min_selections} choice(s).")
    if question.max_selections is not None and len(selections) > question.max_selections:
        raise AskValidationError(field=name, reason=f"Field '{label}' must select at most {question.max_selections} choice(s).")
    validated[name] = tuple(selections)


def validate_ask_answer(request: AskRequest, values: Mapping[str, object]) -> dict[str, object]:
    """Strict shared validator used by BOTH clients and Core (single implementation, no drift).

    Mirrors validate_form_submission (sdk/views.py:481-556): absent-vs-blank
    tri-state, strict integer/number literals (sdk/views.py:454-455), boolean
    tri-state, options membership, text verbatim (sdk/views.py:553-555),
    omit-rather-than-fabricate (sdk/views.py:538); plus multi membership +
    min/max_selections, plus unknown-extra rejection. Raises
    AskValidationError visibly; no coercion, no default filling.
    """
    if not isinstance(request, AskRequest):
        raise ValueError("ask answer validation requires an AskRequest")
    if not isinstance(values, Mapping):
        raise ValueError("ask answer values must be an object")
    known = {question.name for question in request.questions}
    for key in values:
        if key not in known:
            raise AskValidationError(field=key if isinstance(key, str) else str(key), reason="unknown question.")
    validated: dict[str, object] = {}
    for question in request.questions:
        name = question.name
        label = question.label or name
        raw = values.get(name)
        present = name in values
        if isinstance(question, AskMultiSelect):
            _validate_multi(question, name, label, raw, present, validated)
        else:
            _validate_field(question, name, label, raw, present, validated)
    return validated


class SuspendAsk(SuspendPark):
    """Unwind a tool callback to park it for a user ask."""

    def __init__(self, draft: AskRequestDraft, *, idempotency_key: str | None = None) -> None:
        if not isinstance(draft, AskRequestDraft):
            raise ValueError("suspend ask requires an AskRequestDraft")
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key):
            raise ValueError("ask idempotency key must be non-empty text or null")
        self._draft = draft
        self._idempotency_key = idempotency_key
        intent: dict[str, object] = {"draft": draft.as_payload()}
        if idempotency_key is not None:
            intent["idempotency_key"] = idempotency_key
        super().__init__("ask", intent)

    @property
    def draft(self) -> AskRequestDraft:
        return self._draft

    @property
    def idempotency_key(self) -> str | None:
        return self._idempotency_key


__all__ = [
    "AskAlreadyParked",
    "AskAnswer",
    "AskAnswerStatus",
    "AskContextBlock",
    "AskContextKind",
    "AskError",
    "AskMultiSelect",
    "AskQuestion",
    "AskRequest",
    "AskRequestDraft",
    "AskResultBody",
    "AskValidationError",
    "SuspendAsk",
    "validate_ask_answer",
]
