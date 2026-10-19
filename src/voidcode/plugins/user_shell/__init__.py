"""User shell plugin package: the ``!`` escape and its login-shell executor."""

from .user_shell import (
    USER_SHELL_HELP_HINT,
    USER_SHELL_HELP_TITLE,
    USER_SHELL_TIMEOUT_SECONDS,
    USER_SHELL_TRUNCATION_BYTES,
    UserShellResult,
    execute_user_shell_command,
    format_user_shell_record,
    login_shell_args,
    parse_shell_escape,
    truncate_middle_chars,
)
from .user_shell_plugin import ShellCommand, create_plugin

__all__ = [
    "USER_SHELL_HELP_HINT",
    "USER_SHELL_HELP_TITLE",
    "USER_SHELL_TIMEOUT_SECONDS",
    "USER_SHELL_TRUNCATION_BYTES",
    "ShellCommand",
    "UserShellResult",
    "create_plugin",
    "execute_user_shell_command",
    "format_user_shell_record",
    "login_shell_args",
    "parse_shell_escape",
    "truncate_middle_chars",
]
