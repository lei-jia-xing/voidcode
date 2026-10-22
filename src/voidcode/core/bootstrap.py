"""Product wiring: configuration, the per-workspace state directory, and generation adoption."""

from __future__ import annotations

import fcntl
import functools
import hashlib
import json
import os
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from importlib.metadata import version
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

from .. import __version__
from ..sdk.config import CONFIG_FILE_NAME, CoreConfig, parse_core_config
from ..sdk.json_values import json_wire_object
from .activation import (
    PRIVATE_SNAPSHOT_VERSION,
    Admission,
    admit_live,
    bundled_sources,
    config_from_snapshot,
    load_private_snapshot,
    merged_entries,
    plan_state_copies,
    resolved_providers,
    restore_admission,
    write_private_snapshot,
)
from .engine import Core
from .images import IMAGES_DIR_NAME, Image, installed_identity, running_identity
from .images import pin as image_pin
from .plugins import PluginRuntime, PluginSource
from .storage.store import SCHEMA_VERSION, EventDraft, SqliteStore, canonical_json, inspect_existing_workspace


@functools.cache
def _executing_image() -> Image:
    """The image this process executes: its pinned launch identity, or the installed tree.

    The first writer runs inside a materialized image instead (CLI routing), so what gets pinned
    is always recoverable; the plain-tree digest covers unsupervised entry points that never
    adopt by themselves.
    """
    pinned = running_identity()
    return pinned if pinned is not None else installed_identity()


def _launch_protocol() -> int:
    """The supervisor<->child launch interface this build speaks (the preflight refusal vocabulary)."""
    from .preflight import LAUNCH_PROTOCOL_VERSION

    return LAUNCH_PROTOCOL_VERSION


def state_directory(workspace: Path) -> Path:
    identity = hashlib.sha256(os.fsencode(workspace.resolve(strict=True))).hexdigest()
    root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    return root / "voidcode" / "kernel-v1" / identity


def load_config(workspace: Path, *, allow_missing: bool = False) -> CoreConfig:
    path = workspace / CONFIG_FILE_NAME
    if not path.exists():
        if (workspace / ".voidcode.json").exists():
            raise ValueError("legacy .voidcode.json is not supported; create voidcode.json explicitly (no conversion is performed)")
        if allow_missing:
            # First boot of a truly fresh state: bundled plugins already have documented empty
            # defaults, and model execution stays unavailable until a real file is written.
            return CoreConfig()
        raise FileNotFoundError(f"core configuration is required: {path}")
    if not path.is_file():
        raise ValueError(f"core configuration is not a file: {path}")
    if path.stat().st_size > 256 * 1024:
        raise ValueError("core configuration exceeds 256 KiB")
    return parse_core_config(json.loads(path.read_text(encoding="utf-8")))


def open_core(
    workspace: Path,
    *,
    state_dir: Path | None = None,
    auto_drain: bool = True,
) -> Core:
    workspace = workspace.resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("workspace must be a directory")
    state_dir = state_directory(workspace) if state_dir is None else state_dir.resolve()
    if state_dir.is_relative_to(workspace):
        raise ValueError("core state must be outside the workspace writable by file tools")
    database = state_dir / "kernel-v1.sqlite3"

    # Read-only inspection first: a fresh state directory must not write anything before
    # configuration is validated.
    current_revision, current_payload, recorded_schema = inspect_existing_workspace(database, str(workspace))
    # Empty in-memory defaults are legal ONLY for a truly absent database with no adopted
    # revision, no recorded schema, and no legacy file. Any existing DB (even without a
    # workspace row), foreign schema, or legacy file keeps the strict required-file behavior.
    fresh_first_adoption = (
        current_revision is None
        and current_payload is None
        and recorded_schema is None
        and not database.exists()
        and not (workspace / ".voidcode.json").exists()
    )

    def admit_current(*, config: CoreConfig | None = None, revision_id: str | None = None) -> Admission:
        """Re-admit a configuration as a generation of this workspace.

        ``revision_id`` names an already-pinned approval target; otherwise a fresh opaque id is
        minted. The payload is finalized with the runtime's metadata before it is published.
        """
        live = load_config(workspace) if config is None else config
        plugins = admit_live(live, workspace=workspace, bundled_sources=bundled_sources())
        return _admission_from(
            plugins,
            config=live,
            revision_id=uuid4().hex if revision_id is None else revision_id,
        )

    if current_revision is None:
        # First adoption: the live configuration becomes the active revision. Its private
        # snapshot is written before the pointer, so a crash leaves either the old state or
        # a recoverable snapshot, and the workspace is verified free before any write here.
        # A truly fresh state may start with in-memory empty defaults; the file is never
        # created, and a later reload stays strict.
        config = load_config(workspace, allow_missing=fresh_first_adoption)
        _assert_workspace_free(workspace)
        admission = admit_current(config=config)
        write_private_snapshot(state_dir, admission.revision_id, admission.private_snapshot)
    else:
        # A restarted core restores the persisted revision, its recorded configuration and
        # credentials, then imports the CURRENT plugin source at those recorded locations.
        if current_payload is None or recorded_schema is None:
            raise ValueError(f"workspace revision {current_revision} disappeared from the core database")
        if recorded_schema != SCHEMA_VERSION:
            raise ValueError(
                f"workspace state is at schema {recorded_schema}; this build reads schema {SCHEMA_VERSION}: "
                "run the explicit upgrade transition through the supervisor "
                "(`voidcode serve --session-id <id>` for the first adoption of this build, "
                "or `voidcode upgrade --candidate <install>`) instead of starting the core directly"
            )
        _check_image_pin(state_dir, current_payload)
        snapshot = load_private_snapshot(state_dir, current_revision)
        config = config_from_snapshot(snapshot)
        admission = restore_admission(
            current_payload,
            snapshot,
            revision_id=current_revision,
            workspace=workspace,
            bundled_sources=bundled_sources(),
        )
    return Core(
        workspace=workspace,
        state_dir=state_dir,
        config=config,
        plugins=admission.plugins,
        revision_id=admission.revision_id,
        revision_payload=admission.revision_payload,
        admission=admission,
        admits=admit_current,
        auto_drain=auto_drain,
    )


