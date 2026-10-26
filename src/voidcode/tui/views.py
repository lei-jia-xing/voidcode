"""Pure renderers and interactive models for the 8 finite core plugin UI kinds."""

from __future__ import annotations

import difflib
import json
from collections.abc import Mapping
from typing import Any

from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from voidcode.sdk.gates import ApprovalGate
from voidcode.sdk.json_values import json_wire_value
from voidcode.sdk.views import (
    ChoicesViewData,
    ConfirmationViewData,
    DiffViewData,
    FieldsViewData,
    FormViewData,
    MarkdownViewData,
    NoticeViewData,
    PluginViewDescriptor,
    StatusViewData,
    choices_dispatch,
    parse_view_data,
    validate_form_submission,
)

from .keys import Key
from .term import escape_terminal_controls, render_lines
from .theme import Theme, resolve_theme


def _theme_arg(theme: Theme | None) -> Theme:
    """The caller's theme, or the default palette when none travels with the call."""
    return theme if theme is not None else resolve_theme()


class UnsupportedViewError(ValueError):
    """Raised when a plugin attempts to render an unrecognised view type."""


def render_notice(data: NoticeViewData, width: int, theme: Theme | None = None) -> list[str]:
    message = data.message
    level = data.level
    resolved = _theme_arg(theme)
    style_map = {
        "info": ("accent", "[info]"),
        "warning": ("warning", "[warn]"),
        "error": ("error", "[error]"),
    }
    token, prefix = style_map.get(level, ("accent", "[info]"))
    content = Text.assemble((f"{prefix}: ", resolved.style(token, bold=True)), (message, resolved.style("text")))
    return render_lines(content, width, theme=resolved.rich_theme(), color_system=resolved.color_system)


def render_assistant_markdown(text: str, width: int, theme: Theme | None = None) -> list[str]:
    """Assistant prose through the same markdown path as plugin views.

    One shared renderer for both so assistant output and plugin markdown can
    never drift apart in styling or control-byte handling.
    """
    return render_markdown(MarkdownViewData(content=text), width, theme)


def render_tool_call(name: str, arguments: object, width: int, theme: Theme | None = None) -> list[str]:
    """One tool-call header plus its pretty JSON arguments, never one raw line."""
    resolved = _theme_arg(theme)
    pretty = json.dumps(json_wire_value(arguments), indent=2, ensure_ascii=False, default=str)
    icon = resolved.tool_icon(name)
    header = Text.assemble(
        ((f"{icon} " if icon else ""), resolved.style("warning")),
        (f"> Tool call {name}", resolved.style("warning", bold=True)),
    )
    rows = render_lines(header, width, theme=resolved.rich_theme(), color_system=resolved.color_system)
    if pretty and pretty != "{}":
        syntax = Syntax(
            escape_terminal_controls(pretty, preserve_newlines=True, preserve_tabs=True),
            "json",
            theme=resolved.syntax_theme(),
            word_wrap=True,
        )
        rows.extend(render_lines(syntax, width, theme=resolved.rich_theme(), color_system=resolved.color_system))
    return rows


def render_tool_result(
    name: str,
    result: Mapping[str, Any],
    width: int,
    theme: Theme | None = None,
    *,
    interrupted: bool = False,
) -> list[str]:
    """One tool-result block: themed header, then text or a bounds note."""
    resolved = _theme_arg(theme)
    failed = interrupted or result.get("status") == "error"
    token = "error" if failed else "success"
    verb = "failed" if failed else "ok"
    header = Text.assemble((f"[{verb}] Tool {name}", resolved.style(token, bold=True)))
    rows = render_lines(header, width, theme=resolved.rich_theme(), color_system=resolved.color_system)
    output = result.get("output")
    body = ""
    suffix = ""
    if isinstance(output, Mapping):
        output_type = str(output.get("type", ""))
        if output_type == "text":
            presentation = output.get("presentation")
            body = str(presentation) if isinstance(presentation, str) and presentation else str(output.get("text", ""))
        elif output_type == "attachment":
            body = f"[{output.get('mime') or 'attachment'}]"
        bounds = output.get("bounds")
        if isinstance(bounds, Mapping):
            flags = [key for key in ("truncated", "partial") if bounds.get(key) is True]
            source = bounds.get("source")
            note = ", ".join([*flags, str(source)] if isinstance(source, str) and source else flags)
            if note:
                suffix = f"[{note}]"
    if failed:
        error = result.get("error")
        rows.append(resolved.fg("error", escape_terminal_controls(str(error or body or "interrupted"))))
    elif body:
        rows.extend(
            render_lines(
                Text(escape_terminal_controls(body, preserve_newlines=True, preserve_tabs=True), style=resolved.style("text")),
                width,
                theme=resolved.rich_theme(),
                color_system=resolved.color_system,
            )
        )
    if suffix:
        rows.append(resolved.fg("dim", suffix))
    return rows


