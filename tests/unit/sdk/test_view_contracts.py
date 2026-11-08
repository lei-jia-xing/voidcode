"""Behavioral checks for P5 view/command typed contracts and dispatch helpers."""

from __future__ import annotations

import pytest

from voidcode.sdk.commands import CommandCatalog, CommandDescriptor, CommandPresentation, CommandResult, ConfirmationRequest
from voidcode.sdk.tools import ToolEffect
from voidcode.sdk.views import (
    ChoicesViewData,
    ConfirmationViewData,
    FormViewData,
    NoticeViewData,
    PluginViewDescriptor,
    ViewsSnapshot,
    choices_dispatch,
    is_read_only_load,
    parse_view_data,
    validate_form_submission,
)


def _catalog(*effects_sets: tuple[ToolEffect, ...], names: tuple[str, ...] = ("get_item",)) -> CommandCatalog:
    return CommandCatalog(
        revision_id="rev-1",
        commands=tuple(
            CommandDescriptor(name=name, owner="o", description="d", effects=effects) for name, effects in zip(names, effects_sets, strict=True)
        ),
    )


def test_malformed_view_payloads_fail_visibly() -> None:
    with pytest.raises(ValueError):
        NoticeViewData.from_payload({"message": 5})  # type: ignore[dict-item]
    with pytest.raises(ValueError):
        NoticeViewData.from_payload({"message": "m", "zzz": 1})
    with pytest.raises(ValueError):
        NoticeViewData.from_payload({})
    with pytest.raises(ValueError):
        PluginViewDescriptor.from_payload({"owner": "o", "name": "n", "title": "t", "view_type": "notice", "data": {"message": 5}})
    with pytest.raises(ValueError):
        parse_view_data("bogus", {})  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        NoticeViewData.from_payload({"message": "m", "level": None})  # type: ignore[dict-item]
    with pytest.raises(ValueError):
        ViewsSnapshot.from_payload({"revision_id": "", "views": []})
    with pytest.raises(ValueError):
        ViewsSnapshot.from_payload(
            {
                "revision_id": "rev-9",
                "views": [
                    {"owner": "o", "name": "n", "title": "t", "view_type": "notice", "data": {"message": 5}},
                ],
            }
        )


def test_pending_command_result_confirmation_decoding() -> None:
    pending = CommandResult.from_payload(
        {
            "status": "pending_confirmation",
            "confirmation": {
                "request_id": "req-1",
                "owner": "goal",
                "command_name": "goal.set",
                "arguments": {"objective": "x"},
                "ui_type": "confirmation",
                "ui_data": {"request_id": "req-1", "message": "Confirm?"},
            },
        }
    )
    assert isinstance(pending.confirmation, ConfirmationRequest)
    assert pending.confirmation.ui_data["message"] == "Confirm?"
    assert ConfirmationViewData.from_payload(dict(pending.confirmation.ui_data)).request_id == "req-1"
    with pytest.raises(ValueError):
        CommandResult.from_payload({"status": "pending_confirmation"})
    with pytest.raises(ValueError):
        CommandResult.from_payload({"status": "error"})
    with pytest.raises(ValueError):
        CommandResult.from_payload({"status": "bogus"})  # type: ignore[dict-item]


def test_command_catalog_decode_rejects_bad_effects() -> None:
    catalog = CommandCatalog.from_payload(
        {
            "revision_id": "rev-1",
            "commands": [{"name": "get", "owner": "o", "description": "d", "effects": ["read"]}],
        }
    )
    assert catalog.commands[0].effects == (ToolEffect.READ,)
    with pytest.raises(ValueError):
        CommandCatalog.from_payload({"revision_id": "rev-1", "commands": [{"name": "x", "owner": "o", "description": "d", "effects": ["bogus"]}]})


def test_choices_dispatch_rejects_unknown_option() -> None:
    view = ChoicesViewData.from_payload({"options": [{"id": "a", "label": "A"}], "action": "act", "argument_name": "target"})
    assert choices_dispatch(view, "a") == ("act", {"target": "a"})
    with pytest.raises(ValueError, match="zz"):
        choices_dispatch(view, "zz")


def test_form_helper_rejects_garbage_types_and_keeps_boundaries() -> None:
    view = FormViewData.from_payload(
        {
            "fields": [
                {"name": "count", "label": "Count", "field_type": "integer", "required": False},
                {"name": "flag", "label": "Flag", "field_type": "boolean", "required": False},
                {"name": "note", "label": "Note", "field_type": "text", "required": False},
            ],
            "action": "act",
        }
    )
    # Booleans are never accepted as integers; garbage is not coerced.
    with pytest.raises(ValueError):
        validate_form_submission(view, {"flag": 1})
    with pytest.raises(ValueError):
        validate_form_submission(view, {"count": [1]})
    # Scientific notation is not an integer; non-finite is not a number.
    with pytest.raises(ValueError):
        validate_form_submission(view, {"count": "1e3"})
    number_view = FormViewData.from_payload({"fields": [{"name": "ratio", "label": "R", "field_type": "number"}], "action": "act"})
    with pytest.raises(ValueError):
        validate_form_submission(number_view, {"ratio": "1e999"})
    # Loader metadata beyond declared fields is ignored, never an error.
    assert validate_form_submission(view, {"count": "3", "unrelated_meta": {"x": 1}}) == {"count": 3}


