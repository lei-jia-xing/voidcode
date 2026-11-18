"""Ask draft/request strictness, the shared answer validator matrix, and gates/client touchpoints."""

from __future__ import annotations

import pytest

from tests.fixtures.fake_services import FakeServices
from voidcode.sdk.ask import (
    AskAlreadyParked,
    AskAnswer,
    AskContextBlock,
    AskError,
    AskMultiSelect,
    AskRequest,
    AskRequestDraft,
    AskResultBody,
    AskValidationError,
    SuspendAsk,
    validate_ask_answer,
)
from voidcode.sdk.commands import ConfirmationRequest
from voidcode.sdk.gates import AskGate, ConfirmationGate
from voidcode.sdk.services import ArtifactReference
from voidcode.sdk.views import ChoiceOption, FormField

GOOD_SHA = "ab" * 32


def _request(*questions: FormField | AskMultiSelect) -> AskRequest:
    return AskRequest(
        request_id="ask-1",
        session_id="s-1",
        revision_id="rev-1",
        owner="ask",
        title="Pick one",
        questions=questions,
        context=(),
        media=(),
        timeout_ms=None,
    )


def test_multi_select_round_trip() -> None:
    multi = AskMultiSelect(
        name="tags",
        label="Tags",
        options=(ChoiceOption(id="a", label="A"), ChoiceOption(id="b", label="B")),
        required=True,
        min_selections=1,
        max_selections=2,
        context="help",
    )
    assert AskMultiSelect.from_payload(multi.as_payload()) == multi
    bare = AskMultiSelect(name="t", label="T", options=(ChoiceOption(id="a", label="A"),))
    assert bare.required is True and bare.min_selections == 0 and bare.max_selections is None and bare.context is None
    assert AskMultiSelect.from_payload(bare.as_payload()) == bare


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "", "label": "L", "options": (ChoiceOption(id="a", label="A"),)},
        {"name": "n", "label": "", "options": (ChoiceOption(id="a", label="A"),)},
        {"name": "n", "label": "L", "options": ()},
        {"name": "n", "label": "L", "options": (ChoiceOption(id="", label="A"),)},
        {"name": "n", "label": "L", "options": (ChoiceOption(id="a", label="A"), ChoiceOption(id="a", label="B"))},
        {"name": "n", "label": "L", "options": (ChoiceOption(id="a", label="A"),), "required": 1},
        {"name": "n", "label": "L", "options": (ChoiceOption(id="a", label="A"),), "min_selections": -1},
        {"name": "n", "label": "L", "options": (ChoiceOption(id="a", label="A"),), "min_selections": 2, "max_selections": 1},
        {"name": "n", "label": "L", "options": (ChoiceOption(id="a", label="A"),), "context": 5},
    ],
)
def test_multi_select_rejects_bad_shape(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        AskMultiSelect(**kwargs)  # type: ignore[arg-type]


def test_context_block_reuses_view_validation() -> None:
    block = AskContextBlock(view_type="notice", data={"message": "hi"})
    assert block.as_payload() == {"view_type": "notice", "data": {"message": "hi", "level": "info", "style": None}}
    with pytest.raises(ValueError):
        AskContextBlock(view_type="choices", data={})  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        AskContextBlock(view_type="notice", data={"wrong": 1})


def test_draft_and_request_round_trip() -> None:
    draft = AskRequestDraft(
        title="Title",
        questions=(FormField(name="name", label="Name"), AskMultiSelect(name="tags", label="Tags", options=(ChoiceOption(id="a", label="A"),))),
        context=(AskContextBlock(view_type="markdown", data={"content": "# hi"}),),
        media=(ArtifactReference(sha256=GOOD_SHA, bytes=10, media_type="image/png"),),
        timeout_ms=5000,
    )
    assert AskRequestDraft.from_payload(draft.as_payload()) == draft
    request = AskRequest(
        request_id="ask-1",
        session_id="s-1",
        revision_id="rev-1",
        owner="ask",
        title=draft.title,
        questions=draft.questions,
        context=draft.context,
        media=draft.media,
        timeout_ms=draft.timeout_ms,
    )
    assert AskRequest.from_payload(request.as_payload()) == request


@pytest.mark.parametrize("timeout", [0, -1, 1.5, "5000", 9_223_372_036_854_775_808, True])
def test_draft_rejects_bad_timeout(timeout: object) -> None:
    with pytest.raises(ValueError, match="timeout_ms"):
        AskRequestDraft(title="T", questions=(FormField(name="a", label="A"),), timeout_ms=timeout)  # type: ignore[arg-type]


def test_draft_rejects_blank_title_no_questions_and_dup_names() -> None:
    with pytest.raises(ValueError, match="title"):
        AskRequestDraft(title="  ", questions=(FormField(name="a", label="A"),))
    with pytest.raises(ValueError, match="at least one"):
        AskRequestDraft(title="T", questions=())
    with pytest.raises(ValueError, match="unique"):
        AskRequestDraft(title="T", questions=(FormField(name="a", label="A"), FormField(name="a", label="B")))
    with pytest.raises(ValueError, match="non-empty"):
        AskRequestDraft(title="T", questions=(FormField(name="", label="A"),))


@pytest.mark.parametrize(
    "reference",
    [
        ArtifactReference(sha256="xyz", bytes=1, media_type="image/png"),
        ArtifactReference(sha256="AB" * 32, bytes=1, media_type="image/png"),
        ArtifactReference(sha256=GOOD_SHA, bytes=-1, media_type="image/png"),
        ArtifactReference(sha256=GOOD_SHA, bytes=1, media_type=""),
    ],
)
def test_draft_rejects_bad_media_shape(reference: ArtifactReference) -> None:
    with pytest.raises(ValueError):
        AskRequestDraft(title="T", questions=(FormField(name="a", label="A"),), media=(reference,))


def test_draft_from_payload_is_strict() -> None:
    good = {"title": "T", "questions": [{"kind": "field", "name": "a", "label": "A"}]}
    assert AskRequestDraft.from_payload(good).questions[0].name == "a"
    with pytest.raises(ValueError, match="unknown"):
        AskRequestDraft.from_payload({**good, "extra": 1})
    with pytest.raises(ValueError, match="missing"):
        AskRequestDraft.from_payload({"title": "T"})
    with pytest.raises(ValueError, match="kind"):
        AskRequestDraft.from_payload({"title": "T", "questions": [{"name": "a", "label": "A"}]})
    with pytest.raises(ValueError, match="kind"):
        AskRequestDraft.from_payload({"title": "T", "questions": [{"kind": "bogus", "name": "a", "label": "A"}]})


def test_request_rejects_blank_ids() -> None:
    kwargs: dict[str, object] = {
        "request_id": "ask-1",
        "session_id": "s-1",
        "revision_id": "rev-1",
        "owner": "ask",
        "title": "T",
        "questions": (FormField(name="a", label="A"),),
        "context": (),
        "media": (),
        "timeout_ms": None,
    }
    for field in ("request_id", "session_id", "revision_id", "owner"):
        bad = dict(kwargs)
        bad[field] = ""
        with pytest.raises(ValueError, match="non-empty"):
            AskRequest(**bad)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "values", "expected"),
    [
        (FormField(name="ok", label="OK", field_type="boolean"), {"ok": True}, {"ok": True}),
        (FormField(name="ok", label="OK", field_type="boolean"), {"ok": False}, {"ok": False}),
        (FormField(name="ok", label="OK", field_type="boolean", required=False), {}, {}),
        (FormField(name="ok", label="OK", field_type="boolean", required=False), {"ok": None}, {}),
        (FormField(name="ok", label="OK", field_type="boolean", required=False), {"ok": "  "}, {}),
        (FormField(name="n", label="N", field_type="integer"), {"n": 3}, {"n": 3}),
        (FormField(name="n", label="N", field_type="integer"), {"n": "3"}, {"n": 3}),
        (FormField(name="n", label="N", field_type="integer"), {"n": "  -3  "}, {"n": -3}),
        (FormField(name="n", label="N", field_type="integer", required=False), {}, {}),
        (FormField(name="n", label="N", field_type="integer", required=False), {"n": ""}, {}),
        (FormField(name="f", label="F", field_type="number"), {"f": 2.5}, {"f": 2.5}),
        (FormField(name="f", label="F", field_type="number"), {"f": 2}, {"f": 2}),
        (FormField(name="f", label="F", field_type="number"), {"f": "1e3"}, {"f": 1000.0}),
        (FormField(name="f", label="F", field_type="number"), {"f": ".5"}, {"f": 0.5}),
        (FormField(name="t", label="T"), {"t": "  hi  "}, {"t": "  hi  "}),
        (FormField(name="t", label="T", required=False), {}, {}),
        (
            FormField(name="pick", label="Pick", options=(ChoiceOption(id="a", label="A"),)),
            {"pick": "a"},
            {"pick": "a"},
        ),
        (
            AskMultiSelect(name="tags", label="Tags", options=(ChoiceOption(id="a", label="A"), ChoiceOption(id="b", label="B"))),
            {"tags": ["a", "b"]},
            {"tags": ("a", "b")},
        ),
        (
            AskMultiSelect(name="tags", label="Tags", options=(ChoiceOption(id="a", label="A"),), required=False),
            {},
            {},
        ),
        (
            AskMultiSelect(name="tags", label="Tags", options=(ChoiceOption(id="a", label="A"),), required=False),
            {"tags": []},
            {},
        ),
        (
            AskMultiSelect(
                name="tags",
                label="Tags",
                options=(ChoiceOption(id="a", label="A"), ChoiceOption(id="b", label="B"), ChoiceOption(id="c", label="C")),
                min_selections=1,
                max_selections=2,
            ),
            {"tags": ("b",)},
            {"tags": ("b",)},
        ),
    ],
)
def test_validate_accepts(field: FormField | AskMultiSelect, values: dict[str, object], expected: dict[str, object]) -> None:
    assert validate_ask_answer(_request(field), values) == expected