def render_markdown(data: MarkdownViewData, width: int, theme: Theme | None = None) -> list[str]:
    """Render plugin markdown.

    Control bytes are escaped first, so plugin text cannot emit terminal
    commands; rich's Markdown then owns headings, lists, tables and code.
    """
    resolved = _theme_arg(theme)
    content = escape_terminal_controls(data.content, preserve_newlines=True, preserve_tabs=True)
    document = Markdown(content)
    # rich names ``code_theme`` str but hands it straight to ``Syntax(theme=...)``,
    # which takes a SyntaxTheme: set it post-construction instead of lying in the call.
    document.__dict__["code_theme"] = resolved.syntax_theme()
    return render_lines(document, width, theme=resolved.rich_theme(), color_system=resolved.color_system)


def render_fields(data: FieldsViewData, width: int, theme: Theme | None = None) -> list[str]:
    resolved = _theme_arg(theme)
    table = Table(show_header=False, box=None, padding=(0, 2), expand=False)
    table.add_column("Key", style=resolved.style("mdLink", bold=True), no_wrap=True)
    table.add_column("Value", style=resolved.style("text"), overflow="fold")
    for row in data.rows:
        table.add_row(row.label, row.value)
    return render_lines(table, width, theme=resolved.rich_theme(), color_system=resolved.color_system)


def render_diff(data: DiffViewData, width: int, theme: Theme | None = None) -> list[str]:
    before = data.before
    after = data.after
    path = data.path
    resolved = _theme_arg(theme)
    diff_lines = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{path or 'before'}",
            tofile=f"b/{path or 'after'}",
            lineterm="",
        )
    )
    diff_text = Text()
    heading = f"Diff: {escape_terminal_controls(str(path))}" if path else "Diff"
    diff_text.append(heading, style=resolved.style("text", bold=True))
    for line in diff_lines:
        diff_text.append("\n")
        if line.startswith("+") and not line.startswith("+++"):
            diff_text.append(line, style=resolved.style("toolDiffAdded"))
        elif line.startswith("-") and not line.startswith("---"):
            diff_text.append(line, style=resolved.style("toolDiffRemoved"))
        elif line.startswith("@@"):
            diff_text.append(line, style=resolved.style("accent"))
        else:
            diff_text.append(line, style=resolved.style("toolDiffContext"))
    return render_lines(diff_text, width, theme=resolved.rich_theme(), color_system=resolved.color_system)


def render_status(data: StatusViewData, width: int, theme: Theme | None = None) -> list[str]:
    resolved = _theme_arg(theme)
    text = Text.assemble(
        ("* ", resolved.style("success", bold=True)),
        (f"{data.label}: ", resolved.style("text", bold=True)),
        (data.value, resolved.style("text")),
    )
    return render_lines(text, width, theme=resolved.rich_theme(), color_system=resolved.color_system)


class InteractiveChoices:
    """Interactive selector for ChoicesView."""

    def __init__(self, data: ChoicesViewData, *, revision_id: str = "") -> None:
        self.view = data
        #: The revision of the envelope that rendered these options; the
        #: selection is dispatched under exactly that generation.
        self.revision_id = revision_id
        self.selected_index = 0

    @property
    def action(self) -> str:
        return self.view.action

    @property
    def argument_name(self) -> str:
        return self.view.argument_name

    def handle_key(self, key: Key) -> dict[str, Any] | None:
        if key.name in ("up", "k"):
            if self.view.options:
                self.selected_index = (self.selected_index - 1) % len(self.view.options)
            return None
        if key.name in ("down", "j"):
            if self.view.options:
                self.selected_index = (self.selected_index + 1) % len(self.view.options)
            return None
        if key.name == "enter":
            if not self.view.options:
                return {"cancel": True}
            action, arguments = choices_dispatch(self.view, self.view.options[self.selected_index].id)
            return {"action": action, "arguments": arguments}
        if key.name == "esc":
            return {"cancel": True}
        return None

    def render_rows(self, width: int, theme: Theme | None = None) -> list[str]:
        resolved = _theme_arg(theme)
        text = Text()
        text.append("Choose an option:\n", style=resolved.style("accent", bold=True))
        for i, opt in enumerate(self.view.options):
            if i == self.selected_index:
                text.append(f" > [{i + 1}] {opt.label}\n", style=resolved.style("warning", bold=True))
            else:
                text.append(f"   [{i + 1}] {opt.label}\n", style=resolved.style("text"))
        panel = Panel(text, title=f"Action: {self.view.action}", border_style=resolved.style("warning"), expand=True)
        return render_lines(panel, width, theme=resolved.rich_theme(), color_system=resolved.color_system)


