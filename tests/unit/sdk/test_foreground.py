"""Draft/receipt validation, context shape, and suspend intent for compositions."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

import pytest

from tests.fixtures.fake_services import FakeServices
from voidcode.sdk.foreground import (
    CompositionDraft,
    CompositionNestedRefused,
    CompositionReceipt,
    ForegroundContext,
    SuspendComposition,
)
from voidcode.sdk.suspend import ParkError

if TYPE_CHECKING:
    from voidcode.sdk.invocation import ToolContext
    from voidcode.sdk.services import PluginServices, ToolAwaitResult, ToolInvocationReceipt
    from voidcode.sdk.tools import ToolResult


def test_composition_draft_round_trip() -> None:
    draft = CompositionDraft(cell_source="print(1)", namespace_key="ns-1", tool_allowlist=("read", "glob"))
    assert CompositionDraft.from_payload(draft.as_payload()) == draft
    bare = CompositionDraft(cell_source="x = 1", namespace_key="ns-2")
    assert bare.tool_allowlist is None
    assert CompositionDraft.from_payload(bare.as_payload()) == bare


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cell_source": "", "namespace_key": "ns"},
        {"cell_source": "x", "namespace_key": ""},
        {"cell_source": "x", "namespace_key": "ns", "tool_allowlist": ("read", "")},
        {"cell_source": "x", "namespace_key": "ns", "tool_allowlist": ("read", "read")},
        {"cell_source": "x", "namespace_key": "ns", "tool_allowlist": ("read", 3)},
        {"cell_source": None, "namespace_key": "ns"},
    ],
)
def test_composition_draft_rejects_bad_shape(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        CompositionDraft(**kwargs)  # type: ignore[arg-type]


def test_composition_draft_from_payload_is_strict() -> None:
    good = {"cell_source": "x", "namespace_key": "ns", "tool_allowlist": ["read"]}
    assert CompositionDraft.from_payload(good).tool_allowlist == ("read",)
    assert CompositionDraft.from_payload({"cell_source": "x", "namespace_key": "ns"}).tool_allowlist is None
    with pytest.raises(ValueError, match="unknown"):
        CompositionDraft.from_payload({**good, "extra": 1})
    with pytest.raises(ValueError, match="missing"):
        CompositionDraft.from_payload({"cell_source": "x"})
    with pytest.raises(ValueError):
        CompositionDraft.from_payload({**good, "tool_allowlist": "read"})
    with pytest.raises(ValueError, match="object"):
        CompositionDraft.from_payload([])  # type: ignore[arg-type]


def test_composition_receipt_payload_and_validation() -> None:
    receipt = CompositionReceipt(composition_id="comp-1", tool_name="eval_exec", depth=2)
    assert receipt.as_payload() == {"composition_id": "comp-1", "tool_name": "eval_exec", "depth": 2}
    with pytest.raises(ValueError):
        CompositionReceipt(composition_id="", tool_name="eval_exec", depth=0)
    with pytest.raises(ValueError):
        CompositionReceipt(composition_id="c", tool_name="", depth=0)
    with pytest.raises(ValueError):
        CompositionReceipt(composition_id="c", tool_name="t", depth=-1)
    with pytest.raises(ValueError):
        CompositionReceipt(composition_id="c", tool_name="t", depth=True)  # type: ignore[arg-type]


class _FakeForegroundContext:
    """Minimal driver handle proving the Protocol shape; single-shot terminal staging."""

    def __init__(self) -> None:
        self._staged: ToolResult | None = None

    @property
    def composition_id(self) -> str:
        return "comp-1"

    @property
    def session_id(self) -> str:
        return "s-1"

    @property
    def owner(self) -> str:
        return "eval"

    @property
    def payload(self) -> Mapping[str, object]:
        return CompositionDraft(cell_source="x", namespace_key="ns").as_payload()

    @property
    def abort_signal(self) -> threading.Event:
        return threading.Event()

    def invoke[T](self, callback: Callable[[ToolContext, PluginServices], T]) -> T:
        raise NotImplementedError

    def wait_tool(self, receipt: ToolInvocationReceipt) -> ToolAwaitResult:
        raise NotImplementedError

    def stage_terminal(self, result: ToolResult, *, for_tool: str) -> None:
        if self._staged is not None:
            raise ValueError("composition already terminal")
        _ = for_tool
        self._staged = result


def test_foreground_context_protocol_shape() -> None:
    assert isinstance(_FakeForegroundContext(), ForegroundContext)
    assert not isinstance(object(), ForegroundContext)


def test_single_shot_stage_terminal_fake_refuses_second_call() -> None:
    from voidcode.sdk.tools import TextOutput, ToolSuccess

    ctx = _FakeForegroundContext()
    first = ToolSuccess(tool_name="eval_exec", output=TextOutput("one"))
    ctx.stage_terminal(first, for_tool="eval_exec")
    with pytest.raises(ValueError, match="already terminal"):
        ctx.stage_terminal(first, for_tool="eval_exec")


def test_suspend_composition_carries_draft_intent() -> None:
    draft = CompositionDraft(cell_source="x", namespace_key="ns")
    raised = SuspendComposition(draft, idempotency_key="idem-1")
    assert raised.park_kind == "composition"
    assert raised.draft == draft
    assert raised.idempotency_key == "idem-1"
    assert CompositionDraft.from_payload(raised.intent["draft"]) == draft  # type: ignore[arg-type]
    assert raised.intent["idempotency_key"] == "idem-1"
    bare = SuspendComposition(draft)
    assert bare.idempotency_key is None
    assert "idempotency_key" not in bare.intent
    with pytest.raises(ValueError):
        SuspendComposition("nope")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        SuspendComposition(draft, idempotency_key="")


def test_composition_nested_refused_is_park_error() -> None:
    assert issubclass(CompositionNestedRefused, ParkError)


def test_fake_services_suspend_stub_raises() -> None:
    fake = FakeServices()
    with pytest.raises(NotImplementedError):
        fake.suspend_foreground_composition(CompositionDraft(cell_source="x", namespace_key="ns"))
