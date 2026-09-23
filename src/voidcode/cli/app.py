"""Thin CLI entrypoint: root command group, command registration, and exit-code mapping."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path

import click

from .. import __version__
from ..cli_support import EXIT_RUNTIME_ERROR, EXIT_SUCCESS
from .commands.config import config
from .commands.discovery import agents, commands, mcp
from .commands.doctor import doctor
from .commands.provider import provider
from .commands.run import run as run_command
from .commands.server import acp as acp_command
from .commands.server import serve_command, web_command
from .commands.sessions import sessions
from .commands.storage import stats, storage
from .commands.tasks import tasks
from .commands.tui import tui as tui_command
from .errors import CliError

EXAMPLES = """
Examples:
  voidcode run 'read README.md' --workspace .
  voidcode run 'read README.md' --json --workspace .
  voidcode sessions list --json --workspace .
  voidcode commands list --workspace .
  voidcode commands show /review --json --workspace .
""".strip()


def _run_click_command(command: click.Command, argv: Sequence[str] | None) -> int:
    try:
        result = command.main(
            args=argv,
            prog_name="voidcode",
            standalone_mode=False,
        )
        return EXIT_SUCCESS if result is None else result
    except click.exceptions.Exit as exc:
        return exc.exit_code
    except click.ClickException as exc:
        exc.show(file=sys.stderr)
        return exc.exit_code
    except CliError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return exc.code
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR


@click.group(
    invoke_without_command=True,
    help="Voidcode command-line interface.\n\n" + EXAMPLES,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(__version__, "--version", prog_name="voidcode")
@click.option(
    "--db-path",
    "db_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=(
        "Override the runtime SQLite database path. Sets VOIDCODE_DB_PATH "
        "for this invocation; otherwise the path resolves under "
        "$XDG_STATE_HOME/voidcode/sessions.sqlite3."
    ),
)
@click.pass_context
def root_cli(ctx: click.Context, db_path: Path | None) -> None:
    if db_path is not None:
        os.environ["VOIDCODE_DB_PATH"] = str(Path(db_path).expanduser().resolve())
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


def main(argv: Sequence[str] | None = None) -> int:
    return _run_click_command(root_cli, argv)


# Root command registration: one line per documented CLI command.
root_cli.add_command(acp_command)
root_cli.add_command(agents)
root_cli.add_command(commands)
root_cli.add_command(config)
root_cli.add_command(doctor)
root_cli.add_command(mcp)
root_cli.add_command(provider)
root_cli.add_command(run_command)
root_cli.add_command(serve_command)
root_cli.add_command(sessions)
root_cli.add_command(stats)
root_cli.add_command(storage)
root_cli.add_command(tasks)
root_cli.add_command(tui_command)
root_cli.add_command(web_command)