def test_form_helper_nullable_forms_stay_distinct() -> None:
    view = FormViewData.from_payload(
        {
            "fields": [{"name": "budget", "label": "B", "field_type": "integer", "required": False}],
            "action": "act",
        }
    )
    nullable = {"type": "object", "properties": {"budget": {"anyOf": [{"type": "integer"}, {"type": "null"}]}}}
    # Absent optional omits; present blank on nullable clears.
    assert validate_form_submission(view, {}, action_schema=nullable) == {}
    assert validate_form_submission(view, {"budget": ""}, action_schema=nullable) == {"budget": None}
    # Without nullability the same blank omits instead of clearing.
    assert validate_form_submission(view, {"budget": ""}) == {}


def test_read_only_load_requires_exactly_read_effect() -> None:
    view = FormViewData.from_payload({"fields": [], "action": "edit", "load_action": "get_item"})
    assert is_read_only_load(view, _catalog((ToolEffect.READ,))) is True
    assert is_read_only_load(view, _catalog(())) is False
    assert is_read_only_load(view, _catalog((ToolEffect.READ, ToolEffect.SESSION))) is False
    assert is_read_only_load(view, _catalog((ToolEffect.WRITE,))) is False
    assert is_read_only_load(view, _catalog((ToolEffect.READ,), names=("other",))) is False
    no_load = FormViewData.from_payload({"fields": [], "action": "edit"})
    assert is_read_only_load(no_load, _catalog((ToolEffect.READ,))) is False


def test_numeric_prefill_submits_unchanged_and_nullable_blank_clears() -> None:
    """Native numeric prefill (initial_values + load_action) submits as-is; blank clears nullable."""
    from voidcode.tui.keys import Key
    from voidcode.tui.views import InteractiveForm

    view = FormViewData.from_payload(
        {
            "fields": [
                {"name": "count", "label": "Count", "field_type": "integer", "required": False},
                {"name": "ratio", "label": "Ratio", "field_type": "number", "required": False},
                {"name": "budget", "label": "Budget", "field_type": "integer", "required": False},
            ],
            "action": "set_limits",
            "initial_values": {"count": 3},
            "load_action": "get_item",
        }
    )
    nullable = {"type": "object", "properties": {"budget": {"anyOf": [{"type": "integer"}, {"type": "null"}]}}}
    form = InteractiveForm(view, action_schema=nullable)
    form.populate_from_load_action({"ratio": 0.5, "budget": 7, "unrelated_meta": "ignored"})
    assert form.handle_key(Key("ctrl+s")) == {
        "action": "set_limits",
        "arguments": {"count": 3, "ratio": 0.5, "budget": 7},
    }
    form.values["budget"] = ""
    assert form.handle_key(Key("ctrl+s")) == {
        "action": "set_limits",
        "arguments": {"count": 3, "ratio": 0.5, "budget": None},
    }


def test_command_presentation_is_bounded_validated_and_roundtrips() -> None:
    options = [{"id": "explore", "label": "Explore"}]
    presentation = CommandPresentation(
        title="Profiles",
        view_type="choices",
        data={"options": options, "action": "item.show", "argument_name": "name"},
    )
    options.clear()
    result = CommandResult(status="ok", output="Profiles", presentation=presentation)
    decoded = CommandResult.from_payload(result.as_payload())
    assert decoded.presentation is not None
    choices = ChoicesViewData.from_payload(decoded.presentation.data)
    assert choices_dispatch(choices, "explore") == ("item.show", {"name": "explore"})
    with pytest.raises(ValueError, match="only successful"):
        CommandResult(status="error", error="failed", presentation=presentation)
    with pytest.raises(ValueError):
        CommandPresentation(title="bad", view_type="choices", data={"options": []})
    with pytest.raises(ValueError, match="bytes limit"):
        CommandPresentation(title="large", view_type="markdown", data={"content": "x" * (64 * 1024)})
    with pytest.raises(ValueError):
        CommandResult.from_payload({"status": "ok", "presentation": {"title": "no", "view_type": "confirmation", "data": {}}})


def test_form_options_are_immutable_and_reject_unknown_values() -> None:
    form = FormViewData.from_payload({"action": "pick", "fields": [{"name": "target", "label": "Target", "options": [{"id": "a", "label": "A"}]}]})
    assert validate_form_submission(form, {"target": "a"}) == {"target": "a"}
    with pytest.raises(ValueError, match="available choices"):
        validate_form_submission(form, {"target": "unknown"})
    with pytest.raises(ValueError, match="required"):
        validate_form_submission(form, {})
    with pytest.raises(ValueError, match="unique"):
        FormViewData.from_payload(
            {"action": "pick", "fields": [{"name": "target", "label": "T", "options": [{"id": "a", "label": "A"}, {"id": "a", "label": "Again"}]}]}
        )


def test_direct_command_presentation_cannot_fake_a_confirmation() -> None:
    with pytest.raises(ValueError, match="cannot carry confirmations"):
        CommandPresentation(title="Invalid", view_type="confirmation", data={"request_id": "not-durable", "message": "Confirm?"})  # type: ignore[arg-type]


def test_form_fixed_arguments_are_submitted_without_editable_shadow_fields() -> None:
    form = FormViewData.from_payload(
        {"action": "todo.update", "arguments": {"expected_revision": 7, "task_id": "opaque"}, "fields": [{"name": "op", "label": "Operation"}]}
    )
    assert validate_form_submission(form, {"op": "done", "expected_revision": 99}) == {"op": "done", "expected_revision": 7, "task_id": "opaque"}
    with pytest.raises(ValueError, match="cannot shadow"):
        FormViewData.from_payload({"action": "edit", "arguments": {"revision": 7}, "fields": [{"name": "revision", "label": "Revision"}]})