@pytest.mark.parametrize(
    ("field", "values", "match"),
    [
        (FormField(name="ok", label="OK", field_type="boolean"), {}, "required"),
        (FormField(name="ok", label="OK", field_type="boolean"), {"ok": None}, "required"),
        (FormField(name="ok", label="OK", field_type="boolean"), {"ok": ""}, "required"),
        (FormField(name="ok", label="OK", field_type="boolean"), {"ok": "yes"}, "boolean"),
        (FormField(name="ok", label="OK", field_type="boolean"), {"ok": 1}, "boolean"),
        (FormField(name="n", label="N", field_type="integer"), {"n": "1e3"}, "integer"),
        (FormField(name="n", label="N", field_type="integer"), {"n": "3.0"}, "integer"),
        (FormField(name="n", label="N", field_type="integer"), {"n": 3.0}, "text"),
        (FormField(name="n", label="N", field_type="integer"), {"n": True}, "text"),
        (FormField(name="n", label="N", field_type="integer"), {"n": "abc"}, "integer"),
        (FormField(name="n", label="N", field_type="integer"), {}, "required"),
        (FormField(name="n", label="N", field_type="integer"), {"n": "  "}, "required"),
        (FormField(name="f", label="F", field_type="number"), {"f": float("inf")}, "finite"),
        (FormField(name="f", label="F", field_type="number"), {"f": float("nan")}, "finite"),
        (FormField(name="f", label="F", field_type="number"), {"f": "inf"}, "number"),
        (FormField(name="f", label="F", field_type="number"), {"f": "abc"}, "number"),
        (FormField(name="f", label="F", field_type="number"), {"f": True}, "text"),
        (FormField(name="t", label="T"), {}, "required"),
        (FormField(name="t", label="T"), {"t": "   "}, "required"),
        (FormField(name="t", label="T"), {"t": 5}, "text"),
        (
            FormField(name="pick", label="Pick", options=(ChoiceOption(id="a", label="A"),)),
            {"pick": "zz"},
            "choices",
        ),
        (
            FormField(name="pick", label="Pick", options=(ChoiceOption(id="a", label="A"),)),
            {"pick": 5},
            "choices",
        ),
        (
            AskMultiSelect(name="tags", label="Tags", options=(ChoiceOption(id="a", label="A"),)),
            {},
            "required",
        ),
        (
            AskMultiSelect(name="tags", label="Tags", options=(ChoiceOption(id="a", label="A"),)),
            {"tags": None},
            "required",
        ),
        (
            AskMultiSelect(name="tags", label="Tags", options=(ChoiceOption(id="a", label="A"),)),
            {"tags": []},
            "required",
        ),
        (
            AskMultiSelect(name="tags", label="Tags", options=(ChoiceOption(id="a", label="A"),)),
            {"tags": "a"},
            "list of choice ids",
        ),
        (
            AskMultiSelect(name="tags", label="Tags", options=(ChoiceOption(id="a", label="A"),)),
            {"tags": ["zz"]},
            "choices",
        ),
        (
            AskMultiSelect(name="tags", label="Tags", options=(ChoiceOption(id="a", label="A"),)),
            {"tags": ["a", "a"]},
            "repeat",
        ),
        (
            AskMultiSelect(name="tags", label="Tags", options=(ChoiceOption(id="a", label="A"), ChoiceOption(id="b", label="B")), min_selections=2),
            {"tags": ["a"]},
            "at least 2",
        ),
        (
            AskMultiSelect(name="tags", label="Tags", options=(ChoiceOption(id="a", label="A"), ChoiceOption(id="b", label="B")), max_selections=1),
            {"tags": ["a", "b"]},
            "at most 1",
        ),
    ],
)
def test_validate_rejects(field: FormField | AskMultiSelect, values: dict[str, object], match: str) -> None:
    with pytest.raises(AskValidationError, match=match) as excinfo:
        validate_ask_answer(_request(field), values)
    assert excinfo.value.field == field.name
    assert match in excinfo.value.reason


