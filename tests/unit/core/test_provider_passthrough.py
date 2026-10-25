"""Opaque provider fields: delivered verbatim to trusted plugin factories, never to records.

The core never learns a vendor vocabulary, so every configured field that is not the typed
``api_key``/``base_url`` must reach the plugin factories under the reserved ``providers`` key
without being named or redacted, and must never be written to a revision, event, or log record.
"""

from __future__ import annotations

import json
import sqlite3
import stat
import subprocess
import sys
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.core.activation import bundled_sources, config_from_snapshot, load_private_snapshot, snapshot_path
from voidcode.sdk.json_values import json_wire_object

PROVIDER_NAME = "vendor-passthrough"
PROVIDER_PLUGIN = "vendor-provider-fixture"
BYSTANDER_PLUGIN = "vendor-bystander-fixture"

#: Deliberately unguessable markers: a leak check is only meaningful against a value that no
#: other record in the state directory could contain by coincidence.
API_KEY = "vendor-fixture-secret-key"
VENDOR_FIELDS: dict[str, object] = {
    "headers": {"x-vendor-tenant": "vendor-tenant-marker", "x-vendor-endpoint": "vendor-endpoint-marker"},
    "region": "vendor-region-marker",
    "timeout_seconds": 5,
    "stream": True,
    "retries": None,
    "scopes": ["chat", "tools"],
}
MARKERS = (API_KEY, "vendor-tenant-marker", "vendor-endpoint-marker", "vendor-region-marker")

_PROVIDER_MODULE = '''
"""Test-only provider: reports exactly the fields the core delivered for its provider name."""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Mapping

from voidcode.sdk.plugin import Plugin
from voidcode.sdk.provider import ProviderTurnResult

PROVIDER_NAME = "__PROVIDER_NAME__"


class _FixtureProvider:
    name = PROVIDER_NAME

    def __init__(self, config: Mapping[str, object]) -> None:
        # The plugin's own view: the flattened vendor fields plus the typed api_key/base_url.
        entry = dict(dict(config.get("providers", {})).get(PROVIDER_NAME, {}))
        self._api_key = entry.pop("api_key", None)
        self._base_url = str(entry.pop("base_url", ""))
        self._vendor = entry

    def propose_turn(self, request: object) -> ProviderTurnResult:
        outbound = urllib.request.Request(
            self._base_url + "/v1/chat/completions",
            data=b"{}",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
                # One opaque header, round-tripped through the real loopback socket, so the test
                # reads back the fields the child process was handed rather than a core echo.
                "X-Vendor-Fields": json.dumps(self._vendor, sort_keys=True, separators=(",", ":")),
            },
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(outbound, timeout=15) as response:
            response.read()
        return ProviderTurnResult(output="vendor passthrough turn", done_reason="stop", finish_reason_reported=True)


def create_plugin(config=None) -> Plugin:
    return Plugin(providers=(_FixtureProvider(dict(config or {})),))
'''

_BYSTANDER_MODULE = '''
"""Test-only bystander: records the exact configuration the core delivered to it."""

from __future__ import annotations

import json
from pathlib import Path

from voidcode.sdk.plugin import Plugin


def create_plugin(config=None) -> Plugin:
    received = dict(config or {})
    evidence = received.get("evidence")
    if isinstance(evidence, str):
        Path(evidence).write_text(json.dumps(received, sort_keys=True, default=list), encoding="utf-8")
    return Plugin()
'''


def _write_source(source: Path, *, body: str) -> Path:
    source.mkdir(parents=True, exist_ok=True)
    (source / "__init__.py").write_text(body, encoding="utf-8")
    return source


def _write_provider_plugin(root: Path) -> Path:
    return _write_source(root, body=_PROVIDER_MODULE.replace("__PROVIDER_NAME__", PROVIDER_NAME))


def _write_bystander_plugin(root: Path) -> Path:
    return _write_source(root, body=_BYSTANDER_MODULE)


@contextmanager
def _server(replies: list[dict[str, object]]) -> Iterator[OpenAILoopbackServer]:
    server = OpenAILoopbackServer(replies)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def _turn(output: str) -> dict[str, object]:
    return openai_response(message={"role": "assistant", "content": output}, finish="stop")


