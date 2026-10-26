"""Pure renderers and interactive models for the 8 finite core plugin UI kinds."""

from __future__ import annotations

import difflib
import math
import re
from collections.abc import Mapping
from typing import Any

from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .keys import Key
from .term import escape_terminal_controls, render_lines
from .theme import Theme, resolve_theme


def _theme_arg(theme: Theme | None) -> Theme:
    """The caller's theme, or the default palette when none travels with the call."""
    return theme if theme is not None else resolve_theme()


#: Strict numeric literals: no silent truncation, no scientific-integer coercion.
_INTEGER_PATTERN = re.compile(r"^[+-]?\d+$")
_NUMBER_PATTERN = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


def _schema_allows_null(schema: Mapping[str, Any]) -> bool:
    """Whether a JSON Schema (Draft 2020-12) admits ``null`` for this position."""
    field_type = schema.get("type")
    if field_type == "null":
        return True
    if isinstance(field_type, (list, tuple)) and "null" in field_type:
        return True
    for keyword in ("anyOf", "oneOf"):
        branches = schema.get(keyword)
        if isinstance(branches, (list, tuple)):
            if any(isinstance(branch, Mapping) and _schema_allows_null(branch) for branch in branches):
                return True
    return False


class UnsupportedViewError(ValueError):
    """Raised when a plugin attempts to render an unrecognised view type."""


def render_notice(data: Mapping[str, Any], width: int, theme: Theme | None = None) -> list[str]:
    message = str(data.get("message", ""))
    level = str(data.get("level", "info"))
    resolved = _theme_arg(theme)
    style_map = {
        "info": ("accent", "[info]"),
        "warning": ("warning", "[warn]"),
        "error": ("error", "[error]"),
    }
    token, prefix = style_map.get(level, ("accent", "[info]"))
    content = Text.assemble((f"{prefix}: ", resolved.style(token, bold=True)), (message, resolved.style("text")))
    return render_lines(content, width, theme=resolved.rich_theme(), color_system=resolved.color_system)


def render_markdown(data: Mapping[str, Any], width: int, theme: Theme | None = None) -> list[str]:
    """Render plugin markdown.

    Control bytes are escaped first, so plugin text cannot emit terminal
    commands; rich's Markdown then owns headings, lists, tables and code.
    """
    resolved = _theme_arg(theme)
    content = escape_terminal_controls(str(data.get("content", "")), preserve_newlines=True, preserve_tabs=True)
    document = Markdown(content)
    # rich names ``code_theme`` str but hands it straight to ``Syntax(theme=...)``,
    # which takes a SyntaxTheme: set it post-construction instead of lying in the call.
    document.__dict__["code_theme"] = resolved.syntax_theme()
    return render_lines(document, width, theme=resolved.rich_theme(), color_system=resolved.color_system)


def render_fields(data: Mapping[str, Any], width: int, theme: Theme | None = None) -> list[str]:
    rows = data.get("rows", [])
    resolved = _theme_arg(theme)
    table = Table(show_header=False, box=None, padding=(0, 2), expand=False)
    table.add_column("Key", style=resolved.style("mdLink", bold=True), no_wrap=True)
    table.add_column("Value", style=resolved.style("text"), overflow="fold")
    for row in rows:
        if isinstance(row, dict):
            label, value = row.get("label", ""), row.get("value", "")
        elif hasattr(row, "label") and hasattr(row, "value"):
            label, value = row.label, row.value
        else:
            continue
        table.add_row(
            str(label),
            str(value),
        )
    return render_lines(table, width, theme=resolved.rich_theme(), color_system=resolved.color_system)


def render_diff(data: Mapping[str, Any], width: int, theme: Theme | None = None) -> list[str]:
    before = str(data.get("before", ""))
    after = str(data.get("after", ""))
    path = data.get("path")
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


def render_status(data: Mapping[str, Any], width: int, theme: Theme | None = None) -> list[str]:
    label = str(data.get("label", "Status"))
    value = str(data.get("value", ""))
    resolved = _theme_arg(theme)
    text = Text.assemble(
        ("* ", resolved.style("success", bold=True)),
        (f"{label}: ", resolved.style("text", bold=True)),
        (value, resolved.style("text")),
    )
    return render_lines(text, width, theme=resolved.rich_theme(), color_system=resolved.color_system)


