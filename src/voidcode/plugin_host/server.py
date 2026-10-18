"""Executable stdio plugin host server.

Manages isolated plugin lifecycles, validates admitted artifacts, and
dispatches tool and provider invocations over bounded JSONL.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, BinaryIO

import jsonschema

from voidcode.sdk.codec import (
    decode_context,
    decode_provider_request,
    decode_tool_call,
    encode_provider_error,
    encode_provider_result,
    encode_tool_definition,
    encode_tool_result,
)
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.manifest import PluginFileIdentity, PluginManifest, parse_plugin_manifest
from voidcode.sdk.plugin import Plugin, tool_definition
from voidcode.sdk.provider import ProviderExecutionError, ProviderTurnRequest
from voidcode.sdk.tools import ConfirmedStop, TextOutput, ToolFailure

MAX_FRAME_SIZE: int = 1024 * 1024  # 1 MiB

ALLOWED_REQUEST_KEYS: frozenset[str] = frozenset({"protocol_version", "id", "method", "params"})
ALLOWED_METHODS: frozenset[str] = frozenset({"init", "invoke", "propose_turn", "cancel", "close"})


class HostAbortSignal:
    """Thread-safe cancellation handle backed by a threading.Event."""

    def __init__(self, event: threading.Event) -> None:
        self._event = event
        self._reason: str | None = None
        self._lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    def set_cancelled(self, value: bool, *, reason: str | None = None) -> None:
        with self._lock:
            self._reason = reason if value else None
        if value:
            self._event.set()
        else:
            self._event.clear()


class PluginHostServer:
    """Stdio-driven plugin host executing plugins in an isolated process."""

    def __init__(self, in_stream: BinaryIO, out_stream: BinaryIO, *, standalone: bool = True) -> None:
        self._in_stream = in_stream
        self._out_stream = out_stream
        self._standalone = standalone
        self._write_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="plugin-worker")
        self._active_tasks: dict[str, threading.Event] = {}
        self._tasks_lock = threading.Lock()
        self._shutdown_event = threading.Event()

        self._workspace: Path | None = None
        self._revision_id: str | None = None
        self._plugins: list[Plugin] = []
        self._tools: dict[str, tuple[Any, Any, str]] = {}  # name -> (tool, decl, plugin_id)
        self._providers: dict[str, tuple[Any, Any, str]] = {}  # name -> (provider, decl, plugin_id)

    def _send_response(self, payload: dict[str, object]) -> None:
        data = json.dumps(payload, allow_nan=False).encode("utf-8") + b"\n"
        if len(data) > MAX_FRAME_SIZE:
            err_payload = {
                "protocol_version": 1,
                "id": str(payload.get("id", "unknown")),
                "ok": False,
                "error": {"kind": "frame_too_large", "message": "Response exceeds 1MiB frame limit"},
            }
            data = json.dumps(err_payload, allow_nan=False).encode("utf-8") + b"\n"
        with self._write_lock:
            self._out_stream.write(data)
            self._out_stream.flush()

    def run(self) -> int:
        """Main control loop reading commands from binary input stream."""
        try:
            while not self._shutdown_event.is_set():
                raw_line = self._in_stream.readline(MAX_FRAME_SIZE + 1)
                if not raw_line:
                    # EOF
                    break
                if len(raw_line) > MAX_FRAME_SIZE or not raw_line.endswith(b"\n"):
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": "unknown",
                            "ok": False,
                            "error": {"kind": "frame_too_large", "message": "Frame exceeds 1MiB limit or missing newline"},
                        }
                    )
                    break

                line = raw_line.strip()
                if not line:
                    continue

                try:
                    message = json.loads(line.decode("utf-8"))
                except Exception as e:
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": "unknown",
                            "ok": False,
                            "error": {"kind": "invalid_json", "message": f"Malformed JSON: {type(e).__name__}"},
                        }
                    )
                    break

                if not isinstance(message, dict):
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": "unknown",
                            "ok": False,
                            "error": {"kind": "invalid_envelope", "message": "Frame must be a JSON object"},
                        }
                    )
                    break

                if not self._dispatch(message):
                    break
        finally:
            self._cleanup()
        return 0

    def _dispatch(self, message: dict[str, Any]) -> bool:
        """Validate envelope and dispatch command. Returns False if fatal envelope error."""
        if set(message.keys()) != ALLOWED_REQUEST_KEYS:
            self._send_response(
                {
                    "protocol_version": 1,
                    "id": str(message.get("id", "unknown")),
                    "ok": False,
                    "error": {"kind": "invalid_envelope", "message": "Request envelope keys mismatch"},
                }
            )
            return False

        version = message["protocol_version"]
        if type(version) is not int or version != 1:
            self._send_response(
                {
                    "protocol_version": 1,
                    "id": str(message.get("id", "unknown")),
                    "ok": False,
                    "error": {"kind": "invalid_envelope", "message": "Unsupported or non-integer protocol_version"},
                }
            )
            return False

        req_id = message["id"]
        if not isinstance(req_id, str) or not req_id:
            self._send_response(
                {
                    "protocol_version": 1,
                    "id": "unknown",
                    "ok": False,
                    "error": {"kind": "invalid_envelope", "message": "Request id must be a non-empty string"},
                }
            )
            return False

        method = message["method"]
        if not isinstance(method, str) or method not in ALLOWED_METHODS:
            self._send_response(
                {
                    "protocol_version": 1,
                    "id": req_id,
                    "ok": False,
                    "error": {"kind": "unknown_method", "message": f"Unknown or disallowed method: {method}"},
                }
            )
            return True

        params = message["params"]
        if not isinstance(params, dict):
            self._send_response(
                {
                    "protocol_version": 1,
                    "id": req_id,
                    "ok": False,
                    "error": {"kind": "invalid_envelope", "message": "Request params must be a JSON object"},
                }
            )
            return False

        if method == "init":
            if set(params.keys()) != {"workspace", "revision_id", "plugins"}:
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {"kind": "invalid_params", "message": "Init params must contain workspace, revision_id, plugins"},
                    }
                )
                return False
            self._handle_init(req_id, params)
        elif method == "cancel":
            if set(params.keys()) != {"target_id"}:
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {"kind": "invalid_params", "message": "Cancel params must contain only target_id"},
                    }
                )
                return True
            self._handle_cancel(req_id, params)
        elif method == "close":
            if set(params.keys()) != set():
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {"kind": "invalid_params", "message": "Close params must be empty"},
                    }
                )
                return False
            self._handle_close(req_id)
            return False
        elif method == "invoke":
            if set(params.keys()) != {"call", "context"} or not isinstance(params["call"], dict) or not isinstance(params["context"], dict):
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {"kind": "invalid_params", "message": "Invoke params must contain call and context objects"},
                    }
                )
                return False
            self._handle_invoke(req_id, params)
        elif method == "propose_turn":
            if set(params.keys()) not in ({"request"}, {"request", "provider"}) or not isinstance(params["request"], dict):
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {"kind": "invalid_params", "message": "Propose turn params must contain request object"},
                    }
                )
                return False
            self._handle_propose_turn(req_id, params)
        return True

    def _handle_init(self, req_id: str, params: dict[str, Any]) -> None:
        try:
            workspace_raw = params.get("workspace")
            if not workspace_raw or not isinstance(workspace_raw, str):
                raise ValueError("workspace parameter required as non-empty string")
            self._workspace = Path(workspace_raw).resolve()
            if not self._workspace.is_dir():
                raise ValueError(f"Workspace directory does not exist: {self._workspace}")

            revision_id_raw = params.get("revision_id")
            if not revision_id_raw or not isinstance(revision_id_raw, str):
                raise ValueError("revision_id parameter required as non-empty string")
            self._revision_id = revision_id_raw

            plugins_spec = params.get("plugins")
            if not isinstance(plugins_spec, list):
                raise ValueError("plugins parameter must be a list")

            declared_modules: set[str] = set()

            for spec in plugins_spec:
                if not isinstance(spec, dict):
                    raise ValueError("plugin spec must be a dictionary")

                root_raw = spec.get("root")
                if not root_raw or not isinstance(root_raw, str):
                    raise ValueError("plugin root required")
                root = Path(root_raw).resolve()
                if not root.is_dir():
                    raise ValueError(f"Plugin root is not a directory: {root}")

                manifest = parse_plugin_manifest(spec.get("manifest"))
                if manifest.commands:
                    raise ValueError(f"Plugin {manifest.plugin_id} declares commands, which are unsupported in P1")
                config = spec.get("config", {})
                granted_caps = frozenset(str(c) for c in spec.get("capabilities", []))

                # Verify files and hashes
                self._check_plugin_files(root, manifest.files)

                # Validate entrypoint
                module_name, func_name = self._validate_entrypoint(manifest)

                # Check top-level module collision
                top_module = module_name.split(".", 1)[0]
                if top_module in declared_modules:
                    raise ValueError(f"Duplicate plugin top-level module name across plugins: {top_module}")
                declared_modules.add(top_module)

                # Verify all required tool capabilities are granted before import
                for tool_decl in manifest.tools:
                    req_caps = frozenset(tool_decl.capabilities)
                    if not req_caps.issubset(granted_caps):
                        missing = req_caps - granted_caps
                        raise ValueError(f"Plugin {manifest.plugin_id} tool {tool_decl.name} requires capabilities {set(missing)} not granted")

                # Validate config schema using wire-safe objects
                if manifest.config_schema is not None:
                    try:
                        schema_wire = json_wire_object(manifest.config_schema)
                        config_wire = json_wire_object(config)
                        jsonschema.validate(instance=config_wire, schema=schema_wire)
                    except jsonschema.ValidationError as err:
                        raise ValueError(f"Plugin config violates schema: {err.message}") from err

                # Check top-level module conflict with pre-imported modules
                if top_module in sys.modules:
                    existing = sys.modules[top_module]
                    existing_file = getattr(existing, "__file__", None)
                    if existing_file:
                        try:
                            Path(existing_file).resolve().relative_to(root)
                        except ValueError as err:
                            raise ValueError(f"Top-level module conflict: module {top_module} already imported from outside root") from err

                # Add root to sys.path and import module
                root_str = str(root)
                if root_str not in sys.path:
                    sys.path.insert(0, root_str)

                mod = importlib.import_module(module_name)
                mod_file = getattr(mod, "__file__", None)
                if not mod_file:
                    raise ValueError(f"Imported module {module_name} has no __file__")
                try:
                    Path(mod_file).resolve().relative_to(root)
                except ValueError as err:
                    raise ValueError(f"Module {module_name} was imported from outside artifact root: {mod_file}") from err

                factory = getattr(mod, func_name, None)
                if factory is None or not callable(factory):
                    raise ValueError(f"Factory callable {func_name} not found in {module_name}")

                if hasattr(factory, "__code__"):
                    factory_file = Path(factory.__code__.co_filename).resolve()
                    try:
                        factory_file.relative_to(root)
                    except ValueError as err:
                        raise ValueError(f"Factory callable code origin is outside artifact root: {factory_file}") from err

                try:
                    plugin_instance = factory(config)
                except Exception as exc:
                    raise ValueError(f"Plugin factory failed with {type(exc).__name__}") from exc
                if not isinstance(plugin_instance, Plugin):
                    raise ValueError(f"Factory returned {type(plugin_instance).__name__}, expected Plugin")

                # Verify registered tools match manifest exactly without duplicate registered names
                seen_reg_tools: set[str] = set()
                reg_tools_dict: dict[str, Any] = {}
                for t in plugin_instance.tools:
                    name = t.definition.name
                    if name in seen_reg_tools:
                        raise ValueError(f"Plugin {manifest.plugin_id} registered duplicate tool name: {name}")
                    seen_reg_tools.add(name)
                    reg_tools_dict[name] = t

                manifest_tools_dict = {t.name: t for t in manifest.tools}
                reg_names = set(reg_tools_dict)
                manifest_names = set(manifest_tools_dict)
                if reg_names != manifest_names:
                    raise ValueError(f"Plugin {manifest.plugin_id} tools mismatch: registered {reg_names} vs manifest {manifest_names}")

                # Full definition comparison
                for name, t in reg_tools_dict.items():
                    decl = manifest_tools_dict[name]
                    expected_def = tool_definition(decl)
                    if t.definition != expected_def:
                        raise ValueError(f"Tool {name} definition mismatch with manifest: registered {t.definition} vs {expected_def}")

                # Verify registered providers match manifest exactly without duplicate registered names
                seen_reg_provs: set[str] = set()
                reg_provs_dict: dict[str, Any] = {}
                for p in plugin_instance.providers:
                    name = p.name
                    if name in seen_reg_provs:
                        raise ValueError(f"Plugin {manifest.plugin_id} registered duplicate provider name: {name}")
                    seen_reg_provs.add(name)
                    reg_provs_dict[name] = p

                manifest_provs_dict = {p.name: p for p in manifest.providers}
                reg_prov_names = set(reg_provs_dict)
                manifest_prov_names = set(manifest_provs_dict)
                if reg_prov_names != manifest_prov_names:
                    raise ValueError(f"Plugin {manifest.plugin_id} providers mismatch: registered {reg_prov_names} vs manifest {manifest_prov_names}")

                # Store registrations
                for name, tool in reg_tools_dict.items():
                    if name in self._tools:
                        raise ValueError(f"Duplicate tool name registered across plugins: {name}")
                    self._tools[name] = (tool, manifest_tools_dict[name], manifest.plugin_id)

                for name, prov in reg_provs_dict.items():
                    if name in self._providers:
                        raise ValueError(f"Duplicate provider name registered across plugins: {name}")
                    self._providers[name] = (prov, manifest_provs_dict[name], manifest.plugin_id)

                self._plugins.append(plugin_instance)

            # Build catalog
            catalog_tools = [encode_tool_definition(tool.definition) for tool, _, _ in self._tools.values()]
            catalog_providers = list(self._providers.keys())
            owners = {name: plugin_id for name, (_, _, plugin_id) in self._tools.items()}
            capabilities = {name: list(decl.capabilities) for name, (_, decl, _) in self._tools.items()}

            self._send_response(
                {
                    "protocol_version": 1,
                    "id": req_id,
                    "ok": True,
                    "result": {
                        "tools": catalog_tools,
                        "providers": catalog_providers,
                        "tool_owners": owners,
                        "tool_capabilities": capabilities,
                    },
                }
            )
        except Exception as e:
            err_msg = str(e) if isinstance(e, ValueError) else f"Initialization failed with {type(e).__name__}"
            self._send_response(
                {
                    "protocol_version": 1,
                    "id": req_id,
                    "ok": False,
                    "error": {"kind": "init_failed", "message": err_msg},
                }
            )

    def _check_plugin_files(self, root: Path, declared_files: tuple[PluginFileIdentity, ...]) -> None:
        declared_paths: set[str] = set()
        for f in declared_files:
            declared_paths.add(f.path)
            file_path = root / f.path
            if not file_path.is_file():
                raise ValueError(f"Declared plugin file missing: {f.path}")
            computed = hashlib.sha256(file_path.read_bytes()).hexdigest()
            if computed != f.sha256:
                raise ValueError(f"File hash mismatch for {f.path}: expected {f.sha256}, got {computed}")

        for py_path in root.rglob("*.py"):
            rel = py_path.relative_to(root).as_posix()
            if rel not in declared_paths:
                raise ValueError(f"Undeclared python file in artifact root: {rel}")

    def _validate_entrypoint(self, manifest: PluginManifest) -> tuple[str, str]:
        if ":" not in manifest.entrypoint:
            raise ValueError(f"Invalid entrypoint format: {manifest.entrypoint}")
        module_name, func_name = manifest.entrypoint.split(":", 1)
        if not module_name or not func_name:
            raise ValueError(f"Invalid entrypoint format: {manifest.entrypoint}")

        declared_paths = {f.path for f in manifest.files}
        expected_file = f"{module_name.replace('.', '/')}.py"
        expected_package = f"{module_name.replace('.', '/')}/__init__.py"
        if expected_file not in declared_paths and expected_package not in declared_paths:
            raise ValueError(f"Entrypoint module {module_name} not found in declared files (expected {expected_file} or {expected_package})")
        return module_name, func_name

    def _handle_cancel(self, req_id: str, params: dict[str, Any]) -> None:
        target_id = params.get("target_id")
        with self._tasks_lock:
            event = self._active_tasks.get(str(target_id))
        if event is not None:
            event.set()
        self._send_response(
            {
                "protocol_version": 1,
                "id": req_id,
                "ok": True,
                "result": {"cancelled": True},
            }
        )

    def _handle_close(self, req_id: str) -> None:
        self._send_response(
            {
                "protocol_version": 1,
                "id": req_id,
                "ok": True,
                "result": {"closed": True},
            }
        )
        self._shutdown_event.set()

    def _handle_invoke(self, req_id: str, params: dict[str, Any]) -> None:
        cancel_event = threading.Event()
        with self._tasks_lock:
            if req_id in self._active_tasks:
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {"kind": "duplicate_id", "message": f"Request id '{req_id}' is already active"},
                    }
                )
                return
            self._active_tasks[req_id] = cancel_event

        def worker() -> None:
            try:
                try:
                    call = decode_tool_call(params["call"])
                    context = decode_context(params["context"])
                except Exception as dec_err:
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": req_id,
                            "ok": False,
                            "error": {"kind": "invalid_params", "message": f"Invalid params: {type(dec_err).__name__}"},
                        }
                    )
                    return

                # Context check
                if self._workspace is None or context.workspace.resolve() != self._workspace.resolve():
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": req_id,
                            "ok": False,
                            "error": {"kind": "context_mismatch", "message": "Context workspace mismatch"},
                        }
                    )
                    return

                if self._revision_id is None or context.revision_id != self._revision_id:
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": req_id,
                            "ok": False,
                            "error": {"kind": "context_mismatch", "message": "Context revision mismatch"},
                        }
                    )
                    return

                tool_entry = self._tools.get(call.tool_name)
                if tool_entry is None:
                    res = ToolFailure(
                        tool_name=call.tool_name,
                        error=f"Unknown tool: {call.tool_name}",
                        output=TextOutput(text=f"Unknown tool: {call.tool_name}"),
                        execution=ConfirmedStop(cancellation_signalled=cancel_event.is_set()),
                    )
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": req_id,
                            "ok": True,
                            "result": encode_tool_result(res),
                        }
                    )
                    return

                tool_instance, tool_decl, _ = tool_entry

                # Schema validation of tool arguments
                if tool_decl.input_schema:
                    try:
                        args_wire = json_wire_object(call.arguments)
                        schema_wire = json_wire_object(tool_decl.input_schema)
                        jsonschema.validate(instance=args_wire, schema=schema_wire)
                    except jsonschema.ValidationError as val_err:
                        res = ToolFailure(
                            tool_name=call.tool_name,
                            error=f"Input validation error: {val_err.message}",
                            output=TextOutput(text=f"Validation error: {val_err.message}"),
                            execution=ConfirmedStop(cancellation_signalled=cancel_event.is_set()),
                        )
                        self._send_response(
                            {
                                "protocol_version": 1,
                                "id": req_id,
                                "ok": True,
                                "result": encode_tool_result(res),
                            }
                        )
                        return

                # Grants check
                required_caps = frozenset(tool_decl.capabilities)
                if not required_caps.issubset(context.capabilities):
                    missing = required_caps - context.capabilities
                    res = ToolFailure(
                        tool_name=call.tool_name,
                        error=f"Missing capability grants: {set(missing)}",
                        output=TextOutput(text=f"Denied: missing capability grants {set(missing)}"),
                        execution=ConfirmedStop(cancellation_signalled=cancel_event.is_set()),
                    )
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": req_id,
                            "ok": True,
                            "result": encode_tool_result(res),
                        }
                    )
                    return

                # Create context with abort signal
                abort_signal = HostAbortSignal(cancel_event)
                inv_context = ToolContext(
                    workspace=context.workspace,
                    session_id=context.session_id,
                    run_id=context.run_id,
                    invocation_id=context.invocation_id,
                    revision_id=context.revision_id,
                    actor=context.actor,
                    capabilities=context.capabilities,
                    abort_signal=abort_signal,
                )

                try:
                    result = tool_instance.invoke(call, context=inv_context)
                except Exception as exc:
                    result = ToolFailure(
                        tool_name=call.tool_name,
                        error=f"Tool invocation failed with {type(exc).__name__}",
                        output=TextOutput(text=f"Tool invocation failed with {type(exc).__name__}"),
                        execution=ConfirmedStop(cancellation_signalled=cancel_event.is_set()),
                    )

                if cancel_event.is_set() and isinstance(result, ToolFailure) and result.execution is None:
                    result = ToolFailure(
                        tool_name=result.tool_name,
                        error=result.error,
                        output=result.output,
                        body=result.body,
                        diagnostics=result.diagnostics,
                        execution=ConfirmedStop(cancellation_signalled=True),
                        timeout_seconds=result.timeout_seconds,
                    )

                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": True,
                        "result": encode_tool_result(result),
                    }
                )
            except Exception as unexp_err:
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {"kind": "internal_error", "message": f"Internal error: {type(unexp_err).__name__}"},
                    }
                )
            finally:
                with self._tasks_lock:
                    self._active_tasks.pop(req_id, None)

        self._executor.submit(worker)

    def _handle_propose_turn(self, req_id: str, params: dict[str, Any]) -> None:
        cancel_event = threading.Event()
        with self._tasks_lock:
            if req_id in self._active_tasks:
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {"kind": "duplicate_id", "message": f"Request id '{req_id}' is already active"},
                    }
                )
                return
            self._active_tasks[req_id] = cancel_event

        def worker() -> None:
            try:
                try:
                    request = decode_provider_request(params["request"])
                    provider_name = str(params.get("provider", "openai-chat"))
                except Exception as dec_err:
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": req_id,
                            "ok": False,
                            "error": {"kind": "invalid_params", "message": f"Invalid params: {type(dec_err).__name__}"},
                        }
                    )
                    return

                prov_entry = self._providers.get(provider_name)
                if prov_entry is None:
                    err = ProviderExecutionError(
                        kind="not_configured",
                        provider_name=provider_name,
                        model_name=request.model_name or "unknown",
                        message=f"Provider '{provider_name}' not configured",
                    )
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": req_id,
                            "ok": False,
                            "error": {
                                "kind": "provider_error",
                                "message": err.message,
                                "provider_error": encode_provider_error(err),
                            },
                        }
                    )
                    return

                provider_instance, _, _ = prov_entry
                abort_signal = HostAbortSignal(cancel_event)
                prov_req = ProviderTurnRequest(
                    messages=request.messages,
                    available_tools=request.available_tools,
                    raw_model=request.raw_model,
                    provider_name=request.provider_name,
                    model_name=request.model_name,
                    reasoning_effort=request.reasoning_effort,
                    cache_retention=request.cache_retention,
                    attempt=request.attempt,
                    abort_signal=abort_signal,
                    metadata=request.metadata,
                )

                try:
                    res = provider_instance.propose_turn(prov_req)
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": req_id,
                            "ok": True,
                            "result": encode_provider_result(res),
                        }
                    )
                except ProviderExecutionError as p_err:
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": req_id,
                            "ok": False,
                            "error": {
                                "kind": "provider_error",
                                "message": p_err.message,
                                "provider_error": encode_provider_error(p_err),
                            },
                        }
                    )
                except Exception as exc:
                    self._send_response(
                        {
                            "protocol_version": 1,
                            "id": req_id,
                            "ok": False,
                            "error": {
                                "kind": "provider_error",
                                "message": f"Provider turn failed with {type(exc).__name__}",
                            },
                        }
                    )
            except Exception as unexp_err:
                self._send_response(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "ok": False,
                        "error": {"kind": "internal_error", "message": f"Internal error: {type(unexp_err).__name__}"},
                    }
                )
            finally:
                with self._tasks_lock:
                    self._active_tasks.pop(req_id, None)

        self._executor.submit(worker)

    def _cleanup(self) -> None:
        self._shutdown_event.set()
        # 1. Signal cancellation to all running tasks
        with self._tasks_lock:
            events = list(self._active_tasks.values())
        for ev in events:
            ev.set()

        # 2. Call plugin.close() with bounded timeout per plugin
        for plugin in self._plugins:
            if plugin.close is not None and callable(plugin.close):
                th = threading.Thread(target=plugin.close, daemon=True)
                th.start()
                th.join(timeout=0.5)

        # 3. Give running workers brief cooperative grace
        self._executor.shutdown(wait=False, cancel_futures=True)
        time.sleep(0.2)

        # 4. If standalone server process, exit hard so stubborn callbacks cannot hang process
        if self._standalone:
            os._exit(0)


def run_server(in_stream: BinaryIO | None = None, out_stream: BinaryIO | None = None) -> int:
    """Entry point configuring reserved stdout protocol stream and running server."""
    if out_stream is None:
        # Reserve real stdout for protocol by duplicating fd 1, and redirect sys.stdout to sys.stderr
        real_stdout_fd = os.dup(sys.stdout.fileno())
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        protocol_out = os.fdopen(real_stdout_fd, "wb", buffering=0)
        sys.stdout = sys.stderr
    else:
        protocol_out = out_stream

    protocol_in = sys.stdin.buffer if in_stream is None else in_stream
    server = PluginHostServer(in_stream=protocol_in, out_stream=protocol_out, standalone=True)
    return server.run()
