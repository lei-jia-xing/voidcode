"""Independent runtime acceptance and crash harness for P2 kernel governance.

Proves real boundaries across actual subprocess CLI, actual listening HTTP,
listening loopback provider, isolated plugin host process, and SQLite storage.
No fake services, no mocked forwards, no stubbed counts.

Run only against a frozen tree; targeted postfix proofs:
  uv run pytest tests/integration/test_kernel_governance.py -k provider_survives
  uv run pytest tests/integration/test_kernel_governance.py -k budget_window
  uv run pytest tests/integration/test_kernel_governance.py -k durable_claim
  uv run pytest tests/integration/test_kernel_governance.py -k observer_receipt_survives
  uv run pytest tests/integration/test_kernel_governance.py -k confirmed_effect_crash
  uv run pytest tests/integration/test_kernel_governance.py -k new_user_turn_after
  uv run pytest tests/integration/test_kernel_governance.py -k crash_after_turn_append
  uv run pytest tests/integration/test_kernel_governance.py -k crash_after_approval_append
  uv run pytest tests/integration/test_kernel_governance.py -k keyed_leases
  uv run pytest tests/integration/test_kernel_governance.py -k cancelled_finite_root
  uv run pytest tests/integration/test_kernel_governance.py -k terminal_interruption
  uv run pytest tests/integration/test_kernel_governance.py -k two_owner_partial
  uv run pytest tests/integration/test_kernel_governance.py -k owner_cancel
  uv run pytest tests/integration/test_kernel_governance.py -k settled_before_the_next
  uv run pytest tests/integration/test_kernel_governance.py -k unknown_elapsed
  uv run pytest tests/integration/test_kernel_governance.py -k root_action_budget_shares
  uv run pytest tests/integration/test_kernel_governance.py    # full harness
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest
import uvicorn

from tests.provider_loopback import OpenAILoopbackServer, _OpenAILoopbackHandler, openai_response
from voidcode.kernel.storage.store import SqliteStore
from voidcode.kernel.transport.http import create_kernel_app
from voidcode.sdk.manifest import (
    PluginCapabilityRequest,
    PluginCommandDeclaration,
    PluginFileIdentity,
    PluginManifest,
    PluginToolDeclaration,
)
from voidcode.sdk.tools import ToolEffect

_GOV_PLUGIN_ID = "gov-plugin"
_OBSERVER_PLUGIN_ID = "gov-observer2"


def _governance_plugin_source(root: Path) -> None:
    """Fixture plugin: implements tools and commands exercising the 6 acceptance criteria."""
    root.mkdir(parents=True, exist_ok=True)
    code = '''"""Governance test fixture plugin."""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path

from voidcode.sdk.commands import CommandDefinition, CommandResult, ConfirmationRequest
from voidcode.sdk.hooks import ContextContribution, InvocationFinishedFact, RunFinishedFact, UsageFact
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.tools import TextOutput, ToolDefinition, ToolEffect, ToolFailure, ToolSuccess


class GovMutateTool:
    def __init__(self, definition: ToolDefinition) -> None:
        self.definition = definition

    def invoke(self, call, *, context, services):
        workspace = context.workspace
        marker_name = call.arguments.get("marker", "marker.txt")
        marker = workspace / marker_name
        with marker.open("a", encoding="utf-8") as stream:
            stream.write("mutated\\n")
            stream.flush()
            os.fsync(stream.fileno())

        (workspace / "host.pid").write_text(str(os.getpid()), encoding="utf-8")

        if call.arguments.get("block"):
            release_marker = workspace / "release.txt"
            while not release_marker.exists():
                if context.abort_signal is not None and context.abort_signal.cancelled:
                    return ToolFailure(call.tool_name, "tool aborted cooperatively")
                time.sleep(0.02)

        return ToolSuccess(call.tool_name, TextOutput("mutation succeeded"))


class GovFanoutTool:
    def __init__(self, definition: ToolDefinition) -> None:
        self.definition = definition

    def invoke(self, call, *, context, services):
        for index in (1, 2):
            services.enqueue_continuation({"action": "fanout_child", "index": index})
        return ToolSuccess(call.tool_name, TextOutput("fanout queued"))


class GovSelfCancelTool:
    """Native tool: stage cancellation of this owner's own durable work (the claim it runs in)."""

    def __init__(self, definition: ToolDefinition) -> None:
        self.definition = definition

    def invoke(self, call, *, context, services):
        raw = call.arguments.get("continuation_id")
        services.cancel_continuations(continuation_id=str(raw) if raw else None)
        return ToolSuccess(call.tool_name, TextOutput("own cancellation staged"))


class GovReadTool:
    def __init__(self, definition: ToolDefinition) -> None:
        self.definition = definition

    def invoke(self, call, *, context, services):
        path = context.workspace / call.arguments["path"]
        content = path.read_text(encoding="utf-8") if path.is_file() else "missing"
        # Attempt an illegal mutation from a read-only tool lease
        attempt_mutation = call.arguments.get("attempt_mutation", False)
        if attempt_mutation:
            try:
                services.replace_state({"forbidden": True}, expected_version=0, scope="session")
            except Exception as exc:
                content += f";mutation_rejected={type(exc).__name__}"
        return ToolSuccess(call.tool_name, TextOutput(content))


class GovWriteCommand:
    definition = CommandDefinition(
        name="gov_write_cmd",
        description="Non-gated write command.",
        argument_schema={
            "type": "object",
            "properties": {
                "value": {"type": "string"},
                "owner": {"type": "string"},
                "actor": {"type": "string"},
                "scope": {"type": "string"},
            },
        },
        effects=frozenset({ToolEffect.WRITE}),
        capabilities=frozenset({"workspace.write"}),
        busy_gate=False,
    )

    def invoke(self, invocation, services):
        scope = invocation.arguments.get("scope", "session")
        value = invocation.arguments.get("value", "default_val")
        state, version = services.get_state(scope=scope)
        merged = dict(state or {})
        merged["value"] = value
        merged["invoked_actor"] = invocation.context.actor
        services.replace_state(merged, expected_version=version, scope=scope)
        services.stage_event("gov_write_applied", {"value": value})
        return CommandResult(
            status="ok",
            output=f"Stored: {value}",
            data={"value": value, "actor": invocation.context.actor},
        )


class GovBudgetCommand:
    definition = CommandDefinition(
        name="gov_budget_cmd",
        description="Set or clear owner budget.",
        argument_schema={
            "type": "object",
            "properties": {
                "since_sequence": {"type": "integer"},
                "max_total_tokens": {"type": "integer"},
                "max_elapsed_seconds": {"type": "number"},
                "clear": {"type": "boolean"},
            },
        },
        effects=frozenset({ToolEffect.WRITE}),
        capabilities=frozenset({"workspace.write"}),
        busy_gate=False,
    )

    def invoke(self, invocation, services):
        if invocation.arguments.get("clear"):
            services.clear_budget()
            return CommandResult(status="ok", output="Budget cleared")
        since_seq = int(invocation.arguments.get("since_sequence", 0))
        max_tot_val = invocation.arguments.get("max_total_tokens")
        max_tot = int(max_tot_val) if max_tot_val is not None else None
        max_elapsed_val = invocation.arguments.get("max_elapsed_seconds")
        max_elapsed = float(max_elapsed_val) if max_elapsed_val is not None else None
        services.set_budget(since_sequence=since_seq, max_total_tokens=max_tot, max_elapsed_seconds=max_elapsed)
        return CommandResult(status="ok", output=f"Budget set: since={since_seq}, max_total={max_tot}, max_elapsed={max_elapsed}")


class GovKeyedQueueCommand:
    definition = CommandDefinition(
        name="gov_keyed_queue_cmd",
        description="Enqueue one keyed durable continuation, then optionally hold before returning.",
        argument_schema={
            "type": "object",
            "properties": {"key": {"type": "string"}, "tag": {"type": "string"}, "block": {"type": "boolean"}},
            "required": ["key", "tag"],
        },
        effects=frozenset({ToolEffect.WRITE}),
        capabilities=frozenset({"workspace.write"}),
        busy_gate=False,
    )

    def invoke(self, invocation, services):
        key = str(invocation.arguments["key"])
        tag = str(invocation.arguments["tag"])
        continuation_id = services.enqueue_continuation({"action": "keyed_followup", "key": key}, idempotency_key=key)
        # Real concurrent leases: both stage their enqueue, then hold until the
        # test releases them, so each lease commits only after both have staged.
        if invocation.arguments.get("block"):
            marker = invocation.context.workspace / f"keyed.{tag}.entered"
            marker.write_text(continuation_id, encoding="utf-8")
            release = invocation.context.workspace / f"keyed.{tag}.release"
            while not release.exists():
                time.sleep(0.02)
        return CommandResult(status="ok", output=f"Keyed: {continuation_id}", data={"continuation_id": continuation_id})


class GovConfirmCancelCommand:
    """Confirm-gated cancellation of this owner's durable work (routed like an invoke)."""

    definition = CommandDefinition(
        name="gov_confirm_cancel_cmd",
        description="Confirm-gated owner cancellation of this owner's durable work.",
        argument_schema={"type": "object"},
        effects=frozenset({ToolEffect.WRITE, ToolEffect.SESSION}),
        capabilities=frozenset({"workspace.write"}),
        busy_gate=False,
    )

    def invoke(self, invocation, services):
        if invocation.context.actor != "user":
            request_id = f"req-{invocation.context.invocation_id}"
            state, version = services.get_state(scope="session")
            return CommandResult(
                status="pending_confirmation",
                confirmation=ConfirmationRequest(
                    request_id=request_id,
                    owner="gov-plugin",
                    command_name="gov_confirm_cancel_cmd",
                    arguments=invocation.arguments,
                    ui_type="confirmation",
                    ui_data={"request_id": request_id, "message": "Cancel this owner's durable work?"},
                    expected_state_version=version,
                ),
            )
        services.cancel_continuations()
        return CommandResult(status="ok", output="owner durable work cancelled after confirmation")


class GovQueueCommand:
    definition = CommandDefinition(
        name="gov_queue_cmd",
        description="Enqueue a durable continuation for idle follow-up work.",
        argument_schema={
            "type": "object",
            "properties": {"note": {"type": "string"}},
        },
        effects=frozenset({ToolEffect.WRITE}),
        capabilities=frozenset({"workspace.write"}),
        busy_gate=False,
    )

    def invoke(self, invocation, services):
        note = str(invocation.arguments.get("note", "queued"))
        continuation_id = services.enqueue_continuation({"action": "queued_followup", "note": note})
        return CommandResult(status="ok", output=f"Queued: {continuation_id}", data={"continuation_id": continuation_id})


class GovCancelThenQueueCommand:
    definition = CommandDefinition(
        name="gov_cancel_then_queue_cmd",
        description="Cancel all owner continuations, then enqueue one.",
        argument_schema={"type": "object"},
        effects=frozenset({ToolEffect.WRITE}),
        capabilities=frozenset({"workspace.write"}),
        busy_gate=False,
    )

    def invoke(self, invocation, services):
        services.cancel_continuations()
        kept = services.enqueue_continuation({"action": "kept_after_cancel"})
        return CommandResult(status="ok", output=f"Kept: {kept}", data={"continuation_id": kept})


class GovQueueThenCancelCommand:
    definition = CommandDefinition(
        name="gov_queue_then_cancel_cmd",
        description="Enqueue one continuation and cancel exactly that one.",
        argument_schema={"type": "object"},
        effects=frozenset({ToolEffect.WRITE}),
        capabilities=frozenset({"workspace.write"}),
        busy_gate=False,
    )

    def invoke(self, invocation, services):
        cancelled = services.enqueue_continuation({"action": "cancelled_after_queue"})
        services.cancel_continuations(continuation_id=cancelled)
        return CommandResult(status="ok", output=f"Cancelled: {cancelled}", data={"continuation_id": cancelled})


class GovOwnerCancelCommand:
    definition = CommandDefinition(
        name="gov_owner_cancel_cmd",
        description="Cancel this owner's durable continuations.",
        argument_schema={
            "type": "object",
            "properties": {"continuation_id": {"type": "string"}},
        },
        effects=frozenset({ToolEffect.WRITE, ToolEffect.SESSION}),
        capabilities=frozenset({"workspace.write"}),
        busy_gate=False,
    )

    def invoke(self, invocation, services):
        raw = invocation.arguments.get("continuation_id")
        services.cancel_continuations(continuation_id=str(raw) if raw else None)
        return CommandResult(status="ok", output="owner durable work cancelled")


class GovGatedCommand:
    definition = CommandDefinition(
        name="gov_gated_cmd",
        description="Gated action.",
        argument_schema={"type": "object"},
        effects=frozenset({ToolEffect.WRITE}),
        capabilities=frozenset({"workspace.write"}),
        busy_gate=True,
    )

    def invoke(self, invocation, services):
        return CommandResult(status="ok", output="Executed gated action")


class GovReadCommand:
    definition = CommandDefinition(
        name="gov_read_cmd",
        description="Read command verifying lease denial.",
        argument_schema={
            "type": "object",
            "properties": {
                "test_mutation": {"type": "boolean"},
            },
        },
        effects=frozenset({ToolEffect.READ}),
        capabilities=frozenset({"workspace.read"}),
        busy_gate=False,
    )

    def invoke(self, invocation, services):
        state, version = services.get_state(scope="session")
        rejections: list[str] = []

        if invocation.arguments.get("test_mutation"):
            try:
                services.replace_state({"illegal": True}, expected_version=version, scope="session")
            except Exception as exc:
                rejections.append(f"state:{type(exc).__name__}")

            try:
                services.stage_event("illegal_event", {"bad": 1})
            except Exception as exc:
                rejections.append(f"event:{type(exc).__name__}")

            try:
                services.enqueue_continuation({"illegal": 1})
            except Exception as exc:
                rejections.append(f"continuation:{type(exc).__name__}")

            try:
                services.set_budget(since_sequence=0, max_total_tokens=100)
            except Exception as exc:
                rejections.append(f"budget:{type(exc).__name__}")

            try:
                services.clear_budget()
            except Exception as exc:
                rejections.append(f"clear_budget:{type(exc).__name__}")

        return CommandResult(
            status="ok",
            output=f"Read command executed; rejections={len(rejections)}",
            data={"rejections": rejections, "version": version, "state": state or {}},
        )


class GovConfirmCommand:
    definition = CommandDefinition(
        name="gov_confirm_cmd",
        description="Command with pending confirmation.",
        argument_schema={
            "type": "object",
            "properties": {
                "target_file": {"type": "string"},
            },
            "required": ["target_file"],
        },
        effects=frozenset({ToolEffect.WRITE}),
        capabilities=frozenset({"workspace.write"}),
        busy_gate=False,
    )

    def invoke(self, invocation, services):
        target_name = str(invocation.arguments.get("target_file", "confirmed.txt"))
        marker = invocation.context.workspace / target_name

        state, version = services.get_state(scope="session")
        if (state or {}).get("confirmed_done"):
            return CommandResult(status="ok", output="Already confirmed and executed")

        # Only a kernel-owned confirmation resolution invocation carries actor
        # "user"; an ordinary client invocation is actor "client".
        if invocation.context.actor != "user" and not marker.exists():
            request_id = f"req-{invocation.context.invocation_id}"
            return CommandResult(
                status="pending_confirmation",
                confirmation=ConfirmationRequest(
                    request_id=request_id,
                    owner="gov-plugin",
                    command_name="gov_confirm_cmd",
                    arguments=invocation.arguments,
                    ui_type="confirmation",
                    ui_data={"request_id": request_id, "message": f"Write to {target_name}?"},
                    expected_state_version=version,
                ),
            )

        with marker.open("a", encoding="utf-8") as stream:
            stream.write("confirmed_effect\\n")
            stream.flush()
            os.fsync(stream.fileno())

        # Fault injection for the confirmation-crash proof: the real effect is
        # durable on disk, but the handler never returns to commit its result.
        block = invocation.context.workspace / "confirm.block"
        if block.exists():
            (invocation.context.workspace / "confirm.paused").touch()
            release = invocation.context.workspace / "confirm.release"
            while not release.exists():
                time.sleep(0.02)

        merged = dict(state or {})
        merged["confirmed_done"] = True
        services.replace_state(merged, expected_version=version, scope="session")
        return CommandResult(status="ok", output=f"Confirmed and executed: {target_name}")


