"""Concrete overlay outcome contracts: typed results, frozen snapshots, key routing."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from voidcode.sdk.ask import AskMultiSelect, AskRequest
from voidcode.sdk.services import ArtifactPage, ArtifactReference
from voidcode.sdk.views import ChoiceOption, FormField, FormViewData
from voidcode.tui.artifact_viewer import ArtifactOpenRequested, ArtifactPageRequested, ArtifactSource, InteractiveArtifactViewer
from voidcode.tui.keys import Key
from voidcode.tui.views import (
    AskAnswered,
    AskCancelled,
    InteractiveAsk,
    InteractiveForm,
    OverlayActionSelected,
    OverlayCancelled,
)


def test_overlay_action_outcome_snapshots_and_freezes_arguments() -> None:
    arguments: dict[str, object] = {"target": "a"}
    outcome = OverlayActionSelected(action="demo.open", arguments=arguments)
    arguments["target"] = "b"
    assert outcome.arguments == {"target": "a"}
    with pytest.raises(TypeError):
        outcome.arguments["target"] = "c"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        outcome.action = "other"  # type: ignore[misc]


def test_form_prefill_is_a_bounded_json_snapshot() -> None:
    initial: dict[str, object] = {"blob": {"nested": [1]}}
    form = InteractiveForm(
        FormViewData(
            fields=(FormField(name="blob", label="Blob"),),
            action="demo.save",
            initial_values=initial,
        )
    )
    initial["blob"] = {"nested": [2]}  # type: ignore[index]
    assert form.values == {"blob": {"nested": (1,)}}
    assert form.handle_key(Key("ctrl+s")) is None
    assert "must be text" in (form.validation_error or "")


def test_ask_overlay_returns_typed_answer_and_cancel_outcomes() -> None:
    request = AskRequest(
        request_id="ask-1",
        session_id="s1",
        revision_id="rev-1",
        owner="core",
        title="Ask",
        questions=(
            FormField(name="name", label="Name"),
            AskMultiSelect(name="tags", label="Tags", options=(ChoiceOption(id="a", label="A"),), required=False),
        ),
        context=(),
        media=(),
        timeout_ms=None,
    )
    overlay = InteractiveAsk(request)
    overlay.handle_key(Key("paste", text="Ada"))
    overlay.handle_key(Key("tab"))
    overlay.handle_key(Key("space"))
    outcome = overlay.handle_key(Key("ctrl+s"))
    assert isinstance(outcome, AskAnswered)
    assert outcome.request_id == "ask-1"
    assert outcome.values == {"name": "Ada", "tags": ["a"]}
    assert overlay.handle_key(Key("esc")) == AskCancelled(request_id="ask-1")


def test_artifact_overlay_returns_typed_page_cancel_and_open_outcomes() -> None:
    digest = "a" * 64
    reference = ArtifactReference(sha256=digest, bytes=4, media_type="text/plain")
    sources = (ArtifactSource(label="source", sha256=digest, reference=reference),)
    viewer = InteractiveArtifactViewer(sources, revision_id="rev-1", session_id="s1", title="Artifact")
    assert viewer.handle_key(Key("esc")) == OverlayCancelled()
    request_id = viewer.begin_loading(0)
    assert viewer.accept_page(request_id, ArtifactPage(artifact=reference, offset=0, data=b"abc", eof=False))
    assert viewer.handle_key(Key("pageDown")) == ArtifactPageRequested(offset=3)
    assert isinstance(viewer.handle_key(Key("o")), ArtifactOpenRequested)
