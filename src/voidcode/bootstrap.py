"""Product wiring: admission assembly, generation adoption, and core bootstrap."""

from __future__ import annotations

import fcntl
import hashlib
import os
import sys
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple
from uuid import uuid4

from .config import (
    CONFIG_FILE_NAME,
    Config,
    PluginEntry,
    load_config,
    parse_config,
    resolve_providers,
)
from .core.activation import (
    PRIVATE_DIR_NAME,
    Admission,
    ExecutionConfig,
    ExecutionPolicy,
    SandboxPolicy,
    load_private_snapshot,
    plan_state_copies,
    write_private_snapshot,
)
from .core.engine import Core
from .core.events import EventType
from .core.plugins import PluginRuntime, PluginSource
from .core.processes import ProcessHost
from .core.sandbox import SandboxBackend, check_helper
from .core.storage.store import (
    DATABASE_FILENAME,
    CheckpointKind,
    EventDraft,
    SessionStatus,
    SqliteStore,
    canonical_json,
    inspect_existing_workspace,
)
from .sdk.json_values import json_wire_object
from .sdk.plugin import StateScope

if TYPE_CHECKING:
    from .sdk.client import SessionClient

BUNDLED_PLUGIN_IDS = (
    "ast_grep",
    "btw",
    "compaction",
    "cron",
    "subagents",
    "exec",
    "files",
    "find",
    "goal",
    "hooks",
    "lsp",
    "mcp",
    "plan",
    "providers",
    "review",
    "skills",
    "todo",
    "user_shell",
    "web",
    "workflows",
)


def bundled_sources(package_root: Path | None = None) -> dict[str, Path]:
    """The plugin sources shipped with one installation: id -> source directory."""
    root = Path(__file__).resolve().parent if package_root is None else Path(package_root)
    bundled = root / "plugins"
    return {plugin_id: bundled / plugin_id.replace("-", "_") for plugin_id in BUNDLED_PLUGIN_IDS}


def merged_entries(config: Config, bundled_sources_: Mapping[str, Path]) -> dict[str, PluginEntry]:
    """Every configured entry: the shipped bundled defaults first, the user's entries on top."""
    entries = {plugin_id: config.plugins.get(plugin_id, PluginEntry()) for plugin_id in bundled_sources_}
    entries.update(config.plugins)
    return entries


def _plugin_root(
    workspace: Path,
    bundled: Mapping[str, Path],
    plugin_id: str,
    entry: PluginEntry,
) -> Path:
    if entry.path is not None:
        root = (workspace / entry.path).resolve(strict=True)
        if not root.is_dir():
            raise ValueError(f"plugin {plugin_id} path is not a directory: {root}")
    else:
        source = bundled.get(plugin_id)
        if source is None:
            raise ValueError(f"plugin {plugin_id} requires an explicit path")
        if not source.is_dir():
            raise ValueError(f"bundled plugin {plugin_id} is missing from this installation: {source}")
        root = source.resolve(strict=True)
    if not (root / "__init__.py").is_file():
        raise ValueError(f"plugin {plugin_id} source is not a package with an __init__.py: {root}")
    return root


class _PluginInputs(NamedTuple):
    """One admission's resolved plugin layer: runtime inputs plus the private record."""

    providers: Mapping[str, object]
    plugins: tuple[PluginSource, ...]
    snapshot_plugins: dict[str, object]


def _factory_config(plugin_config: Mapping[str, object], providers: Mapping[str, object]) -> dict[str, object]:
    """Factory input for one plugin: its own config plus the resolved providers map.

    Single injection point for the reserved ``config["providers"]`` contract; the
    private snapshot keeps the raw per-plugin config without it.
    """
    merged = dict(plugin_config)
    if providers:
        merged["providers"] = providers
    return merged


