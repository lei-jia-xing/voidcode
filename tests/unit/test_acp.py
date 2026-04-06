from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from voidcode.runtime.config import RuntimeAcpConfig


def _load_acp_symbols() -> tuple[Any, Any]:
    module: Any = import_module("voidcode.runtime.acp")
    return module.AcpConfigState, module.DisabledAcpAdapter


AcpConfigState: Any
DisabledAcpAdapter: Any
AcpConfigState, DisabledAcpAdapter = _load_acp_symbols()


def test_acp_config_state_defaults_to_disabled() -> None:
    state = AcpConfigState.from_runtime_config(None)

    assert state.configured_enabled is False


def test_acp_config_state_wraps_runtime_acp_config() -> None:
    state = AcpConfigState.from_runtime_config(RuntimeAcpConfig(enabled=True))

    assert state.configured_enabled is True


def test_disabled_acp_adapter_reports_disabled_unavailable_state() -> None:
    adapter = DisabledAcpAdapter(RuntimeAcpConfig(enabled=True))

    state = adapter.current_state()

    assert adapter.configuration.configured_enabled is True
    assert state.mode == "disabled"
    assert state.configuration is adapter.configuration
    assert state.configured is True
    assert state.available is False
