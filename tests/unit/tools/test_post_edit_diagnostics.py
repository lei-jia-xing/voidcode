from __future__ import annotations

from pathlib import Path

from voidcode.tools._post_edit_diagnostics import post_edit_lsp_diagnostics
from voidcode.tools.runtime_context import (
    RuntimeLspToolFacade,
    RuntimeToolInvocationContext,
    bind_runtime_tool_context,
)


class _FakeLspFacade:
    def __init__(self, *, expected_workspace: str) -> None:
        self._expected_workspace = expected_workspace

    def request_diagnostics(self, *, file_path: str, workspace: str) -> dict[str, object]:
        assert file_path == "main.py"
        assert workspace == self._expected_workspace
        return {
            "lsp_response": {
                "result": {
                    "items": [
                        {
                            "message": "Example type error",
                            "severity": 1,
                            "code": "example",
                            "range": {
                                "start": {"line": 0, "character": 4},
                                "end": {"line": 0, "character": 9},
                            },
                        }
                    ]
                }
            }
        }


def test_post_edit_lsp_diagnostics_disabled_by_default(tmp_path: Path) -> None:
    facade: RuntimeLspToolFacade = _FakeLspFacade(expected_workspace=str(tmp_path.resolve()))
    with bind_runtime_tool_context(
        RuntimeToolInvocationContext(
            session_id="session-1",
            lsp=facade,
        )
    ):
        assert post_edit_lsp_diagnostics(workspace=tmp_path, paths=["main.py"]) == []


def test_post_edit_lsp_diagnostics_enabled_collects_diagnostics(tmp_path: Path) -> None:
    facade: RuntimeLspToolFacade = _FakeLspFacade(expected_workspace=str(tmp_path.resolve()))
    with bind_runtime_tool_context(
        RuntimeToolInvocationContext(
            session_id="session-1",
            lsp=facade,
            lsp_diagnostics_on_write=True,
        )
    ):
        diagnostics = post_edit_lsp_diagnostics(workspace=tmp_path, paths=["main.py"])

    assert len(diagnostics) == 1
    assert diagnostics[0]["source"] == "lsp"
    assert diagnostics[0]["path"] == "main.py"
    assert diagnostics[0]["message"] == "Example type error"
    assert diagnostics[0]["line"] == 1
    assert diagnostics[0]["character"] == 5