def _plugin_inputs(
    config: Config,
    *,
    workspace: Path,
    bundled_sources: Mapping[str, Path],
    use_environment: bool,
) -> _PluginInputs:
    """Resolve entries, providers, runtime plugin inputs, and the private plugin record."""
    workspace = Path(workspace).resolve(strict=True)
    entries = merged_entries(config, bundled_sources)
    provider_entry = entries.get("providers")
    providers = resolve_providers(
        config,
        use_environment=use_environment,
        discover=provider_entry is not None and provider_entry.enabled and provider_entry.path is None,
    )
    plugins: list[PluginSource] = []
    snapshot_plugins: dict[str, object] = {}
    for plugin_id, entry in sorted(entries.items()):
        snapshot_plugins[plugin_id] = {
            "enabled": entry.enabled,
            "path": entry.path,
            "config": dict(entry.config),
        }
        if not entry.enabled:
            continue
        root = _plugin_root(workspace, bundled_sources, plugin_id, entry)
        if "providers" in entry.config:
            raise ValueError(f"plugin {plugin_id} must configure provider credentials in the root providers section")
        plugins.append(PluginSource(root=root, plugin_id=plugin_id, config=_factory_config(entry.config, providers)))
    return _PluginInputs(providers=providers, plugins=tuple(plugins), snapshot_plugins=snapshot_plugins)


def _execution_config(config: Config) -> ExecutionConfig:
    """Runtime view of model/policy: typed and range-checked for the core."""
    return ExecutionConfig(
        model=config.model.model,
        reasoning_effort=config.model.reasoning_effort,
        policy=ExecutionPolicy(
            approval_mode=config.policy.approval_mode,
            read_only=config.policy.read_only,
        ),
        max_root_children=config.policy.max_root_children,
    )


def admit_live(
    config: Config,
    *,
    workspace: Path,
    bundled_sources: Mapping[str, Path],
    revision_id: str | None = None,
    use_environment: bool = True,
) -> Admission:
    """Resolve every enabled configured plugin to a runtime input and build Admission."""
    inputs = _plugin_inputs(config, workspace=workspace, bundled_sources=bundled_sources, use_environment=use_environment)
    # The model/policy sections are built once and shared: the private snapshot owns the
    # credential-bearing record, the public payload carries the same values credential-free,
    # and Admission deep-copies each side into its own read-only snapshot.
    model = {"model": config.model.model, "reasoning_effort": config.model.reasoning_effort}
    policy = {
        "approval_mode": config.policy.approval_mode,
        "read_only": config.policy.read_only,
        "max_root_children": config.policy.max_root_children,
    }
    snapshot: dict[str, object] = {
        "providers": inputs.providers,
        "model": model,
        "policy": policy,
        "plugins": inputs.snapshot_plugins,
    }
    snapshot["sandbox"] = (
        {
            "mode": config.sandbox.mode,
            "network": config.sandbox.effective_network,
            "writable_roots": list(config.sandbox.writable_roots),
        }
        if config.sandbox is not None
        else None
    )
    payload: dict[str, object] = {
        "python": sys.implementation.cache_tag,
        "dependencies": {name: version(name) for name in ("openai", "pydantic", "jsonschema")},
        "model": model,
        "policy": policy,
        "plugins": sorted(plugin.plugin_id for plugin in inputs.plugins),
        "private_config_sha256": hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest(),
    }
    return Admission(
        revision_id=uuid4().hex if revision_id is None else revision_id,
        plugins=inputs.plugins,
        revision_payload=payload,
        private_snapshot=snapshot,
        config=_execution_config(config),
        validate=lambda registered: validate_providers(config, registered, providers=inputs.providers),
    )


def config_from_snapshot(snapshot: Mapping[str, object]) -> Config:
    """Rebuild the effective configuration a stored revision was admitted with.

    ``snapshot`` is the private, digest-verified record written at admission time, so
    credentials are read from it and never from the live file or the environment. Every
    section must be present explicitly: ``Config`` gives a live file defaults, but a
    recorded revision without a section is malformed. Section shapes are then validated
    once by ``parse_config``; the snapshot loader already froze the payload to JSON.
    """
    missing = [name for name in ("providers", "model", "policy", "plugins", "sandbox") if name not in snapshot]
    if missing:
        raise ValueError("private configuration snapshot is missing " + ", ".join(missing))
    sections = {name: snapshot[name] for name in ("model", "policy", "providers", "plugins", "sandbox")}
    return parse_config({"schema_version": 1, **sections})


