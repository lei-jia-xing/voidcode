"""LSP tool ingress and request construction over real SDK immutable arguments."""

from hashlib import sha256
from pathlib import Path

import pytest

from voidcode.plugins.lsp.lsp_manager import LspRequestResult
from voidcode.plugins.lsp.lsp_tool import LspTool
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import ArtifactReference
from voidcode.sdk.tools import ToolCall


class Services:
    def spill_artifact(self, content, *, media_type):
        return ArtifactReference(sha256=sha256(content.encode()).hexdigest(), bytes=len(content.encode()), media_type=media_type)


class Manager:
    def __init__(self, results):
        self.results = iter(results)
        self.requests = []

    def request(self, request):
        self.requests.append(request)
        return LspRequestResult(response={"jsonrpc": "2.0", "id": len(self.requests), "result": next(self.results)})


def invoke(tmp_path, arguments, manager):
    (tmp_path / "file.py").write_text("x = 1\n", encoding="utf-8")
    context = ToolContext(workspace=tmp_path, session_id="session", run_id="run", invocation_id="inv", revision_id="rev", actor="model")
    return LspTool(manager).invoke(ToolCall("lsp", arguments), context=context, services=Services())


@pytest.mark.parametrize(
    "extra",
    [
        {"limit": 51},
        {"limit": True},
        {"offset": 0},
        {"offset": "2"},
        {"line": 1.5},
        {"character": True},
        {"endLine": 0},
        {"unknown": True},
    ],
)
def test_invalid_arguments_fail_before_server_request(tmp_path: Path, extra: dict[str, object]):
    manager = Manager([])
    with pytest.raises(ValueError):
        invoke(tmp_path, {"operation": "hover", "path": "file.py", "line": 1, "character": 1, **extra}, manager)
    assert manager.requests == []


def test_code_action_accepts_nested_immutable_diagnostics(tmp_path: Path):
    diagnostic = {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}, "message": "example"}
    manager = Manager([[]])
    result = invoke(tmp_path, {"operation": "codeAction", "path": "file.py", "line": 1, "character": 1, "diagnostics": [diagnostic]}, manager)
    assert result.status == "ok"
    request = manager.requests[0]
    assert request.method == "textDocument/codeAction"
    assert request.params["context"]["diagnostics"] == [diagnostic]
    assert request.params["range"] == {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}


@pytest.mark.parametrize("operation", ["incomingCalls", "outgoingCalls"])
def test_call_hierarchy_shares_final_request_path(tmp_path: Path, operation: str):
    point = {"line": 0, "character": 0}
    span = {"start": point, "end": point}
    item = {"name": "function", "kind": 12, "uri": (tmp_path / "file.py").as_uri(), "range": span, "selectionRange": span}
    manager = Manager([[item], []])
    result = invoke(tmp_path, {"operation": operation, "path": "file.py", "line": 1, "character": 1}, manager)
    assert result.status == "ok"
    assert [request.method for request in manager.requests] == ["textDocument/prepareCallHierarchy", f"callHierarchy/{operation}"]
    assert manager.requests[1].params == {"item": item}


@pytest.mark.parametrize("kind,label", [(26, "TypeParameter"), (12, "Function")])
def test_document_symbol_labels_use_protocol_enum(tmp_path: Path, kind: int, label: str):
    point = {"line": 0, "character": 0}
    span = {"start": point, "end": point}
    manager = Manager([[{"name": "T", "kind": kind, "range": span, "selectionRange": span}]])
    result = invoke(tmp_path, {"operation": "documentSymbol", "path": "file.py"}, manager)
    assert result.status == "ok"
    assert f"T ({label})" in result.output.text


@pytest.mark.parametrize(
    "operation,payload",
    [
        ("hover", False),
        ("hover", {"unexpected": True}),
        ("documentSymbol", {}),
        ("diagnostics", {}),
        ("diagnostics", {"kind": "full", "items": [None]}),
        ("codeAction", {"actions": []}),
    ],
)
def test_malformed_protocol_results_are_not_empty_success(tmp_path: Path, operation: str, payload: object):
    with pytest.raises(ValueError, match="returned an invalid result"):
        invoke(tmp_path, {"operation": operation, "path": "file.py", "line": 1, "character": 1}, Manager([payload]))


@pytest.mark.parametrize("operation,payload", [("hover", None), ("documentSymbol", []), ("diagnostics", {"kind": "full", "items": []})])
def test_valid_empty_protocol_results_remain_success(tmp_path: Path, operation: str, payload: object):
    result = invoke(tmp_path, {"operation": operation, "path": "file.py", "line": 1, "character": 1}, Manager([payload]))
    assert result.status == "ok"