def gov_context(ctx, services):
    workspace = ctx.workspace
    if (workspace / "context.fail").exists():
        raise RuntimeError("context contributor exploded")
    block = workspace / "context.block"
    if block.exists():
        (workspace / "context.entered").touch()
        release = workspace / "context.release"
        while not release.exists():
            time.sleep(0.02)
    state, _ = services.get_state("session")
    val = (state or {}).get("value", "initial")
    return ContextContribution(content=f"GOV_STATE={val}")


def gov_observer(fact, ctx, services):
    if isinstance(fact, RunFinishedFact):
        state, version = services.get_state("session")
        merged = dict(state or {})
        merged["run_finished_seen"] = int(merged.get("run_finished_seen", 0)) + 1
        merged["last_run_status"] = fact.status
        merged["last_run_stop_cause"] = fact.stop_cause
        merged["last_run_elapsed_seconds"] = fact.elapsed_seconds
        merged["last_run_event_sequence"] = fact.event_sequence
        services.replace_state(merged, expected_version=version, scope="session")
    if isinstance(fact, InvocationFinishedFact):
        state, version = services.get_state("session")
        seen = int((state or {}).get("invocations_seen", 0))
        merged = dict(state or {})
        merged["invocations_seen"] = seen + 1
        services.replace_state(merged, expected_version=version, scope="session")
    if isinstance(fact, UsageFact):
        workspace = ctx.workspace
        state, version = services.get_state("session")
        seen = int((state or {}).get("usage_facts_seen", 0))
        merged = dict(state or {})
        merged["usage_facts_seen"] = seen + 1
        services.replace_state(merged, expected_version=version, scope="session")
        # Fault injection for the receipt-crash proof: signal entry, then pause
        # BEFORE returning so the staged state is never committed.
        block = workspace / "observer.block"
        if block.exists():
            (workspace / "observer.entered").write_text(str(seen), encoding="utf-8")
            release = workspace / "observer.release"
            while not release.exists():
                time.sleep(0.02)
        # Budget injection: an observer that caps its owner on the fact it just
        # observed; the admission check that follows must already see it.
        if (workspace / "observer.zerobudget").exists():
            services.set_budget(since_sequence=0, max_total_tokens=0)
        # Crash injection for the checkpoint-reference proof: kill the KERNEL
        # process (this plugin lives in the host subprocess) the instant its
        # callback runs, which is exactly after the source commit it observes.
        if (workspace / "observer.kill").exists():
            os.kill(os.getppid(), signal.SIGKILL)


def create_plugin(config):
    manifest_data = json.loads((Path(__file__).with_name("voidcode-plugin.json")).read_text(encoding="utf-8"))
    mutate_decl = next(t for t in manifest_data["tools"] if t["name"] == "gov_mutate")
    read_decl = next(t for t in manifest_data["tools"] if t["name"] == "gov_read")

    mutate_tool = GovMutateTool(
        ToolDefinition(
            name=mutate_decl["name"],
            description=mutate_decl["description"],
            input_schema=mutate_decl["input_schema"],
            effects=frozenset({ToolEffect.WRITE}),
            replay_policy="never",
        )
    )
    read_tool = GovReadTool(
        ToolDefinition(
            name=read_decl["name"],
            description=read_decl["description"],
            input_schema=read_decl["input_schema"],
            effects=frozenset({ToolEffect.READ}),
            replay_policy="safe",
        )
    )

    fanout_decl = next(t for t in manifest_data["tools"] if t["name"] == "gov_fanout")
    fanout_tool = GovFanoutTool(
        ToolDefinition(
            name=fanout_decl["name"],
            description=fanout_decl["description"],
            input_schema=fanout_decl["input_schema"],
            effects=frozenset({ToolEffect.WRITE}),
            replay_policy="never",
        )
    )

    self_cancel_decl = next(t for t in manifest_data["tools"] if t["name"] == "gov_self_cancel")
    self_cancel_tool = GovSelfCancelTool(
        ToolDefinition(
            name=self_cancel_decl["name"],
            description=self_cancel_decl["description"],
            input_schema=self_cancel_decl["input_schema"],
            effects=frozenset({ToolEffect.WRITE}),
            replay_policy="never",
        )
    )

    return Plugin(
        tools=(mutate_tool, read_tool, fanout_tool, self_cancel_tool),
        commands=(
            GovWriteCommand(),
            GovBudgetCommand(),
            GovKeyedQueueCommand(),
            GovQueueCommand(),
            GovCancelThenQueueCommand(),
            GovQueueThenCancelCommand(),
            GovOwnerCancelCommand(),
            GovGatedCommand(),
            GovReadCommand(),
            GovConfirmCommand(),
            GovConfirmCancelCommand(),
        ),
        context=gov_context,
        observer=gov_observer,
    )
'''
    plugin_path = root / "gov_plugin.py"
    plugin_path.write_text(code, encoding="utf-8")
    content_hash = hashlib.sha256(plugin_path.read_bytes()).hexdigest()

    manifest = PluginManifest(
        plugin_id=_GOV_PLUGIN_ID,
        version="1.0.0",
        entrypoint="gov_plugin:create_plugin",
        files=(PluginFileIdentity(path="gov_plugin.py", sha256=content_hash),),
        tools=(
            PluginToolDeclaration(
                name="gov_mutate",
                description="Mutating tool with never replay policy and optional block.",
                input_schema={
                    "type": "object",
                    "properties": {"marker": {"type": "string"}, "block": {"type": "boolean"}},
                    "additionalProperties": False,
                },
                effects=(ToolEffect.WRITE,),
                replay_policy="never",
                capabilities=("workspace.write",),
            ),
            PluginToolDeclaration(
                name="gov_fanout",
                description="Queue two durable follow-up continuations.",
                input_schema={"type": "object", "additionalProperties": False},
                effects=(ToolEffect.WRITE,),
                replay_policy="never",
                capabilities=("workspace.write",),
            ),
            PluginToolDeclaration(
                name="gov_self_cancel",
                description="Stage cancellation of this owner's own durable work.",
                input_schema={
                    "type": "object",
                    "properties": {"continuation_id": {"type": "string"}},
                    "additionalProperties": False,
                },
                effects=(ToolEffect.WRITE,),
                replay_policy="never",
                capabilities=("workspace.write",),
            ),
            PluginToolDeclaration(
                name="gov_read",
                description="Safe read tool.",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "attempt_mutation": {"type": "boolean"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
                effects=(ToolEffect.READ,),
                replay_policy="safe",
                capabilities=("workspace.read",),
            ),
        ),
        commands=(
            PluginCommandDeclaration(
                name="gov_write_cmd",
                description="Non-gated write command.",
                argument_schema={
                    "type": "object",
                    "properties": {
                        "value": {"type": "string"},
                        "owner": {"type": "string"},
                        "actor": {"type": "string"},
                        "scope": {"type": "string"},
                    },
                },
                effects=(ToolEffect.WRITE,),
                capabilities=("workspace.write",),
                busy_gate=False,
            ),
            PluginCommandDeclaration(
                name="gov_budget_cmd",
                description="Set or clear owner budget.",
                argument_schema={
                    "type": "object",
                    "properties": {
                        "since_sequence": {"type": "integer"},
                        "max_total_tokens": {"type": "integer"},
                        "max_elapsed_seconds": {"type": "number"},
                        "clear": {"type": "boolean"},
                    },
                },
                effects=(ToolEffect.WRITE,),
                capabilities=("workspace.write",),
                busy_gate=False,
            ),
            PluginCommandDeclaration(
                name="gov_queue_cmd",
                description="Enqueue a durable continuation for idle follow-up work.",
                argument_schema={
                    "type": "object",
                    "properties": {"note": {"type": "string"}},
                },
                effects=(ToolEffect.WRITE,),
                capabilities=("workspace.write",),
                busy_gate=False,
            ),
            PluginCommandDeclaration(
                name="gov_keyed_queue_cmd",
                description="Enqueue one keyed durable continuation, then optionally hold before returning.",
                argument_schema={
                    "type": "object",
                    "properties": {"key": {"type": "string"}, "tag": {"type": "string"}, "block": {"type": "boolean"}},
                    "required": ["key", "tag"],
                },
                effects=(ToolEffect.WRITE,),
                capabilities=("workspace.write",),
                busy_gate=False,
            ),
            PluginCommandDeclaration(
                name="gov_cancel_then_queue_cmd",
                description="Cancel all owner continuations, then enqueue one.",
                argument_schema={"type": "object"},
                effects=(ToolEffect.WRITE,),
                capabilities=("workspace.write",),
                busy_gate=False,
            ),
            PluginCommandDeclaration(
                name="gov_queue_then_cancel_cmd",
                description="Enqueue one continuation and cancel exactly that one.",
                argument_schema={"type": "object"},
                effects=(ToolEffect.WRITE,),
                capabilities=("workspace.write",),
                busy_gate=False,
            ),
            PluginCommandDeclaration(
                name="gov_owner_cancel_cmd",
                description="Cancel this owner's durable continuations.",
                argument_schema={
                    "type": "object",
                    "properties": {"continuation_id": {"type": "string"}},
                },
                effects=(ToolEffect.WRITE, ToolEffect.SESSION),
                capabilities=("workspace.write",),
                busy_gate=False,
            ),
            PluginCommandDeclaration(
                name="gov_confirm_cancel_cmd",
                description="Confirm-gated owner cancellation of this owner's durable work.",
                argument_schema={"type": "object"},
                effects=(ToolEffect.WRITE, ToolEffect.SESSION),
                capabilities=("workspace.write",),
                busy_gate=False,
            ),
            PluginCommandDeclaration(
                name="gov_gated_cmd",
                description="Gated action.",
                argument_schema={"type": "object"},
                effects=(ToolEffect.WRITE,),
                capabilities=("workspace.write",),
                busy_gate=True,
            ),
            PluginCommandDeclaration(
                name="gov_read_cmd",
                description="Read command verifying lease denial.",
                argument_schema={
                    "type": "object",
                    "properties": {"test_mutation": {"type": "boolean"}},
                },
                effects=(ToolEffect.READ,),
                capabilities=("workspace.read",),
                busy_gate=False,
            ),
            PluginCommandDeclaration(
                name="gov_confirm_cmd",
                description="Command with pending confirmation.",
                argument_schema={
                    "type": "object",
                    "properties": {"target_file": {"type": "string"}},
                    "required": ["target_file"],
                },
                effects=(ToolEffect.WRITE,),
                capabilities=("workspace.write",),
                busy_gate=False,
            ),
        ),
        capabilities=(
            PluginCapabilityRequest(name="workspace.read", reason="read files"),
            PluginCapabilityRequest(name="workspace.write", reason="mutate files"),
        ),
        context=True,
        observed_facts=("usage", "invocation_finished", "run_finished"),
    )
    (root / "voidcode-plugin.json").write_text(json.dumps(manifest.as_payload()), encoding="utf-8")


def _observer_plugin_source(root: Path) -> None:
    """Second real observing plugin: no tools/commands, records the exact fact identity it receives."""
    root.mkdir(parents=True, exist_ok=True)
    code = '''"""Second governance observer fixture plugin."""

from __future__ import annotations

import json
from pathlib import Path

from voidcode.sdk.commands import CommandDefinition, CommandResult
from voidcode.sdk.hooks import UsageFact
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.tools import TextOutput, ToolDefinition, ToolEffect, ToolSuccess


def obs2_observer(fact, ctx, services):
    if not isinstance(fact, UsageFact):
        return
    state, version = services.get_state("session")
    merged = dict(state or {})
    merged["usage_facts_seen"] = int(merged.get("usage_facts_seen", 0)) + 1
    merged["last_event_sequence"] = fact.event_sequence
    merged["last_dispatch_sequence"] = fact.dispatch_sequence
    merged["last_session_id"] = fact.session_id
    merged["last_run_id"] = fact.run_id
    merged["last_revision_id"] = fact.revision_id
    merged["last_logical_turn"] = fact.logical_turn
    merged["last_elapsed_seconds"] = fact.elapsed_seconds
    services.replace_state(merged, expected_version=version, scope="session")
    services.stage_event("obs2_fact_seen", {"event_sequence": fact.event_sequence})


class Obs2ForeignCancelTool:
    """Native tool: attempt to cancel another plugin's continuation id from a real run batch."""

    def __init__(self, definition) -> None:
        self.definition = definition

    def invoke(self, call, *, context, services):
        raw = call.arguments.get("continuation_id")
        services.cancel_continuations(continuation_id=str(raw) if raw else None)
        return ToolSuccess(call.tool_name, TextOutput("foreign cancellation staged"))


class Obs2ForeignCancelCommand:
    """A DIFFERENT owner names somebody else's continuation id: it may cancel only its own rows."""

    definition = CommandDefinition(
        name="obs2_cancel_cmd",
        description="Attempt to cancel a continuation id owned by another plugin.",
        argument_schema={
            "type": "object",
            "properties": {"continuation_id": {"type": "string"}},
            "required": ["continuation_id"],
        },
        effects=frozenset({ToolEffect.WRITE}),
        capabilities=frozenset({"workspace.write"}),
        busy_gate=False,
    )

    def invoke(self, invocation, services):
        services.cancel_continuations(continuation_id=str(invocation.arguments["continuation_id"]))
        return CommandResult(status="ok", output="foreign cancellation staged")


def create_plugin(config):
    manifest_data = json.loads((Path(__file__).with_name("voidcode-plugin.json")).read_text(encoding="utf-8"))
    cancel_decl = next(t for t in manifest_data["tools"] if t["name"] == "obs2_native_cancel")
    cancel_tool = Obs2ForeignCancelTool(
        ToolDefinition(
            name=cancel_decl["name"],
            description=cancel_decl["description"],
            input_schema=cancel_decl["input_schema"],
            effects=frozenset({ToolEffect.WRITE}),
            replay_policy="never",
        )
    )
    return Plugin(tools=(cancel_tool,), commands=(Obs2ForeignCancelCommand(),), observer=obs2_observer)
