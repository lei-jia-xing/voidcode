"""Executable entrypoint for voidcode.plugin_host."""

import sys

from voidcode.plugin_host.server import run_server

if __name__ == "__main__":
    sys.exit(run_server())
