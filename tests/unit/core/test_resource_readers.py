"""Resource-reader boundaries through a real runtime, store, and artifact bytes.

A throwaway third-party plugin registers its own scheme against the real
files plugin: no read edits, provider-owner leases, distinct absence vs
failure outcomes, and bounded shared paging.
"""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import pytest

from voidcode.core.plugins import PluginRuntime, PluginSource
from voidcode.core.services import RuntimeServices
from voidcode.core.storage.store import SqliteStore
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.resources import ResourceReadRequest
from voidcode.sdk.tools import ToolCall, ToolFailure, ToolSuccess

FILES_ROOT = Path("src/voidcode/plugins/files").resolve()

_MEMO_PLUGIN = """\
from collections.abc import Mapping

from voidcode.sdk.plugin import Plugin
from voidcode.sdk.resources import ResourcePage, ResourceReadCaller, ResourceReader, ResourceReaderServices, ResourceReadRequest
from voidcode.sdk.services import QueryUnavailable
from voidcode.sdk.tools import OutputBounds, TextOutput


def memo_handler(request: ResourceReadRequest, caller: ResourceReadCaller, services: ResourceReaderServices) -> ResourcePage | None:
    name = request.uri[len("memo://"):]
    if not name:
        raise ValueError("memo://<name> requires a name")
    if name == "missing":
        return None
    if name == "boom":
        raise RuntimeError("memo backend exploded")
    if name == "badshape":
        return {"not": "a page"}  # type: ignore[return-value]
    if name == "peer-gone":
        raise QueryUnavailable("peer query gone")
    state, version = services.get_state("session")
    assert services.owner == "memo", services.owner
    assert caller.session_id == "session-resource"
    return ResourcePage(
        output=TextOutput(f"memo {name} v{version} state={state}", bounds=OutputBounds()),
        body={"path": request.uri, "type": "memo", "name": name},
    )


def create_plugin(config: Mapping[str, object] | None = None) -> Plugin:
    del config
    return Plugin(resource_readers=(ResourceReader("memo", memo_handler),))
"""