def test_validate_rejects_unknown_extras_and_bad_inputs() -> None:
    request = _request(FormField(name="a", label="A"))
    with pytest.raises(AskValidationError, match="unknown") as excinfo:
        validate_ask_answer(request, {"a": "x", "zzz": 1})
    assert excinfo.value.field == "zzz"
    with pytest.raises(ValueError, match="AskRequest"):
        validate_ask_answer("nope", {})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="object"):
        validate_ask_answer(request, [])  # type: ignore[arg-type]


def test_answer_and_result_body_shapes() -> None:
    answer = AskAnswer(request_id="ask-1", values={"a": "x"}, status="answered", terminal_sequence=7)
    assert answer.as_payload() == {"request_id": "ask-1", "values": {"a": "x"}, "status": "answered", "terminal_sequence": 7}
    with pytest.raises(ValueError):
        AskAnswer(request_id="", values={}, status="answered", terminal_sequence=1)
    with pytest.raises(ValueError):
        AskAnswer(request_id="a", values={}, status="bogus", terminal_sequence=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        AskAnswer(request_id="a", values={}, status="answered", terminal_sequence=0)
    body = AskResultBody(answers={"a": "x"})
    assert body.as_payload() == {"answers": {"a": "x"}, "status": "answered"}
    with pytest.raises(ValueError):
        AskResultBody(answers={}, status="cancelled")  # type: ignore[arg-type]


def test_suspend_ask_carries_draft_intent() -> None:
    draft = AskRequestDraft(title="T", questions=(FormField(name="a", label="A"),))
    raised = SuspendAsk(draft, idempotency_key="idem-1")
    assert raised.park_kind == "ask"
    assert raised.draft == draft
    assert AskRequestDraft.from_payload(raised.intent["draft"]) == draft  # type: ignore[arg-type]
    bare = SuspendAsk(draft)
    assert "idempotency_key" not in bare.intent
    with pytest.raises(ValueError):
        SuspendAsk("nope")  # type: ignore[arg-type]


def test_ask_errors_are_plain_values() -> None:
    assert issubclass(AskError, ValueError)
    assert issubclass(AskValidationError, ValueError)
    err = AskValidationError(field="a", reason="is required.")
    assert err.field == "a" and "required" in err.reason and "required" in str(err)


def test_ask_already_parked_is_an_ask_error() -> None:
    assert issubclass(AskAlreadyParked, AskError)
    assert isinstance(AskAlreadyParked("session holds a live park"), AskError)


def test_fake_services_ask_stub_raises() -> None:
    fake = FakeServices()
    with pytest.raises(NotImplementedError):
        fake.suspend_for_ask(AskRequestDraft(title="T", questions=(FormField(name="a", label="A"),)))


def _ask_gate(request_id: str, revision_id: str = "rev-1") -> AskGate:
    request = AskRequest(
        request_id=request_id,
        session_id="s-1",
        revision_id=revision_id,
        owner="ask",
        title="T",
        questions=(FormField(name="a", label="A"),),
        context=(),
        media=(),
        timeout_ms=None,
    )
    return AskGate.from_request(request, revision_id=revision_id)


def _confirmation_gate(request_id: str, revision_id: str = "rev-1") -> ConfirmationGate:
    request = ConfirmationRequest(
        request_id=request_id,
        owner="goal",
        command_name="goal.set",
        arguments={},
        ui_type="confirmation",
        ui_data={"request_id": request_id, "message": "Apply?"},
    )
    return ConfirmationGate.from_request(request, revision_id=revision_id)


def test_ask_gate_reducer_and_arrival_order() -> None:
    from voidcode.sdk.gates import (
        AskRequested,
        ConfirmationRequested,
        GateDecided,
        GateState,
        pending_gates,
        reduce_gates,
    )

    first = _confirmation_gate("conf-1")
    ask = _ask_gate("ask-1")
    second = _confirmation_gate("conf-2")
    state = GateState()
    state = reduce_gates(state, ConfirmationRequested(first))
    state = reduce_gates(state, AskRequested(ask))
    state = reduce_gates(state, ConfirmationRequested(second))
    assert [gate.request_id for gate in pending_gates(state)] == ["conf-1", "ask-1", "conf-2"]
    repeat = reduce_gates(state, AskRequested(ask))
    assert repeat is state
    decided = reduce_gates(state, GateDecided("ask-1"))
    assert [gate.request_id for gate in pending_gates(decided)] == ["conf-1", "conf-2"]
    assert "ask-1" in decided.decided
    reopened = reduce_gates(decided, AskRequested(ask))
    assert reopened is decided


def test_ask_gate_activation_drops_stale_revision() -> None:
    from voidcode.sdk.gates import ActivationCommitted, AskRequested, GateState, pending_gates, reduce_gates

    state = reduce_gates(GateState(), AskRequested(_ask_gate("ask-old", revision_id="rev-1")))
    assert [gate.request_id for gate in pending_gates(state)] == ["ask-old"]
    state = reduce_gates(state, ActivationCommitted("rev-2"))
    assert pending_gates(state) == ()
    assert "ask-old" in state.decided
    state = reduce_gates(state, AskRequested(_ask_gate("ask-old", revision_id="rev-1")))
    assert pending_gates(state) == ()


def test_ask_gate_validates_request() -> None:
    from voidcode.sdk.gates import AskGate

    with pytest.raises(ValueError):
        AskGate(request="nope", revision_id="rev-1")  # type: ignore[arg-type]


def test_interaction_fields_and_pending_ask() -> None:
    from voidcode.sdk.client import ResumeSpec, RunResult, RunSpec

    assert RunSpec(prompt="hi").interaction is None
    assert RunSpec(prompt="hi", interaction="unattended").interaction == "unattended"
    assert ResumeSpec().interaction is None
    assert ResumeSpec(interaction="interactive").interaction == "interactive"
    with pytest.raises(ValueError, match="interaction"):
        RunSpec(prompt="hi", interaction="bogus")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="interaction"):
        ResumeSpec(interaction="bogus")  # type: ignore[arg-type]
    result = RunResult(session_id="s", run_id="r", revision_id="v", status="waiting")
    assert "pending_ask" not in result.as_payload()
    parked = RunResult(session_id="s", run_id="r", revision_id="v", status="waiting", pending_ask={"request_id": "ask-1"})
    payload = parked.as_payload()
    assert payload["pending_ask"] == {"request_id": "ask-1"}
    assert RunResult.from_payload(payload) == parked
