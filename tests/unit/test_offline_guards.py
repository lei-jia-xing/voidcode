"""Permanent teeth for the autouse offline guards defined in ``tests/conftest.py``.

The guards are load-bearing: they are what keeps a test from waiting out a real
connect timeout, hitting the live provider model-discovery endpoint, or launching
an external tool just to read its version. Each case below fails if its guard is
removed, so the offline behaviour cannot silently rot away.
"""

from __future__ import annotations

import socket
import ssl
import subprocess
import urllib.request

import pytest

_GUARD_MESSAGE = "tests must not open a real network connection"


def test_non_loopback_socket_connects_are_denied() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with pytest.raises(OSError, match=_GUARD_MESSAGE):
        sock.connect(("93.184.216.34", 80))
    with pytest.raises(OSError, match=_GUARD_MESSAGE):
        sock.connect_ex(("93.184.216.34", 80))
    tls = ssl.create_default_context().wrap_socket(sock, server_hostname="example.com")
    with pytest.raises(OSError, match=_GUARD_MESSAGE):
        tls.connect(("93.184.216.34", 443))
    sock.close()


def test_loopback_connect_still_succeeds() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    client = socket.create_connection(("127.0.0.1", listener.getsockname()[1]), timeout=2)
    server, _ = listener.accept()
    client.close()
    server.close()
    listener.close()


def test_live_model_discovery_is_cut_offline() -> None:
    from voidcode.provider.model_catalog import ProviderEndpointConfig, discover_available_models

    result = discover_available_models("openai", ProviderEndpointConfig(base_url="https://api.openai.com"))

    assert result.source == "fallback"
    assert result.last_refresh_status == "failed"
    assert result.models == ()


def test_doctor_version_probe_answers_without_spawning(monkeypatch: pytest.MonkeyPatch) -> None:
    from voidcode.doctor import checker

    def _forbid_spawn(*args: object, **kwargs: object) -> None:
        raise AssertionError("the canned doctor probe must not launch a real tool")

    monkeypatch.setattr(subprocess, "Popen", _forbid_spawn)
    result = checker.ExecutableChecker("python", "python").check()

    assert result.status is checker.CapabilityCheckStatus.READY
    assert result.details["command"] == "python"


def test_allow_live_network_restores_the_real_seams(allow_live_network: None) -> None:
    import voidcode.provider.model_catalog as catalog

    assert allow_live_network is None
    assert catalog.urlopen is urllib.request.urlopen
    assert socket.socket.connect.__name__ == "connect"
    assert ssl.SSLSocket.connect.__name__ == "connect"