'''
    plugin_path = root / "obs2_plugin.py"
    plugin_path.write_text(code, encoding="utf-8")
    content_hash = hashlib.sha256(plugin_path.read_bytes()).hexdigest()
    manifest = PluginManifest(
        plugin_id=_OBSERVER_PLUGIN_ID,
        version="1.0.0",
        entrypoint="obs2_plugin:create_plugin",
        files=(PluginFileIdentity(path="obs2_plugin.py", sha256=content_hash),),
        tools=(
            PluginToolDeclaration(
                name="obs2_native_cancel",
                description="Native tool: attempt to cancel another plugin's continuation id.",
                input_schema={
                    "type": "object",
                    "properties": {"continuation_id": {"type": "string"}},
                    "additionalProperties": False,
                },
                effects=(ToolEffect.WRITE,),
                replay_policy="never",
                capabilities=("workspace.write",),
            ),
        ),
        commands=(
            PluginCommandDeclaration(
                name="obs2_cancel_cmd",
                description="Attempt to cancel a continuation id owned by another plugin.",
                argument_schema={
                    "type": "object",
                    "properties": {"continuation_id": {"type": "string"}},
                    "required": ["continuation_id"],
                },
                effects=(ToolEffect.WRITE,),
                capabilities=("workspace.write",),
                busy_gate=False,
            ),
        ),
        capabilities=(PluginCapabilityRequest(name="workspace.write", reason="foreign cancellation must stay scoped to its own rows"),),
        context=False,
        observed_facts=("usage",),
    )
    (root / "voidcode-plugin.json").write_text(json.dumps(manifest.as_payload()), encoding="utf-8")


def _configure(
    workspace: Path,
    server: OpenAILoopbackServer,
    source: Path,
    *,
    observer_source: Path | None = None,
    **policy: object,
) -> None:
    plugins = {_GOV_PLUGIN_ID: {"path": str(source)}}
    if observer_source is not None:
        plugins[_OBSERVER_PLUGIN_ID] = {"path": str(observer_source)}
    config = {
        "schema_version": 1,
        "model": {"model": "endpoint/exact-model"},
        "policy": policy or {"approval_mode": "yolo"},
        "providers": {"endpoint": {"api_key": "loopback-secret-not-for-storage", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
        "plugins": plugins,
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None, usage: dict[str, object] | None | bool = True) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "reasoning_content": "loopback reasoning", "tool_calls": list(calls)},
        finish="tool_calls" if calls else "stop",
        usage=usage,
    )


@contextmanager
def _provider(replies: list[dict[str, object]]) -> Iterator[OpenAILoopbackServer]:
    server = OpenAILoopbackServer(replies)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


#: Cold start admits the whole bundled plugin set and spawns the plugin host before the run can
#: reach the provider or write a marker, and these tests run under parallel xdist workers. The
#: old 10-20 s cutoffs failed here while the durable state showed the process still progressing.
_STARTUP_DEADLINE_SECONDS = 60.0


class _GatedHandler(_OpenAILoopbackHandler):
    """Loopback handler that holds each response until the test releases it."""

    def do_POST(self) -> None:
        server = cast(_GatedLoopbackServer, self.server)
        server.arrivals.append(self.path)
        server.release.wait(timeout=60)
        super().do_POST()


class _GatedLoopbackServer(OpenAILoopbackServer):
    """Real listening provider whose reply is withheld until `release` is set."""

    def __init__(self, replies: list[dict[str, object]]) -> None:
        super().__init__(replies)
        self.RequestHandlerClass = _GatedHandler
        self.arrivals: list[str] = []
        self.release = threading.Event()

    def await_entry(self, timeout: float = _STARTUP_DEADLINE_SECONDS) -> None:
        deadline = time.monotonic() + timeout
        while not self.arrivals and time.monotonic() < deadline:
            time.sleep(0.02)
        assert self.arrivals, "provider request never reached the listening server"


@contextmanager
def _gated_provider(replies: list[dict[str, object]]) -> Iterator[_GatedLoopbackServer]:
    server = _GatedLoopbackServer(replies)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.release.set()
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def _command(workspace: Path, state: Path, *arguments: str, trust: Sequence[str] = (_GOV_PLUGIN_ID,)) -> list[str]:
    if arguments and arguments[0] in {"invoke", "confirm"} and "--expected-revision" not in arguments:
        arguments = (*arguments, "--expected-revision", _cli_catalog_revision(workspace, state, trust))
    trusted: list[str] = []
    for plugin_id in trust:
        trusted += ["--trust-plugin", plugin_id]
    return [
        sys.executable,
        "-m",
        "voidcode.kernel",
        "--workspace",
        str(workspace),
        "--state-dir",
        str(state),
        *trusted,
        *arguments,
    ]


_CLI_REVISION_CACHE: dict[tuple[Path, Path], str] = {}


def _cli_catalog_revision(workspace: Path, state: Path, trust: Sequence[str]) -> str:
    key = (workspace, state)
    cached = _CLI_REVISION_CACHE.get(key)
    if cached is None:
        res = subprocess.run(_command(workspace, state, "catalog", trust=trust), capture_output=True, text=True, timeout=60, check=False)
        assert res.returncode == 0, res.stdout + res.stderr
        cached = str(json.loads(res.stdout)["revision_id"])
        _CLI_REVISION_CACHE[key] = cached
    return cached


def _cli(workspace: Path, state: Path, *arguments: str, trust: Sequence[str] = (_GOV_PLUGIN_ID,), code: int = 0) -> dict[str, Any]:
    res = subprocess.run(_command(workspace, state, *arguments, trust=trust), capture_output=True, text=True, timeout=60, check=False)
    assert res.returncode == code, f"CLI exited with {res.returncode} (expected {code}): stdout={res.stdout} stderr={res.stderr}"
    return json.loads(res.stdout)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextmanager
def _listening_http(workspace: Path, state: Path, token: str, *, trust: Sequence[str] = (_GOV_PLUGIN_ID,)) -> Iterator[str]:
    app = create_kernel_app(workspace=workspace, state_dir=state, trusted_plugins=frozenset(trust), bearer_token=token)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    worker = threading.Thread(target=server.run, daemon=True)
    worker.start()
    deadline = time.monotonic() + 20
    base = f"http://127.0.0.1:{port}"
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                break
        except OSError as exc:
            if time.monotonic() > deadline:
                raise TimeoutError("HTTP server failed to bind") from exc
            time.sleep(0.05)
    try:
        yield base
    finally:
        server.should_exit = True
        worker.join(timeout=10)


_REVISION_CACHE: dict[str, str] = {}


def _catalog_revision(base: str, token: str | None) -> str:
    """The revision the client displayed: taken from the command catalog exactly like a real client."""
    cached = _REVISION_CACHE.get(base)
    if cached is None:
        status, payload = _request(base, "GET", "/api/commands", token=token)
        assert status == 200, payload.decode()
        cached = str(json.loads(payload)["revision_id"])
        _REVISION_CACHE[base] = cached
    return cached


def _request(base: str, method: str, path: str, *, token: str | None = None, body: object = None) -> tuple[int, bytes]:
    # Mutating invokes and confirmations are bound to the displayed catalog revision; the
    # transport requires it, so this helper does what a real client does instead of omitting it.
    if method == "POST" and ("/invoke/" in path or path.endswith("/confirm")):
        revision = _catalog_revision(base, token)
        if body is None:
            body = {"expected_revision": revision}
        elif isinstance(body, dict) and "expected_revision" not in body:
            body = {**body, "expected_revision": revision}
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _await_marker(path: Path, timeout: float = _STARTUP_DEADLINE_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while not path.is_file():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Marker {path} did not appear within {timeout}s")
        time.sleep(0.02)


def _await_lease_release(workspace: Path, timeout: float = _STARTUP_DEADLINE_SECONDS) -> None:
    desc = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(desc, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Plugin host retained workspace lock") from None
                time.sleep(0.02)
    finally:
        os.close(desc)


# ---------------------------------------------------------------------------
# Criterion 1: Concurrency & Cancellation Isolation
# ---------------------------------------------------------------------------


def test_active_run_concurrency_and_cancellation_isolation(tmp_path: Path) -> None:
    """Prove:
    1. Active run A blocked in tool; non-gated command commits state in same session.
    2. Gated command rejects active run.
    3. Idle session B cancel does not stop A.
    4. Active session A cancel halts owned work cleanly with verified state.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _governance_plugin_source(source)
    state = tmp_path / "state"

    replies = [
        _turn(_call("mut-1", "gov_mutate", marker="mut.txt", block=True)),
        _turn(output="run A completed after unblock"),
    ]
    with _provider(replies) as server:
        _configure(workspace, server, source, approval_mode="yolo")
        token = "gov-token-1"

        with _listening_http(workspace, state, token) as base:
            # Create session A and session B
            _request(base, "POST", "/api/sessions", token=token, body={"session_id": "session-a"})
            _request(base, "POST", "/api/sessions", token=token, body={"session_id": "session-b"})

            # Start run A in background thread (blocked in tool)
            run_a_result: dict[str, Any] = {}

            def _drive_run_a() -> None:
                status, body = _request(base, "POST", "/api/sessions/session-a/run", token=token, body={"prompt": "start blocking run"})
                run_a_result["status_code"] = status
                run_a_result["payload"] = json.loads(body.decode())

            t_run = threading.Thread(target=_drive_run_a, daemon=True)
            t_run.start()

            # Wait for run A to reach tool block
            _await_marker(workspace / "host.pid")
            assert (workspace / "mut.txt").is_file()

            # 1. Non-gated command commits state in SAME session while run A is active
            status, body = _request(
                base,
                "POST",
                "/api/sessions/session-a/invoke/gov_write_cmd",
                token=token,
                body={"arguments": {"value": "concurrent_mid_run_state"}},
            )
            assert status == 200, body.decode()
            resp = json.loads(body)
            assert resp["status"] == "ok"
            assert resp["data"]["value"] == "concurrent_mid_run_state"

            # 2. Gated command rejects invocation while run A is active
            status, body = _request(
                base,
                "POST",
                "/api/sessions/session-a/invoke/gov_gated_cmd",
                token=token,
                body={},
            )
            assert status in (400, 409) or (status == 200 and json.loads(body)["status"] == "error")

            # 3. Targeted cancellation of idle session B does NOT cancel session A
            status, body = _request(base, "POST", "/api/sessions/session-b/cancel", token=token)
            assert status == 200
            assert json.loads(body)["cancellation_requested"] is False
            assert t_run.is_alive()  # Run A still executing

            # 4. Target cancellation on active session A stops owned work
            status, body = _request(base, "POST", "/api/sessions/session-a/cancel", token=token)
            assert status == 200
            assert json.loads(body)["cancellation_requested"] is True

            t_run.join(timeout=_STARTUP_DEADLINE_SECONDS)
            assert not t_run.is_alive()
            assert run_a_result["payload"]["status"] == "interrupted"
            assert run_a_result["payload"]["stop_cause"] == "user_stop"

    # The real side effect happened exactly once, and the interruption is durable.
    assert (workspace / "mut.txt").read_text(encoding="utf-8") == "mutated\n"
    with SqliteStore(state / "kernel-v1.sqlite3") as store:
        checkpoint = store.checkpoint("session-a")
        assert checkpoint is not None
        assert checkpoint.kind != "ready"
        s_state, _ = store.plugin_state(_GOV_PLUGIN_ID, "session", "session-a")
        assert s_state is not None
        assert s_state.get("value") == "concurrent_mid_run_state"


def test_active_run_blocked_in_real_provider_survives_midrun_control(tmp_path: Path) -> None:
    """Prove a run blocked inside the real provider (response withheld, not the tool):
    1. Mid-run non-gated command commits state while the provider call is in flight.
    2. The owned response is honoured afterwards: real output, real usage, effect once.
    3. Mid-run control neither cancels the run nor duplicates the provider bill.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _governance_plugin_source(source)
    state = tmp_path / "state"

    replies = [
        _turn(_call("p1", "gov_mutate", marker="provider_mut.txt")),
        _turn(output="released after mid-run control"),
    ]
    with _gated_provider(replies) as server:
        _configure(workspace, server, source, approval_mode="yolo")
        token = "provider-block-token"

        with _listening_http(workspace, state, token) as base:
            _request(base, "POST", "/api/sessions", token=token, body={"session_id": "provider-blocked"})

            run_result: dict[str, Any] = {}

            def _drive_run() -> None:
                status, body = _request(base, "POST", "/api/sessions/provider-blocked/run", token=token, body={"prompt": "blocked provider turn"})
                run_result["status_code"] = status
                run_result["payload"] = json.loads(body.decode())

            worker = threading.Thread(target=_drive_run, daemon=True)
            worker.start()

            # The run is genuinely blocked waiting for the provider response.
            server.await_entry()
            assert len(server.arrivals) == 1
            assert not (workspace / "provider_mut.txt").exists()

            # Mid-run control in the same session while the provider call is in flight.
            status, body = _request(
                base,
                "POST",
                "/api/sessions/provider-blocked/invoke/gov_write_cmd",
                token=token,
                body={"arguments": {"value": "committed_during_provider_wait"}},
            )
            assert status == 200, body.decode()
            assert json.loads(body)["data"]["value"] == "committed_during_provider_wait"
            assert len(server.arrivals) == 1, "a command must not dispatch the provider"

            server.release.set()
            worker.join(timeout=_STARTUP_DEADLINE_SECONDS)
            assert not worker.is_alive()
            assert run_result["status_code"] == 200, run_result
            assert run_result["payload"]["status"] == "completed", run_result
            assert run_result["payload"]["output"] == "released after mid-run control"
            assert run_result["payload"]["stop_cause"] == "completed"

    # One dispatch per turn, native effect exactly once, usage counted per dispatch.
    assert len(server.requests) == 2, server.requests
    assert len(server.arrivals) == 2, server.arrivals
    assert (workspace / "provider_mut.txt").read_text(encoding="utf-8") == "mutated\n"
    usage_report = _cli(workspace, state, "usage", "provider-blocked")
    assert usage_report["turn_count"] == 2, usage_report
    assert usage_report["has_unknown_usage"] is False, usage_report
    with SqliteStore(state / "kernel-v1.sqlite3") as store:
        stored, _ = store.plugin_state(_GOV_PLUGIN_ID, "session", "provider-blocked")
        assert stored is not None and stored["value"] == "committed_during_provider_wait"


def test_new_user_turn_after_user_stop_is_admitted(tmp_path: Path) -> None:
    """Prove a stopped run leaves a usable session: after a real user stop the
    terminal checkpoint still admits a fresh user turn in the same session.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _governance_plugin_source(source)
    state = tmp_path / "state"

    replies = [
        _turn(_call("s1", "gov_mutate", marker="stop_mut.txt", block=True)),
        _turn(output="fresh turn after stop"),
    ]
    with _provider(replies) as server:
        _configure(workspace, server, source, approval_mode="yolo")
        token = "after-stop-token"

        with _listening_http(workspace, state, token) as base:
            _request(base, "POST", "/api/sessions", token=token, body={"session_id": "after-stop"})

            stopped: dict[str, Any] = {}

            def _drive() -> None:
                status, body = _request(base, "POST", "/api/sessions/after-stop/run", token=token, body={"prompt": "stop me"})
                stopped["status_code"] = status
                stopped["payload"] = json.loads(body.decode())

            worker = threading.Thread(target=_drive, daemon=True)
            worker.start()
            _await_marker(workspace / "host.pid")

            status, body = _request(base, "POST", "/api/sessions/after-stop/cancel", token=token)
            assert status == 200
            assert json.loads(body)["cancellation_requested"] is True
            worker.join(timeout=_STARTUP_DEADLINE_SECONDS)
            assert not worker.is_alive()
            assert stopped["payload"]["status"] == "interrupted"

            # A fresh user turn on the same session must be admitted and must run
            # a real provider turn (no stranded terminal checkpoint).
            status, body = _request(base, "POST", "/api/sessions/after-stop/run", token=token, body={"prompt": "new user turn"})
            assert status == 200, body.decode()
            fresh = json.loads(body)
            assert fresh["status"] == "completed", fresh
            assert fresh["output"] == "fresh turn after stop", fresh
            assert fresh["run_id"] != stopped["payload"]["run_id"]

    assert (workspace / "stop_mut.txt").read_text(encoding="utf-8") == "mutated\n"


def test_command_committed_during_context_rpc_does_not_shift_dispatch_refs(tmp_path: Path) -> None:
    """Prove a non-gated command committing while the kernel is blocked inside the
    context RPC cannot make the later dispatch point at the command's events:
    the model turn must reference the provider intent it was actually dispatched
    under, and the run must complete.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _governance_plugin_source(source)
    state = tmp_path / "state"

    with _provider([_turn(output="after the race")]) as server:
        _configure(workspace, server, source, approval_mode="yolo")
        token = "context-race-token"
        with _listening_http(workspace, state, token) as base:
            _request(base, "POST", "/api/sessions", token=token, body={"session_id": "context-race"})
            (workspace / "context.block").touch()

            finished: dict[str, Any] = {}

            def _drive() -> None:
                status, body = _request(base, "POST", "/api/sessions/context-race/run", token=token, body={"prompt": "race the context RPC"})
                finished["status_code"] = status
                finished["payload"] = json.loads(body.decode())

            worker = threading.Thread(target=_drive, daemon=True)
            worker.start()
            deadline = time.monotonic() + _STARTUP_DEADLINE_SECONDS
            while not (workspace / "context.entered").is_file() and "payload" not in finished and time.monotonic() < deadline:
                time.sleep(0.02)
            assert (workspace / "context.entered").is_file(), finished

            # The kernel is inside the context RPC: this command commits events first.
            status, body = _request(
                base,
                "POST",
                "/api/sessions/context-race/invoke/gov_write_cmd",
                token=token,
                body={"arguments": {"value": "committed_during_context_rpc"}},
            )
            assert status == 200, body.decode()

            (workspace / "context.release").touch()
            worker.join(timeout=_STARTUP_DEADLINE_SECONDS)
            assert not worker.is_alive()
            assert finished["status_code"] == 200, finished
            assert finished["payload"]["status"] == "completed", finished
            assert finished["payload"]["output"] == "after the race", finished

    with SqliteStore(state / "kernel-v1.sqlite3") as store:
        events = store.events("context-race")
        intent = next(event for event in events if event.event_type == "kernel.provider_intent")
        model_turn = next(event for event in events if event.event_type == "kernel.model_turn")
        command = next(event for event in events if event.event_type == "kernel.command_completed")
        # The dispatch really happened after the intervening command commit, and the
        # model turn references that intent sequence - not the command's events.
        assert intent.sequence > command.sequence, [(event.sequence, event.event_type) for event in events]
        assert model_turn.payload["dispatch_sequence"] == intent.sequence, model_turn.payload


# ---------------------------------------------------------------------------
# Criterion 2: Owner/Effect Authority
# ---------------------------------------------------------------------------


def test_owner_authority_and_read_only_enforcement(tmp_path: Path) -> None:
    """Prove:
    1. READ command attempting state/budget/queue mutation is denied without mutation.
    2. Business arguments named 'owner' and 'actor' do not override authentic lease identity.
    3. Distinct workspaces sharing a DB isolate workspace-scoped state.
    4. read_only config policy overrides yolo without dispatch.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _governance_plugin_source(source)
    state = tmp_path / "state"

    with _provider([]) as server:
        _configure(workspace, server, source, approval_mode="yolo")
        _cli(workspace, state, "create-session", "--session-id", "auth-session")

        # 1. READ command attempting mutations has them all rejected by read-only lease
        res = _cli(workspace, state, "invoke", "auth-session", "gov_read_cmd", "--arguments", '{"test_mutation": true}')
        assert res["status"] == "ok"
        rejections = res["data"]["rejections"]
        assert len(rejections) == 5
        assert any("state:" in r for r in rejections)
        assert any("budget:" in r for r in rejections)
        assert any("continuation:" in r for r in rejections)

        # 2. Business args 'owner' and 'actor' do not forge identity
        cmd_res = _cli(
            workspace,
            state,
            "invoke",
            "auth-session",
            "gov_write_cmd",
            "--arguments",
            '{"value": "legit", "owner": "forged_owner", "actor": "forged_actor"}',
        )
        assert cmd_res["status"] == "ok"
        assert cmd_res["data"]["actor"] == "client"  # Authentic context actor, not forged_actor

    # Verify in DB: state stored strictly under gov-plugin, not forged_owner
    with SqliteStore(state / "kernel-v1.sqlite3") as store:
        assert store.plugin_state("forged_owner", "session", "auth-session")[0] is None
        real_state, _ = store.plugin_state(_GOV_PLUGIN_ID, "session", "auth-session")
        assert real_state is not None
        assert real_state["value"] == "legit"

    # 3. Workspace scope keys isolation across distinct workspaces in shared DB
    ws2 = tmp_path / "workspace2"
    ws2.mkdir()
    with _provider([]) as server:
        _configure(ws2, server, source, approval_mode="yolo")
        _cli(workspace, state, "invoke", "auth-session", "gov_write_cmd", "--arguments", '{"value": "ws1_data", "scope": "workspace"}')
        _cli(ws2, state, "create-session", "--session-id", "ws2-session")
        _cli(ws2, state, "invoke", "ws2-session", "gov_write_cmd", "--arguments", '{"value": "ws2_data", "scope": "workspace"}')

    with SqliteStore(state / "kernel-v1.sqlite3") as store:
        ws1_state, _ = store.plugin_state(_GOV_PLUGIN_ID, "workspace", str(workspace.resolve()))
        ws2_state, _ = store.plugin_state(_GOV_PLUGIN_ID, "workspace", str(ws2.resolve()))
        assert ws1_state is not None and ws1_state["value"] == "ws1_data"
        assert ws2_state is not None and ws2_state["value"] == "ws2_data"


# ---------------------------------------------------------------------------
# Criterion 2 (continued): core command argument contract
# ---------------------------------------------------------------------------


def test_core_run_command_validates_arguments_before_spending(tmp_path: Path) -> None:
    """Prove core commands share one typed arg contract: an invalid prompt is
    refused before any provider spend, and a waiting run is reported truthfully
    instead of as an error with a null message.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _governance_plugin_source(source)
    state = tmp_path / "state"

    with _provider([_turn(_call("w1", "gov_mutate", marker="core_cmd.txt"))]) as server:
        _configure(workspace, server, source, approval_mode="ask")
        token = "core-cmd-token"
        with _listening_http(workspace, state, token) as base:
            _request(base, "POST", "/api/sessions", token=token, body={"session_id": "core-cmd-session"})

            # A non-string prompt must be refused without dispatching the provider.
            status, body = _request(
                base,
                "POST",
                "/api/sessions/core-cmd-session/invoke/run",
                token=token,
                body={"arguments": {"prompt": 123}},
            )
            assert status in (400, 409) or (status == 200 and json.loads(body)["status"] == "error"), body.decode()
            assert len(server.requests) == 0, server.requests

            # A real run that parks for approval is reported as a waiting outcome.
            status, body = _request(
                base,
                "POST",
                "/api/sessions/core-cmd-session/invoke/run",
                token=token,
                body={"arguments": {"prompt": "write something"}},
            )
            assert status == 200, body.decode()
            result = json.loads(body)
            assert result["status"] == "ok", result
            assert result["data"]["status"] == "waiting", result
            assert result["data"]["pending_approval"] is not None, result
            assert len(server.requests) == 1, server.requests


# ---------------------------------------------------------------------------
# Criterion 3: Optional Usage, Baselines, and Active Budget Invariants
# ---------------------------------------------------------------------------


def test_optional_usage_and_budget_window_enforcement(tmp_path: Path) -> None:
    """Prove:
    1. Turns with usage=None succeed and record truthful unknown usage (elapsed measured, tokens None).
    2. Budget with since_sequence baseline excludes earlier unknown usage.
    3. Exceeding known metric budget blocks next billable call with limit_reached.
    4. Missing metric under active budget stops next call but retains prior billing facts.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _governance_plugin_source(source)
    state = tmp_path / "state"

    replies = [
        _turn(output="first turn unbudgeted", usage=None),
        _turn(output="second turn budgeted", usage={"prompt_tokens": 100, "completion_tokens": 50}),
        _turn(output="third turn exceeding", usage={"prompt_tokens": 80, "completion_tokens": 40}),
    ]
    with _provider(replies) as server:
        _configure(workspace, server, source, approval_mode="yolo")
        _cli(workspace, state, "create-session", "--session-id", "budget-session")

        # Turn 1: usage=None across dispatch
        res1 = _cli(workspace, state, "run", "first prompt", "--session-id", "budget-session")
        assert res1["status"] == "completed"

        # Check truthful usage report: has_unknown_usage=True, tokens None, elapsed > 0
        rep1 = _cli(workspace, state, "usage", "budget-session")
        assert rep1["has_unknown_usage"] is True
        assert rep1["input_tokens"] is None
        assert rep1["elapsed_seconds"] is not None and rep1["elapsed_seconds"] >= 0.0

        # Baseline: sequence after turn 1
        baseline = rep1["through_sequence"]
        assert baseline > 0

        # Turn 2: Set budget via gov_budget_cmd since baseline: max_total_tokens=200 (150 allowed)
        b_res = _cli(
            workspace,
            state,
            "invoke",
            "budget-session",
            "gov_budget_cmd",
            "--arguments",
            json.dumps({"since_sequence": baseline, "max_total_tokens": 200}),
        )
        assert b_res["status"] == "ok"

        res2 = _cli(workspace, state, "run", "second prompt", "--session-id", "budget-session")
        assert res2["status"] == "completed"

        # Partial usage: known tokens, no cache detail -> cache stays unknown, never 0.
        rep2 = _cli(workspace, state, "usage", "budget-session", "--since-sequence", str(baseline))
        assert rep2["input_tokens"] == 100, rep2
        assert rep2["output_tokens"] == 50, rep2
        assert rep2["cache_read_tokens"] is None, rep2

        # Turn 3: Set budget tighter than turn 2's usage (e.g. 100 tokens max)
        b_res2 = _cli(
            workspace,
            state,
            "invoke",
            "budget-session",
            "gov_budget_cmd",
            "--arguments",
            json.dumps({"since_sequence": baseline, "max_total_tokens": 100}),
        )
        assert b_res2["status"] == "ok"

        res3 = _cli(workspace, state, "run", "third prompt", "--session-id", "budget-session", code=130)
        assert res3["status"] == "interrupted"
        assert res3["stop_cause"] == "limit_reached"
        assert "budget limit exceeded" in res3["error"]


# ---------------------------------------------------------------------------
# Criterion 3 (continued): failed/invalid provider finishes keep the bill
# ---------------------------------------------------------------------------


def test_invalid_provider_finish_fails_loudly_but_keeps_the_bill(tmp_path: Path) -> None:
    """Prove an invalid provider finish fails the run visibly while the dispatch's
    real bill (usage fact) stays durable and countable exactly once.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _governance_plugin_source(source)
    state = tmp_path / "state"

    replies = [openai_response(message={"role": "assistant", "content": "partial answer"}, finish="length")]
    with _provider(replies) as server:
        _configure(workspace, server, source, approval_mode="yolo")
        _cli(workspace, state, "create-session", "--session-id", "finish-session")

        failed = _cli(workspace, state, "run", "ask", "--session-id", "finish-session", code=1)
        assert failed["status"] == "failed", failed
        assert "did not complete its turn" in failed["error"], failed

        usage_report = _cli(workspace, state, "usage", "finish-session")
        assert usage_report["turn_count"] == 1, usage_report
        assert usage_report["has_unknown_usage"] is False, usage_report
        assert usage_report["input_tokens"] == 120, usage_report
        assert usage_report["output_tokens"] == 45, usage_report

        with SqliteStore(state / "kernel-v1.sqlite3") as store:
            events = store.events("finish-session")
            assert [event.event_type for event in events if event.event_type == "kernel.model_turn"]
            receipt_owner_events = [
                event.sequence
                for event in events
                if event.event_type == "kernel.model_turn" and not store.has_observer_receipt("finish-session", event.sequence, _GOV_PLUGIN_ID)
            ]
            assert receipt_owner_events == [], receipt_owner_events


# ---------------------------------------------------------------------------
# Criterion 4: Durable Claim Crash & Atomic Recovery
# ---------------------------------------------------------------------------


def test_durable_claim_crash_recovery_and_prefix_preservation(tmp_path: Path) -> None:
    """Prove:
    1. Queue continuation, claim and start.
    2. Kill kernel after first mutating tool effect.
    3. Restart resumes authentic checkpoint; completed prefix not replayed.
    4. Terminal + claim completion is atomic; second drain is empty.
    5. Same idempotency_key returns identical continuation row.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _governance_plugin_source(source)
    state = tmp_path / "state"

    (workspace / "input.txt").write_text("initial observation\\n", encoding="utf-8")

    replies = [
        _turn(
            _call("c1", "gov_read", path="input.txt"),
            _call("c2", "gov_mutate", marker="cont_mut.txt", block=True),
            _call("c3", "gov_mutate", marker="cont_tail.txt"),
        ),
        _turn(output="resumed after crash"),
    ]

    with _provider(replies) as server:
        _configure(workspace, server, source, approval_mode="yolo")
        _cli(workspace, state, "create-session", "--session-id", "cont-crash-session")

        # Test idempotency key deduplication
        with SqliteStore(state / "kernel-v1.sqlite3") as store:
            revision_id = store.session("cont-crash-session").active_revision_id
            cid1 = store.enqueue_continuation(
                "cont-001",
                _GOV_PLUGIN_ID,
                "cont-crash-session",
                "plugin.gov-plugin",
                "run-init",
                revision_id,
                {"action": "test_crash"},
                idempotency_key="unique-action-key",
            )
            cid2 = store.enqueue_continuation(
                "cont-002",
                _GOV_PLUGIN_ID,
                "cont-crash-session",
                "plugin.gov-plugin",
                "run-init",
                revision_id,
                {"action": "test_crash"},
                idempotency_key="unique-action-key",
            )
            assert cid1 == cid2 == "cont-001"
            assert store.continuation_id_for_idempotency("cont-crash-session", _GOV_PLUGIN_ID, "unique-action-key") == "cont-001"

        # Start drain in subprocess
        proc = subprocess.Popen(
            _command(workspace, state, "drain", "cont-crash-session"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        try:
            _await_marker(workspace / "host.pid")
            assert (workspace / "cont_mut.txt").is_file()

            # Kill kernel process abruptly mid-mutation
            proc.kill()
            proc.communicate(timeout=10)
            _await_lease_release(workspace)

            # Unblock the tool for resumption
            (workspace / "release.txt").touch()

            # Mutating tool was replay_policy="never". On resume, it must NOT execute tail or replay effect.
            recovered = _cli(workspace, state, "resume", "cont-crash-session")
            assert recovered["status"] == "completed"

            # Check: cont_mut.txt only written once
            assert (workspace / "cont_mut.txt").read_text(encoding="utf-8") == "mutated\n"
            # Tail tool was never dispatched
            assert not (workspace / "cont_tail.txt").exists()

            # Claim-once: second drain is empty
            drained_again = _cli(workspace, state, "drain", "cont-crash-session")
            assert drained_again["continuations"] == []

            # Terminal commit finalized the claim atomically: no row stays
            # pending or claimed, so no later drain can re-run the action.
            with SqliteStore(state / "kernel-v1.sqlite3") as store:
                assert store.pending_continuations("cont-crash-session") == ()
                assert store.claimed_continuations("cont-crash-session") == ()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=5)


# ---------------------------------------------------------------------------
# Criterion 4 (continued): automatic idle continuation drive
# ---------------------------------------------------------------------------


def test_idle_command_enqueue_drives_continuation_automatically(tmp_path: Path) -> None:
    """Prove a command that enqueues durable work on an IDLE session gets it run
    automatically (no explicit drain), through a real provider turn, once.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _governance_plugin_source(source)
    state = tmp_path / "state"

    replies = [_turn(output="automatic follow-up completed")]
    with _provider(replies) as server:
        _configure(workspace, server, source, approval_mode="yolo")
        _cli(workspace, state, "create-session", "--session-id", "auto-drive-session")

        queued = _cli(
            workspace,
            state,
            "invoke",
            "auto-drive-session",
            "gov_queue_cmd",
            "--arguments",
            '{"note": "auto"}',
        )
        assert queued["status"] == "ok", queued

        # The kernel itself must have driven the queued continuation.
        assert len(server.requests) == 1, server.requests
        prompt = json.dumps(server.requests[0]["messages"], ensure_ascii=False)
        assert "queued_followup" in prompt, prompt

        with SqliteStore(state / "kernel-v1.sqlite3") as store:
            assert store.pending_continuations("auto-drive-session") == ()
            assert store.claimed_continuations("auto-drive-session") == ()

        # Nothing was left for a later explicit drain.
        drained = _cli(workspace, state, "drain", "auto-drive-session")
        assert drained["continuations"] == []


# ---------------------------------------------------------------------------
# Criterion 4 (continued): inherited root action budget
# ---------------------------------------------------------------------------


def test_continuation_chain_budget_is_inherited_across_drains(tmp_path: Path) -> None:
    """Prove the root action budget of a continuation chain is finite and
    inherited: exhausting it in one drain cannot be reset by another drain.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _governance_plugin_source(source)
    state = tmp_path / "state"
    (workspace / "input.txt").write_text("chain input\n", encoding="utf-8")

    replies = [_turn(_call("c1", "gov_read", path="input.txt")) for _ in range(64)]
    with _provider(replies) as server:
        _configure(workspace, server, source, approval_mode="yolo")
        _cli(workspace, state, "create-session", "--session-id", "chain-session")
        with SqliteStore(state / "kernel-v1.sqlite3") as store:
            store.enqueue_continuation(
                "chain-cont",
                _GOV_PLUGIN_ID,
                "chain-session",
                "plugin.gov-plugin",
                "run-init",
                store.session("chain-session").active_revision_id,
                {"action": "chain"},
            )

        first = _cli(workspace, state, "drain", "chain-session")
        assert [item["status"] for item in first["continuations"]] == ["claimed"], first
        assert first["continuations"][0]["run_status"] == "interrupted", first
        spent = len(server.requests)
        assert spent == 32, (spent, first)

        # A second drain must inherit the exhausted root budget: no new provider
        # turn may be billed and the claim stays claimed with its real cause.
        second = _cli(workspace, state, "drain", "chain-session")
        assert [item["status"] for item in second["continuations"]] == ["claimed"], second
        assert second["continuations"][0]["stop_cause"] == "limit_reached", second
        assert len(server.requests) == spent, (len(server.requests), second)


# ---------------------------------------------------------------------------
# Criterion 4 (continued): ordered cancel/enqueue semantics
# ---------------------------------------------------------------------------


def test_ordered_cancel_and_enqueue_keep_the_right_rows(tmp_path: Path) -> None:
    """Prove staged continuation ops apply in their recorded order:
    cancel-then-enqueue keeps the new row, enqueue-then-cancel (by returned id)
    cancels exactly that row.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _governance_plugin_source(source)
    state = tmp_path / "state"

    with _provider([_turn(output="kept continuation ran")]) as server:
        _configure(workspace, server, source, approval_mode="yolo")
        _cli(workspace, state, "create-session", "--session-id", "order-session")

        kept = _cli(workspace, state, "invoke", "order-session", "gov_cancel_then_queue_cmd")
        assert kept["status"] == "ok", kept
        kept_id = kept["data"]["continuation_id"]
        assert len(server.requests) == 1, server.requests
        with SqliteStore(state / "kernel-v1.sqlite3") as store:
            assert store.continuation_status(kept_id) == "completed", kept_id
            assert store.pending_continuations("order-session") == ()
            assert store.claimed_continuations("order-session") == ()

        dropped = _cli(workspace, state, "invoke", "order-session", "gov_queue_then_cancel_cmd")
        assert dropped["status"] == "ok", dropped
        dropped_id = dropped["data"]["continuation_id"]
        assert len(server.requests) == 1, server.requests
        with SqliteStore(state / "kernel-v1.sqlite3") as store:
            assert store.continuation_status(dropped_id) == "cancelled", dropped_id


def test_owner_cancel_stops_own_claimed_run_without_next_turn(tmp_path: Path) -> None:
    """Prove the owner-cancel consumer path with the real SDK and two owners:
    a DIFFERENT plugin naming this claim's id cannot stop the run or its row
    (no bill, no re-effect), while the real owner's cancellation stops the run
    as owner_cancel with the effect landed exactly once.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    observer_source = tmp_path / "external-observer"
    _governance_plugin_source(source)
    _observer_plugin_source(observer_source)
    state = tmp_path / "state"
    trust = (_GOV_PLUGIN_ID, _OBSERVER_PLUGIN_ID)

    replies = [
        _turn(_call("o1", "gov_mutate", marker="owner_cancel.txt", block=True)),
        _turn(output="must never be requested"),
    ]
    with _provider(replies) as server:
        _configure(workspace, server, source, observer_source=observer_source, approval_mode="yolo")
        _cli(workspace, state, "create-session", "--session-id", "owner-cancel-session", trust=trust)
        with SqliteStore(state / "kernel-v1.sqlite3") as store:
            store.enqueue_continuation(
                "owner-claim",
                _GOV_PLUGIN_ID,
                "owner-cancel-session",
                "plugin.gov-plugin",
                "run-init",
                store.session("owner-cancel-session").active_revision_id,
                {"action": "owner_cancel"},
            )

        token = "owner-cancel-token"
        with _listening_http(workspace, state, token, trust=trust) as base:
            drained: dict[str, Any] = {}

            def _drive() -> None:
                status, body = _request(base, "POST", "/api/sessions/owner-cancel-session/continuations", token=token, body={"limit": 1})
                drained["status_code"] = status
                drained["payload"] = json.loads(body.decode())

            worker = threading.Thread(target=_drive, daemon=True)
            worker.start()
            deadline = time.monotonic() + _STARTUP_DEADLINE_SECONDS
            while not (workspace / "host.pid").is_file() and "payload" not in drained and time.monotonic() < deadline:
                time.sleep(0.02)
            assert (workspace / "owner_cancel.txt").is_file(), drained

            # A DIFFERENT owner naming the same continuation id must not be able to
            # stop this run, cancel its row, or re-effect anything.
            foreign = _request(
                base,
                "POST",
                "/api/sessions/owner-cancel-session/invoke/obs2_cancel_cmd",
                token=token,
                body={"arguments": {"continuation_id": "owner-claim"}},
            )
            assert foreign[0] == 200, foreign[1].decode()
            assert json.loads(foreign[1])["status"] == "ok", foreign[1].decode()
            time.sleep(1.0)
            assert worker.is_alive(), "a foreign owner cancelled another owner's run"
            with sqlite3.connect(state / "kernel-v1.sqlite3") as conn:
                foreign_row = conn.execute("SELECT status FROM continuations WHERE continuation_id = ?", ("owner-claim",)).fetchone()
            assert foreign_row == ("claimed",), foreign_row
            assert len(server.requests) == 1, server.requests
            assert (workspace / "owner_cancel.txt").read_text(encoding="utf-8") == "mutated\n"

            # The real owner's cancellation still stops the claimed run.
            status, body = _request(
                base,
                "POST",
                "/api/sessions/owner-cancel-session/invoke/gov_owner_cancel_cmd",
                token=token,
                body={"arguments": {}},
            )
            assert status == 200, body.decode()
            assert json.loads(body)["status"] == "ok", body.decode()

            worker.join(timeout=_STARTUP_DEADLINE_SECONDS)
            assert not worker.is_alive()
            claims = drained["payload"]["continuations"]
            assert [item["status"] for item in claims] == ["cancelled"], claims
            assert claims[0]["run_status"] == "interrupted", claims
            assert claims[0]["stop_cause"] == "owner_cancel", claims

    # The native effect happened exactly once and the next turn was never billed.
    assert (workspace / "owner_cancel.txt").read_text(encoding="utf-8") == "mutated\n"
    assert len(server.requests) == 1, server.requests
    with SqliteStore(state / "kernel-v1.sqlite3") as store:
        assert store.continuation_status("owner-claim") == "cancelled"
        assert store.pending_continuations("owner-cancel-session") == ()
        assert store.claimed_continuations("owner-cancel-session") == ()


def test_root_action_budget_shares_main_and_child_dispatches(tmp_path: Path) -> None:
    """Prove the ORIGINAL root action budget is finite and shared by real fan-out:
    the main run bills 2 of its 3 steps (native fan-out tool, then its own stop),
    a real child continuation bills the third and completes, and the second
    sibling can never bill a fourth - not in fresh drains, not after a restart.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _governance_plugin_source(source)
    state = tmp_path / "state"

    replies = [
        _turn(_call("f1", "gov_fanout")),
        _turn(output="main finished after fanout"),
        _turn(output="first child finished"),
    ]
    with _provider(replies) as server:
        _configure(workspace, server, source, approval_mode="yolo")
        _cli(workspace, state, "create-session", "--session-id", "fanout-session")

        started = _cli(workspace, state, "run", "start fanout", "--session-id", "fanout-session", "--max-steps", "3")
        # The caller gets the main run's own output, never a drained child's result.
        assert started["status"] == "completed", started
        assert started["output"] == "main finished after fanout", started
        assert len(server.requests) == 3, (len(server.requests), started)

        # Later drains and a reopen must not extend the exhausted root budget.
        drained = _cli(workspace, state, "drain", "fanout-session")
        assert len(server.requests) == 3, (len(server.requests), drained)
        assert [item["status"] for item in drained["continuations"]] == ["claimed"], drained
        assert drained["continuations"][0]["stop_cause"] == "limit_reached", drained
        shown = _cli(workspace, state, "show", "fanout-session")
        assert shown["status"] == "interrupted", shown
        assert len(server.requests) == 3, (len(server.requests), shown)

    with SqliteStore(state / "kernel-v1.sqlite3") as store:
        checkpoint = store.checkpoint("fanout-session")
        assert checkpoint is not None
        root = str(checkpoint.payload["root_id"])
        assert root, checkpoint.payload
        assert store.root_budget_state(root) == (3, 3), store.root_budget_state(root)
        events = store.events("fanout-session")
        intents = [event for event in events if event.event_type == "kernel.provider_intent"]
        assert len(intents) == 3, intents
    with sqlite3.connect(state / "kernel-v1.sqlite3") as conn:
        rows = conn.execute(
            "SELECT continuation_id, status, root_id, run_id FROM continuations WHERE session_id = ?",
            ("fanout-session",),
        ).fetchall()
    assert len(rows) == 2, rows
    # Both siblings belong to the ORIGINAL parent root, which is a real id.
    assert {row[2] for row in rows} == {root}, rows
    assert sorted(row[1] for row in rows) == ["claimed", "completed"], rows
    completed_child = next(row for row in rows if row[1] == "completed")
    claimed_sibling = next(row for row in rows if row[1] == "claimed")

    # The third bill happened under a distinct child run, not under the main run.
    assert [event.run_id for event in intents[:2]] == [started["run_id"]] * 2, intents
    child_run = intents[2].run_id
    assert child_run == completed_child[3], (child_run, completed_child)
    assert child_run != started["run_id"], (child_run, started["run_id"])
    assert claimed_sibling[3] not in (child_run, started["run_id"]), claimed_sibling


# ---------------------------------------------------------------------------
# Criterion 4 (continued): user-stop claim cancellation
# ---------------------------------------------------------------------------


def test_user_stop_cancels_claim_without_auto_resume(tmp_path: Path) -> None:
    """Prove a user stop on an in-flight claimed continuation cancels the claim
    durably: the effect lands once, the claim is not auto-resumed by later drains.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _governance_plugin_source(source)
    state = tmp_path / "state"

    replies = [_turn(_call("u1", "gov_mutate", marker="claim_mut.txt", block=True))]
    with _provider(replies) as server:
        _configure(workspace, server, source, approval_mode="yolo")
        _cli(workspace, state, "create-session", "--session-id", "user-stop-session")

        with SqliteStore(state / "kernel-v1.sqlite3") as store:
            store.enqueue_continuation(
                "cont-user-stop",
                _GOV_PLUGIN_ID,
                "user-stop-session",
                "plugin.gov-plugin",
                "run-init",
                store.session("user-stop-session").active_revision_id,
                {"action": "user_stop_claim"},
            )

        token = "user-stop-token"
        with _listening_http(workspace, state, token) as base:
            drain_result: dict[str, Any] = {}

            def _drive_drain() -> None:
                status, body = _request(base, "POST", "/api/sessions/user-stop-session/continuations", token=token, body={"limit": 1})
                drain_result["status"] = status
                drain_result["payload"] = json.loads(body.decode())

            worker = threading.Thread(target=_drive_drain, daemon=True)
            worker.start()

            deadline = time.monotonic() + _STARTUP_DEADLINE_SECONDS
            while not (workspace / "host.pid").is_file() and "payload" not in drain_result and time.monotonic() < deadline:
                time.sleep(0.02)
            assert (workspace / "host.pid").is_file(), drain_result
            assert (workspace / "claim_mut.txt").is_file()

            status, body = _request(base, "POST", "/api/sessions/user-stop-session/cancel", token=token)
            assert status == 200, body.decode()
            assert json.loads(body)["cancellation_requested"] is True

            worker.join(timeout=_STARTUP_DEADLINE_SECONDS)
            assert not worker.is_alive()
            assert drain_result["status"] == 200, drain_result
            claims = drain_result["payload"]["continuations"]
            assert [claim["status"] for claim in claims] == ["cancelled"], claims
            assert claims[0]["run_status"] == "interrupted", claims

            # No auto resume: a later drain finds nothing to claim.
            status, body = _request(base, "POST", "/api/sessions/user-stop-session/continuations", token=token, body={"limit": 1})
            assert status == 200, body.decode()
            assert json.loads(body)["continuations"] == []

    # The real external effect happened exactly once before the stop.
    assert (workspace / "claim_mut.txt").read_text(encoding="utf-8") == "mutated\n"


# ---------------------------------------------------------------------------
# Criterion 5: Observer Receipt Crash Deduplication
# ---------------------------------------------------------------------------


def test_observer_counts_each_turn_fact_once_across_restarts(tmp_path: Path) -> None:
    """Prove (steady-state counting, not the crash boundary):
    1. Each provider turn's usage fact increments durable observer state once.
    2. Re-reading history in a fresh process neither re-invokes observers nor double counts.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _governance_plugin_source(source)
    state = tmp_path / "state"

    replies = [
        _turn(output="first turn"),
        _turn(output="second turn"),
    ]
    with _provider(replies) as server:
        _configure(workspace, server, source, approval_mode="yolo")
        _cli(workspace, state, "create-session", "--session-id", "obs-crash-session")

        res1 = _cli(workspace, state, "run", "first", "--session-id", "obs-crash-session")
        assert res1["status"] == "completed"

        res2 = _cli(workspace, state, "run", "second", "--session-id", "obs-crash-session")
        assert res2["status"] == "completed"

        # Check observer counted exactly 2 usage facts
        cmd_res = _cli(workspace, state, "invoke", "obs-crash-session", "gov_read_cmd")
        assert cmd_res["data"]["state"]["usage_facts_seen"] == 2

        # Rescue/show path: does not re-invoke observers or mutate state
        shown = _cli(workspace, state, "show", "obs-crash-session", "--events")
        assert shown["status"] == "completed"

        cmd_res_again = _cli(workspace, state, "invoke", "obs-crash-session", "gov_read_cmd")
        assert cmd_res_again["data"]["state"]["usage_facts_seen"] == 2


def test_two_same_owner_facts_in_one_commit_do_not_cas_conflict(tmp_path: Path) -> None:
    """Prove two source facts for the same owner staged inside ONE core commit
    both persist as independent contributions (never last-wins), each with its
    own durable receipt and no observer diagnostics.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _governance_plugin_source(source)
    state = tmp_path / "state"

    replies = [
        _turn(_call("r1", "gov_read", path="input.txt"), _call("r2", "gov_read", path="input.txt")),
        _turn(output="batch finished"),
    ]
    (workspace / "input.txt").write_text("observed by both calls\n", encoding="utf-8")
    with _provider(replies) as server:
        _configure(workspace, server, source, approval_mode="yolo")
        result = _cli(workspace, state, "run", "read twice", "--session-id", "two-fact-session")
        assert result["status"] == "completed"

        snapshot = _cli(workspace, state, "invoke", "two-fact-session", "gov_read_cmd")
        stored = snapshot["data"]["state"]
        # Both contributions of the same owner survived the single commit.
        assert stored.get("invocations_seen", 0) == 2, snapshot
        assert stored.get("usage_facts_seen", 0) == 2, snapshot

        shown = _cli(workspace, state, "show", "two-fact-session", "--events")
        diagnostics = [event for event in shown["events"] if event["type"] == "kernel.observer_error"]
        assert diagnostics == [], diagnostics

        # Every fact-carrying event has its own receipt row, so neither fact can
        # be silently dropped or redelivered later.
        with SqliteStore(state / "kernel-v1.sqlite3") as store:
            facts = [
                event
                for event in store.events("two-fact-session")
                if event.event_type in {"kernel.model_turn", "kernel.tool_completed", "kernel.tool_interrupted"}
            ]
            assert len(facts) == 4, [(event.sequence, event.event_type) for event in facts]
            for event in facts:
                assert store.has_observer_receipt("two-fact-session", event.sequence, _GOV_PLUGIN_ID), event


def test_observer_receipt_survives_kernel_kill_exactly_once(tmp_path: Path) -> None:
    """Prove the receipt-crash boundary with real processes:
    1. Provider turn commits the bill/fact durably.
    2. Observer pauses before returning; kill the kernel mid-callback.
    3. Reopen the same revision: pending fact is delivered, state persisted once.
    4. Repeated restart does not increment again, and no extra provider bill occurs.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _governance_plugin_source(source)
    state = tmp_path / "state"

    replies = [_turn(output="billed turn")]
    with _provider(replies) as server:
        _configure(workspace, server, source, approval_mode="yolo")
        _cli(workspace, state, "create-session", "--session-id", "obs-kill-session")

        (workspace / "observer.block").touch()
        proc = subprocess.Popen(
            _command(workspace, state, "run", "billed prompt", "--session-id", "obs-kill-session"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _await_marker(workspace / "observer.entered")
            assert len(server.requests) == 1

            proc.kill()
            proc.communicate(timeout=10)
            _await_lease_release(workspace)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=5)

        # The bill/fact is already durable even though the observer never returned.
        with SqliteStore(state / "kernel-v1.sqlite3") as store:
            events = store.events("obs-kill-session")
            assert [event.event_type for event in events if event.event_type == "kernel.model_turn"]
            checkpoint = store.checkpoint("obs-kill-session")
            assert checkpoint is not None
            assert checkpoint.kind == "terminal"

        # Reopen the same revision (a fresh real process). The bill must not be
        # charged again, and the paused fact must be delivered exactly once.
        (workspace / "observer.block").unlink()
        first = _cli(workspace, state, "invoke", "obs-kill-session", "gov_read_cmd")
        assert first["data"]["state"].get("usage_facts_seen", 0) == 1, first
        assert len(server.requests) == 1, "recovery must finalize the stored turn, not bill again"

        # A real receipt row now exists for the delivered fact (durable dedupe authority).
        with SqliteStore(state / "kernel-v1.sqlite3") as store:
            model_turn = next(event for event in store.events("obs-kill-session") if event.event_type == "kernel.model_turn")
            assert store.has_observer_receipt("obs-kill-session", model_turn.sequence, _GOV_PLUGIN_ID)

        # A second reopen of the same committed fact must not increment again.
        second = _cli(workspace, state, "invoke", "obs-kill-session", "gov_read_cmd")
        assert second["data"]["state"].get("usage_facts_seen", 0) == 1, second
        assert len(server.requests) == 1


# ---------------------------------------------------------------------------
# Criterion 6: Confirmation Durable Intent
# ---------------------------------------------------------------------------


def test_confirmation_durable_intent_and_stale_rejection(tmp_path: Path) -> None:
    """Prove:
    1. Command returns pending_confirmation bound to session and state version.
    2. User deny decision resolves confirmation without side effects.
    3. State version mismatch rejects confirmation as stale.
    4. Allow decision executes side effect exactly once; duplicate confirm rejected.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _governance_plugin_source(source)
    state = tmp_path / "state"

    with _provider([]) as server:
        _configure(workspace, server, source, approval_mode="yolo")
        token = "confirm-token"

        with _listening_http(workspace, state, token) as base:
            _request(base, "POST", "/api/sessions", token=token, body={"session_id": "confirm-session"})

            # 0. Business args named like decisions are never authority: the
            # command still asks, and nothing is written.
            status, body = _request(
                base,
                "POST",
                "/api/sessions/confirm-session/invoke/gov_confirm_cmd",
                token=token,
                body={"arguments": {"target_file": "bypass_test.txt", "decision": "allow", "allow": True, "confirmed": True}},
            )
            assert status == 200, body.decode()
            assert json.loads(body)["status"] == "pending_confirmation", body.decode()
            assert not (workspace / "bypass_test.txt").exists()

            # 1. Invoke gov_confirm_cmd -> returns pending_confirmation
            status, body = _request(
                base,
                "POST",
                "/api/sessions/confirm-session/invoke/gov_confirm_cmd",
                token=token,
                body={"arguments": {"target_file": "final_confirm.txt"}},
            )
            assert status == 200, body.decode()
            inv_res = json.loads(body)
            assert inv_res["status"] == "pending_confirmation", body.decode()
            req_id = inv_res["confirmation"]["request_id"]
            assert req_id.startswith("req-")
            assert not (workspace / "final_confirm.txt").exists()

            # 2. Deny decision
            status, body = _request(
                base,
                "POST",
                "/api/sessions/confirm-session/confirm",
                token=token,
                body={"request_id": req_id, "decision": "deny"},
            )
            assert status == 200, body.decode()
            deny_res = json.loads(body)
            assert deny_res["status"] == "ok"
            assert not (workspace / "final_confirm.txt").exists()

            # Duplicate confirm on already denied request is rejected
            status, body = _request(
                base,
                "POST",
                "/api/sessions/confirm-session/confirm",
                token=token,
                body={"request_id": req_id, "decision": "allow"},
            )
            assert status in (400, 409)

            # 3. Request a new confirmation, then advance state version before confirming -> stale
            status, body = _request(
                base,
                "POST",
                "/api/sessions/confirm-session/invoke/gov_confirm_cmd",
                token=token,
                body={"arguments": {"target_file": "stale_test.txt"}},
            )
            req2_id = json.loads(body)["confirmation"]["request_id"]

            # Intervene and advance session state version
            _request(
                base,
                "POST",
                "/api/sessions/confirm-session/invoke/gov_write_cmd",
                token=token,
                body={"arguments": {"value": "advance_version"}},
            )

            # Now confirm req2 -> must be rejected as stale
            status, body = _request(
                base,
                "POST",
                "/api/sessions/confirm-session/confirm",
                token=token,
                body={"request_id": req2_id, "decision": "allow"},
            )
            assert status in (400, 409)
            assert not (workspace / "stale_test.txt").exists()

            # 4. Fresh confirmation -> allow -> executes side effect exactly once
            status, body = _request(
                base,
                "POST",
                "/api/sessions/confirm-session/invoke/gov_confirm_cmd",
                token=token,
                body={"arguments": {"target_file": "executed_action.txt"}},
            )
            req3_id = json.loads(body)["confirmation"]["request_id"]

            status, body = _request(
                base,
                "POST",
                "/api/sessions/confirm-session/confirm",
                token=token,
                body={"request_id": req3_id, "decision": "allow"},
            )
            assert status == 200, body.decode()
            assert (workspace / "executed_action.txt").read_text(encoding="utf-8") == "confirmed_effect\n"

            # Duplicate confirm on already allowed request is rejected
            status, body = _request(
                base,
                "POST",
                "/api/sessions/confirm-session/confirm",
                token=token,
                body={"request_id": req3_id, "decision": "allow"},
            )
            assert status in (400, 409)


def test_confirmed_effect_crash_never_replays_and_stays_diagnosable(tmp_path: Path) -> None:
    """Prove the confirmation crash boundary with real processes:
    1. Approval resolves through the actual control path, the handler appends its
       real effect, then the process is killed before the handler returns.
    2. The effect is NOT replayed on a later confirmation attempt.
    3. The outcome is diagnosable rather than a fake completion.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _governance_plugin_source(source)
    state = tmp_path / "state"

    with _provider([]) as server:
        _configure(workspace, server, source, approval_mode="yolo")
        _cli(workspace, state, "create-session", "--session-id", "confirm-crash")

        asked = _cli(
            workspace,
            state,
            "invoke",
            "confirm-crash",
            "gov_confirm_cmd",
            "--arguments",
            '{"target_file": "crash_effect.txt"}',
            code=2,
        )
        assert asked["status"] == "pending_confirmation", asked
        request_id = asked["confirmation"]["request_id"]

        (workspace / "confirm.block").touch()
        proc = subprocess.Popen(
            _command(workspace, state, "confirm", "confirm-crash", request_id, "--decision", "allow"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _await_marker(workspace / "confirm.paused")
            assert (workspace / "crash_effect.txt").read_text(encoding="utf-8") == "confirmed_effect\n"

            proc.kill()
            proc.communicate(timeout=10)
            _await_lease_release(workspace)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=5)

        # Durable truth: approval was recorded, the handler's own commit never ran.
        with SqliteStore(state / "kernel-v1.sqlite3") as store:
            record = store.confirmation(request_id)
            assert record is not None and record.status == "allowed", record
            stored, _ = store.plugin_state(_GOV_PLUGIN_ID, "session", "confirm-crash")
            assert not (stored or {}).get("confirmed_done"), stored

        # Reconciling must not blindly repeat the never-replay effect and must not
        # pretend the action completed; the operator sees the unresolved truth.
        (workspace / "confirm.block").unlink()
        retry = _cli(workspace, state, "confirm", "confirm-crash", request_id, "--decision", "allow", code=1)
        assert "not pending" in retry["error"], retry
        assert (workspace / "crash_effect.txt").read_text(encoding="utf-8") == "confirmed_effect\n"

        with SqliteStore(state / "kernel-v1.sqlite3") as store:
            stored_after, _ = store.plugin_state(_GOV_PLUGIN_ID, "session", "confirm-crash")
            assert not (stored_after or {}).get("confirmed_done"), stored_after


def _kill_between_first_append_and_caller(workspace: Path, state: Path, *run_arguments: str) -> subprocess.Popen[str]:
    """SIGKILL the kernel the instant its first provider turn is durably appended.

    The kill is triggered from inside the observing callback that runs directly
    after that append, so the process dies after the source commit and before the
    caller that requested the turn ever regains control: a real crash boundary,
    not a simulated one.
    """
    (workspace / "observer.kill").touch()
    proc = subprocess.Popen(
        _command(workspace, state, *run_arguments),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        proc.communicate(timeout=30)
        assert proc.returncode == -signal.SIGKILL, (
            f"kernel was expected to die by SIGKILL at the durable boundary, got {proc.returncode}: stdout={proc.stdout} stderr={proc.stderr}"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=10)
        _await_lease_release(workspace)
    (workspace / "observer.kill").unlink()
    return proc


def test_crash_after_turn_append_resumes_the_authentic_native_batch(tmp_path: Path) -> None:
    """Prove the checkpoint-reference crash boundary with real processes:
    1. A provider turn with a tool call is appended, then the kernel is killed
       before its caller regains control (inside the observing callback).
    2. The durable checkpoint references that turn in the SAME transaction, so a
       fresh process resumes the authentic native batch instead of refusing it.
    3. The never-replay effect lands exactly once and no dispatch is billed twice.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _governance_plugin_source(source)
    state = tmp_path / "state"

    replies = [
        _turn(_call("m1", "gov_mutate", marker="crash_batch.txt")),
        _turn(output="resumed after the crash"),
    ]
    with _provider(replies) as server:
        _configure(workspace, server, source, approval_mode="yolo")
        _cli(workspace, state, "create-session", "--session-id", "crash-batch")

        _kill_between_first_append_and_caller(workspace, state, "run", "billed prompt", "--session-id", "crash-batch")

        # Durable truth: the turn and its member list are billed and referenced by
        # one checkpoint, and the effect did not happen yet.
        with SqliteStore(state / "kernel-v1.sqlite3") as store:
            events = store.events("crash-batch")
            turn = next(event for event in events if event.event_type == "kernel.model_turn")
            checkpoint = store.checkpoint("crash-batch")
            assert checkpoint is not None
            assert checkpoint.kind == "ready"
            assert checkpoint.payload["batch_sequence"] == turn.sequence, checkpoint.payload
            assert checkpoint.payload["next_call"] == 0, checkpoint.payload
            assert store.session("crash-batch").status == "running"
            intents = [event for event in events if event.event_type == "kernel.provider_intent"]
            assert len(intents) == 1, [(event.sequence, event.event_type) for event in events]
        assert not (workspace / "crash_batch.txt").exists()

        resumed = _cli(workspace, state, "resume", "crash-batch")
        assert resumed["status"] == "completed", resumed
        assert resumed["output"] == "resumed after the crash", resumed

    # One bill before and one after the crash: the authentic prefix was re-entered,
    # never re-dispatched, and the irreversible effect landed exactly once.
    assert len(server.requests) == 2, server.requests
    assert (workspace / "crash_batch.txt").read_text(encoding="utf-8") == "mutated\n"
    with SqliteStore(state / "kernel-v1.sqlite3") as store:
        intents = [event for event in store.events("crash-batch") if event.event_type == "kernel.provider_intent"]
        assert len(intents) == 2, [(event.sequence, event.payload["dispatch_id"]) for event in intents]
        assert len({event.payload["dispatch_id"] for event in intents}) == 2


def test_crash_after_approval_append_keeps_the_native_ask(tmp_path: Path) -> None:
    """Prove the approval checkpoint reference survives a real process kill:
    1. The ask is durable (event + referencing checkpoint in one transaction).
    2. The kernel is killed while that ask is still pending: the exact request id
       and a decision are still required, a bare resume cannot bypass it.
    3. Denying it executes nothing; the session stays usable.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _governance_plugin_source(source)
    state = tmp_path / "state"

    replies = [
        _turn(_call("w1", "gov_mutate", marker="crash_ask.txt")),
        _turn(output="completed after the denied ask"),
    ]
    with _provider(replies) as server:
        _configure(workspace, server, source, approval_mode="ask")
        _cli(workspace, state, "create-session", "--session-id", "crash-ask")

        proc = subprocess.Popen(
            _command(workspace, state, "run", "ask then die", "--session-id", "crash-ask"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            # Terminate the kernel as soon as the ask is durable, i.e. before any
            # approval decision could exist.
            database = state / "kernel-v1.sqlite3"
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    probe = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
                except sqlite3.Error:
                    time.sleep(0.01)
                    continue
                try:
                    row = probe.execute("SELECT count(*) FROM session_events WHERE event_type = 'kernel.approval_requested'").fetchone()
                finally:
                    probe.close()
                if row is not None and row[0] == 1:
                    break
                time.sleep(0.005)
            else:
                raise TimeoutError("approval request never became durable")
            proc.kill()
            proc.communicate(timeout=10)
            _await_lease_release(workspace)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=10)

        with SqliteStore(state / "kernel-v1.sqlite3") as store:
            events = store.events("crash-ask")
            approval = next(event for event in events if event.event_type == "kernel.approval_requested")
            request_id = approval.payload["request_id"]
            checkpoint = store.checkpoint("crash-ask")
            assert checkpoint is not None
            assert checkpoint.kind == "approval_wait", checkpoint.payload
            assert checkpoint.payload["approval_sequence"] == approval.sequence, checkpoint.payload
            assert checkpoint.payload["batch_sequence"] == approval.payload["batch_sequence"], checkpoint.payload
            assert store.session("crash-ask").status == "waiting"
            assert len([event for event in events if event.event_type == "kernel.provider_intent"]) == 1
        assert not (workspace / "crash_ask.txt").exists()

        # The ask cannot be bypassed: a bare resume refuses, and the exact identity
        # with a deny decision resolves it without executing anything.
        refused = _cli(workspace, state, "resume", "crash-ask", code=1)
        assert "exact pending request identity and decision" in refused["error"], refused
        denied = _cli(workspace, state, "resume", "crash-ask", "--approval-request-id", request_id, "--decision", "deny")
        assert denied["status"] == "completed", denied
        assert denied["output"] == "completed after the denied ask", denied

    assert not (workspace / "crash_ask.txt").exists()
    assert len(server.requests) == 2, server.requests


def test_two_concurrent_keyed_leases_return_one_durable_continuation(tmp_path: Path) -> None:
    """Prove keyed continuation identity with two real concurrent leases:
    both SDK commands stage their enqueue for the same session/owner/key before
    either commits, both return the SAME id, and that id names exactly one
    durable continuation row (never a fake id for a silently deduped write).
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _governance_plugin_source(source)
    state = tmp_path / "state"

    replies = [
        _turn(_call("k1", "gov_mutate", marker="keyed_run.txt", block=True)),
        _turn(output="run finished while the keyed work waited"),
        _turn(output="keyed continuation ran"),
    ]
    with _provider(replies) as server:
        _configure(workspace, server, source, approval_mode="yolo")
        _cli(workspace, state, "create-session", "--session-id", "keyed-session")

        token = "keyed-token"
        with _listening_http(workspace, state, token) as base:
            run_result: dict[str, Any] = {}

            def _drive_run() -> None:
                status, body = _request(base, "POST", "/api/sessions/keyed-session/run", token=token, body={"prompt": "hold"})
                run_result["payload"] = json.loads(body.decode())

            worker = threading.Thread(target=_drive_run, daemon=True)
            worker.start()
            _await_marker(workspace / "host.pid")

            responses: list[dict[str, Any]] = []

            def _invoke_keyed(tag: str) -> None:
                status, body = _request(
                    base,
                    "POST",
                    "/api/sessions/keyed-session/invoke/gov_keyed_queue_cmd",
                    token=token,
                    body={"arguments": {"key": "shared-key", "tag": tag, "block": True}},
                )
                assert status == 200, body.decode()
                responses.append(json.loads(body))

            # Two leases for the SAME session/owner/key both stage their enqueue
            # before either one is allowed to return and commit.
            tags = ("lease-a", "lease-b")
            threads = [threading.Thread(target=_invoke_keyed, args=(tag,), daemon=True) for tag in tags]
            for thread in threads:
                thread.start()
            for tag in tags:
                _await_marker(workspace / f"keyed.{tag}.entered")
            for tag in tags:
                (workspace / f"keyed.{tag}.release").touch()
            for thread in threads:
                thread.join(timeout=_STARTUP_DEADLINE_SECONDS)
                assert not thread.is_alive()

            assert [response["status"] for response in responses] == ["ok", "ok"], responses
            returned = {response["data"]["continuation_id"] for response in responses}
            assert len(returned) == 1, responses
            continuation_id = next(iter(returned))
            assert continuation_id

            # Both returned ids name exactly one durable row for that key.
            database = state / "kernel-v1.sqlite3"
            with sqlite3.connect(database) as conn:
                rows = conn.execute(
                    "SELECT continuation_id, status FROM continuations WHERE session_id = ? AND owner = ? AND idempotency_key = ?",
                    ("keyed-session", _GOV_PLUGIN_ID, "shared-key"),
                ).fetchall()
            assert rows == [(continuation_id, "pending")], rows

            (workspace / "release.txt").touch()
            worker.join(timeout=_STARTUP_DEADLINE_SECONDS)
            assert not worker.is_alive()
            assert run_result["payload"]["status"] == "completed", run_result

    # The single durable row was driven once, by the run's own auto-drain.
    assert len(server.requests) == 3, server.requests
    with sqlite3.connect(state / "kernel-v1.sqlite3") as conn:
        rows = conn.execute(
            "SELECT continuation_id, status FROM continuations WHERE session_id = ? AND owner = ?",
            ("keyed-session", _GOV_PLUGIN_ID),
        ).fetchall()
    assert rows == [(continuation_id, "completed")], rows


def test_cancelled_finite_root_admits_a_fresh_authorized_root(tmp_path: Path) -> None:
    """Prove explicit cancellation settles recoverable parked work (not only an
    approval wait): a root-exhausted interrupted session is cancelled, and a
    fresh same-session action then gets a NEW root of its own while the old
    exhausted root keeps its reservations.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _governance_plugin_source(source)
    state = tmp_path / "state"
    (workspace / "input.txt").write_text("root input\n", encoding="utf-8")

    replies = [
        _turn(_call("b1", "gov_read", path="input.txt")),
        _turn(_call("b2", "gov_read", path="input.txt")),
        _turn(output="fresh root turn"),
    ]
    with _provider(replies) as server:
        _configure(workspace, server, source, approval_mode="yolo")
        _cli(workspace, state, "create-session", "--session-id", "root-cancel")

        burned = _cli(workspace, state, "run", "burn the root", "--session-id", "root-cancel", "--max-steps", "2", code=130)
        assert burned["stop_cause"] == "limit_reached", burned
        assert len(server.requests) == 2, server.requests

        with SqliteStore(state / "kernel-v1.sqlite3") as store:
            checkpoint = store.checkpoint("root-cancel")
            assert checkpoint is not None and checkpoint.kind != "terminal"
            old_root = str(checkpoint.payload["root_id"])
            assert old_root
            assert store.root_budget_state(old_root) == (2, 2), store.root_budget_state(old_root)

        # Parked work: a fresh turn on the same session is refused before the cancel.
        refused = _cli(workspace, state, "run", "fresh", "--session-id", "root-cancel", code=1)
        assert "unfinished session requires explicit resume" in refused["error"], refused

        token = "root-cancel-token"
        with _listening_http(workspace, state, token) as base:
            status, body = _request(base, "POST", "/api/sessions/root-cancel/cancel", token=token)
            assert status == 200, body.decode()
            assert json.loads(body)["cancellation_requested"] is True, body.decode()

        with SqliteStore(state / "kernel-v1.sqlite3") as store:
            assert store.session("root-cancel").status == "interrupted"
            assert store.checkpoint("root-cancel").kind == "terminal"
            # Settled to a terminal checkpoint: nothing is left to claim or resume.
            assert store.pending_continuations("root-cancel") == ()
            assert store.claimed_continuations("root-cancel") == ()

        drained = _cli(workspace, state, "drain", "root-cancel")
        assert drained["continuations"] == [], drained

        fresh = _cli(workspace, state, "run", "fresh authorized root", "--session-id", "root-cancel", "--max-steps", "4")
        assert fresh["status"] == "completed", fresh
        assert fresh["output"] == "fresh root turn", fresh

    assert len(server.requests) == 3, server.requests
    with SqliteStore(state / "kernel-v1.sqlite3") as store:
        new_root = str(store.checkpoint("root-cancel").payload["root_id"])
        assert new_root and new_root != old_root, (new_root, old_root)
        assert store.root_budget_state(old_root) == (2, 2), store.root_budget_state(old_root)
        assert store.root_budget_state(new_root) == (4, 1), store.root_budget_state(new_root)


def test_terminal_interruption_auto_drives_queued_continuation(tmp_path: Path) -> None:
    """Prove the shared fresh-work admission predicate: work queued by a command
    on a session that was cancelled at a terminal checkpoint is driven
    automatically with a real provider turn, exactly like an idle session.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _governance_plugin_source(source)
    state = tmp_path / "state"
    (workspace / "input.txt").write_text("terminal input\n", encoding="utf-8")

    replies = [
        _turn(_call("t1", "gov_read", path="input.txt")),
        _turn(output="queued work after terminal cancel"),
    ]
    with _provider(replies) as server:
        _configure(workspace, server, source, approval_mode="yolo")
        _cli(workspace, state, "create-session", "--session-id", "terminal-cancel")

        burned = _cli(workspace, state, "run", "burn one step", "--session-id", "terminal-cancel", "--max-steps", "1", code=130)
        assert burned["stop_cause"] == "limit_reached", burned

        token = "terminal-cancel-token"
        with _listening_http(workspace, state, token) as base:
            status, body = _request(base, "POST", "/api/sessions/terminal-cancel/cancel", token=token)
            assert status == 200, body.decode()
            assert json.loads(body)["cancellation_requested"] is True, body.decode()

            queued = _request(
                base,
                "POST",
                "/api/sessions/terminal-cancel/invoke/gov_queue_cmd",
                token=token,
                body={"arguments": {"note": "after terminal cancel"}},
            )
            assert queued[0] == 200, queued[1].decode()
            assert json.loads(queued[1])["status"] == "ok", queued[1].decode()

            # The kernel drove the queued continuation itself, through a real turn.
            deadline = time.monotonic() + 20
            while len(server.requests) < 2 and time.monotonic() < deadline:
                time.sleep(0.02)

    assert len(server.requests) == 2, server.requests
    driven = json.dumps(server.requests[1]["messages"], ensure_ascii=False)
    assert "queued_followup" in driven and "after terminal cancel" in driven, driven
    with SqliteStore(state / "kernel-v1.sqlite3") as store:
        assert store.pending_continuations("terminal-cancel") == ()
        assert store.claimed_continuations("terminal-cancel") == ()
        events = store.events("terminal-cancel")
        started = next(event for event in events if event.event_type == "kernel.continuation_started")
        assert started.run_id != burned["run_id"], (started.run_id, burned["run_id"])
        assert [event.event_type for event in events].count("kernel.continuation_started") == 1
        intents = [event for event in events if event.event_type == "kernel.provider_intent"]
        assert [event.run_id for event in intents] == [burned["run_id"], started.run_id], intents


def test_two_owner_partial_receipt_delivers_only_the_missing_owner(tmp_path: Path) -> None:
    """Prove per-owner observer receipt authority with TWO real observing plugins:
    given a durable partial receipt state (one owner already receipted for an
    event, the other missing), a fresh process delivers the fact to exactly the
    missing owner: the receipted owner's callback is never re-invoked, and the
    late owner receives the original logical turn, run, and revision identity.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    observer_source = tmp_path / "external-observer"
    _governance_plugin_source(source)
    _observer_plugin_source(observer_source)
    state = tmp_path / "state"
    trust = (_GOV_PLUGIN_ID, _OBSERVER_PLUGIN_ID)

    replies = [_turn(output="observed once by both owners")]
    with _provider(replies) as server:
        _configure(workspace, server, source, observer_source=observer_source, approval_mode="yolo")
        _cli(workspace, state, "create-session", "--session-id", "two-owner", trust=trust)
        first = _cli(workspace, state, "run", "observe me", "--session-id", "two-owner", trust=trust)
        assert first["status"] == "completed", first

        with SqliteStore(state / "kernel-v1.sqlite3") as store:
            turn = next(event for event in store.events("two-owner") if event.event_type == "kernel.model_turn")
            dispatch = next(event for event in store.events("two-owner") if event.event_type == "kernel.provider_intent")
            assert store.has_observer_receipt("two-owner", turn.sequence, _GOV_PLUGIN_ID)
            assert store.has_observer_receipt("two-owner", turn.sequence, _OBSERVER_PLUGIN_ID)
            first_owner_state, _ = store.plugin_state(_GOV_PLUGIN_ID, "session", "two-owner")
            assert first_owner_state is not None
            assert first_owner_state["usage_facts_seen"] == 1, first_owner_state

        # Durable partial state: the late owner's receipt for that exact event is
        # gone (as an interrupted delivery would leave it), the first owner's is not.
        with sqlite3.connect(state / "kernel-v1.sqlite3") as conn:
            conn.execute(
                "DELETE FROM observer_receipts WHERE session_id = ? AND event_sequence = ? AND owner = ?",
                ("two-owner", turn.sequence, _OBSERVER_PLUGIN_ID),
            )
            conn.commit()

        # Two fresh processes: the missing owner is delivered the fact once, the
        # receipted owner is not called again, and the receipt prevents a repeat.
        for _ in range(2):
            snapshot = _cli(workspace, state, "invoke", "two-owner", "gov_read_cmd", trust=trust)
            assert snapshot["status"] == "ok", snapshot

        with SqliteStore(state / "kernel-v1.sqlite3") as store:
            late_state, _ = store.plugin_state(_OBSERVER_PLUGIN_ID, "session", "two-owner")
            assert late_state is not None
            assert late_state["usage_facts_seen"] == 2, late_state
            # The late owner received the ORIGINAL fact identity, not a rewritten one.
            assert late_state["last_event_sequence"] == turn.sequence, late_state
            assert late_state["last_dispatch_sequence"] == dispatch.sequence, late_state
            assert late_state["last_session_id"] == "two-owner", late_state
            assert late_state["last_run_id"] == turn.run_id, late_state
            assert late_state["last_revision_id"] == turn.revision_id, late_state
            assert late_state["last_logical_turn"] == dispatch.payload["logical_turn"], late_state

    with SqliteStore(state / "kernel-v1.sqlite3") as store:
        owner_state, _ = store.plugin_state(_GOV_PLUGIN_ID, "session", "two-owner")
        assert owner_state is not None
        # The receipted owner's own callback never ran again.
        assert owner_state["usage_facts_seen"] == 1, owner_state
        assert store.has_observer_receipt("two-owner", turn.sequence, _OBSERVER_PLUGIN_ID)
        late_events = [event.event_type for event in store.events("two-owner") if event.event_type.startswith(f"plugin.{_OBSERVER_PLUGIN_ID}.")]
        assert len(late_events) == 2, late_events


def test_confirmed_owner_cancel_stops_claimed_run(tmp_path: Path) -> None:
    """Prove a confirmed action routes its owner cancellation like an ordinary invoke:
    the command asks first, and only the confirmed (user) invocation cancels the
    owner's claimed run - which stops as owner_cancel without a further bill.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _governance_plugin_source(source)
    state = tmp_path / "state"

    replies = [
        _turn(_call("c1", "gov_mutate", marker="confirm_cancel.txt", block=True)),
        _turn(output="must never be requested"),
    ]
    with _provider(replies) as server:
        _configure(workspace, server, source, approval_mode="yolo")
        _cli(workspace, state, "create-session", "--session-id", "confirm-cancel-session")
        with SqliteStore(state / "kernel-v1.sqlite3") as store:
            store.enqueue_continuation(
                "confirm-claim",
                _GOV_PLUGIN_ID,
                "confirm-cancel-session",
                "plugin.gov-plugin",
                "run-init",
                store.session("confirm-cancel-session").active_revision_id,
                {"action": "confirm_cancel"},
            )

        token = "confirm-cancel-token"
        with _listening_http(workspace, state, token) as base:
            drained: dict[str, Any] = {}

            def _drive() -> None:
                status, body = _request(base, "POST", "/api/sessions/confirm-cancel-session/continuations", token=token, body={"limit": 1})
                drained["payload"] = json.loads(body.decode())

            worker = threading.Thread(target=_drive, daemon=True)
            worker.start()
            _await_marker(workspace / "confirm_cancel.txt")

            asked = _request(
                base,
                "POST",
                "/api/sessions/confirm-cancel-session/invoke/gov_confirm_cancel_cmd",
                token=token,
                body={"arguments": {}},
            )
            assert asked[0] == 200, asked[1].decode()
            pending = json.loads(asked[1])
            assert pending["status"] == "pending_confirmation", pending
            request_id = pending["confirmation"]["request_id"]
            # Asking alone must not cancel the owner's run.
            time.sleep(0.5)
            assert worker.is_alive(), "an unconfirmed ask cancelled the owner's run"

            confirmed = _request(
                base,
                "POST",
                "/api/sessions/confirm-cancel-session/confirm",
                token=token,
                body={"request_id": request_id, "decision": "allow"},
            )
            assert confirmed[0] == 200, confirmed[1].decode()
            assert json.loads(confirmed[1])["status"] == "ok", confirmed[1].decode()

            worker.join(timeout=_STARTUP_DEADLINE_SECONDS)
            assert not worker.is_alive()
            claims = drained["payload"]["continuations"]
            assert [item["status"] for item in claims] == ["cancelled"], claims
            assert claims[0]["stop_cause"] == "owner_cancel", claims

    assert len(server.requests) == 1, server.requests
    with SqliteStore(state / "kernel-v1.sqlite3") as store:
        assert store.continuation_status("confirm-claim") == "cancelled"


@pytest.mark.parametrize("policy", ["read-only", "ask"])
def test_internal_session_effects_need_no_read_only_waiver_or_approval(tmp_path: Path, policy: str) -> None:
    """Prove internal SESSION state operations are classified as non-external work:
    a real agentic goal flow (native create_goal/update_goal) reaches a completed
    goal with no approval wait and no filesystem mutation, while a real WRITE tool
    is still denied under read_only and still waits under ask.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    goal_source = Path(__file__).resolve().parents[2] / "src" / "voidcode" / "plugins" / "goal"

    replies = [
        _turn(_call("g1", "create_goal", objective="ship the read-only goal vertical")),
        _turn(_call("g2", "update_goal", status="complete")),
        _turn(output="goal completed"),
        _turn(_call("w1", "write", path="gated.txt", content="effect\n")),
        _turn(output="write path settled"),
    ]
    with _provider(replies) as server:
        config: dict[str, object] = {
            "schema_version": 1,
            "model": {"model": "endpoint/exact-model"},
            "policy": {"approval_mode": "ask", "read_only": policy == "read-only"},
            "providers": {"endpoint": {"api_key": "k", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
            "plugins": {"goal": {"path": str(goal_source)}},
        }
        (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
        trust = ("goal",)
        _cli(workspace, state, "create-session", "--session-id", "session-effects-goal", trust=trust)
        _cli(workspace, state, "create-session", "--session-id", "session-effects-write", trust=trust)

        goal_run = _cli(workspace, state, "run", "pursue the explicit goal", "--session-id", "session-effects-goal", trust=trust)
        assert goal_run["status"] == "completed", goal_run
        assert goal_run["pending_approval"] is None, goal_run
        assert goal_run["output"] == "goal completed", goal_run

        write_run = _cli(workspace, state, "run", "write a file", "--session-id", "session-effects-write", trust=trust)

    with SqliteStore(state / "kernel-v1.sqlite3") as store:
        goal_state, _ = store.plugin_state("goal", "session", "session-effects-goal")
        assert goal_state is not None and goal_state["status"] == "complete", goal_state
        goal_events = [event.event_type for event in store.events("session-effects-goal")]
        assert "kernel.tool_intent" in goal_events, goal_events
        assert "plugin.goal.goal_created" in goal_events and "plugin.goal.goal_completed" in goal_events, goal_events
        assert "kernel.approval_requested" not in goal_events, goal_events
        write_events = [event.event_type for event in store.events("session-effects-write")]

    # Internal state work never touched the workspace, and the external write did
    # not happen without the decision this policy requires.
    assert not (workspace / "gated.txt").exists()
    assert sorted(path.name for path in workspace.iterdir()) == ["voidcode.json"]
    if policy == "read-only":
        assert write_run["status"] == "completed", write_run
        assert write_run["pending_approval"] is None, write_run
        assert "kernel.tool_intent" not in write_events, write_events
        assert "kernel.approval_requested" not in write_events, write_events
    else:
        assert write_run["status"] == "waiting", write_run
        assert write_run["pending_approval"] is not None, write_run
        assert write_run["pending_approval"]["call"]["tool_name"] == "write", write_run
    # read-only reports the denial and finishes its turn; ask parks after one
    # dispatch, so the write session bills one turn, not two.
    assert len(server.requests) == (5 if policy == "read-only" else 4), server.requests


@pytest.mark.parametrize("case", ["foreign", "own-targeted", "own-unscoped"])
def test_native_tool_cancellation_is_scoped_to_its_owner(tmp_path: Path, case: str) -> None:
    """Prove a native tool's staged cancellation only scopes work its OWN owner owns.

    Two real owners on an actual claimed native run: a foreign tool naming this
    claim's id (and a foreign unscoped cancellation) neither stops the run nor its
    row nor suppresses the next bill, while the claim's own owner stopping its own
    claim does stop it as owner_cancel.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    observer_source = tmp_path / "external-observer"
    _governance_plugin_source(source)
    _observer_plugin_source(observer_source)
    state = tmp_path / "state"
    trust = (_GOV_PLUGIN_ID, _OBSERVER_PLUGIN_ID)
    claim_id = f"native-claim-{case}"

    if case == "foreign":
        replies = [
            _turn(
                _call("x1", "obs2_native_cancel", continuation_id=claim_id),
                _call("x2", "obs2_native_cancel"),
            ),
            _turn(output="victim survived foreign native cancels"),
        ]
    elif case == "own-targeted":
        replies = [
            _turn(_call("o1", "gov_self_cancel", continuation_id=claim_id)),
            _turn(output="must never be requested"),
        ]
    else:
        replies = [
            _turn(_call("o1", "gov_self_cancel")),
            _turn(output="must never be requested"),
        ]

    with _provider(replies) as server:
        _configure(workspace, server, source, observer_source=observer_source, approval_mode="yolo")
        session_id = f"native-scope-{case}"
        _cli(workspace, state, "create-session", "--session-id", session_id, trust=trust)
        with SqliteStore(state / "kernel-v1.sqlite3") as store:
            store.enqueue_continuation(
                claim_id,
                _GOV_PLUGIN_ID,
                session_id,
                "plugin.gov-plugin",
                "run-init",
                store.session(session_id).active_revision_id,
                {"action": "native_scope"},
            )

        token = f"native-scope-token-{case}"
        with _listening_http(workspace, state, token, trust=trust) as base:
            drained: dict[str, Any] = {}

            def _drive() -> None:
                status, body = _request(base, "POST", f"/api/sessions/{session_id}/continuations", token=token, body={"limit": 1})
                drained["payload"] = json.loads(body.decode())

            worker = threading.Thread(target=_drive, daemon=True)
            worker.start()
            worker.join(timeout=25)
            assert not worker.is_alive(), drained
            claims = drained["payload"]["continuations"]
            assert len(claims) == 1, claims

    with SqliteStore(state / "kernel-v1.sqlite3") as store:
        row_status = store.continuation_status(claim_id)
        events = [event.event_type for event in store.events(session_id)]

    if case == "foreign":
        # The foreign cancellations were no-ops: the claim ran to completion and the
        # next provider turn was still billed.
        assert claims[0]["run_status"] == "completed", claims
        assert row_status == "completed", row_status
        assert len(server.requests) == 2, server.requests
        assert "kernel.run_interrupted" not in events, events
    else:
        # The claim's own owner stopped its own claim: no next turn was billed.
        assert claims[0]["run_status"] == "interrupted", claims
        assert claims[0]["stop_cause"] == "owner_cancel", claims
        assert row_status == "cancelled", row_status
        assert len(server.requests) == 1, server.requests
        assert "kernel.run_interrupted" in events, events


def test_context_failure_finishes_the_run_failed_without_a_dispatch(tmp_path: Path) -> None:
    """Prove a failing context contributor is a critical admission failure:
    the run durably finishes failed with no provider intent or bill (never left
    running with no driver, never dispatched without its context), and the same
    session admits a fresh turn once context works again.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _governance_plugin_source(source)
    state = tmp_path / "state"

    replies = [_turn(output="context recovered")]
    with _provider(replies) as server:
        _configure(workspace, server, source, approval_mode="yolo")
        _cli(workspace, state, "create-session", "--session-id", "context-fail")

        (workspace / "context.fail").touch()
        failed = _cli(workspace, state, "run", "context must fail", "--session-id", "context-fail", code=1)
        assert failed["status"] == "failed", failed
        assert failed["stop_cause"] == "error", failed
        assert failed["error"], failed
        assert len(server.requests) == 0, server.requests

        with SqliteStore(state / "kernel-v1.sqlite3") as store:
            assert store.session("context-fail").status == "failed"
            checkpoint = store.checkpoint("context-fail")
            assert checkpoint is not None and checkpoint.kind == "terminal"
            events = [event.event_type for event in store.events("context-fail")]
            assert "kernel.run_failed" in events, events
            assert "kernel.provider_intent" not in events, events

        (workspace / "context.fail").unlink()
        fresh = _cli(workspace, state, "run", "context works now", "--session-id", "context-fail")
        assert fresh["status"] == "completed", fresh
        assert fresh["output"] == "context recovered", fresh

    assert len(server.requests) == 1, server.requests


def test_replayed_run_finished_fact_keeps_its_measured_elapsed(tmp_path: Path) -> None:
    """Prove run-finished timing identity: the fact carries the duration the
    originating process measured and persisted, so a later process delivering
    that same fact again (after real downtime) reports the identical elapsed,
    never a re-measured wall clock and never a fabricated cause.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _governance_plugin_source(source)
    state = tmp_path / "state"

    replies = [_turn(output="timed turn")]
    with _provider(replies) as server:
        _configure(workspace, server, source, approval_mode="yolo")
        _cli(workspace, state, "create-session", "--session-id", "timing-session")
        first = _cli(workspace, state, "run", "time this run", "--session-id", "timing-session")
        assert first["status"] == "completed", first

        with SqliteStore(state / "kernel-v1.sqlite3") as store:
            events = store.events("timing-session")
            finished = next(event for event in events if event.event_type == "kernel.run_completed")
            original_ms = finished.payload["elapsed_ms"]
            owner_state, _ = store.plugin_state(_GOV_PLUGIN_ID, "session", "timing-session")
            assert owner_state is not None
            assert owner_state["run_finished_seen"] == 1, owner_state
            assert owner_state["last_run_elapsed_seconds"] == original_ms / 1000.0, owner_state
            assert owner_state["last_run_stop_cause"] == "completed", owner_state
            assert store.has_observer_receipt("timing-session", finished.sequence, _GOV_PLUGIN_ID)

        # Durable partial delivery state, then real downtime before the replay.
        with sqlite3.connect(state / "kernel-v1.sqlite3") as conn:
            conn.execute(
                "DELETE FROM observer_receipts WHERE session_id = ? AND event_sequence = ? AND owner = ?",
                ("timing-session", finished.sequence, _GOV_PLUGIN_ID),
            )
            conn.commit()
        time.sleep(2.0)
        _cli(workspace, state, "invoke", "timing-session", "gov_read_cmd")

        with SqliteStore(state / "kernel-v1.sqlite3") as store:
            replayed, _ = store.plugin_state(_GOV_PLUGIN_ID, "session", "timing-session")
            assert replayed is not None
            assert replayed["run_finished_seen"] == 2, replayed
            assert replayed["last_run_status"] == "completed", replayed
            assert replayed["last_run_stop_cause"] == "completed", replayed
            # Identical to the persisted measurement, not the replay's wall clock.
            assert replayed["last_run_elapsed_seconds"] == original_ms / 1000.0, (replayed, original_ms)


def _die_with_unsettled_dispatch(workspace: Path, state: Path, session_id: str, server: _GatedLoopbackServer) -> None:
    """Start a real run, wait until its provider call is in flight, and SIGKILL it.

    The dispatch intent is durable before the native call, so the dead process
    leaves an unsettled dispatch with no measured provider duration while the
    provider request itself really happened.
    """
    proc = subprocess.Popen(
        _command(workspace, state, "run", "dispatch then die", "--session-id", session_id),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        server.await_entry()
        assert len(server.arrivals) == 1, server.arrivals
        proc.kill()
        proc.communicate(timeout=15)
        _await_lease_release(workspace)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=10)


def test_observer_budget_is_settled_before_the_next_dispatch(tmp_path: Path) -> None:
    """Prove unsettled dispatches settle before the next admission check:
    a real dead-process dispatch is settled exactly once (its unknown-usage fact
    reaches the observing owner), that owner caps itself from the fact it just
    observed, and the dispatch that follows is refused - with no new provider
    request and no second bill.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _governance_plugin_source(source)
    state = tmp_path / "state"

    replies = [_turn(output="must never be dispatched")]
    with _gated_provider(replies) as server:
        _configure(workspace, server, source, approval_mode="yolo")
        _cli(workspace, state, "create-session", "--session-id", "settled-budget")

        _die_with_unsettled_dispatch(workspace, state, "settled-budget", server)

        with SqliteStore(state / "kernel-v1.sqlite3") as store:
            events = store.events("settled-budget")
            assert [event.event_type for event in events].count("kernel.provider_intent") == 1
            assert not any(event.event_type == "kernel.model_turn" for event in events)
            assert store.session("settled-budget").status == "running"

        # The observer caps its owner on the fact it observes while that dead
        # process dispatch is settled.
        (workspace / "observer.zerobudget").touch()
        report = _cli(workspace, state, "usage", "settled-budget")
        assert report["elapsed_seconds"] is None, report
        assert report["has_unknown_usage"] is True, report

        refused = _cli(workspace, state, "resume", "settled-budget", code=1)
        assert refused["status"] == "failed", refused
        assert "budget limit reached" in refused["error"], refused

    # The cap was honored before any native dispatch: still exactly one bill.
    assert len(server.arrivals) == 1, server.arrivals
    with SqliteStore(state / "kernel-v1.sqlite3") as store:
        budgets = store.session_budgets("settled-budget")
        assert [budget.owner for budget in budgets] == [_GOV_PLUGIN_ID], budgets
        assert budgets[0].max_total_tokens == 0, budgets
        owner_state, _ = store.plugin_state(_GOV_PLUGIN_ID, "session", "settled-budget")
        assert owner_state is not None
        # The settled fact really reached the observer, whose cap then blocked the run.
        assert owner_state["usage_facts_seen"] == 1, owner_state


def test_unknown_elapsed_budget_stops_before_any_provider_dispatch(tmp_path: Path) -> None:
    """Prove an elapsed cap with an unknown measured duration fails visibly:
    after a dead-process dispatch leaves elapsed unknown, a max_elapsed_seconds
    owner cap refuses the next dispatch instead of admitting it fail-open.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _governance_plugin_source(source)
    state = tmp_path / "state"

    replies = [_turn(output="must never be dispatched")]
    with _gated_provider(replies) as server:
        _configure(workspace, server, source, approval_mode="yolo")
        _cli(workspace, state, "create-session", "--session-id", "unknown-elapsed")

        _die_with_unsettled_dispatch(workspace, state, "unknown-elapsed", server)

        capped = _cli(
            workspace,
            state,
            "invoke",
            "unknown-elapsed",
            "gov_budget_cmd",
            "--arguments",
            json.dumps({"since_sequence": 0, "max_elapsed_seconds": 30.0}),
        )
        assert capped["status"] == "ok", capped

        refused = _cli(workspace, state, "resume", "unknown-elapsed", code=1)
        assert refused["status"] == "failed", refused
        assert "elapsed time unknown" in refused["error"], refused

    assert len(server.arrivals) == 1, server.arrivals
    with SqliteStore(state / "kernel-v1.sqlite3") as store:
        events = store.events("unknown-elapsed")
        assert [event.event_type for event in events].count("kernel.provider_intent") == 1
        assert not any(event.event_type == "kernel.model_turn" for event in events)
