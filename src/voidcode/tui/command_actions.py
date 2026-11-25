"""Extracted CoreTuiApp behavior: command actions.

Methods are applied to the app through RuntimeActions mixins; the facade stays in
terminal_app.py and owns composition, not these responsibilities.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from voidcode.sdk.client import (
    ClientCapability,
    CommandCatalog,
    CommandResult,
    CoreClientError,
    RunSpec,
)
from voidcode.sdk.commands import CommandDescriptor
from voidcode.sdk.tools import (
    ToolEffect,
)
from voidcode.tui.gates import (
    approval_gate_from_payload,
)

from . import catalog, events, operations
from .attachments import (
    PendingImage,
)
from .formatting import (
    command_result_rows as _command_result_rows,
)
from .formatting import (
    levels_text as _levels_text,
)
from .formatting import (
    model_segment as _model_segment,
)
from .formatting import (
    safe as _safe,
)
from .formatting import (
    shortcut_bindings as _shortcut_bindings,
)
from .formatting import (
    slash_public_path as _slash_public_path,
)
from .help_view import TUI_CONTROLS_BY_NAME, render_help
from .rendering import QUIT_ARM_SECONDS
from .views import (
    InteractiveConfirmation,
    InteractiveForm,
)

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from .terminal_app import CoreTuiApp


class CommandActions:
    def _client_syntax_command(self: CoreTuiApp, line: str) -> tuple[CommandDescriptor, str] | None:
        """Match a non-slash catalog prefix (``!`` for shell), never a client table."""
        descriptors = catalog.get(self)
        if not self.catalog_revision:
            catalog.unavailable(self)
            return None
        for descriptor in descriptors:
            syntax = descriptor.client_syntax
            if syntax and not syntax.startswith("/") and line.startswith(syntax):
                return descriptor, line[len(syntax) :]
        return None

    def _dispatch_user_prompt(self: CoreTuiApp, prompt: str, *, images: Sequence[PendingImage] = ()) -> None:
        # No optimistic commit: the durable core.user_message event is the one
        # and only source of the prompt row on screen.
        attachments = tuple(image.attachment for image in images)
        if self.upgrade is not None and self.upgrade.fenced:
            # Never queue a prompt behind a fence: the user re-sends after
            # reconnect. Composer._submit already cleared the draft, so restore
            # the exact text — but never overwrite a newer draft the user typed
            # while this key was being handled.
            if not self.composer.value.strip():
                self.composer.set_text(prompt)
                self._restore_attachments(attachments)
            self.status_line = "Core is draining for an upgrade; retry after it restarts"
            return
        if self._run_active:
            session_id, generation = self.session_id, self._generation

            def followup_worker() -> None:
                try:
                    res = self.client.run(session_id, RunSpec(prompt, images=attachments))
                    if res.status == "queued":
                        self.event_queue.put(events.FollowupQueuedEvent(session_id=session_id, generation=generation))
                    elif res.status in ("failed", "interrupted") and res.error:
                        self.event_queue.put(
                            events.FollowupFailedEvent(
                                prompt=prompt, error=res.error, images=attachments, session_id=session_id, generation=generation
                            )
                        )
                    else:
                        self.event_queue.put(
                            events.RunStatusEvent(
                                status=res.status,
                                error=res.error,
                                run_id=res.run_id,
                                session_id=session_id,
                                generation=generation,
                            )
                        )
                except Exception as exc:
                    self.event_queue.put(
                        events.FollowupFailedEvent(prompt=prompt, error=str(exc), images=attachments, session_id=session_id, generation=generation)
                    )

            self._work.submit("CoreFollowup", followup_worker)
            return
        self.status_line = ""
        self._mark_run_active()
        session_id, generation = self.session_id, self._generation

        def run_worker() -> None:
            try:
                res = self.client.run(session_id, RunSpec(prompt, images=attachments))
                status = res.status
                self.event_queue.put(
                    events.RunStatusEvent(
                        status=status,
                        error=res.error,
                        run_id=res.run_id,
                        session_id=session_id,
                        generation=generation,
                    )
                )
                if status == "waiting" and res.pending_approval is not None:
                    try:
                        gate = approval_gate_from_payload(res.pending_approval, run_id=res.run_id, revision_id=res.revision_id)
                    except ValueError as exc:
                        self._queue_notice(f"Approval receipt malformed: {exc}", tone="error", session_id=session_id, generation=generation)
                    else:
                        self.event_queue.put(events.RunWaitingEvent(gate=gate, session_id=session_id, generation=generation))
            except CoreClientError as exc:
                if exc.code == "upgrade_pending":
                    self.event_queue.put(events.RunRejectedEvent(prompt=prompt, images=attachments, session_id=session_id, generation=generation))
                    self._upgrade_pending_notice("the prompt", session_id=session_id, generation=generation)
                else:
                    self.event_queue.put(events.RunStatusEvent(status="failed", error=str(exc), session_id=session_id, generation=generation))
            except Exception as exc:
                self.event_queue.put(events.RunStatusEvent(status="failed", error=str(exc), session_id=session_id, generation=generation))

        self._work.submit("CoreRun", run_worker)

    def _dispatch_slash_command(self: CoreTuiApp, line: str) -> bool:
        """Dispatch one slash line; False means the draft must be restored."""
        rest = line[1:].strip()
        if not rest:
            return True
        head, _, arg_str = rest.partition(" ")

        # "/tui <control>" always reaches the client's own controls, so a plugin
        # that declares e.g. "help" cannot make local navigation unreachable.
        if head == "tui":
            local, _, local_args = arg_str.strip().partition(" ")
            if not local:
                self._show_help()
                return True
            if self._run_local_control(local, local_args.strip()):
                return True
            self.terminal.commit_rows([self.styles.fg("error", f"Unknown TUI control: /tui {_safe(local)}")])
            return True

        descriptors = catalog.get(self, retry=True)
        if not self.catalog_revision:
            catalog.unavailable(self)
            return False
        # A hidden command whose declared entry is a non-slash client syntax has no
        # slash alias: `!` is its only surface. Hidden internal helpers without a
        # client syntax remain exact-callable as before.
        descriptors = tuple(
            entry for entry in descriptors if entry.slash_visible or entry.client_syntax is None or entry.client_syntax.startswith("/")
        )
        tokens = rest.split()
        ranked = sorted(
            (
                (len(parts), index, entry)
                for index, entry in enumerate(descriptors)
                if (parts := _slash_public_path(entry).split()) and parts == tokens[: len(parts)]
            ),
            key=lambda item: (item[0], -item[1]),
            reverse=True,
        )
        if ranked and (len(ranked) == 1 or ranked[0][0] > ranked[1][0]):
            width, _, entry = ranked[0]
            tail = rest.split(maxsplit=width)[width] if len(tokens) > width else ""
            self._dispatch_catalog_command(entry, tail)
            return True
        if ranked:
            self.terminal.commit_rows([f"{self.styles.fg('error', f'Unknown command: /{_safe(rest)}')} (try /help)"])
            return True

        # Exact and namespace command paths were matched above; only a local
        # control can own the remaining bare name.
        if self._run_local_control(head, arg_str):
            return True
        self._dispatch_catalog_command(None, rest)
        hint = self._namespace_hint(tokens, descriptors)
        if hint:
            self.terminal.commit_rows([self.styles.fg("muted", hint)])
        return True

    @staticmethod
    def _namespace_hint(tokens: list[str], descriptors: Sequence[CommandDescriptor]) -> str | None:
        """Name the subcommands of the longest matching visible namespace prefix."""
        paths = [_slash_public_path(entry).split() for entry in descriptors if entry.slash_visible]
        for width in range(len(tokens), 0, -1):
            prefix = tokens[:width]
            children = sorted({parts[width] for parts in paths if len(parts) > width and parts[:width] == prefix})
            if children:
                return f"/{' '.join(prefix)} subcommands: {', '.join(children)}"
        return None

    def _run_local_control(self: CoreTuiApp, cmd: str, arg_str: str) -> bool:
        """Run one client-local control; False when the name is not a control."""
        control = TUI_CONTROLS_BY_NAME.get(cmd)
        if control is None:
            return False
        name = control.name
        if name == "help":
            self._show_help()
        elif name == "output":
            self._show_truncated_output()
        elif name == "sessions":
            self._show_sessions()
        elif name == "session":
            if arg_str:
                self._switch_session(arg_str.strip())
        elif name == "new":
            self._create_session()
        elif name == "cancel":
            self._cancel_active_run()
        elif name == "views":
            self._show_views(arg_str)
        elif name == "attach":
            if not arg_str.strip():
                self.status_line = "Usage: /tui attach <path> [path...]"
            else:
                self._attach_image_paths(self._attachment_paths(arg_str))
        elif name == "image":
            self._attach_clipboard_image()
        elif name == "detach":
            self._detach_images(arg_str)
        elif name == "model":
            self._tui_model(arg_str.strip())
        elif name == "thinking":
            self._tui_thinking(arg_str.strip())
        elif name == "quit":
            self.running = False
        return True

    def _action_schema(self: CoreTuiApp, name: str) -> Mapping[str, object] | None:
        """The target command's declared argument schema, the form's field authority."""
        for descriptor in catalog.get(self, retry=True):
            if descriptor.name == name:
                return descriptor.argument_schema
        if not self.catalog_revision:
            catalog.unavailable(self)
        return None

    def _refresh_followups(self: CoreTuiApp) -> None:
        session_id, generation = self.session_id, self._generation
        self._work.submit(
            "CoreFollowups",
            lambda: self.event_queue.put(operations.load_followups(self.client, session_id=session_id, generation=generation)),
        )

    def _withdraw_last_followup(self: CoreTuiApp) -> None:
        if not self._queued_followups:
            self.status_line = "No queued follow-ups to edit"
            return
        if self.composer.value.strip():
            self.status_line = "Clear or submit the current draft before recalling a queued follow-up"
            return
        if self._followup_withdrawing:
            return
        target = self._queued_followups[-1]
        expected_revision = self.catalog_revision or target.revision_id
        if not expected_revision:
            self.status_line = "Capability revision unknown; retrying catalog"
            catalog.get(self, retry=True)
            return
        self._followup_withdrawing = True
        self.status_line = "Withdrawing queued follow-up…"
        session_id, generation = self.session_id, self._generation
        self._work.submit(
            "CoreWithdrawFollowup",
            lambda: self.event_queue.put(
                operations.withdraw_followup(
                    self.client,
                    session_id=session_id,
                    continuation_id=target.continuation_id,
                    expected_revision=expected_revision,
                    generation=generation,
                )
            ),
        )

    def _command_receipt_rows(self: CoreTuiApp, name: str, result: CommandResult) -> list[str]:
        """Render one command receipt; core help uses the declarative help screen."""
        if name == "help":
            help_rows = self._help_receipt_rows(result)
            if help_rows is not None:
                return help_rows
        return _command_result_rows(name, result, self.styles, self.terminal.width)

    def _help_receipt_rows(self: CoreTuiApp, result: CommandResult) -> list[str] | None:
        """Render a core help receipt from its catalog data, or None to fall back.

        The core ``help`` command is the only producer of a full catalog payload
        in a receipt. The TUI never invents commands from ``output`` text; if an
        older/foreign server sends only text, the generic receipt renderer wins.
        """
        if result.status != "ok" or result.data is None:
            return None
        try:
            catalog = CommandCatalog.from_payload(result.data)
        except ValueError:
            return None
        bindings, conflicts = _shortcut_bindings(catalog.commands)
        return render_help(
            catalog.commands,
            revision=catalog.revision_id,
            width=self.terminal.content_width,
            styles=self.styles,
            bindings=bindings,
            conflicts=conflicts,
            quit_arm_seconds=QUIT_ARM_SECONDS,
            capabilities=self.client.capabilities,
        )

    def _show_help(self: CoreTuiApp) -> None:
        """Commit the client help screen; catalog failure degrades to local controls."""
        catalog.get(self)
        catalog_error = self._catalog_error
        if catalog_error is not None:
            catalog.unavailable(self)
        self.terminal.commit_rows(
            render_help(
                self.catalog_cache,
                revision=self.catalog_revision,
                width=self.terminal.content_width,
                styles=self.styles,
                bindings=self._shortcut_bindings,
                conflicts=self._shortcut_conflicts,
                quit_arm_seconds=QUIT_ARM_SECONDS,
                capabilities=self.client.capabilities,
                catalog_error=catalog_error,
            )
        )

    def _tui_model(self: CoreTuiApp, arg: str) -> None:
        """Show the admitted model detail, or switch it (declared capability, idle only)."""
        model = getattr(self.upgrade, "model", None)
        if not arg:
            self.terminal.commit_rows(
                [
                    self.styles.render("accent", "=== Model (admitted) ==="),
                    f"  model: {_safe(_model_segment(model))}",
                    f"  thinking: {_safe(getattr(model, 'reasoning_effort', None))}",
                    f"  supported levels: {_safe(_levels_text(model))}",
                ]
            )
            return
        self._tui_switch_model("model", arg)

    def _tui_thinking(self: CoreTuiApp, arg: str) -> None:
        """Show current + supported levels, or set one (declared capability, idle only)."""
        model = getattr(self.upgrade, "model", None)
        if not arg:
            self.terminal.commit_rows(
                [
                    self.styles.render("accent", "=== Thinking (admitted) ==="),
                    f"  current: {_safe(getattr(model, 'reasoning_effort', None))}",
                    f"  supported levels: {_safe(_levels_text(model))}",
                ]
            )
            return
        if arg.lstrip("+-").isdigit():
            self.terminal.commit_rows([self.styles.fg("error", "Numeric budgets are hidden here: use a supported named level or off")])
            return
        self._tui_switch_model("reasoning_effort", arg)

    def _tui_switch_model(self: CoreTuiApp, field: Literal["model", "reasoning_effort"], value: str) -> None:
        """Rewrite the workspace model section and reload once, entirely off the UI thread."""
        capability = ClientCapability.MODEL_SWITCH if field == "model" else ClientCapability.THINKING_SWITCH
        if not self.client.capabilities.supports(capability):
            self.terminal.commit_rows([self.styles.fg("error", "Model config lives with the server: switch from the server workspace")])
            return
        if self._run_active or self.pending_approval is not None or isinstance(self.active_overlay, InteractiveConfirmation):
            self.terminal.commit_rows([self.styles.fg("error", "Session is busy: resolve the run or gate first")])
            return
        if field == "model":
            if not value.strip():
                self.terminal.commit_rows([self.styles.fg("error", "Model must be a non-empty provider/model string")])
                return
            new_value: str | int = value
        else:
            new_value = value
            levels = getattr(getattr(self.upgrade, "model", None), "reasoning_levels", None)
            if isinstance(levels, (list, tuple)) and levels and new_value != "off" and new_value not in levels:
                supported = _levels_text(getattr(self.upgrade, "model", None))
                self.terminal.commit_rows([self.styles.fg("error", f"Unsupported level for this model: {_safe(new_value)} ({_safe(supported)})")])
                return
        if self._workspace_dir is None:
            self.terminal.commit_rows([self.styles.fg("error", "No workspace: cannot rewrite configuration")])
            return
        if not self.catalog_revision:
            catalog.get(self, retry=True)
            if not self.catalog_revision:
                catalog.unavailable(self)
                return
        workspace = Path(self._workspace_dir)
        revision = self.catalog_revision
        session_id, generation = self.session_id, self._generation
        client = self.client
        self.status_line = f"Switching {field}..."
        kind = field

        self._work.submit(
            "ModelSwitch",
            lambda: self.event_queue.put(
                operations.switch_model(
                    client,
                    workspace=workspace,
                    field=kind,
                    value=new_value,
                    revision=revision,
                    session_id=session_id,
                    generation=generation,
                )
            ),
        )

    def _dispatch_shortcut(self: CoreTuiApp, descriptor: CommandDescriptor) -> None:
        """Run one cached catalog chord without touching the composer draft."""
        if self.upgrade is not None and self.upgrade.fenced:
            self.status_line = "Core is draining for an upgrade; retry after it restarts"
            return
        if descriptor.has_input:
            self._prepare_command_input_async(descriptor)
            return
        self._dispatch_catalog_command(descriptor, "")

    def _dispatch_catalog_command(self: CoreTuiApp, cmd_desc: CommandDescriptor | None, arg_str: str) -> None:
        if cmd_desc is None:
            self.terminal.commit_rows([f"{self.styles.fg('error', f'Unknown command: /{_safe(arg_str)}')} (try /help)"])
            return
        # The revision of the catalog this descriptor was read from.
        revision = self.catalog_revision
        if not arg_str and cmd_desc.has_input:
            self._prepare_command_input_async(cmd_desc)
            return
        # Core read commands answer through the session client; plugin commands and run/resume
        # already project through durable events. Classify by contract metadata,
        # never by a client-side command name list.
        effects = frozenset(cmd_desc.effects)
        surface_result = cmd_desc.owner == "core" and effects <= {ToolEffect.READ}

        schema = cmd_desc.argument_schema
        raw_properties = schema.get("properties")
        properties: dict[str, object] = (
            {name: value for name, value in raw_properties.items() if isinstance(name, str)} if isinstance(raw_properties, Mapping) else {}
        )
        raw_required = schema.get("required")
        required = tuple(name for name in raw_required if isinstance(name, str)) if isinstance(raw_required, (list, tuple)) else ()
        single_required = properties.get(required[0]) if len(required) == 1 else None

        arguments: dict[str, object]
        if not arg_str and required and properties:
            from voidcode.sdk.views import FormField, FormFieldType, FormViewData

            field_types: dict[str, FormFieldType] = {"string": "text", "integer": "integer", "number": "number", "boolean": "boolean"}
            field_specs: list[tuple[str, Mapping[str, object], FormFieldType]] = []
            for name, spec in properties.items():
                if not isinstance(spec, Mapping):
                    field_specs.clear()
                    break
                type_name = spec.get("type")
                if not isinstance(type_name, str) or type_name not in field_types:
                    field_specs.clear()
                    break
                field_specs.append((name, spec, field_types[type_name]))
            if properties and len(field_specs) == len(properties):
                fields = tuple(
                    FormField(
                        name=name,
                        label=str(spec.get("description") or name),
                        field_type=field_type,
                        required=name in required,
                    )
                    for name, spec, field_type in field_specs
                )
                self._overlays.show(
                    InteractiveForm(
                        FormViewData(fields=fields, action=cmd_desc.name),
                        action_schema=schema,
                        revision_id=revision,
                        title=cmd_desc.description,
                    )
                )
                return
        if not arg_str:
            arguments = {}
        elif cmd_desc.text_argument is not None and not arg_str.lstrip().startswith("{"):
            arguments = {cmd_desc.text_argument: arg_str}
        elif (
            len(properties) == 1
            and len(required) == 1
            and required[0] in properties
            and isinstance(single_required, Mapping)
            and single_required.get("type") == "string"
        ):
            # A single required string schema maps slash trailing text generically.
            arguments = {required[0]: arg_str}
        else:
            try:
                parsed = json.loads(arg_str)
            except json.JSONDecodeError:
                if len(properties) == 1:
                    arguments = {next(iter(properties)): arg_str}
                else:
                    self.terminal.commit_rows([self.styles.fg("error", f"Arguments must be valid JSON: {_safe(arg_str)}")])
                    return
            else:
                if not isinstance(parsed, dict):
                    self.terminal.commit_rows([self.styles.fg("error", f"Arguments must be a JSON object: {_safe(arg_str)}")])
                    return
                arguments = parsed

        self.status_line = f"Dispatching /{_slash_public_path(cmd_desc)}..."
        self._invoke_async(cmd_desc.name, arguments, revision, surface_result=surface_result, display_name=_slash_public_path(cmd_desc))