def test_unchanged_diagnostic_report_does_not_invent_zero_items(tmp_path: Path):
    result = invoke(tmp_path, {"operation": "diagnostics", "path": "file.py"}, Manager([{"kind": "unchanged", "resultId": "previous"}]))
    assert result.status == "ok"
    assert result.body.as_payload()["item_count"] is None
    assert "Diagnostics unchanged" in result.output.text
    assert "No diagnostics reported" not in result.output.text


@pytest.mark.parametrize("payload", [None, []])
def test_empty_references_report_zero_items(tmp_path: Path, payload: object):
    result = invoke(tmp_path, {"operation": "findReferences", "path": "file.py", "line": 1, "character": 1}, Manager([payload]))
    assert result.status == "ok"
    assert result.body.as_payload()["item_count"] == 0
    assert "No reference found" in result.output.text


def test_references_render_real_locations(tmp_path: Path):
    span = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}
    result = invoke(
        tmp_path,
        {"operation": "findReferences", "path": "file.py", "line": 1, "character": 1},
        Manager([[{"uri": (tmp_path / "file.py").as_uri(), "range": span}]]),
    )
    assert result.body.as_payload()["item_count"] == 1
    assert "file.py:1:1-1:2" in result.output.text


@pytest.mark.parametrize("operation", ["incomingCalls", "outgoingCalls"])
@pytest.mark.parametrize("prepared", [None, []])
def test_empty_call_hierarchy_preparation_is_success_without_followup(tmp_path: Path, operation: str, prepared: object):
    manager = Manager([prepared])
    result = invoke(tmp_path, {"operation": operation, "path": "file.py", "line": 1, "character": 1}, manager)
    assert result.status == "ok"
    assert result.body.as_payload()["item_count"] == 0
    assert "No call hierarchy results" in result.output.text
    assert [request.method for request in manager.requests] == ["textDocument/prepareCallHierarchy"]
    assert result.output.bounds.source == "lsp:textDocument/prepareCallHierarchy"


@pytest.mark.parametrize("prepared", [{}, {"name": "old-shape"}, [None], [{"name": "missing-fields"}]])
def test_malformed_call_hierarchy_preparation_is_not_empty_success(tmp_path: Path, prepared: object):
    manager = Manager([prepared])
    with pytest.raises(ValueError, match="prepareCallHierarchy returned an invalid result"):
        invoke(tmp_path, {"operation": "incomingCalls", "path": "file.py", "line": 1, "character": 1}, manager)
    assert len(manager.requests) == 1


def test_will_rename_files_renders_resource_operations(tmp_path: Path):
    old_uri = (tmp_path / "file.py").as_uri()
    new_uri = (tmp_path / "renamed.py").as_uri()
    result = invoke(
        tmp_path,
        {"operation": "willRenameFiles", "path": "file.py", "newPath": "renamed.py"},
        Manager([{"documentChanges": [{"kind": "rename", "oldUri": old_uri, "newUri": new_uri}]}]),
    )
    assert "No workspace edits needed" not in result.output.text
    assert "RENAME file.py -> renamed.py" in result.output.text
    assert result.body.as_payload()["item_count"] == 1


def test_will_rename_files_renders_create_and_delete_operations(tmp_path: Path):
    old_uri = (tmp_path / "file.py").as_uri()
    new_uri = (tmp_path / "created.py").as_uri()
    result = invoke(
        tmp_path,
        {"operation": "willRenameFiles", "path": "file.py", "newPath": "created.py"},
        Manager(
            [
                {
                    "documentChanges": [
                        {"kind": "create", "uri": new_uri},
                        {"kind": "delete", "uri": old_uri},
                    ]
                }
            ]
        ),
    )
    assert "No workspace edits needed" not in result.output.text
    assert "CREATE created.py" in result.output.text
    assert "DELETE file.py" in result.output.text
    assert result.body.as_payload()["item_count"] == 2


def test_code_action_lists_resource_operation_paths(tmp_path: Path):
    old_uri = (tmp_path / "file.py").as_uri()
    new_uri = (tmp_path / "renamed.py").as_uri()
    action = {
        "title": "rename file",
        "kind": "refactor",
        "edit": {"documentChanges": [{"kind": "rename", "oldUri": old_uri, "newUri": new_uri}]},
    }
    result = invoke(tmp_path, {"operation": "codeAction", "path": "file.py", "line": 1, "character": 1}, Manager([[action]]))
    assert "affects: file.py, renamed.py" in result.output.text