@pytest.fixture
def harness(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    memo_root = tmp_path / "memo_plugin"
    memo_root.mkdir()
    (memo_root / "__init__.py").write_text(textwrap.dedent(_MEMO_PLUGIN), encoding="utf-8")
    with SqliteStore(tmp_path / "store.db") as store:
        revision = store.put_revision({"resource": "test"})
        store.record_workspace_revision(str(workspace), revision)
        store.create_session("session-resource", str(workspace), revision)
        with PluginRuntime(
            (
                PluginSource(root=FILES_ROOT, plugin_id="files"),
                PluginSource(root=memo_root, plugin_id="memo"),
            ),
            workspace=workspace,
            revision_id=revision,
            store=store,
            artifacts_dir=tmp_path / "artifacts",
        ) as runtime:
            context = ToolContext(
                workspace=workspace,
                session_id="session-resource",
                run_id="run-resource",
                invocation_id="inv-resource",
                revision_id=revision,
                actor="model",
            )
            yield runtime, store, context


def _read(harness, path: str, **arguments: object):
    runtime, _store, context = harness
    return runtime.invoke(ToolCall(tool_name="read", arguments={"path": path, **arguments}), context=context)


def _spill(harness, data: bytes, *, media_type: str = "text/plain"):
    runtime, _store, context = harness
    lease = runtime._open_lease(
        owner="files",
        session_id=context.session_id,
        run_id=context.run_id,
        invocation_id="inv-spill",
        readonly=False,
        actor="model",
        internal_work=True,
    )
    try:
        return RuntimeServices(runtime, lease).spill_artifact(data, media_type=media_type)
    finally:
        runtime._revoke_lease(lease)


def test_third_party_scheme_reads_through_unmodified_read_with_owner_state(harness) -> None:
    result = _read(harness, "memo://hello")
    assert isinstance(result, ToolSuccess)
    assert "memo hello v0 state=None" in result.output.text  # type: ignore[union-attr]
    assert result.coverage == ()


def test_duplicate_scheme_admission_rejected_naming_both_owners(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = tmp_path / "first"
    second = tmp_path / "second"
    for root in (first, second):
        root.mkdir()
        (root / "__init__.py").write_text(textwrap.dedent(_MEMO_PLUGIN), encoding="utf-8")
    with (
        SqliteStore(tmp_path / "store.db") as store,
        pytest.raises(ValueError, match="Duplicate resource scheme 'memo' registered by second and first"),
    ):
        PluginRuntime(
            (
                PluginSource(root=first, plugin_id="first"),
                PluginSource(root=second, plugin_id="second"),
            ),
            workspace=workspace,
            revision_id="rev-duplicate",
            store=store,
        )


def test_unknown_scheme_unknown_resource_and_backend_failure_stay_distinct(harness) -> None:
    unknown = _read(harness, "nosuch://x")
    assert isinstance(unknown, ToolFailure)
    assert "unsupported resource URI: nosuch://x" in unknown.error

    missing = _read(harness, "memo://missing")
    assert isinstance(missing, ToolFailure)
    assert missing.error == "read cannot resolve resource URI memo://missing: no such resource"

    boom = _read(harness, "memo://boom")
    assert isinstance(boom, ToolFailure)
    assert "memo backend exploded" in boom.error
    assert "unsupported resource URI" not in boom.error


def test_handler_query_unavailable_is_provider_failure_not_unknown_scheme(harness) -> None:
    result = _read(harness, "memo://peer-gone")
    assert isinstance(result, ToolFailure)
    assert "peer query gone" in result.error
    assert "unsupported resource URI" not in result.error


def test_malformed_page_is_contract_error_not_absence(harness) -> None:
    result = _read(harness, "memo://badshape")
    assert isinstance(result, ToolFailure)
    assert "must answer with a ResourcePage or None" in result.error


def test_byte_args_refused_for_files_and_non_byte_schemes(harness, tmp_path: Path) -> None:
    runtime, _store, context = harness
    target = context.workspace / "plain.txt"
    target.write_text("plain\n", encoding="utf-8")
    refused = _read(harness, "plain.txt", byte_offset=0)
    assert isinstance(refused, ToolFailure)
    assert refused.error == "byte_offset and byte_limit require a resource URI"

    tool_refused = _read(harness, "tool://read", byte_offset=0)
    assert isinstance(tool_refused, ToolFailure)
    assert "byte_offset and byte_limit are not supported for tool:// URIs" in tool_refused.error


def test_missing_artifact_is_absence_while_unreadable_bytes_propagate(harness) -> None:
    runtime, _store, context = harness
    missing = _read(harness, "artifact://" + "0" * 64)
    assert isinstance(missing, ToolFailure)
    assert missing.error.endswith(": no such resource")

    if sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0):
        pytest.skip("chmod(0o000) unreadability needs a POSIX filesystem enforced for non-root users")
    reference = _spill(harness, b"secret\n")
    blob = next(iter((context.workspace.parent / "artifacts").glob(reference.sha256)))
    blob.chmod(0o000)
    try:
        with blob.open("rb"):
            pass
    except OSError:
        enforceable = True
    else:
        enforceable = False
    try:
        if not enforceable:
            pytest.skip("filesystem does not enforce chmod(0o000) unreadability here")
        unreadable = _read(harness, reference.uri)
    finally:
        blob.chmod(0o600)
    assert isinstance(unreadable, ToolFailure)
    assert "no such resource" not in unreadable.error
    assert "Permission denied" in unreadable.error


def test_mid_read_cancellation_stops_shared_paging(harness, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, _store, context = harness
    reference = _spill(harness, b"a\n" * 60000)
    calls: list[int] = []
    from voidcode.core.services import RuntimeServices as _RuntimeServices

    original_read_artifact = _RuntimeServices.read_artifact

    def _flipping_read_artifact(self, sha256: str, *, offset: int = 0, limit: int = 65536):  # type: ignore[no-untyped-def]
        calls.append(offset)
        if len(calls) >= 2:
            parent = next(
                lease
                for lease in self._runtime._active_leases.values()
                if lease.identity.owner == "files" and lease.identity.invocation_id == "inv-cancel-mid"
            )
            self._runtime.cancel_event_for(parent).set()
        return original_read_artifact(self, sha256, offset=offset, limit=limit)

    monkeypatch.setattr(_RuntimeServices, "read_artifact", _flipping_read_artifact)
    result = runtime.invoke(
        ToolCall(tool_name="read", arguments={"path": reference.uri, "offset": 50000, "limit": 2000}),
        context=ToolContext(
            workspace=context.workspace,
            session_id=context.session_id,
            run_id=context.run_id,
            invocation_id="inv-cancel-mid",
            revision_id=context.revision_id,
            actor="model",
        ),
    )
    assert isinstance(result, ToolFailure)
    assert result.error == "read cancelled"
    assert len(calls) == 2


def test_disabled_provider_removes_scheme_with_no_fallback(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with SqliteStore(tmp_path / "store.db") as store:
        revision = store.put_revision({"resource": "disabled"})
        store.record_workspace_revision(str(workspace), revision)
        store.create_session("session-disabled", str(workspace), revision)
        with PluginRuntime(
            (PluginSource(root=FILES_ROOT, plugin_id="files"),),
            workspace=workspace,
            revision_id=revision,
            store=store,
            artifacts_dir=tmp_path / "artifacts",
        ) as runtime:
            context = ToolContext(
                workspace=workspace,
                session_id="session-disabled",
                run_id="run-disabled",
                invocation_id="inv-disabled",
                revision_id=revision,
                actor="model",
            )
            result = runtime.invoke(ToolCall(tool_name="read", arguments={"path": "memo://hello"}), context=context)
            assert isinstance(result, ToolFailure)
            assert "unsupported resource URI: memo://hello" in result.error


def test_files_plugin_is_the_only_resource_dispatcher(tmp_path: Path) -> None:
    """Without `files`, schemes still dispatch in Core but no tool can reach them."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    memo_root = tmp_path / "memo_plugin"
    memo_root.mkdir()
    (memo_root / "__init__.py").write_text(textwrap.dedent(_MEMO_PLUGIN), encoding="utf-8")
    with SqliteStore(tmp_path / "store.db") as store:
        revision = store.put_revision({"resource": "no-files"})
        store.record_workspace_revision(str(workspace), revision)
        store.create_session("session-resource", str(workspace), revision)
        with PluginRuntime(
            (PluginSource(root=memo_root, plugin_id="memo"),),
            workspace=workspace,
            revision_id=revision,
            store=store,
            artifacts_dir=tmp_path / "artifacts",
        ) as runtime:
            assert "read" not in runtime.tool_definitions
            context = ToolContext(
                workspace=workspace,
                session_id="session-resource",
                run_id="run-resource",
                invocation_id="inv-resource",
                revision_id=revision,
                actor="model",
            )
            with pytest.raises(ValueError, match="Unknown tool"):
                runtime.invoke(ToolCall(tool_name="read", arguments={"path": "memo://hello"}), context=context)
            # The scheme itself is still registered: the coupling is the missing tool.
            page = runtime.read_resource(ResourceReadRequest(uri="memo://hello"), context=context)
            assert page is not None
            assert page.body["type"] == "memo"