def _check_image_pin(state_dir: Path, payload: Mapping[str, object]) -> None:
    """Refuse to serve state another image was elected for, with the recovery spelled out."""
    recorded = payload.get("image")
    if not isinstance(recorded, Mapping):
        raise ValueError("this state records no elected image; it predates this build's generation shape")
    digest = recorded.get("digest")
    if not isinstance(digest, str) or not digest:
        raise ValueError("this state records no elected image digest; it predates this build's generation shape")
    if digest != _executing_image().digest:
        raise ValueError(
            f"this state was elected to image {digest}; start that image "
            f"({state_dir / IMAGES_DIR_NAME / digest}) instead of {_executing_image().digest}"
        )


def adopt_generation(*, workspace: Path, state_dir: Path, session_id: str | None) -> dict[str, object]:
    """Adopt this build as the workspace's new generation and return the transition report.

    Every enabled plugin is re-admitted from this installation with the recorded configuration;
    plugin state copies are proven by a throwaway runtime, and then one transaction moves the schema,
    the pointer, every session ref, and every state copy. An adoption needs an acting session
    whenever the workspace has a session; it never reads the live configuration.

    The exclusive workspace lease is held for the whole transaction: a live core makes this
    refuse, so a ``running`` session found here can only belong to a core that is gone. Such
    orphans are settled as interrupted at their last committed boundary (cursor untouched) and
    stay explicitly resumable; nothing is replayed or reported as a confirmed stop.
    """
    workspace = workspace.resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("workspace must be a directory")
    database = state_dir / "kernel-v1.sqlite3"
    with _workspace_lease(workspace):
        store = SqliteStore(database)
        try:
            expected_revision = store.workspace_current(str(workspace))
            if expected_revision is None:
                raise ValueError("adoption needs an adopted workspace; run a normal command for first adoption instead")
            snapshot = load_private_snapshot(state_dir, expected_revision)
            config = config_from_snapshot(snapshot)
            plugins = admit_live(config, workspace=workspace, bundled_sources=bundled_sources())
            admission = _admission_from(plugins, config=config, revision_id=uuid4().hex)

            candidate_runtime: PluginRuntime | None = None
            try:
                candidate_runtime = PluginRuntime(
                    admission.plugins,
                    workspace=workspace,
                    revision_id="adopt-target",
                    store=None,
                    builtin_providers=resolved_providers(admission.config),
                )
                # Frozen BEFORE the transaction: a recovery fact must carry the same durable
                # eligibility the engine stamps on its own facts, never a later roster.
                fact_observers = dict(candidate_runtime.observer_owners)
                session_ids = [record.session_id for record in store.sessions(workspace=str(workspace))]
                states = candidate_runtime.state_metadata()
                plan = plan_state_copies(
                    store=store,
                    workspace=str(workspace),
                    session_ids=session_ids,
                    old_states=states,
                    candidate_states=states,
                    migrate=lambda owner, payload, from_version, to_version: candidate_runtime.migrate_owner_state(
                        owner, payload, from_version=from_version, to_version=to_version
                    ),
                    # Adoption keeps the recorded configuration, so no owner loses its deactivation
                    # transform here. A bundled plugin removed from this installation must be disabled
                    # in the running old core first, where its real deactivation transform still exists.
                    deactivate=_deactivation_unavailable,
                )
            finally:
                if candidate_runtime is not None:
                    candidate_runtime.close()

            write_private_snapshot(state_dir, admission.revision_id, admission.private_snapshot)

            session = store.session(session_id) if session_id is not None else None
            checkpoint = store.checkpoint(session_id) if session_id is not None else None
            checkpoint_payload = None
            checkpoint_kind: Literal["ready", "approval_wait", "terminal", "interrupted"] | None = None
            if checkpoint is not None and isinstance(checkpoint.payload, Mapping):
                checkpoint_payload = json_wire_object(checkpoint.payload)
                # The checkpoints CHECK constraint admits exactly these four kinds.
                checkpoint_kind = cast("Literal['ready', 'approval_wait', 'terminal', 'interrupted']", checkpoint.kind)
            run_identity = uuid4().hex if session is None else (session.run_id or uuid4().hex)
            events: tuple[EventDraft, ...] = ()
            status: str | None = None
            if session is not None:
                drafts: list[EventDraft] = []
                if session.status == "running":
                    # Under the held lease this row cannot have a live owner: adopt it as
                    # interrupted with a durable fact, keeping its cursor for `resume`.
                    if checkpoint_payload is not None:
                        checkpoint_kind = "interrupted"
                    logical_turn = checkpoint_payload.get("logical_turn", "") if checkpoint_payload is not None else ""
                    drafts.append(
                        EventDraft(
                            "kernel.run_interrupted",
                            "core",
                            "core",
                            {
                                "reason": "the core that owned this run is gone; adoption recovered the session at its last committed boundary",
                                "stop_cause": "shutdown",
                                "logical_turn": logical_turn if isinstance(logical_turn, str) else "",
                                "eligible_owners": list(fact_observers.get("run_finished", ())),
                            },
                        )
                    )
                    status = "interrupted"
                else:
                    status = session.status
                drafts.append(
                    EventDraft(
                        "kernel.revision_adopted",
                        "core",
                        "core",
                        {
                            "request_id": None,
                            "source": "adoption",
                            "from_revision_id": expected_revision,
                            "to_revision_id": admission.revision_id,
                            "image_digest": _executing_image().digest,
                            "migrated_owners": list(plan.migrated_owners),
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
                "migrated_owners": list(plan.migrated_owners),
                "deactivated_owners": list(plan.deactivated_owners),
            }
        finally:
            store.close()


def _deactivation_unavailable(
    owner: str, payload: Mapping[str, object], scope: str, scope_key: str, tip_sequence: int | None
) -> Mapping[str, object]:
    del payload, scope, scope_key, tip_sequence
    raise ValueError(f"plugin {owner} needs deactivation but this installation no longer ships it; disable it before upgrading")


@contextmanager
def _workspace_lease(workspace: Path) -> Iterator[None]:
    """Hold the workspace lease, proving no core owns it for the duration of the block.

    The Core takes the same exclusive lease from its directory inode, so a successful flock is
    the existing ownership proof: a live core (same or another process) makes this refuse.
    """
    descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another core owns this workspace") from exc
        yield
    finally:
        os.close(descriptor)


def _assert_workspace_free(workspace: Path) -> None:
    """Verify no other core owns the workspace before this process writes anything for it."""
    with _workspace_lease(workspace):
        pass


def _admission_from(plugins: tuple[PluginSource, ...], *, config: CoreConfig, revision_id: str) -> Admission:
    """Assemble the public revision payload and the private snapshot for one admitted generation."""
    entries = merged_entries(config, bundled_sources())
    snapshot_plugins: dict[str, object] = {}
    for plugin_id, entry in entries.items():
        snapshot_plugins[plugin_id] = {
            "enabled": entry.enabled,
            "path": entry.path,
            "config": dict(entry.config),
        }
    snapshot: dict[str, object] = {
        "version": PRIVATE_SNAPSHOT_VERSION,
        "providers": resolved_providers(config),
        "model": {"model": config.model.model, "reasoning_effort": config.model.reasoning_effort},
        "policy": {"approval_mode": config.policy.approval_mode, "read_only": config.policy.read_only},
        "plugins": snapshot_plugins,
    }
    # Composition only: ids and the private snapshot digest. Credentials and arbitrary plugin
    # configuration never reach events or revisions.
    payload: dict[str, object] = {
        "image": image_pin(_executing_image(), core_version=__version__, launch_protocol=_launch_protocol()),
        "python": sys.implementation.cache_tag,
        "dependencies": {name: version(name) for name in ("openai", "pydantic", "jsonschema")},
        "model": {"model": config.model.model, "reasoning_effort": config.model.reasoning_effort},
        "policy": {"approval_mode": config.policy.approval_mode, "read_only": config.policy.read_only},
        "plugins": sorted(plugin.plugin_id for plugin in plugins),
        # Digest only: the credential-bearing configuration stays in the private snapshot.
        "private_config_sha256": hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest(),
    }
    return Admission(
        revision_id=revision_id,
        plugins=plugins,
        revision_payload=payload,
        private_snapshot=snapshot,
        config=config,
    )
