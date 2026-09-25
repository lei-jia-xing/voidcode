"""A minimal stdio MCP server shared by the MCP lifecycle tests.

The runtime owns MCP *lifecycle* (when a server is started, reused, released),
so its tests need a real stdio server that completes the initialize/tools-list
handshake without being a real capability. The script appends one line to a
``starts`` marker when it boots and one to a ``stops`` marker when its stdin
closes, which is how a test tells a reused process from a restarted one.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

_SERVER_SOURCE = r"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _mark(path: Path, line: str) -> None:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(f"{existing}{line}\n", encoding="utf-8")


def _send(message: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


starts, stops = Path(sys.argv[1]), Path(sys.argv[2])
_mark(starts, "start")

for raw_line in sys.stdin:
    message = json.loads(raw_line)
    method = message.get("method")
    if method == "initialize":
        _send(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake", "version": "0.1.0"},
                },
            }
        )
        continue
    if method == "notifications/initialized":
        continue
    if method == "tools/list":
        _send(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "tools": [
                        {
                            "name": "echo",
                            "description": "Echo input.",
                            "inputSchema": {"type": "object"},
                        }
                    ]
                },
            }
        )
        continue

_mark(stops, "stop")
"""


@dataclass(frozen=True, slots=True)
class FakeMcpStdioServer:
    """An on-disk stdio MCP server plus the markers its process writes."""

    command: tuple[str, ...]
    starts: Path
    stops: Path

    @property
    def start_count(self) -> int:
        return len(self._lines(self.starts))

    @property
    def stop_count(self) -> int:
        return len(self._lines(self.stops))

    @staticmethod
    def _lines(path: Path) -> list[str]:
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8").splitlines()


def write_fake_mcp_stdio_server(tmp_path: Path) -> FakeMcpStdioServer:
    """Write the server script under ``tmp_path`` and return its launch command."""
    script = tmp_path / "fake_mcp_stdio_server.py"
    script.write_text(_SERVER_SOURCE, encoding="utf-8")
    starts = tmp_path / "mcp-starts.txt"
    stops = tmp_path / "mcp-stops.txt"
    return FakeMcpStdioServer(
        command=(sys.executable, str(script), str(starts), str(stops)),
        starts=starts,
        stops=stops,
    )


__all__ = ["FakeMcpStdioServer", "write_fake_mcp_stdio_server"]