class InteractiveChoices:
    """Interactive selector for ChoicesView."""

    def __init__(self, data: Mapping[str, Any], *, revision_id: str = "") -> None:
        #: The revision of the envelope that rendered these options; the
        #: selection is dispatched under exactly that generation.
        self.revision_id = revision_id
        raw_options = data.get("options", [])
        self.options: list[dict[str, str]] = []
        for opt in raw_options:
            if isinstance(opt, dict):
                self.options.append({"id": str(opt.get("id", "")), "label": str(opt.get("label", ""))})
            elif hasattr(opt, "id") and hasattr(opt, "label"):
                self.options.append({"id": str(opt.id), "label": str(opt.label)})
        self.action = str(data.get("action", ""))
        self.argument_name = str(data.get("argument_name", "choice"))
        self.selected_index = 0

    def handle_key(self, key: Key) -> dict[str, Any] | None:
        if key.name in ("up", "k"):
            if self.options:
                self.selected_index = (self.selected_index - 1) % len(self.options)
            return None
        if key.name in ("down", "j"):
            if self.options:
                self.selected_index = (self.selected_index + 1) % len(self.options)
            return None
        if key.name == "enter":
            if not self.options:
                return {"cancel": True}
            choice_id = self.options[self.selected_index]["id"]
            return {"action": self.action, "arguments": {self.argument_name: choice_id}}
        if key.name == "esc":
            return {"cancel": True}
        return None

    def render_rows(self, width: int, theme: Theme | None = None) -> list[str]:
        resolved = _theme_arg(theme)
        text = Text()
        text.append("Choose an option:\n", style=resolved.style("accent", bold=True))
        for i, opt in enumerate(self.options):
            if i == self.selected_index:
                text.append(f" > [{i + 1}] {opt['label']}\n", style=resolved.style("warning", bold=True))
            else:
                text.append(f"   [{i + 1}] {opt['label']}\n", style=resolved.style("text"))
        panel = Panel(text, title=f"Action: {self.action}", border_style=resolved.style("warning"), expand=True)
        return render_lines(panel, width, theme=resolved.rich_theme(), color_system=resolved.color_system)


class InteractiveConfirmation:
    """Interactive confirmation dialog binding request_id."""

    def __init__(self, data: Mapping[str, Any], *, revision_id: str = "") -> None:
        self.request_id = str(data.get("request_id", ""))
        #: The revision this confirmation was displayed under; the stored
        #: request id stays the identity, this is the generation the user saw.
        self.revision_id = revision_id
        self.message = str(data.get("message", "Approve action?"))
        raw_actions = data.get("actions", ("allow", "deny"))
        self.actions = list(raw_actions) or ["allow", "deny"]
        self.selected_index = 0

    def handle_key(self, key: Key) -> dict[str, Any] | None:
        if key.name in ("left", "h", "up", "k"):
            self.selected_index = (self.selected_index - 1) % len(self.actions)
            return None
        if key.name in ("right", "l", "down", "j", "tab"):
            self.selected_index = (self.selected_index + 1) % len(self.actions)
            return None
        if key.name == "enter":
            return {"request_id": self.request_id, "decision": self.actions[self.selected_index]}
        if key.name == "esc":
            return {"request_id": self.request_id, "decision": "deny"}
        return None

    def render_rows(self, width: int, theme: Theme | None = None) -> list[str]:
        resolved = _theme_arg(theme)
        text = Text()
        text.append(f"{self.message}\n\n", style=resolved.style("text", bold=True))
        text.append("Request ID: ", style=resolved.style("dim"))
        text.append(f"{self.request_id}\n\n", style=resolved.style("accent"))
        for i, act in enumerate(self.actions):
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


