"""Core configuration schema and strict payload parser for ``voidcode.json``."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .json_values import own_json_object

CONFIG_FILE_NAME = "voidcode.json"
CONFIG_SCHEMA_VERSION = 1


ReasoningEffort = Literal["off", "none", "minimal", "low", "medium", "high", "xhigh", "max"] | int


class CoreModelConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    model: str | None = None
    reasoning_effort: ReasoningEffort | None = None

    @field_validator("model")
    @classmethod
    def _validate_model(cls, v: str | None) -> str | None:
        if v is not None:
            if not v.strip():
                raise ValueError("core config model.model must be a non-empty string")
            return v.strip()
        return v

    @field_validator("reasoning_effort")
    @classmethod
    def _validate_reasoning_effort(cls, v: ReasoningEffort | None) -> ReasoningEffort | None:
        if isinstance(v, int) and v < 0:
            raise ValueError("reasoning_effort budget must be >= 0")
        return v


class CorePolicyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    approval_mode: Literal["ask", "write", "yolo"] = "ask"
    read_only: bool = False


class CoreProviderEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, arbitrary_types_allowed=True)

    api_key: str | None = Field(default=None, min_length=1, repr=False)
    base_url: str | None = Field(default=None, min_length=1)
    extra: Mapping[str, Any] = Field(default_factory=dict, repr=False, validate_default=True)

    @model_validator(mode="before")
    @classmethod
    def _extract_vendor_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        raw = dict(data)
        api_key = raw.pop("api_key", None)
        base_url = raw.pop("base_url", None)
        explicit_extra = raw.pop("extra", None)
        if explicit_extra is not None:
            if not isinstance(explicit_extra, (dict, Mapping)):
                raise ValueError("provider extra fields must be an object")
            if raw:
                raise ValueError("unexpected fields in provider entry")
            extra = explicit_extra
        else:
            extra = raw

        if "api_key" in extra or "base_url" in extra:
            raise ValueError("provider extra fields must not repeat api_key or base_url")
        for k in extra:
            if not isinstance(k, str) or not k:
                raise ValueError("provider extra field names must be non-empty strings")

        owned_extra = own_json_object(extra)
        result: dict[str, Any] = {"extra": owned_extra}
        if api_key is not None:
            result["api_key"] = api_key
        if base_url is not None:
            result["base_url"] = base_url
        return result

    @field_validator("extra", mode="after")
    @classmethod
    def _freeze_extra(cls, v: Any) -> Mapping[str, Any]:
        if not isinstance(v, Mapping):
            raise ValueError("provider extra fields must be an object")
        return own_json_object(v)


class CorePluginEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, arbitrary_types_allowed=True)

    enabled: bool = True
    config: Mapping[str, Any] = Field(default_factory=dict, validate_default=True)
    path: str | None = None

    @field_validator("path")
    @classmethod
    def _validate_path(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("plugin entry path must be a non-empty string")
        return v

    @field_validator("config", mode="after")
    @classmethod
    def _freeze_config(cls, v: Any) -> Mapping[str, Any]:
        if not isinstance(v, Mapping):
            raise ValueError("plugin entry config must be an object")
        return own_json_object(v)


class CoreConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal[1] = 1
    model: CoreModelConfig = Field(default_factory=CoreModelConfig)
    policy: CorePolicyConfig = Field(default_factory=CorePolicyConfig)
    providers: Mapping[
        Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]*$")],
        CoreProviderEntry,
    ] = Field(default_factory=dict)
    plugins: Mapping[
        Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]*$")],
        CorePluginEntry,
    ] = Field(default_factory=dict)

    @field_validator("schema_version", mode="before")
    @classmethod
    def _validate_schema_version(cls, v: Any) -> int:
        if type(v) is not int or v != CONFIG_SCHEMA_VERSION:
            raise ValueError(f"core config schema_version must be {CONFIG_SCHEMA_VERSION}")
        return v


def parse_core_config(payload: object) -> CoreConfig:
    """Validate an untrusted ``voidcode.json`` payload."""
    if not isinstance(payload, dict):
        raise ValueError("core config must be an object")
    try:
        return CoreConfig.model_validate(payload)
    except ValidationError as err:
        raise ValueError(str(err)) from err
