"""Strict argument ingress for the ``task``, ``task_batch``, and ``wait`` tools."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator, model_validator

from voidcode.sdk.child_schema import canonical_result_schema

from .constants import (
    _BATCH_KEYS,
    _BATCH_OPERATIONS,
    _CONTROL_INPUT_KEYS,
    _FOREGROUND_SPAWN_INPUT_KEYS,
    _ITEM_KEYS,
    _SPAWN_INPUT_KEYS,
    _TASK_FIELD_INPUT_NAMES,
    _TASK_INPUT_KEYS,
    _TASK_OPERATIONS,
    DEFAULT_HISTORY_EVENTS,
    DEFAULT_JOIN_TIMEOUT_SECONDS,
    MAX_BATCH_SIZE,
    MAX_HISTORY_EVENTS,
    MAX_OUTPUT_PAGE_BYTES,
    OUTPUT_PAGE_BYTES,
)


def _validate_label(value: object, *, where: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{where} label must be a string")
    return value


def _validate_allowlist(value: object, *, where: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"{where} tool_allowlist must be a non-empty array of tool names")
    names: list[str] = []
    for name in value:
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{where} tool_allowlist entries must be non-empty tool names")
        names.append(name)
    return tuple(names)


def _validate_subagent_type(value: object, *, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} subagent_type must be a non-empty profile id")
    return value


def _validate_max_steps(value: object, *, where: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 1:
        raise ValueError(f"{where} max_steps must be a positive integer")
    return value


def _validate_result_schema(value: object, *, where: str) -> Mapping[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} result_schema must be an object")
    try:
        return canonical_result_schema(value)
    except ValueError as exc:
        raise ValueError(f"{where} {exc}") from exc


@dataclass(frozen=True, slots=True)
class _BatchItem:
    prompt: str
    label: str = ""
    tool_allowlist: tuple[str, ...] | None = None
    max_steps: int | None = None
    subagent_type: str | None = None
    result_schema: Mapping[str, object] | None = None


class TaskArgsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    prompt: str | None = None
    label: str = ""
    tool_allowlist: tuple[str, ...] | None = None
    max_steps: int | None = None
    run_in_background: bool = True
    operation: str | None = None
    task_id: str | None = None
    timeout_seconds: float = Field(default=DEFAULT_JOIN_TIMEOUT_SECONDS, alias="timeout")
    subagent_type: str | None = None
    result_schema: dict[str, object] | None = None
    output_offset: int = 0
    output_limit: int = OUTPUT_PAGE_BYTES
    after_sequence: int = 0
    history_offset: int = 0
    history_limit: int = Field(default=DEFAULT_HISTORY_EVENTS, alias="limit")

    @model_validator(mode="before")
    @classmethod
    def _known_fields(cls, data: object) -> object:
        if isinstance(data, Mapping):
            unknown = sorted(str(key) for key in data if key not in _TASK_INPUT_KEYS)
            if unknown:
                raise ValueError(f"task field '{unknown[0]}' is not supported")
        return data

    @field_validator("prompt", mode="before")
    @classmethod
    def _prompt(cls, value: object) -> object:
        if value is not None and not isinstance(value, str):
            raise ValueError("task prompt must be a string")
        if value is not None and not value.strip():
            raise ValueError("task prompt must be non-empty")
        return value

    @field_validator("operation", mode="before")
    @classmethod
    def _operation(cls, value: object) -> object:
        if value is not None and (not isinstance(value, str) or value not in _TASK_OPERATIONS):
            raise ValueError(f"task operation must be one of {', '.join(_TASK_OPERATIONS)}")
        return value

    @field_validator("task_id", mode="before")
    @classmethod
    def _task_id(cls, value: object) -> object:
        if value is not None and (not isinstance(value, str) or not value):
            raise ValueError("task task_id must be a non-empty string")
        return value

    @field_validator("label", mode="before")
    @classmethod
    def _label(cls, value: object) -> object:
        return _validate_label(value, where="task")

    @field_validator("tool_allowlist", mode="before")
    @classmethod
    def _allowlist(cls, value: object) -> object:
        return _validate_allowlist(value, where="task")

    @field_validator("max_steps", mode="before")
    @classmethod
    def _steps(cls, value: object) -> object:
        return _validate_max_steps(value, where="task")

    @field_validator("subagent_type", mode="before")
    @classmethod
    def _profile(cls, value: object) -> object:
        return _validate_subagent_type(value, where="task")

    @field_validator("result_schema", mode="before")
    @classmethod
    def _schema(cls, value: object) -> object:
        return _validate_result_schema(value, where="task")

    @field_validator("run_in_background", mode="before")
    @classmethod
    def _background(cls, value: object) -> object:
        if value is None:
            return True
        if type(value) is not bool:
            raise ValueError("task run_in_background must be a boolean")
        return value

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def _timeout(cls, value: object) -> object:
        if value is None:
            return DEFAULT_JOIN_TIMEOUT_SECONDS
        if type(value) is not int or value < 0:
            raise ValueError("task timeout must be a non-negative integer of milliseconds")
        return value / 1000.0

    @field_validator("output_offset", "after_sequence", "history_offset", mode="before")
    @classmethod
    def _non_negative(cls, value: object, info: ValidationInfo) -> object:
        if value is None:
            return 0
        if type(value) is not int or value < 0:
            raise ValueError(f"task {info.field_name} must be a non-negative integer")
        return value

    @field_validator("output_limit", mode="before")
    @classmethod
    def _output_limit(cls, value: object) -> object:
        if value is None:
            return OUTPUT_PAGE_BYTES
        if type(value) is not int or value < 1 or value > MAX_OUTPUT_PAGE_BYTES:
            raise ValueError(f"task output_limit must be an integer within 1..{MAX_OUTPUT_PAGE_BYTES}")
        return value

    @field_validator("history_limit", mode="before")
    @classmethod
    def _history_limit(cls, value: object) -> object:
        if value is None:
            return DEFAULT_HISTORY_EVENTS
        if type(value) is not int or value < 1 or value > MAX_HISTORY_EVENTS:
            raise ValueError(f"task limit must be an integer within 1..{MAX_HISTORY_EVENTS}")
        return value

    @model_validator(mode="after")
    def _modes(self) -> TaskArgsModel:
        if self.operation is None:
            if self.task_id is not None:
                raise ValueError("task accepts one mode per call: a prompt to spawn a child, or an operation with a task_id")
            if self.prompt is None:
                raise ValueError("task requires a prompt to spawn a child, or an operation with a task_id")
        elif self.operation == "send":
            if self.task_id is None:
                raise ValueError("task operation=send requires a task_id")
            if self.prompt is None:
                raise ValueError("task operation=send requires a prompt")
        else:
            if self.prompt is not None:
                raise ValueError("task accepts one mode per call: spawn, or one operation with a task_id")
            if self.task_id is None:
                raise ValueError(f"task operation={self.operation} requires a task_id")
        if self.operation is not None:
            if self.subagent_type is not None:
                raise ValueError("task subagent_type selects a child profile and applies to a spawn only")
            if self.tool_allowlist is not None:
                raise ValueError("task tool_allowlist applies to a spawn only")
            if self.result_schema is not None:
                raise ValueError("task result_schema declares the delivery shape and applies to a spawn only")
        if self.operation is None:
            allowed = _SPAWN_INPUT_KEYS if self.run_in_background else _FOREGROUND_SPAWN_INPUT_KEYS
            mode = "a background spawn" if self.run_in_background else "a foreground spawn"
        else:
            allowed = _CONTROL_INPUT_KEYS[self.operation]
            mode = f'operation="{self.operation}"'
        supplied = {_TASK_FIELD_INPUT_NAMES.get(field, field) for field in self.model_fields_set}
        irrelevant = sorted(supplied - allowed)
        if irrelevant:
            raise ValueError(f"task field '{irrelevant[0]}' does not apply to {mode}")
        return self


def _validate_batch_item(index: int, raw: object) -> _BatchItem:
    where = f"task_batch item {index}"
    if not isinstance(raw, Mapping):
        raise ValueError(f"{where} must be an object")
    unknown = sorted(str(key) for key in raw if key not in _ITEM_KEYS)
    if unknown:
        raise ValueError(f"{where} field '{unknown[0]}' is not supported")
    prompt = raw.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError(f"{where} requires a non-empty prompt")
    subagent_type = _validate_subagent_type(raw.get("subagent_type"), where=where)
    tool_allowlist = _validate_allowlist(raw.get("tool_allowlist"), where=where)
    return _BatchItem(
        prompt=prompt,
        label=_validate_label(raw.get("label"), where=where),
        tool_allowlist=tool_allowlist,
        max_steps=_validate_max_steps(raw.get("max_steps"), where=where),
        subagent_type=subagent_type,
        result_schema=_validate_result_schema(raw.get("result_schema"), where=where),
    )


def _merge_batch_prompt(shared_context: str | None, prompt: str) -> str:
    """One item's effective prompt: shared background plus its own task, under fixed labels.

    The shared context comes only from the spawning batch request and is merged here,
    before any profile prefixing; it is never persisted, so later calls cannot inherit
    it. An absent or blank context returns the item prompt byte-identical.
    """
    if shared_context is None or not shared_context.strip():
        return prompt
    return f"Shared context:\n{shared_context}\n\nTask:\n{prompt}"


class BatchArgsModel(BaseModel):
    """Validated task_batch arguments; unknown keys fail closed with the legacy message."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    tasks: tuple[_BatchItem, ...] = ()
    operation: str | None = None
    parallel_group_id: str | None = None
    context: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_unknown(cls, data: object) -> object:
        if isinstance(data, Mapping):
            unknown = sorted(str(key) for key in data if key not in _BATCH_KEYS)
            if unknown:
                raise ValueError(f"task_batch field '{unknown[0]}' is not supported")
        return data

    @field_validator("operation", mode="before")
    @classmethod
    def _operation(cls, value: object) -> object:
        if value is not None and (not isinstance(value, str) or value not in _BATCH_OPERATIONS):
            raise ValueError(f"task_batch operation must be one of {', '.join(_BATCH_OPERATIONS)}")
        return value

    @field_validator("parallel_group_id", mode="before")
    @classmethod
    def _group_id(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise ValueError("task_batch parallel_group_id must be a non-empty string")
        return value

    @field_validator("context", mode="before")
    @classmethod
    def _context(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("task_batch context must be a string")
        return value

    @field_validator("tasks", mode="before")
    @classmethod
    def _tasks(cls, value: object) -> object:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError("task_batch tasks must be an array")
        if len(value) > MAX_BATCH_SIZE:
            raise ValueError(f"task_batch accepts at most {MAX_BATCH_SIZE} tasks, got {len(value)}")
        if not value:
            raise ValueError("task_batch requires at least one task")
        return tuple(_validate_batch_item(index, item) for index, item in enumerate(value))

    @model_validator(mode="after")
    def _modes(self) -> BatchArgsModel:
        if self.operation is None:
            if not self.tasks:
                raise ValueError("task_batch requires tasks to spawn a group, or operation=output with a parallel_group_id")
            if self.parallel_group_id is not None:
                raise ValueError("task_batch accepts one mode per call: tasks to spawn a group, or operation=output with a parallel_group_id")
        else:
            if self.tasks:
                raise ValueError("task_batch accepts one mode per call: tasks to spawn a group, or operation=output with a parallel_group_id")
            if self.parallel_group_id is None:
                raise ValueError(f"task_batch operation={self.operation} requires a parallel_group_id")
            if self.context is not None:
                raise ValueError("task_batch context declares shared background and applies to a spawn only")
        return self


class WaitArgsModel(BaseModel):
    """``wait`` takes no arguments; any supplied key fails closed."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def _model_args[T: BaseModel](model: type[T], arguments: Mapping[str, object]) -> T:
    """Validate tool arguments; surface the first failure with its legacy message."""
    try:
        return model.model_validate(dict(arguments))
    except ValidationError as exc:
        [first] = exc.errors(include_url=False)[:1]
        ctx = first.get("ctx")
        error = ctx.get("error") if isinstance(ctx, dict) else None
        raise ValueError(str(error) if isinstance(error, ValueError) else first["msg"]) from None
