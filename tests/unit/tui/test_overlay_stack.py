"""Explicit overlay-stack transitions: mount, suspend, queue, decide, present."""

from __future__ import annotations

from voidcode.sdk.ask import AskRequest
from voidcode.sdk.tools import ToolCall
from voidcode.sdk.views import ChoiceOption, ChoicesViewData, ConfirmationViewData, FormField
from voidcode.tui.events import LiveInput
from voidcode.tui.gates import ApprovalGate
from voidcode.tui.overlays import OverlayStack
from voidcode.tui.views import InteractiveApproval, InteractiveAsk, InteractiveChoices, InteractiveConfirmation


def _confirmation(request_id: str, revision_id: str = "rev-1") -> InteractiveConfirmation:
    return InteractiveConfirmation(ConfirmationViewData(request_id=request_id, message=f"{request_id}?"), revision_id=revision_id)


def _live(revision_id: str = "rev-1") -> InteractiveChoices:
    return InteractiveChoices(ChoicesViewData(options=(ChoiceOption(id="a", label="A"),), action="pick"), revision_id=revision_id)


def _ask(request_id: str, revision_id: str = "rev-1") -> InteractiveAsk:
    return InteractiveAsk(
        AskRequest(
            request_id=request_id,
            session_id="s1",
            revision_id=revision_id,
            owner="test",
            title="Prompt",
            questions=(FormField(name="q", label="Q"),),
            context=(),
            media=(),
            timeout_ms=None,
        ),
        revision_id=revision_id,
    )


def _approval(request_id: str = "appr-1") -> InteractiveApproval:
    return InteractiveApproval(
        ApprovalGate(call=ToolCall(tool_name="write", arguments={}), request_id=request_id, run_id="run-1", revision_id="rev-1")
    )


def test_confirmation_shows_or_queues_then_presents_in_request_order() -> None:
    stack = OverlayStack()
    first = _confirmation("conf-1")
    second = _confirmation("conf-2")

    assert stack.request_confirmation(first, decided=frozenset(), live_input=None) == "shown"
    assert stack.active is first
    assert stack.request_confirmation(second, decided=frozenset(), live_input=None) == "queued"
    assert stack.queued_confirmation_ids == ("conf-2",)

    assert stack.decide("conf-1") is True
    assert stack.active is None
    presented = stack.present(decided=frozenset(), catalog_revision="rev-1")
    assert presented.kind == "confirmation"
    assert stack.active is second
    assert stack.queued_confirmation_ids == ()


def test_duplicate_and_decided_requests_never_reset_the_modal() -> None:
    stack = OverlayStack()
    overlay = _confirmation("conf-1")
    stack.request_confirmation(overlay, decided=frozenset(), live_input=None)

    assert stack.request_confirmation(_confirmation("conf-1"), decided=frozenset(), live_input=None) == "duplicate"
    assert stack.active is overlay
    assert stack.request_confirmation(_confirmation("conf-2"), decided=frozenset({"conf-2"}), live_input=None) == "ignored"
    assert stack.queued_confirmation_ids == ()


def test_live_view_suspends_under_its_own_confirmation_and_restores_once() -> None:
    stack = OverlayStack()
    live = _live()
    live_input = LiveInput(name="monitor", arguments={}, revision="rev-1", source=live)
    stack.show(live)

    assert stack.request_confirmation(_confirmation("cancel-1"), decided=frozenset(), live_input=live_input) == "suspended"
    assert stack.active is not live
    assert stack.suspended is not None and stack.suspended.overlay is live

    assert stack.decide("cancel-1") is True
    presented = stack.present(decided=frozenset(), catalog_revision="rev-1")
    assert presented.kind == "live"
    assert presented.suspended is not None and presented.suspended.live_input is live_input
    assert stack.active is live
    assert stack.suspended is None
    assert stack.present(decided=frozenset(), catalog_revision="rev-1").kind == "none"


def test_queued_gates_present_before_the_suspended_live_view() -> None:
    stack = OverlayStack()
    live = _live()
    live_input = LiveInput(name="monitor", arguments={}, revision="rev-1", source=live)
    stack.show(live)
    stack.request_confirmation(_confirmation("cancel-1"), decided=frozenset(), live_input=live_input)
    stack.request_confirmation(_confirmation("other-1"), decided=frozenset(), live_input=None)
    stack.request_ask(_ask("ask-1"), decided=frozenset())

    assert stack.decide("cancel-1") is True
    assert stack.present(decided=frozenset(), catalog_revision="rev-1").kind == "confirmation"
    assert stack.decide("other-1") is True
    assert stack.present(decided=frozenset(), catalog_revision="rev-1").kind == "ask"
    assert stack.decide("ask-1") is True
    assert stack.present(decided=frozenset(), catalog_revision="rev-1").kind == "live"
    assert stack.active is live


def test_activation_drops_queued_and_suspended_views_from_another_revision() -> None:
    stack = OverlayStack()
    live = _live("rev-1")
    stack.show(live)
    stack.request_confirmation(
        _confirmation("cancel-1", "rev-1"), decided=frozenset(), live_input=LiveInput(name="monitor", arguments={}, revision="rev-1", source=live)
    )
    stack.request_ask(_ask("ask-1", "rev-1"), decided=frozenset())

    assert stack.retain_revision("rev-2") is True
    assert stack.suspended is None
    assert stack.queued_confirmation_ids == ()
    assert stack.queued_ask_ids == ()
    assert stack.present(decided=frozenset(), catalog_revision="rev-2").kind == "none"


def test_approval_masks_a_new_confirmation_and_reset_keeps_it() -> None:
    stack = OverlayStack()
    approval = _approval()
    stack.show_approval(approval)
    assert stack.request_confirmation(_confirmation("conf-1"), decided=frozenset(), live_input=None) == "queued"
    assert stack.active is None
    assert stack.decide("appr-1") is False
    assert stack.approval is None

    stack.show(_confirmation("conf-2"))
    stack.reset_session()
    assert stack.active is None
    assert stack.queued_confirmation_ids == ()
