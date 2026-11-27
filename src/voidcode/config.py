"""Product configuration schema, voidcode.toml parser, and provider resolution."""

from __future__ import annotations

import math
import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .capabilities.providers.registry import PROVIDER_TABLE, PROVIDER_TABLE_BY_ID
from .sdk.json_values import own_json_object
from .sdk.provider import ProviderReasoningEffort
from .sdk.views import StyleColor

CONFIG_FILE_NAME = "voidcode.toml"
CONFIG_SCHEMA_VERSION = 1

_TOML_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_TOML_ESCAPE_MAP = str.maketrans(
    {
        "\\": "\\\\",
        '"': '\\"',
        "\b": "\\b",
        "\t": "\\t",
        "\n": "\\n",
        "\f": "\\f",
        "\r": "\\r",
    }
)


def _toml_string(value: str) -> str:
    escaped = value.translate(_TOML_ESCAPE_MAP)
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in escaped):
        escaped = "".join(f"\\u{ord(ch):04x}" if ord(ch) < 0x20 or ord(ch) == 0x7F else ch for ch in escaped)
    return f'"{escaped}"'


def _toml_key(key: object) -> str:
    if not isinstance(key, str) or not key:
        raise ValueError("TOML keys must be non-empty strings")
    return key if _TOML_BARE_KEY.fullmatch(key) is not None else _toml_string(key)


def _toml_value(value: object) -> str:
    if value is None:
        raise ValueError("TOML does not support null values")
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("TOML does not support non-finite floats")
        return repr(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, Mapping):
        return "{" + ", ".join(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in value.items()) + "}"
    raise ValueError(f"unsupported TOML value type: {type(value).__name__}")


def _write_toml_table(lines: list[str], path: tuple[str, ...], table: Mapping[str, object]) -> None:
    scalars: list[tuple[str, object]] = []
    subtables: list[tuple[str, Mapping[str, object]]] = []
    for key, value in table.items():
        if not isinstance(key, str) or not key:
            raise ValueError("TOML keys must be non-empty strings")
        if isinstance(value, Mapping):
            subtables.append((key, value))
        else:
            scalars.append((key, value))
    if path and (scalars or not subtables):
        if lines:
            lines.append("")
        lines.append("[" + ".".join(_toml_key(part) for part in path) + "]")
    for key, value in scalars:
        lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    for key, subtable in subtables:
        _write_toml_table(lines, (*path, key), subtable)


def dumps_toml(payload: Mapping[str, object]) -> str:
    """Serialize a configuration mapping cleanly to canonical TOML text."""
    if not isinstance(payload, Mapping):
        raise ValueError("config must be a table")
    lines: list[str] = []
    _write_toml_table(lines, (), payload)
    return ("\n".join(lines) + "\n") if lines else ""


ReasoningEffort = ProviderReasoningEffort


class ModelConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    model: str | None = None
    reasoning_effort: ReasoningEffort | None = None

    @field_validator("model")
    @classmethod
    def _validate_model(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("model must be a non-empty string")
        return v

    @field_validator("reasoning_effort")
    @classmethod
    def _validate_reasoning_effort(cls, v: ReasoningEffort | None) -> ReasoningEffort | None:
        if isinstance(v, int) and v < 0:
            raise ValueError("reasoning_effort budget must be >= 0")
        return v


class BannerConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    text: str | None = None
    colors: Annotated[tuple[StyleColor, ...], Field(min_length=1)] = (
        "#9d7cfe",
        "#9388fe",
        "#8997fb",
        "#7da7f7",
        "#75b9f6",
        "#76ccf5",
        "#7dcfff",
    )

    @field_validator("colors", mode="before")
    @classmethod
    def _freeze_colors(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value


class TuiConfig(BaseModel):
    """Local TUI chrome prefs: display-only, never admitted, never snapshotted."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    border: Literal["lines", "box", "borderless"] = "lines"
    footer: Literal["full", "compact", "off"] = "full"
    show_session: bool = False
    banner: BannerConfig | None = Field(default_factory=BannerConfig)

    @field_validator("banner", mode="before")
    @classmethod
    def _validate_banner(cls, value: Any) -> Any:
        if value is False:
            return None
        if value is True:
            raise ValueError("tui.banner cannot be true; omit it or configure [tui.banner]")
        return value


class PolicyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    approval_mode: Literal["ask", "write", "yolo"] = "ask"
    read_only: bool = False
    max_root_children: int = Field(default=4, ge=1, le=64)


class SandboxConfig(BaseModel):
    """Optional transparent exec backend: absent section means unconfined, today's behavior."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    mode: Literal["workspace-write", "read-only", "danger-full-access"] = "workspace-write"
    network: bool | None = None
    writable_roots: tuple[str, ...] = ()

    @field_validator("writable_roots", mode="before")
    @classmethod
    def _validate_roots(cls, v: Any) -> tuple[str, ...]:
        if not isinstance(v, (list, tuple)):
            raise ValueError("sandbox writable_roots must be a list of absolute paths")
        for root in v:
            if not isinstance(root, str) or not root or not root.startswith("/"):
                raise ValueError(f"sandbox writable_roots must be absolute paths: {root!r}")
        return tuple(v)

    @model_validator(mode="after")
    def _validate_mode(self) -> SandboxConfig:
        if self.mode == "danger-full-access" and self.network is False:
            raise ValueError("sandbox danger-full-access with network=false is contradictory")
        if self.mode == "read-only" and self.writable_roots:
            raise ValueError("sandbox writable_roots must be empty under read-only")
        if self.mode == "danger-full-access" and self.writable_roots:
            raise ValueError("sandbox danger-full-access takes no confinement options")
        return self

    @property
    def effective_network(self) -> bool:
        if self.network is not None:
            return self.network
        return self.mode == "danger-full-access"


class ProviderEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, arbitrary_types_allowed=True, hide_input_in_errors=True)

    api_key: str | None = Field(default=None, min_length=1, repr=False)
    base_url: str | None = Field(default=None, min_length=1)
    extra: Mapping[str, Any] = Field(default_factory=dict, repr=False)

    @model_validator(mode="before")
    def _extract_vendor_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        raw = dict(data)
        if "extra" in raw:
            raise ValueError("nested 'extra' in provider entry is forbidden; configure provider fields flat")
        has_key = "api_key" in raw
        has_url = "base_url" in raw
        api_key = raw.pop("api_key", None)
        base_url = raw.pop("base_url", None)
        for k in raw:
            if not isinstance(k, str) or not k:
                raise ValueError("provider field names must be non-empty strings")
        result: dict[str, Any] = {"extra": raw}
        if has_key:
            result["api_key"] = None if api_key is False else api_key
        if has_url:
            result["base_url"] = base_url
        return result

    @field_validator("extra", mode="after")
    @classmethod
    def _freeze_extra(cls, v: Any) -> Mapping[str, Any]:
        if not isinstance(v, Mapping):
            raise ValueError("provider extra fields must be an object")
        return own_json_object(v)


class PluginEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, arbitrary_types_allowed=True, hide_input_in_errors=True)

    enabled: bool = True
    config: Mapping[str, Any] = Field(default_factory=dict)
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


class Config(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, hide_input_in_errors=True)

    schema_version: Literal[1] = 1
    model: ModelConfig = Field(default_factory=ModelConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    sandbox: SandboxConfig | None = None
    providers: Mapping[
        Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]*$")],
        ProviderEntry,
    ] = Field(default_factory=dict)
    plugins: Mapping[
        Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]*$")],
        PluginEntry,
    ] = Field(default_factory=dict)
    tui: TuiConfig = Field(default_factory=TuiConfig)

    @field_validator("schema_version", mode="before")
    @classmethod
    def _validate_schema_version(cls, v: Any) -> int:
        if type(v) is not int or v != CONFIG_SCHEMA_VERSION:
            raise ValueError(f"config schema_version must be {CONFIG_SCHEMA_VERSION}")
        return v