def _cli(workspace: Path, state: Path, *arguments: str) -> dict:
    command = [
        sys.executable,
        "-m",
        "voidcode.core",
        "--workspace",
        str(workspace),
        "--state-dir",
        str(state),
        *arguments,
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def _field_headers(server: OpenAILoopbackServer) -> list[dict[str, str]]:
    """The opaque field header of every request that reached the loopback endpoint."""
    fields: list[dict[str, str]] = []
    for headers in server.request_headers:
        lowered = {name.lower(): value for name, value in headers.items()}
        if "x-vendor-fields" in lowered:
            fields.append(lowered)
    return fields


def _revision_payload(state: Path, revision_id: str) -> Mapping[str, object]:
    connection = sqlite3.connect(f"{(state / 'voidcode.db').resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = connection.execute("SELECT payload_json FROM revisions WHERE revision_id = ?", (revision_id,)).fetchone()
        assert row is not None, "revision is not durable"
        events = connection.execute("SELECT COUNT(*) FROM session_events").fetchone()
        assert events is not None and events[0] > 0, "the fixture run left no durable events to inspect"
        return json.loads(str(row[0]))
    finally:
        connection.close()


def test_vendor_fields_reach_only_the_declaring_plugin(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    evidence = tmp_path / "bystander-config.json"
    provider_source = _write_provider_plugin(tmp_path / "provider-plugin")
    bystander_source = _write_bystander_plugin(tmp_path / "bystander-plugin")

    # One reply per expected provider turn: any extra dispatch fails loudly instead of being
    # absorbed by an elastic reply list.
    with _server([_turn("first"), _turn("after restart")]) as server:
        base_url = f"http://127.0.0.1:{server.server_port}"
        config = {
            "schema_version": 1,
            "model": {"model": f"{PROVIDER_NAME}/fixture-model"},
            "policy": {"approval_mode": "yolo", "read_only": False},
            "providers": {PROVIDER_NAME: {"api_key": API_KEY, "base_url": base_url, **VENDOR_FIELDS}},
            "plugins": {
                # This test is about provider fields, not bundled plugins: admit only the two
                # fixtures, so an unrelated plugin edit cannot make the subject unobservable.
                **{plugin_id: {"enabled": False} for plugin_id in bundled_sources()},
                PROVIDER_PLUGIN: {"path": str(provider_source)},
                BYSTANDER_PLUGIN: {"path": str(bystander_source), "config": {"evidence": str(evidence)}},
            },
        }
        (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")

        first = _cli(workspace, state, "run", "vendor fields", "--session-id", "vendor-first")
        assert first["status"] == "completed"
        assert first["output"] == "vendor passthrough turn"
        revision_id = str(first["revision_id"])

        # A restart never re-admits the workspace file: the vendor fields reach the plugin from
        # the private snapshot, so a rewritten workspace provider entry changes nothing.
        (workspace / "voidcode.json").write_text(
            json.dumps({**config, "providers": {PROVIDER_NAME: {"api_key": "workspace-rewritten", "base_url": base_url}}}),
            encoding="utf-8",
        )
        second = _cli(workspace, state, "run", "vendor fields after restart", "--session-id", "vendor-second")
        assert second["status"] == "completed"
        assert second["revision_id"] == revision_id

        # The owning plugin was handed the vendor fields verbatim, nested values included, and
        # put them on the real loopback wire.
        fields = _field_headers(server)
        assert len(fields) == len(server.requests) == 2
        for headers in fields:
            assert headers["authorization"] == f"Bearer {API_KEY}"
            assert json.loads(headers["x-vendor-fields"]) == VENDOR_FIELDS

        # Every trusted plugin factory also receives the reserved providers key verbatim.
        recorded = json.loads(evidence.read_text(encoding="utf-8"))
        assert recorded == {
            "evidence": str(evidence),
            "providers": {PROVIDER_NAME: {"api_key": API_KEY, "base_url": base_url, **VENDOR_FIELDS}},
        }

    # The credential-bearing snapshot is private, and it is where the fields live.
    location = snapshot_path(state, revision_id)
    assert stat.S_IMODE(location.stat().st_mode) == 0o600
    assert stat.S_IMODE(location.parent.stat().st_mode) == 0o700
    snapshot = load_private_snapshot(state, revision_id)
    assert snapshot["providers"][PROVIDER_NAME] == {"api_key": API_KEY, "base_url": base_url, **VENDOR_FIELDS}

    # A configuration restored from that snapshot keeps every opaque field, not just the two
    # the core knows by name.
    restored = config_from_snapshot(snapshot)
    assert restored.providers[PROVIDER_NAME].api_key == API_KEY
    assert restored.providers[PROVIDER_NAME].base_url == base_url
    assert json_wire_object(restored.providers[PROVIDER_NAME].extra) == VENDOR_FIELDS

    # Public records carry only the generation's plugin composition, never a credential field.
    assert PROVIDER_PLUGIN in _revision_payload(state, revision_id)["plugins"]
    database = b"".join(path.read_bytes() for path in sorted(state.glob("voidcode.db*")))
    for marker in MARKERS:
        assert marker.encode("utf-8") not in database