class InteractiveConfirmation:
    """Interactive confirmation dialog binding request_id."""

    def __init__(self, data: ConfirmationViewData, *, revision_id: str = "") -> None:
        self.view = data
        #: The revision this confirmation was displayed under; the stored
        #: request id stays the identity, this is the generation the user saw.
        self.revision_id = revision_id
        self.selected_index = 0

    @property
    def request_id(self) -> str:
        return self.view.request_id

    @property
    def message(self) -> str:
        return self.view.message

    @property
    def actions(self) -> tuple[str, ...]:
        return self.view.actions

    def handle_key(self, key: Key) -> dict[str, Any] | None:
        if key.name in ("left", "h", "up", "k"):
            self.selected_index = (self.selected_index - 1) % len(self.view.actions)
            return None
        if key.name in ("right", "l", "down", "j", "tab"):
            self.selected_index = (self.selected_index + 1) % len(self.view.actions)
            return None
        if key.name == "enter":
            return {"request_id": self.view.request_id, "decision": self.view.actions[self.selected_index]}
        if key.name == "esc":
            return {"request_id": self.view.request_id, "decision": "deny"}
        return None

    def render_rows(self, width: int, theme: Theme | None = None) -> list[str]:
        resolved = _theme_arg(theme)
        text = Text()
        text.append(f"{self.view.message}\n\n", style=resolved.style("text", bold=True))
        text.append("Request ID: ", style=resolved.style("dim"))
        text.append(f"{self.view.request_id}\n\n", style=resolved.style("accent"))
        for i, act in enumerate(self.view.actions):
            is_selected = i == self.selected_index
            if is_selected and act == "allow":
                style = resolved.style("success", bold=True, reverse=True)
            elif is_selected:
                style = resolved.style("error", bold=True, reverse=True)
            else:
                style = resolved.style("dim")
            text.append(f" [ {act.upper()} ] ", style=style)
            text.append("  ")
        panel = Panel(text, title="Action Confirmation", border_style=resolved.style("error"), expand=True)
        return render_lines(panel, width, theme=resolved.rich_theme(), color_system=resolved.color_system)