def parse_config(payload: object) -> Config:
    """Validate an untrusted ``voidcode.toml`` payload."""
    if not isinstance(payload, dict):
        raise ValueError("config must be a TOML table")
    try:
        return Config.model_validate(payload)
    except ValidationError as err:
        for error in err.errors():
            if error.get("type") == "extra_forbidden":
                field = ".".join(str(p) for p in error.get("loc", ()))
                raise ValueError(f"unsupported field: {field}") from err
        raise ValueError(str(err)) from err


def resolve_providers(config: Config, *, use_environment: bool = True, discover: bool = True) -> dict[str, object]:
    """Resolve live credentials once; recorded admissions never read the environment."""
    credentials: dict[str, str] = {}
    if use_environment:
        for row in PROVIDER_TABLE:
            if not discover and row.id not in config.providers:
                continue
            variables = row.env_vars[:1] if row.id == "endpoint" else row.env_vars
            for variable in variables:
                key = os.environ.get(variable)
                if key and key.strip():
                    credentials[row.id] = key
                    break

    private: dict[str, object] = {}
    for name in sorted(config.providers.keys() | credentials.keys()):
        entry = config.providers.get(name)
        row = PROVIDER_TABLE_BY_ID.get(name)
        base_url = None if entry is None else entry.base_url
        if base_url is None and row is not None:
            base_url = os.environ.get("ENDPOINT_BASE_URL") if use_environment and name == "endpoint" else None
            base_url = base_url or row.default_base_url
        if base_url is None:
            raise ValueError(f"provider {name} requires an explicit base_url")
        if entry is not None and "api_key" in entry.model_fields_set:
            key = entry.api_key
        elif entry is not None and "auth" in entry.extra:
            # A nested auth block is the provider's own credential choice (for
            # example Google OAuth or a service-account file). A flat key
            # discovered from the table would conflict with it at the plugin
            # boundary, so explicit auth stays authoritative.
            key = None
        else:
            key = credentials.get(name)
            if key is None and use_environment:
                key = os.environ.get(f"{name.upper().replace('-', '_')}_API_KEY")
        private[name] = {**({} if entry is None else entry.extra), "api_key": key, "base_url": base_url}
    return private


def load_config(workspace: Path, *, allow_missing: bool = False) -> Config:
    path = workspace / CONFIG_FILE_NAME
    if not path.exists():
        if allow_missing:
            return Config()
        raise FileNotFoundError(f"configuration not found: {path.name}")
    if not path.is_file():
        raise ValueError(f"not a regular file: {path.name}")
    if path.stat().st_size > 256 * 1024:
        raise ValueError("configuration file too large")
    try:
        payload = tomllib.loads(path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"invalid configuration TOML: {error}") from error
    return parse_config(payload)


type ModelField = Literal["model", "reasoning_effort"]


def update_model_section(workspace: Path, field: ModelField, value: object) -> Config:
    """Validate and atomically rewrite one ``model`` field in ``voidcode.toml``.

    The whole payload is revalidated before the compare-and-swap replace, so an
    invalid value or an unknown/ill-typed section leaves the file untouched.
    ``value`` is validated by the product schema (named effort levels or a
    non-negative integer budget); callers never normalize it themselves.
    """
    if field not in ("model", "reasoning_effort"):
        raise ValueError(f"unsupported model field: {field}")
    path = workspace / CONFIG_FILE_NAME
    payload: dict[str, object]
    if path.exists():
        if not path.is_file():
            raise ValueError(f"not a regular file: {path.name}")
        if path.stat().st_size > 256 * 1024:
            raise ValueError("configuration file too large")
        expected: bytes | None = path.read_bytes()
        try:
            parsed = tomllib.loads(expected.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
            raise ValueError(f"invalid configuration TOML: {error}") from error
        payload = dict(parsed)
    else:
        expected = None
        payload = {"schema_version": CONFIG_SCHEMA_VERSION}
    raw_section = payload.get("model")
    if raw_section is None:
        section: dict[str, object] = {}
    elif isinstance(raw_section, dict):
        section = dict(raw_section)
    else:
        raise ValueError("model section must be a table")
    section[field] = value
    payload["model"] = section
    config = parse_config(payload)
    content = dumps_toml(payload).encode("utf-8")
    from .sdk.atomic_file import replace_file

    replace_file(path, content, expected_content=expected)
    return config
