"""Language-server browsing must never start a server."""

from pathlib import Path
from types import SimpleNamespace

from voidcode.plugins.lsp.inventory import LspInventoryCommand
from voidcode.sdk.commands import CommandInvocation
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.views import ChoicesViewData, FieldsViewData, choices_dispatch


def test_lsp_selector_retains_failure_and_configuration(tmp_path: Path):
    configuration = SimpleNamespace(
        configured_enabled=True, servers={"python": SimpleNamespace(command=["unused"], extensions=[".py"], languages=["python"])}
    )
    state = SimpleNamespace(mode="workspace", servers={"python": SimpleNamespace(status="failed", last_error="startup failed")})
    manager = SimpleNamespace(current_state=lambda: state, configuration_for=lambda workspace: configuration)
    command = LspInventoryCommand(manager)
    context = ToolContext(workspace=tmp_path, session_id="session", run_id="run", invocation_id="inv", revision_id="rev", actor="client")
    listed = command.invoke(CommandInvocation(context=context, name="lsp"), None)
    action, arguments = choices_dispatch(ChoicesViewData.from_payload(listed.presentation.data), "python")
    result = command.invoke(CommandInvocation(context=context, name=action, arguments=arguments), None)
    rows = {row.label: row.value for row in FieldsViewData.from_payload(result.presentation.data).rows}
    assert rows["Status"] == "failed"
    assert rows["Last error"] == "startup failed"
    assert rows["Languages"] == "python"
    assert rows["Extensions"] == ".py"
    assert command.invoke(CommandInvocation(context=context, name="lsp", arguments={"server": "missing"}), None).status == "error"
    configuration.configured_enabled = False
    disabled = command.invoke(CommandInvocation(context=context, name="lsp"), None)
    assert disabled.presentation.view_type == "notice"
    assert "disabled" in disabled.presentation.data["message"]