class InteractiveApproval:
    """Native tool-approval gate: full arguments plus a diff preview, scrollable.

    The decision is the only outcome: ``{"request_id", "decision"}`` on allow,
    deny, or esc (deny). Scroll keys move the preview; they never decide.
    """

    def __init__(self, gate: ApprovalGate) -> None:
        self.gate = gate
        self.request_id = gate.request_id
        self.tool_name = gate.tool_name
        self.arguments: Mapping[str, Any] = gate.call.arguments
        self.scroll = 0

    def handle_key(self, key: Key) -> dict[str, Any] | None:
        if key.name in ("up", "k", "pageUp"):
            self.scroll = max(0, self.scroll - 1)
            return None
        if key.name in ("down", "j", "pageDown", "space"):
            self.scroll += 1
            return None
        if key.name in ("a", "enter"):
            return {"request_id": self.request_id, "decision": "allow"}
        if key.name in ("d", "esc"):
            return {"request_id": self.request_id, "decision": "deny"}
        return None

    def _edit_preview(self) -> tuple[str, str, str] | None:
        """(path, before, after) for a file edit, else None."""
        if self.tool_name != "edit":
            return None
        path = self.arguments.get("path")
        if not isinstance(path, str) or not path:
            return None
        edits = self.arguments.get("edits")
        if isinstance(edits, (list, tuple)) and edits:
            before_parts: list[str] = []
            after_parts: list[str] = []
            for edit in edits:
                if not isinstance(edit, Mapping):
                    return None
                old, new = edit.get("oldString"), edit.get("newString")
                if not isinstance(old, str) or not isinstance(new, str):
                    return None
                before_parts.append(old)
                after_parts.append(new)
            return path, "\n".join(before_parts), "\n".join(after_parts)
        old, new = self.arguments.get("oldString"), self.arguments.get("newString")
        if isinstance(old, str) and isinstance(new, str):
            return path, old, new
        return None

    def render_rows(self, width: int, theme: Theme | None = None) -> list[str]:
        resolved = _theme_arg(theme)
        icon = resolved.tool_icon(self.tool_name)
        header = Text.assemble(
            ((f"{icon} " if icon else ""), resolved.style("warning")),
            ("Tool approval required", resolved.style("warning", bold=True)),
        )
        rows = render_lines(header, width, theme=resolved.rich_theme(), color_system=resolved.color_system)
        rows.append(resolved.fg("text", f"  tool: {escape_terminal_controls(self.tool_name)}"))
        rows.append(resolved.fg("dim", f"  request: {escape_terminal_controls(self.request_id)}"))
        preview = self._edit_preview()
        if preview is not None:
            path, before, after = preview
            rows.extend(render_diff(DiffViewData(before=before, after=after, path=path), width, resolved))
        pretty = json.dumps(json_wire_value(self.arguments), indent=2, ensure_ascii=False, default=str)
        if pretty and pretty != "{}":
            rows.append(resolved.fg("dim", "  arguments:"))
            rows.extend(
                render_lines(
                    Syntax(
                        escape_terminal_controls(pretty, preserve_newlines=True, preserve_tabs=True),
                        "json",
                        theme=resolved.syntax_theme(),
                        word_wrap=True,
                    ),
                    width,
                    theme=resolved.rich_theme(),
                    color_system=resolved.color_system,
                )
            )
        visible = rows[self.scroll :] if self.scroll < len(rows) else []
        visible.append(resolved.fg("text", "  [a] allow once    [d] deny    [up/down] scroll"))
        return visible