class InteractiveForm:
    """Interactive Form View supporting text, multiline, integer, number, boolean."""

    def __init__(self, data: Mapping[str, Any], *, action_schema: Mapping[str, Any] | None = None, revision_id: str = "") -> None:
        self.action = str(data.get("action", ""))
        #: The revision of the views envelope that rendered this form: the
        #: read prefill and the submit both travel under it.
        self.revision_id = revision_id
        self.load_action = data.get("load_action")
        self.load_error: str | None = None
        # The target command's own argument schema is the authority on whether a
        # blank field means null (clearable) or "leave unset".
        properties = (action_schema or {}).get("properties")
        self._field_schema: Mapping[str, Any] = properties if isinstance(properties, Mapping) else {}
        raw_fields = data.get("fields", [])
        self.fields: list[dict[str, Any]] = []
        for f in raw_fields:
            if isinstance(f, dict):
                self.fields.append(
                    {
                        "name": str(f.get("name", "")),
                        "label": str(f.get("label", "")),
                        "field_type": str(f.get("field_type", "text")),
                        "required": bool(f.get("required", True)),
                    }
                )
            elif hasattr(f, "name"):
                self.fields.append(
                    {
                        "name": str(f.name),
                        "label": str(getattr(f, "label", f.name)),
                        "field_type": str(getattr(f, "field_type", "text")),
                        "required": bool(getattr(f, "required", True)),
                    }
                )

        initial = data.get("initial_values", {})
        self.values: dict[str, Any] = {}
        if isinstance(initial, Mapping):
            self.values.update(initial)

        # No field is materialised: a missing optional value stays missing so it
        # can be omitted, cleared (null) or explicitly set at submit time.

        self.active_index = 0
        self.dirty_fields: set[str] = set()
        self.validation_error: str | None = None

    def _allows_null(self, name: str) -> bool:
        """True when the target command's schema declares this field nullable.

        Draft 2020-12 declares nullability through a ``"null"`` type, a type
        array containing ``"null"``, or a ``null`` branch of ``anyOf``/``oneOf``.
        """
        field_schema = self._field_schema.get(name)
        return isinstance(field_schema, Mapping) and _schema_allows_null(field_schema)

    def populate_from_load_action(self, loaded_data: Mapping[str, Any]) -> None:
        """Prefill from load_action; fields the user already edited keep their draft."""
        for key, value in loaded_data.items():
            if key not in self.dirty_fields:
                self.values[key] = value

    def handle_key(self, key: Key) -> dict[str, Any] | None:
        self.validation_error = None
        if key.name in ("tab", "down"):
            if self.fields:
                self.active_index = (self.active_index + 1) % len(self.fields)
            return None
        if key.name in ("shift+tab", "up"):
            if self.fields:
                self.active_index = (self.active_index - 1) % len(self.fields)
            return None
        if key.name == "esc":
            return {"cancel": True}
        if key.name in ("ctrl+s", "ctrl+enter"):
            return self._submit()

        if not self.fields:
            return None

        current_field = self.fields[self.active_index]
        name = current_field["name"]
        ftype = current_field["field_type"]
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
            if self.active_index == len(self.fields) - 1:
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
        arguments: dict[str, Any] = {}
        for f in self.fields:
            name = f["name"]
            label = f["label"] or name
            ftype = f["field_type"]
            raw = self.values.get(name)

            # Absent (never loaded, never typed) is not the same as present-but-blank
            # (the user cleared it): only the latter may clear a nullable value.
            present = name in self.values

            if ftype == "boolean":
                if raw is True or raw is False:
                    arguments[name] = raw
                    continue
                if f["required"] and (not present or not self._allows_null(name)):
                    self.validation_error = f"Field '{label}' is required."
                    return None
                if present and self._allows_null(name):
                    arguments[name] = None
                    continue
                continue  # absent or optional non-nullable: omit

            text = "" if raw is None else str(raw)
            if not text.strip():
                if f["required"] and (not present or not self._allows_null(name)):
                    self.validation_error = f"Field '{label}' is required."
                    return None
                if present and self._allows_null(name):
                    # A deliberate blank clears the nullable value.
                    arguments[name] = None
                    continue
                # Absent or optional non-nullable: omit rather than fabricate.
                continue

            # Numeric candidates are trimmed for parsing only; text stays verbatim.
            candidate = text.strip()
            if ftype == "integer":
                if not _INTEGER_PATTERN.match(candidate):
                    self.validation_error = f"Field '{label}' must be an integer."
                    return None
                arguments[name] = int(candidate)
            elif ftype == "number":
                if not _NUMBER_PATTERN.match(candidate):
                    self.validation_error = f"Field '{label}' must be a number."
                    return None
                value = float(candidate)
                if not math.isfinite(value):
                    self.validation_error = f"Field '{label}' must be a finite number."
                    return None
                arguments[name] = value
            else:
                # Text is preserved exactly as typed, including whitespace.
                arguments[name] = text

        return {"action": self.action, "arguments": arguments}

    def render_rows(self, width: int, theme: Theme | None = None) -> list[str]:
        resolved = _theme_arg(theme)
        text = Text()
        if self.load_error:
            text.append(f"[warn] Load failure: {self.load_error}\n", style=resolved.style("error", bold=True))
        if self.validation_error:
            text.append(f"[error] Validation: {self.validation_error}\n", style=resolved.style("error", bold=True))

        text.append(f"Form: {self.action} (Enter/Tab next, Ctrl+S submit, Esc cancel)\n\n", style=resolved.style("dim"))
        for i, f in enumerate(self.fields):
            name = f["name"]
            label = f["label"]
            ftype = f["field_type"]
            val = self.values.get(name, "")
            is_active = i == self.active_index
            req_mark = "*" if f["required"] else ""

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

        panel = Panel(text, title=f"Form: {self.action}", border_style=resolved.style("accent"), expand=True)
        return render_lines(panel, width, theme=resolved.rich_theme(), color_system=resolved.color_system)


def render_plugin_view(descriptor: Mapping[str, Any], width: int, theme: Theme | None = None) -> list[str]:
    """Render any of the 8 finite view kinds into ANSI rows.

    Raises UnsupportedViewError if the view_type is outside the 8 finite kinds.
    """
    view_type = descriptor.get("view_type")
    data = descriptor.get("data", {})
    if not isinstance(data, Mapping):
        data = {}

    if view_type == "notice":
        return render_notice(data, width, theme)
    if view_type == "markdown":
        return render_markdown(data, width, theme)
    if view_type == "fields":
        return render_fields(data, width, theme)
    if view_type == "diff":
        return render_diff(data, width, theme)
    if view_type == "status":
        return render_status(data, width, theme)
    if view_type == "choices":
        choices = InteractiveChoices(data)
        return choices.render_rows(width, theme)
    if view_type == "confirmation":
        conf = InteractiveConfirmation(data)
        return conf.render_rows(width, theme)
    if view_type == "form":
        form = InteractiveForm(data)
        return form.render_rows(width, theme)

    raise UnsupportedViewError(f"unsupported view type: {view_type}")