def restore_admission(
    payload: Mapping[str, object],
    snapshot: Mapping[str, object],
    *,
    revision_id: str,
    workspace: Path,
    bundled_sources: Mapping[str, Path],
    config: Config,
) -> Admission:
    """Rebuild a generation from durable records, importing the CURRENT plugin source.

    ``config`` is the recorded configuration, parsed once by the caller from the same
    snapshot whose digest is verified here. The recorded payload/snapshot are reused
    verbatim; only the plugin inputs and runtime config are freshly resolved.
    """
    payload = json_wire_object(payload)
    snapshot = json_wire_object(snapshot)
    digest = hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()
    if payload.get("private_config_sha256") != digest:
        raise ValueError("private configuration snapshot does not match the revision it belongs to")
    inputs = _plugin_inputs(config, workspace=workspace, bundled_sources=bundled_sources, use_environment=False)
    recorded = payload.get("plugins")
    if not isinstance(recorded, list) or not all(isinstance(plugin_id, str) for plugin_id in recorded):
        raise ValueError("revision payload has no plugin composition")
    if set(recorded) != {plugin.plugin_id for plugin in inputs.plugins}:
        raise ValueError("recorded plugin composition does not match the recorded configuration")
    return Admission(
        revision_id=revision_id,
        plugins=inputs.plugins,
        revision_payload=payload,
        private_snapshot=snapshot,
        config=_execution_config(config),
        validate=lambda registered: validate_providers(config, registered, providers=inputs.providers),
    )


def state_directory(workspace: Path) -> Path:
    identity = hashlib.sha256(os.fsencode(workspace.resolve(strict=True))).hexdigest()
    root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    return root / "voidcode" / identity


@contextmanager
def _workspace_lease(workspace: Path) -> Generator[None]:
    descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another process owns this workspace") from exc
        yield
    finally:
        os.close(descriptor)


def _deactivation_unavailable(
    owner: str, payload: Mapping[str, object], scope: StateScope, scope_key: str, tip_sequence: int | None
) -> Mapping[str, object]:
    del payload, scope, scope_key, tip_sequence
    raise ValueError(f"plugin {owner} needs deactivation but this installation no longer ships it; disable it before upgrading")


def validate_providers(config: Config, registered: Sequence[str], *, providers: Mapping[str, object]) -> None:
    names = set(registered)
    model = config.model.model
    if model is not None:
        provider_name, separator, model_name = model.partition("/")
        if not separator or not model_name or provider_name not in providers:
            raise ValueError("model must name a configured provider/model")
        if provider_name not in names:
            raise ValueError(f"model provider {provider_name!r} is not registered by any admitted plugin")
    if not config.providers:
        return
    missing = sorted(set(config.providers) - names)
    if missing:
        raise ValueError(f"provider {missing[0]!r} is not registered by any admitted plugin")


def _sandbox_env_baseline() -> dict[str, str]:
    """Minimal deliberate env baseline for confined children: login/shell basics only."""
    baseline: dict[str, str] = {}
    for key in ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TMPDIR", "TZ", "LANG", "LC_ALL"):
        value = os.environ.get(key)
        if value:
            baseline[key] = value
    return baseline


def locate_sandbox_helper() -> Path | None:
    """Resolve the native helper binary for this installation, or None when not shipped."""
    override = os.environ.get("VOIDCODE_SANDBOX_HELPER")
    if override:
        candidate = Path(override)
        return candidate if candidate.is_file() else None
    package_root = Path(__file__).resolve().parent
    suffix = "voidcode-sandbox-helper.exe" if os.name == "nt" else "voidcode-sandbox-helper"
    for candidate in (package_root / "bin" / suffix, package_root / "native" / suffix):
        if candidate.is_file():
            return candidate
    return None


def sandbox_backend_for(
    *,
    config: Config | None,
    workspace: Path,
    state_dir: Path,
    admission: Admission,
) -> SandboxBackend | None:
    """Assemble the transparent exec backend, or None when no sandbox section is configured.

    Product maps config to neutral core policy; core never sees voidcode.json. A configured
    sandbox with a missing backend refuses loudly, naming the missing piece. No silent
    degrade. Absent section means unconfined, today's behavior exactly.
    """
    if config is None or config.sandbox is None:
        return None
    helper = locate_sandbox_helper()
    if helper is None:
        raise ValueError(
            "sandbox is configured but the native helper is missing from this installation "
            "(expected voidcode/bin/voidcode-sandbox-helper). Remove the sandbox section to run unconfined."
        )
    check_helper(helper)
    workspace = workspace.resolve(strict=True)
    protected = [str(workspace / CONFIG_FILE_NAME)]
    for source in admission.plugins:
        try:
            protected.append(str(source.root.resolve(strict=True)))
        except OSError:
            protected.append(str(source.root))
    protected.append(str(helper))
    protected.append(str(state_dir / PRIVATE_DIR_NAME))
    protected.append(str(state_dir / DATABASE_FILENAME))
    protected.append(str(state_dir / "process-spool"))
    return SandboxBackend(
        policy=SandboxPolicy(
            mode=config.sandbox.mode,
            network=config.sandbox.effective_network,
            writable_roots=tuple(config.sandbox.writable_roots),
        ),
        helper_path=str(helper),
        protected_paths=tuple(dict.fromkeys(protected)),
        env_baseline=tuple(sorted(_sandbox_env_baseline().items())),
    )