class InteractiveForm:
    """Interactive Form View supporting text, multiline, integer, number, boolean."""

    def __init__(self, data: FormViewData, *, action_schema: Mapping[str, Any] | None = None, revision_id: str = "") -> None:
        self.view = data
        #: The revision of the views envelope that rendered this form: the
        #: read prefill and the submit both travel under it.
        self.revision_id = revision_id
        self.load_error: str | None = None
        self._action_schema = action_schema
        self._field_names = frozenset(field.name for field in data.fields)

        self.values: dict[str, Any] = {}
        if isinstance(data.initial_values, Mapping):
            for key, value in data.initial_values.items():
                if key in self._field_names:
                    self.values[key] = value

        # No field is materialised: a missing optional value stays missing so it
        # can be omitted, cleared (null) or explicitly set at submit time.

        self.active_index = 0
        self.dirty_fields: set[str] = set()
        self.validation_error: str | None = None

    @property
    def action(self) -> str:
        return self.view.action

    @property
    def load_action(self) -> str | None:
        return self.view.load_action

    def populate_from_load_action(self, loaded_data: Mapping[str, Any]) -> None:
        """Prefill from load_action; fields the user already edited keep their draft."""
        for key, value in loaded_data.items():
            if key in self._field_names and key not in self.dirty_fields:
                self.values[key] = value

    def handle_key(self, key: Key) -> dict[str, Any] | None:
        self.validation_error = None
        if key.name in ("tab", "down"):
            if self.view.fields:
                self.active_index = (self.active_index + 1) % len(self.view.fields)
            return None
        if key.name in ("shift+tab", "up"):
            if self.view.fields:
                self.active_index = (self.active_index - 1) % len(self.view.fields)
            return None
        if key.name == "esc":
            return {"cancel": True}
        if key.name in ("ctrl+s", "ctrl+enter"):
            return self._submit()

        if not self.view.fields:
            return None

        current_field = self.view.fields[self.active_index]
        name = current_field.name
        ftype = current_field.field_type
        val = self.values.get(name, "")

        if ftype == "boolean":
            if key.name in ("space", "enter"):
                # Tri-state cycle: unset -> true -> false -> unset.
                self.values[name] = {None: True, True: False, False: None}.get(self.values.get(name) if name in self.values else None)
                self.dirty_fields.add(name)
            return None

        if key.name == "enter":
            if ftype == "multiline":
                self.values[name] = str(val) + "\n"
                self.dirty_fields.add(name)
                return None
            if self.active_index == len(self.view.fields) - 1:
                return self._submit()
            self.active_index += 1
            return None

        if key.name == "backspace":
            sval = "" if val is None else str(val)
            if sval:
                self.values[name] = sval[:-1]
                self.dirty_fields.add(name)
            return None

        if key.text and not key.name.startswith("ctrl+") and not key.name.startswith("alt+"):
            # Numeric fields stay raw text: nothing is parsed or coerced while
            # typing, so "1e3" is never silently truncated to 1 and cannot be
            # auto-submitted as a different number than the user typed.
            # A present None is an empty editor: typing must not append to "None".
            self.values[name] = ("" if val is None else str(val)) + key.text
            self.dirty_fields.add(name)
            return None

        return None

    def _submit(self) -> dict[str, Any] | None:
        try:
            arguments = validate_form_submission(self.view, self.values, action_schema=self._action_schema)
        except ValueError as exc:
            self.validation_error = str(exc)
            return None
        return {"action": self.view.action, "arguments": arguments}

    def render_rows(self, width: int, theme: Theme | None = None) -> list[str]:
        resolved = _theme_arg(theme)
        text = Text()
        if self.load_error:
            text.append(f"[warn] Load failure: {self.load_error}\n", style=resolved.style("error", bold=True))
        if self.validation_error:
            text.append(f"[error] Validation: {self.validation_error}\n", style=resolved.style("error", bold=True))

        text.append(f"Form: {self.view.action} (Enter/Tab next, Ctrl+S submit, Esc cancel)\n\n", style=resolved.style("dim"))
        for i, field in enumerate(self.view.fields):
            name = field.name
            label = field.label
            ftype = field.field_type
            val = self.values.get(name, "")
            is_active = i == self.active_index
            req_mark = "*" if field.required else ""

            prefix = "> " if is_active else "  "
            label_style = resolved.style("accent", bold=True) if is_active else resolved.style("text")
            text.append(f"{prefix}{label}{req_mark} [{ftype}]: ", style=label_style)

            if ftype == "boolean":
                if val is True:
                    bool_str = "[X] True"
                elif val is False:
                    bool_str = "[ ] False"
                else:
                    bool_str = "[ ] (unset)"
                present = val is not None
                if val is True:
                    value_style = resolved.style("success", bold=True)
                elif present:
                    value_style = resolved.style("dim")
                else:
                    value_style = resolved.style("text", italic=True)
                text.append(f"{bool_str}\n", style=value_style)
            elif ftype == "multiline":
                lines = str(val).split("\n")
                value_style = resolved.style("warning") if is_active else resolved.style("dim")
                if len(lines) <= 1:
                    text.append(f"'{val}'\n", style=value_style)
                else:
                    text.append("\n", style=resolved.style("text"))
                    for line in lines:
                        text.append(f"    | {line}\n", style=value_style)
            else:
                text.append(f"'{val}'\n", style=resolved.style("warning") if is_active else resolved.style("dim"))

        panel = Panel(text, title=f"Form: {self.view.action}", border_style=resolved.style("accent"), expand=True)
        return render_lines(panel, width, theme=resolved.rich_theme(), color_system=resolved.color_system)


def render_plugin_view(descriptor: PluginViewDescriptor, width: int, theme: Theme | None = None) -> list[str]:
    """Render any of the 8 finite view kinds into ANSI rows.

    Raises UnsupportedViewError if the view_type is outside the 8 finite kinds.
    """
    data = parse_view_data(descriptor.view_type, descriptor.data)

    if isinstance(data, NoticeViewData):
        return render_notice(data, width, theme)
    if isinstance(data, MarkdownViewData):
        return render_markdown(data, width, theme)
    if isinstance(data, FieldsViewData):
        return render_fields(data, width, theme)
    if isinstance(data, DiffViewData):
        return render_diff(data, width, theme)
    if isinstance(data, StatusViewData):
        return render_status(data, width, theme)
    if isinstance(data, ChoicesViewData):
        return InteractiveChoices(data).render_rows(width, theme)
    if isinstance(data, ConfirmationViewData):
        return InteractiveConfirmation(data).render_rows(width, theme)
    if isinstance(data, FormViewData):
        return InteractiveForm(data).render_rows(width, theme)

    raise UnsupportedViewError(f"unsupported view type: {descriptor.view_type}")
