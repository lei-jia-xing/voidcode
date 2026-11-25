"""Interactive form and model-question overlays with bounded JSON draft snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from rich.panel import Panel
from rich.text import Text

from voidcode.sdk.ask import AskMultiSelect, AskRequest
from voidcode.sdk.views import FormViewData, validate_form_submission

from .keys import Key
from .styles import Styles, markdown_theme, resolve_component
from .term import escape_terminal_controls, render_lines
from .view_outcomes import AskAnswered, AskCancelled, AskValue, FormValue, OverlayActionSelected, OverlayCancelled
from .view_renderers import _styles_arg


def _draft_json_value(value: object) -> FormValue:
    """Snapshot one plugin-provided draft value, rejecting non-JSON objects."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        owned: dict[str, FormValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("form draft object keys must be strings")
            owned[key] = _draft_json_value(item)
        return MappingProxyType(owned)
    if isinstance(value, (list, tuple)):
        return tuple(_draft_json_value(item) for item in value)
    raise ValueError(f"form draft value is not JSON: {type(value).__name__}")


class InteractiveForm:
    """Interactive Form View supporting text, multiline, integer, number, boolean."""

    def __init__(
        self, data: FormViewData, *, action_schema: Mapping[str, object] | None = None, revision_id: str = "", title: str = "Command input"
    ) -> None:
        self.title = title
        self.view = data
        #: The revision of the views envelope that rendered this form: the
        #: read prefill and the submit both travel under it.
        self.revision_id = revision_id
        self.load_error: str | None = None
        self._action_schema = action_schema
        self._field_names = frozenset(field.name for field in data.fields)

        self.values: dict[str, FormValue] = {}
        if isinstance(data.initial_values, Mapping):
            for key, value in data.initial_values.items():
                if key in self._field_names:
                    self.values[key] = _draft_json_value(value)

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

    def populate_from_load_action(self, loaded_data: Mapping[str, object]) -> None:
        """Prefill from load_action; fields the user already edited keep their draft."""
        for key, value in loaded_data.items():
            if key in self._field_names and key not in self.dirty_fields:
                self.values[key] = _draft_json_value(value)

    def handle_key(self, key: Key) -> OverlayCancelled | OverlayActionSelected | None:
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
            return OverlayCancelled()
        if key.name in ("ctrl+s", "ctrl+enter"):
            return self._submit()
        if not self.view.fields:
            return None

        current_field = self.view.fields[self.active_index]
        name = current_field.name
        ftype = current_field.field_type
        val = self.values.get(name, "")

        if current_field.options is not None:
            options = [option.id for option in current_field.options]
            if options and key.name in ("left", "right", "space"):
                index = options.index(val) if isinstance(val, str) and val in options else (-1 if key.name != "left" else 0)
                self.values[name] = options[(index + (-1 if key.name == "left" else 1)) % len(options)]
                self.dirty_fields.add(name)
                return None
            if key.name != "enter":
                if key.text:
                    match = next((option.id for option in current_field.options if option.label.casefold().startswith(key.text.casefold())), None)
                    if match is None:
                        match = next((option for option in options if option.casefold().startswith(key.text.casefold())), None)
                    if match is not None:
                        self.values[name] = match
                        self.dirty_fields.add(name)
                return None

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

    def _submit(self) -> OverlayActionSelected | None:
        try:
            arguments = validate_form_submission(self.view, self.values, action_schema=self._action_schema)
        except ValueError as exc:
            self.validation_error = str(exc)
            return None
        return OverlayActionSelected(action=self.view.action, arguments=arguments)

    def render_rows(self, width: int, styles: Styles | None = None) -> list[str]:
        resolved = _styles_arg(styles)
        fg, bg, tone_slot = resolve_component(self.view.style)
        text = Text()
        if self.load_error:
            text.append(f"[warn] Load failure: {escape_terminal_controls(self.load_error)}\n", style=resolved.style("error", bold=True))
        if self.validation_error:
            text.append(f"[error] Validation: {escape_terminal_controls(self.validation_error)}\n", style=resolved.style("error", bold=True))

        text.append(
            "Enter/Tab next · Ctrl+S submit · Esc cancel\n\n",
            style=resolved.style(tone_slot or "dim", color=fg, background=bg),
        )
        for i, field in enumerate(self.view.fields):
            name = field.name
            label = escape_terminal_controls(field.label)
            ftype = field.field_type
            raw = self.values.get(name, "")
            val = escape_terminal_controls("" if raw is None else str(raw), preserve_newlines=True)
            is_active = i == self.active_index
            req_mark = "*" if field.required else ""

            prefix = "> " if is_active else "  "
            if is_active:
                label_style = resolved.style(tone_slot or "accent", color=fg, background=bg, bold=True)
            else:
                label_style = resolved.style(tone_slot or "text", color=fg, background=bg)
            text.append(f"{prefix}{label}{req_mark}: ", style=label_style)

            if field.options is not None:
                selected = next((option.label for option in field.options if option.id == raw), "(unset)")
                text.append(escape_terminal_controls(selected) + " (←/→ select)\n", style=resolved.style("text"))
            elif ftype == "boolean":
                if raw is True:
                    bool_str = "[X] True"
                elif raw is False:
                    bool_str = "[ ] False"
                else:
                    bool_str = "[ ] (unset)"
                present = raw is not None
                if raw is True:
                    value_style = resolved.style("success", bold=True)
                elif present:
                    value_style = resolved.style("dim")
                else:
                    value_style = resolved.style("text", italic=True)
                text.append(f"{bool_str}\n", style=value_style)
            elif ftype == "multiline":
                lines = val.split("\n")
                value_style = resolved.style("warning") if is_active else resolved.style("dim")
                if len(lines) <= 1:
                    text.append(f"'{val}'\n", style=value_style)
                else:
                    text.append("\n", style=resolved.style("text"))
                    for line in lines:
                        text.append(f"    | {line}\n", style=value_style)
            else:
                text.append(f"'{val}'\n", style=resolved.style("warning") if is_active else resolved.style("dim"))

        panel = Panel(
            text,
            title=escape_terminal_controls(self.title),
            border_style=resolved.style(tone_slot or "accent", color=fg, background=bg),
            expand=True,
        )
        return render_lines(panel, width, theme=markdown_theme(), color_system=resolved.color_system)