def open_core(
    workspace: Path,
    *,
    state_dir: Path | None = None,
) -> Core:
    workspace = workspace.resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("workspace must be a directory")
    state_dir = state_directory(workspace) if state_dir is None else state_dir.resolve()
    if state_dir.is_relative_to(workspace):
        raise ValueError("state directory must be outside the workspace writable by file tools")
    database = state_dir / DATABASE_FILENAME

    current_revision, current_payload, recorded_schema = inspect_existing_workspace(database, str(workspace))
    fresh_first_adoption = current_revision is None and current_payload is None and recorded_schema is None and not database.exists()

    def admit_callback(
        *,
        config: Config | None = None,
        snapshot: Mapping[str, object] | None = None,
        revision_id: str | None = None,
    ) -> Admission:
        if snapshot is not None:
            cfg = config_from_snapshot(snapshot)
        elif config is not None:
            cfg = config
        else:
            cfg = load_config(workspace)
        return admit_live(
            cfg,
            workspace=workspace,
            bundled_sources=bundled_sources(),
            revision_id=revision_id,
            use_environment=snapshot is None,
        )

    if current_revision is None:
        config = load_config(workspace, allow_missing=fresh_first_adoption)
        admission = admit_callback(config=config)
        write_private_snapshot(state_dir, admission.revision_id, admission.private_snapshot)
    else:
        if current_payload is None or recorded_schema is None:
            raise ValueError(f"workspace revision {current_revision} disappeared from the database")
        snapshot = load_private_snapshot(state_dir, current_revision)
        config = config_from_snapshot(snapshot)
        admission = restore_admission(
            current_payload,
            snapshot,
            revision_id=current_revision,
            workspace=workspace,
            bundled_sources=bundled_sources(),
            config=config,
        )
    # One core-lifetime host keeps detached jobs and their stable spool across reloads; a
    # candidate/standalone runtime must create its own local host instead of sharing this one.
    process_host = ProcessHost(spool_dir=state_dir / "process-spool")
    # The recorded configuration — not the live file — decides the backend: a restart
    # rebuilds the generation it recorded, and only an explicit reload changes it.
    return Core(
        workspace=workspace,
        state_dir=state_dir,
        config=admission.config,
        plugins=admission.plugins,
        revision_id=admission.revision_id,
        revision_payload=admission.revision_payload,
        admission=admission,
        admits=admit_callback,
        sandbox_backend=sandbox_backend_for(config=config, workspace=workspace, state_dir=state_dir, admission=admission),
        process_host=process_host,
        config_filename=CONFIG_FILE_NAME,
    )


@contextmanager
def open_session_client(
    *,
    workspace: Path | None = None,
    state_dir: Path | None = None,
    server_url: str | None = None,
    token: str | None = None,
) -> Generator[SessionClient]:
    """Open one typed session client: local owned core or remote WebSocket RPC.

    Exactly one of ``workspace`` (local arm, owns its ``Core``) or
    ``server_url`` (remote arm) is required. Closing the context closes the
    client, and the local arm's core with it.
    """
    from .tui.client import CoreRpcClient
    from .tui.local_client import CoreLocalClient

    if (workspace is None) == (server_url is None):
        raise ValueError("open_session_client needs exactly one of workspace or server_url")
    if workspace is not None:
        core = open_core(workspace, state_dir=state_dir)
        try:
            yield CoreLocalClient(core, workspace=core.workspace)
        finally:
            core.close()
    else:
        assert server_url is not None
        client = CoreRpcClient(server_url, token=token)
        try:
            yield client
        finally:
            client.close()


