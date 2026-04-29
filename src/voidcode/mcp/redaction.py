from __future__ import annotations

from collections.abc import Sequence

_SECRET_OPTION_NAMES = {
    "--api-key",
    "--apikey",
    "--token",
    "--access-token",
    "--secret",
    "--password",
}
_SECRET_OPTION_FRAGMENTS = ("key", "token", "secret", "password", "credential")
_SECRET_ENV_SUFFIXES = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


def redact_mcp_command(command: Sequence[str]) -> list[str]:
    """Return an MCP command safe for diagnostics and user-visible status."""

    redacted: list[str] = []
    redact_next = False
    for part in command:
        if redact_next:
            redacted.append("<redacted>")
            redact_next = False
            continue

        option_name, separator, option_value = part.partition("=")
        lowered_option = option_name.lower()
        if option_name.isidentifier() and separator and _looks_like_secret_env(option_name):
            redacted.append(f"{option_name}=<redacted>")
            continue
        if lowered_option in _SECRET_OPTION_NAMES or (
            lowered_option.startswith("--")
            and any(fragment in lowered_option for fragment in _SECRET_OPTION_FRAGMENTS)
        ):
            if separator:
                redacted.append(f"{option_name}=<redacted>")
            else:
                redacted.append(part)
                redact_next = True
            continue
        if option_value:
            redacted.append(part)
            continue
        redacted.append(part)
    return redacted


def format_redacted_mcp_command(command: Sequence[str]) -> str:
    return " ".join(redact_mcp_command(command))


def _looks_like_secret_env(name: str) -> bool:
    upper_name = name.upper()
    return any(
        upper_name == suffix or upper_name.endswith(f"_{suffix}") for suffix in _SECRET_ENV_SUFFIXES
    )