class InteractiveAsk:
    """Interactive Ask dialog collecting answers for one model-initiated AskRequest."""

    def __init__(self, request: AskRequest, *, revision_id: str = "") -> None:
        self.request = request
        self.revision_id = revision_id
        self.request_id = request.request_id
        self.title = request.title
        self.questions = request.questions
        self.active_index = 0
        self.values: dict[str, AskValue] = {}
        self.multiselect_focused_opt: dict[str, int] = {}
        self.validation_error: str | None = None

        for q in self.questions:
            if isinstance(q, AskMultiSelect):
                self.values[q.name] = []
                self.multiselect_focused_opt[q.name] = 0

    def _selections(self, name: str) -> list[str]:
        """The current multi-select ids; a malformed draft degrades to none."""
        value = self.values.get(name)
        return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []

    def handle_key(self, key: Key) -> AskAnswered | AskCancelled | None:
        self.validation_error = None
        if key.name in ("tab", "down"):
            if self.questions:
                self.active_index = (self.active_index + 1) % len(self.questions)
            return None
        if key.name in ("shift+tab", "up"):
            if self.questions:
                self.active_index = (self.active_index - 1) % len(self.questions)
            return None
        if key.name == "esc":
            return AskCancelled(request_id=self.request_id)
        if key.name in ("ctrl+s", "ctrl+enter"):
            return self._submit()

        if not self.questions:
            return None

        current = self.questions[self.active_index]
        name = current.name

        if isinstance(current, AskMultiSelect):
            opt_idx = self.multiselect_focused_opt.get(name, 0)
            if key.name in ("left", "h"):
                self.multiselect_focused_opt[name] = (opt_idx - 1) % len(current.options)
                return None
            if key.name in ("right", "l"):
                self.multiselect_focused_opt[name] = (opt_idx + 1) % len(current.options)
                return None
            if key.name == "space":
                chosen = self._selections(name)
                opt_id = current.options[opt_idx].id
                if opt_id in chosen:
                    chosen.remove(opt_id)
                else:
                    chosen.append(opt_id)
                self.values[name] = chosen
                return None
            if key.name == "enter":
                if self.active_index == len(self.questions) - 1:
                    return self._submit()
                self.active_index += 1
                return None
            return None

        ftype = current.field_type
        val = self.values.get(name, "")
        if current.options is not None:
            options = [option.id for option in current.options]
            if options and key.name in ("left", "right", "space"):
                index = options.index(val) if isinstance(val, str) and val in options else (-1 if key.name != "left" else 0)
                self.values[name] = options[(index + (-1 if key.name == "left" else 1)) % len(options)]
                return None
            if key.name != "enter":
                return None

        if ftype == "boolean":
            if key.name in ("space", "enter"):
                self.values[name] = not bool(self.values.get(name, False))
            return None

        if key.name == "enter":
            if ftype == "multiline":
                self.values[name] = str(val) + "\n"
                return None
            if self.active_index == len(self.questions) - 1:
                return self._submit()
            self.active_index += 1
            return None

        if key.name == "backspace":
            sval = "" if val is None else str(val)
            if sval:
                self.values[name] = sval[:-1]
            return None

        if key.text and not key.name.startswith("ctrl+") and not key.name.startswith("alt+"):
            self.values[name] = ("" if val is None else str(val)) + key.text
            return None

        return None

    def _submit(self) -> AskAnswered | None:
        for q in self.questions:
            if isinstance(q, AskMultiSelect):
                chosen = self._selections(q.name)
                if q.required and not chosen:
                    self.validation_error = f"'{q.label}' requires at least 1 selection"
                    return None
                if len(chosen) < q.min_selections:
                    self.validation_error = f"'{q.label}' requires at least {q.min_selections} selections"
                    return None
                if q.max_selections is not None and len(chosen) > q.max_selections:
                    self.validation_error = f"'{q.label}' allows at most {q.max_selections} selections"
                    return None
            else:
                val = self.values.get(q.name)
                if q.required and (val is None or val == ""):
                    self.validation_error = f"'{q.label}' is required"
                    return None
        return AskAnswered(request_id=self.request_id, values=self.values)

    def render_rows(self, width: int, styles: Styles | None = None) -> list[str]:
        resolved = _styles_arg(styles)
        text = Text()
        text.append(f"{escape_terminal_controls(self.title)}\n", style=resolved.style("accent", bold=True))
        text.append(f"Request: {escape_terminal_controls(self.request_id[:12])}\n\n", style=resolved.style("dim"))

        for i, q in enumerate(self.questions):
            is_active = i == self.active_index
            prefix = "❯ " if is_active else "  "
            cursor_style = resolved.style("accent", bold=True) if is_active else resolved.style("dim")
            label_style = resolved.style("text", bold=is_active)
            text.append(prefix, style=cursor_style)
            text.append(f"{escape_terminal_controls(q.label)}", style=label_style)
            if getattr(q, "required", False):
                text.append(" *", style=resolved.style("error"))
            text.append(": ")

            if isinstance(q, AskMultiSelect):
                chosen = set(self._selections(q.name))
                f_idx = self.multiselect_focused_opt.get(q.name, 0)
                text.append("\n")
                for o_idx, opt in enumerate(q.options):
                    is_opt_focused = is_active and (o_idx == f_idx)
                    box = "[x]" if opt.id in chosen else "[ ]"
                    opt_style = resolved.style("warning" if is_opt_focused else ("accent" if opt.id in chosen else "dim"))
                    text.append(f"    {box} {escape_terminal_controls(opt.label)}", style=opt_style)
                    if o_idx < len(q.options) - 1:
                        text.append("  ")
                text.append("\n")
            elif getattr(q, "options", None) is not None:
                val = self.values.get(q.name, "")
                text.append(f"<{escape_terminal_controls(str(val))}>\n", style=resolved.style("warning" if is_active else "dim"))
            elif getattr(q, "field_type", None) == "boolean":
                val = self.values.get(q.name)
                b_str = "true" if val is True else "false" if val is False else "unset"
                text.append(f"[{b_str}]\n", style=resolved.style("warning" if is_active else "dim"))
            else:
                val = self.values.get(q.name, "")
                text.append(f"'{escape_terminal_controls(str(val))}'\n", style=resolved.style("warning" if is_active else "dim"))

        if self.validation_error:
            text.append(f"\n  Error: {escape_terminal_controls(self.validation_error)}\n", style=resolved.style("error", bold=True))

        text.append("\n[Enter] next/submit    [Space] toggle/select    [Esc] cancel\n", style=resolved.style("dim"))
        panel = Panel(
            text,
            title="Interactive Ask",
            border_style=resolved.style("accent"),
            expand=True,
        )
        return render_lines(panel, width, theme=markdown_theme(), color_system=resolved.color_system)