def adopt_generation(*, workspace: Path, state_dir: Path, session_id: str | None) -> dict[str, object]:
    """Adopt this build as the workspace's new generation and return the transition report."""
    workspace = workspace.resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("workspace must be a directory")
    database = state_dir / DATABASE_FILENAME
    with _workspace_lease(workspace):
        store = SqliteStore(database)
        try:
            expected_revision = store.workspace_current(str(workspace))
            if expected_revision is None:
                raise ValueError("adoption needs an adopted workspace; run a normal command for first adoption instead")
            snapshot = load_private_snapshot(state_dir, expected_revision)
            config = config_from_snapshot(snapshot)
            plugins_sources = bundled_sources()
            admission = admit_live(config, workspace=workspace, bundled_sources=plugins_sources, revision_id=uuid4().hex, use_environment=False)

            candidate_runtime: PluginRuntime | None = None
            try:
                candidate_runtime = PluginRuntime(
                    admission.plugins,
                    workspace=workspace,
                    revision_id="adopt-target",
                    store=None,
                )
                validate_providers(config, candidate_runtime.providers, providers=config.providers)
                fact_observers = dict(candidate_runtime.observer_owners)
                session_ids = [record.session_id for record in store.sessions(workspace=str(workspace))]
                states = candidate_runtime.state_metadata()
                plan = plan_state_copies(
                    store=store,
                    workspace=str(workspace),
                    session_ids=session_ids,
                    old_states=states,
                    candidate_states=states,
                    deactivate=_deactivation_unavailable,
                )
            finally:
                if candidate_runtime is not None:
                    candidate_runtime.close()

            write_private_snapshot(state_dir, admission.revision_id, admission.private_snapshot)

            session = store.session(session_id) if session_id is not None else None
            checkpoint = store.checkpoint(session_id) if session_id is not None else None
            checkpoint_payload = None
            checkpoint_kind: CheckpointKind | None = None
            if checkpoint is not None and isinstance(checkpoint.payload, Mapping):
                checkpoint_payload = json_wire_object(checkpoint.payload)
                checkpoint_kind = checkpoint.kind
            run_identity = uuid4().hex if session is None else (session.run_id or uuid4().hex)
            events: tuple[EventDraft, ...] = ()
            status: SessionStatus | None = None
            if session is not None:
                drafts: list[EventDraft] = []
                if session.status is SessionStatus.RUNNING:
                    if checkpoint_payload is not None:
                        checkpoint_kind = CheckpointKind.INTERRUPTED
                    logical_turn = checkpoint_payload["logical_turn"] if checkpoint_payload is not None else ""
                    drafts.append(
                        EventDraft(
                            EventType.RUN_INTERRUPTED,
                            "core",
                            "core",
                            {
                                "reason": "the process that owned this run is gone; adoption recovered the session at its last committed boundary",
                                "stop_cause": "shutdown",
                                "logical_turn": logical_turn,
                                "eligible_owners": list(fact_observers.get("run_finished", ())),
                            },
                        )
                    )
                    status = SessionStatus.INTERRUPTED
                else:
                    status = session.status
                    drafts.append(
                        EventDraft(
                            EventType.ACTIVATION_COMMITTED,
                            "core",
                            "core",
                            {
                                "request_id": None,
                                "source": "adoption",
                                "from_revision_id": expected_revision,
                                "to_revision_id": admission.revision_id,
                                "deactivated_owners": list(plan.deactivated_owners),
                            },
                        )
                    )
                events = tuple(drafts)
            store.adopt_revision(
                workspace=str(workspace),
                session_id=session_id,
                expected_sequence=None if session is None else session.last_event_sequence,
                expected_run_id=None if session is None else session.run_id,
                expected_revision_id=expected_revision,
                target_revision_id=admission.revision_id,
                target_payload=admission.revision_payload,
                run_id=run_identity,
                status=status,
                events=events,
                checkpoint_kind=checkpoint_kind,
                checkpoint_payload=checkpoint_payload,
                state_updates=plan.updates,
                cancelled_owners=plan.deactivated_owners,
                cleared_budget_owners=plan.deactivated_owners,
                require_quiescent=True,
                recover_orphaned_runs=True,
                fact_observers=fact_observers,
            )
            return {
                "from_revision_id": expected_revision,
                "to_revision_id": admission.revision_id,
                "deactivated_owners": list(plan.deactivated_owners),
            }
        finally:
            store.close()
