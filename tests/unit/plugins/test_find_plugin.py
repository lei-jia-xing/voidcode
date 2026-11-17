"""Behavioral find: prescreen, judge, validate, and tell every status apart."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.fixtures.fake_services import FakeServices
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.capabilities.files.errors import ToolDiagnosticError
from voidcode.capabilities.files.grep_tool import GrepTool
from voidcode.capabilities.files.read_tool import ReadResultBody, ReadTool
from voidcode.core.engine import Core
from voidcode.plugins.find import create_plugin
from voidcode.plugins.find.find_tool import FindResultBody, FindTool
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import StructuredModelResult
from voidcode.sdk.tools import ToolCall, ToolEffect, ToolFailure, ToolResult, ToolSuccess

_CART = '''"""Shopping cart."""


def checkout(cart):
    total = sum(item.price for item in cart)
    total = _apply_discount(total)
    return total


def _apply_discount(total):
    if total > 100:
        return total * 0.9
    return total
'''

_TAX = '''"""Tax helpers."""


def compute_tax(total):
    return total * 0.2
'''

_SHIP = '''"""Shipping."""


def ship_after_checkout(order):
    return order
'''

_WORDS = "the and or\n"


def _tree(root: Path) -> Path:
    (root / "shop").mkdir(parents=True, exist_ok=True)
    (root / "shop" / "cart.py").write_text(_CART, encoding="utf-8")
    (root / "shop" / "tax.py").write_text(_TAX, encoding="utf-8")
    (root / "shop" / "ship.py").write_text(_SHIP, encoding="utf-8")
    (root / "misc").mkdir(exist_ok=True)
    (root / "misc" / "words.txt").write_text(_WORDS, encoding="utf-8")
    (root / "big").mkdir(exist_ok=True)
    (root / "big" / "gen.py").write_text("".join(f"toktok = {index}\n" for index in range(210)), encoding="utf-8")
    (root / "big2").mkdir(exist_ok=True)
    many = "".join(f"snipmark = {index}\n" if index != 2 else f"snipmark_{'y' * 600} = {index}\n" for index in range(16))
    (root / "big2" / "many.py").write_text(many, encoding="utf-8")
    return root


def _context(root: Path) -> ToolContext:
    return ToolContext(
        workspace=root,
        session_id="session",
        run_id="run",
        invocation_id="invocation",
        revision_id="revision",
        actor="model",
    )


def _invoke_raw(root: Path, services: FakeServices, arguments: Mapping[str, object]) -> ToolResult:
    return FindTool().invoke(
        ToolCall(tool_name="find", arguments=dict(arguments), tool_call_id="find-call"),
        context=_context(root),
        services=services,
    )


def _invoke(root: Path, services: FakeServices, arguments: Mapping[str, object]) -> FindResultBody:
    result = _invoke_raw(root, services, arguments)
    assert isinstance(result, ToolSuccess)
    assert isinstance(result.body, FindResultBody)
    return result.body


def _invoke_failed(root: Path, services: FakeServices, arguments: Mapping[str, object], *, kind: str) -> FindResultBody:
    result = _invoke_raw(root, services, arguments)
    assert isinstance(result, ToolFailure)
    assert isinstance(result.body, FindResultBody)
    assert result.diagnostics is not None and result.diagnostics.kind == kind
    assert f"find_{result.body.status}" == kind
    return result.body


def _scripted(verdicts: list[dict[str, object]]) -> StructuredModelResult:
    payload = {"verdicts": verdicts}
    raw = json.dumps(payload)
    return StructuredModelResult(raw_text=raw, data=payload)


def _read_text(root: Path, path: str, *, offset: int, limit: int) -> str:
    result = ReadTool().invoke(
        ToolCall(tool_name="read", arguments={"path": path, "offset": offset, "limit": limit}),
        context=_context(root),
        services=FakeServices(),
    )
    assert isinstance(result, ToolSuccess)
    assert isinstance(result.body, ReadResultBody)
    return "\n".join(f"{record.line}: {record.text}" for record in result.body.lines)


def test_nested_tools_stay_read_only() -> None:
    # Tripwire for the nested-composition fence in find_tool._run_grep: find
    # invokes the shared grep/read tools directly, bypassing the tool
    # pipeline, which is defensible only while both nested tools are
    # READ-effect. If either ever gains a non-read effect, direct composition
    # must be re-examined — do not "fix" this test by deleting it.
    assert GrepTool.definition.effects == frozenset({ToolEffect.READ})
    assert ReadTool.definition.effects == frozenset({ToolEffect.READ})


def test_plugin_factory_exposes_find_tool() -> None:
    plugin = create_plugin(None)
    assert [tool.definition.name for tool in plugin.tools] == ["find"]


def test_auto_locates_behavior_with_read_verified_evidence(tmp_path: Path) -> None:
    root = _tree(tmp_path / "workspace")
    services = FakeServices(
        structured_answer=_scripted(
            [
                {
                    "candidate_id": 0,
                    "path": "shop/cart.py",
                    "verdict": "match",
                    "line": 10,
                    "end_line": 13,
                    "rationale": "applies the bulk discount for large totals",
                }
            ]
        )
    )
    body = _invoke(root, services, {"query": "applies discount at checkout"})
    assert body.status == "ok"
    assert body.cost == {"model_calls": 1, "candidates_judged": 2}
    assert len(services.prompts) == 1
    (location,) = body.locations
    assert location.path == "shop/cart.py"
    assert (location.start_line, location.end_line) == (10, 13)
    assert location.via == "judge"
    assert location.rationale == "applies the bulk discount for large totals"
    assert location.excerpt == _read_text(root, "shop/cart.py", offset=location.evidence_offset, limit=location.evidence_limit)
    assert "10: def _apply_discount(total):" in location.excerpt
    assert list(body.coverage.judged_files) == ["shop/cart.py", "shop/ship.py"]
    assert "shop/cart.py" in body.coverage.screened_files
    assert body.coverage.truncated is False


def test_genuine_no_hit_when_judge_declines(tmp_path: Path) -> None:
    root = _tree(tmp_path / "workspace")
    services = FakeServices(
        structured_answer=_scripted(
            [
                {
                    "candidate_id": 0,
                    "path": "shop/cart.py",
                    "verdict": "no_match",
                    "line": 4,
                    "rationale": "checkout totals, no discount behavior",
                }
            ]
        )
    )
    body = _invoke(root, services, {"query": "applies discount at checkout"})
    assert body.status == "no_hit"
    assert body.locations == ()
    assert body.cost["model_calls"] == 1
    assert body.coverage.declined == 1


def test_timeout_and_unavailable_are_distinct_typed_failures(tmp_path: Path) -> None:
    root = _tree(tmp_path / "workspace")
    timed_out = _invoke_failed(
        root,
        FakeServices(model_error=TimeoutError("judge too slow")),
        {"query": "applies discount at checkout"},
        kind="find_timeout",
    )
    assert timed_out.status == "timeout"
    assert timed_out.locations == ()
    assert "timed out" in timed_out.detail

    unavailable = _invoke_failed(
        root,
        FakeServices(model_error=RuntimeError("provider down")),
        {"query": "applies discount at checkout"},
        kind="find_judge_unavailable",
    )
    assert unavailable.status == "judge_unavailable"
    assert unavailable.locations == ()
    assert "provider down" in unavailable.detail


def test_unparseable_judge_reply_is_judge_error(tmp_path: Path) -> None:
    root = _tree(tmp_path / "workspace")
    body = _invoke_failed(
        root,
        FakeServices(model_answer="I cannot answer that."),
        {"query": "applies discount at checkout"},
        kind="find_judge_error",
    )
    assert body.status == "judge_error"
    assert body.locations == ()
    assert "invalid_json" in body.detail


def test_partial_when_candidate_cap_skips_files(tmp_path: Path) -> None:
    root = _tree(tmp_path / "workspace")
    services = FakeServices(
        structured_answer=_scripted(
            [
                {
                    "candidate_id": 0,
                    "path": "shop/cart.py",
                    "verdict": "match",
                    "line": 10,
                    "rationale": "applies the bulk discount",
                }
            ]
        )
    )
    result = _invoke_raw(root, services, {"query": "discount tax checkout", "max_candidates": 1})
    assert isinstance(result, ToolSuccess)
    assert isinstance(result.body, FindResultBody)
    body = result.body
    assert body.status == "partial"
    assert len(body.locations) == 1  # partial still ships verified hits
    assert body.coverage.truncated is True
    assert body.coverage.truncation_reasons != ()
    assert {skip["reason"] for skip in body.coverage.skipped} == {"candidate_cap"}
    assert result.output.bounds.truncated is True


def test_judge_hallucinations_rejected_visibly(tmp_path: Path) -> None:
    root = _tree(tmp_path / "workspace")
    services = FakeServices(
        structured_answer=_scripted(
            [
                {
                    "candidate_id": 0,
                    "path": "shop/cart.py",
                    "verdict": "match",
                    "line": 999,
                    "rationale": "invented line",
                },
                {
                    "candidate_id": 0,
                    "path": "shop/cart.py",
                    "verdict": "match",
                    "line": 4,
                    "rationale": "second verdict for the same candidate",
                },
                {
                    "candidate_id": 9,
                    "path": "nope.py",
                    "verdict": "match",
                    "line": 1,
                    "rationale": "never prescreened",
                },
                {
                    "candidate_id": 1,
                    "path": "shop/cart.py",
                    "verdict": "match",
                    "line": 4,
                    "rationale": "wrong path for this candidate",
                },
                {
                    "candidate_id": 2,
                    "path": "shop/ship.py",
                    "verdict": "match",
                    "line": 4,
                    "end_line": 99,
                    "rationale": "range runs past end of file",
                },
            ]
        )
    )
    body = _invoke(root, services, {"query": "applies discount tax at checkout"})
    assert body.status == "no_hit"
    assert body.locations == ()
    reasons = [rejection["reason"] for rejection in body.coverage.rejected]
    assert reasons == [
        "line_not_prescreened",
        "duplicate_verdict",
        "unknown_candidate",
        "path_mismatch",
        "line_out_of_range",
    ]
    assert "line_not_prescreened" in body.detail


def test_literal_mode_is_grep_only(tmp_path: Path) -> None:
    root = _tree(tmp_path / "workspace")
    services = FakeServices(model_error=AssertionError("literal mode must not call the model"))
    body = _invoke(root, services, {"query": "_apply_discount", "mode": "literal"})
    assert body.status == "ok"
    assert body.cost == {"model_calls": 0, "candidates_judged": 0}
    assert len(services.prompts) == 0
    paths = {(location.path, location.start_line, location.via) for location in body.locations}
    assert paths == {("shop/cart.py", 6, "grep"), ("shop/cart.py", 10, "grep")}
    for location in body.locations:
        assert location.excerpt == _read_text(root, location.path, offset=location.evidence_offset, limit=location.evidence_limit)


def test_grep_fallback_keeps_literal_queries_working(tmp_path: Path) -> None:
    root = _tree(tmp_path / "workspace")
    services = FakeServices(model_error=AssertionError("fallback must not call the model"))
    body = _invoke(root, services, {"query": "the and or"})
    assert body.status == "ok"
    assert body.cost["model_calls"] == 0
    (location,) = body.locations
    assert (location.path, location.start_line, location.via) == ("misc/words.txt", 1, "grep_fallback")


def test_partial_when_grep_truncates_prescreen(tmp_path: Path) -> None:
    root = _tree(tmp_path / "workspace")
    services = FakeServices(
        structured_answer=_scripted(
            [
                {
                    "candidate_id": 0,
                    "path": "big/gen.py",
                    "verdict": "match",
                    "line": 7,
                    "rationale": "generated marker assignment",
                }
            ]
        )
    )
    body = _invoke(root, services, {"query": "toktok"})
    assert body.status == "partial"
    assert body.coverage.truncated is True
    assert any("truncated" in reason for reason in body.coverage.truncation_reasons)
    assert any(note.get("truncated") is True for note in body.coverage.prescreen)
    (location,) = body.locations
    assert (location.path, location.start_line) == ("big/gen.py", 7)


def test_prescreen_match_cap_skips_later_keywords(tmp_path: Path) -> None:
    root = _tree(tmp_path / "workspace")
    services = FakeServices(
        structured_answer=_scripted(
            [
                {
                    "candidate_id": 0,
                    "path": "big/gen.py",
                    "verdict": "no_match",
                    "line": 1,
                    "rationale": "not the discount behavior",
                }
            ]
        )
    )
    body = _invoke(root, services, {"query": "toktok discount"})
    assert body.status == "partial"
    assert any("match cap" in reason for reason in body.coverage.truncation_reasons)
    assert any(note.get("skipped") == "prescreen match cap reached" for note in body.coverage.prescreen)


def test_long_line_diagnostic_propagates_typed(tmp_path: Path) -> None:
    scope = tmp_path / "workspace" / "sub"
    scope.mkdir(parents=True)
    (scope / "weird.py").write_text("quirkblurb " + "x" * 70000 + "\n", encoding="utf-8")
    with pytest.raises(ToolDiagnosticError, match="exceeds"):
        _invoke(tmp_path / "workspace", FakeServices(), {"query": "quirkblurb", "path": "sub"})


def test_non_string_rationale_rejected_visibly(tmp_path: Path) -> None:
    root = _tree(tmp_path / "workspace")
    services = FakeServices(
        structured_answer=_scripted(
            [
                {
                    "candidate_id": 0,
                    "path": "shop/cart.py",
                    "verdict": "match",
                    "line": 10,
                    "rationale": 42,
                }
            ]
        )
    )
    body = _invoke(root, services, {"query": "applies discount", "path": "shop/cart.py"})
    assert body.status == "no_hit"
    assert body.locations == ()
    (rejection,) = body.coverage.rejected
    assert rejection["reason"] == "invalid_verdict_field"


def test_snippet_caps_leave_visible_cuts_signal(tmp_path: Path) -> None:
    root = _tree(tmp_path / "workspace")
    services = FakeServices(
        structured_answer=_scripted(
            [
                {
                    "candidate_id": 0,
                    "path": "big2/many.py",
                    "verdict": "match",
                    "line": 1,
                    "rationale": "generated marker assignment",
                }
            ]
        )
    )
    body = _invoke(root, services, {"query": "snipmark"})
    assert body.status == "ok"
    (cut,) = body.coverage.snippet_cuts
    assert cut["path"] == "big2/many.py"
    assert cut["matches_total"] == 16
    assert cut["matches_shown"] == 12
    assert cut["long_lines_cut"] == 1
    assert "caps" in str(cut["reason"])


def test_empty_query_and_missing_path_raise(tmp_path: Path) -> None:
    root = _tree(tmp_path / "workspace")
    with pytest.raises(ValueError, match="query"):
        _invoke(root, FakeServices(), {"query": "  "})
    with pytest.raises(ValueError, match="does not exist"):
        _invoke(root, FakeServices(), {"query": "discount", "path": "no-such-dir"})


class _CoreJudgeServices(FakeServices):
    """Fake leases everywhere except the judge, which runs through a real Core."""

    core: Core
    core_session_id: str = ""

    def model_query_structured(
        self,
        prompt: str,
        *,
        result_schema: Mapping[str, object],
        model: str | None = None,
        max_output_tokens: int | None = None,
        include_context: bool = False,
    ) -> StructuredModelResult:
        self.prompts.append(prompt)
        return self.core.governed_model_query_structured(
            self.core_session_id,
            prompt,
            result_schema,
            model=model,
            max_output_tokens=max_output_tokens,
            include_context=include_context,
        )


@contextmanager
def _loopback(replies: list[dict[str, object]]) -> Iterator[OpenAILoopbackServer]:
    server = OpenAILoopbackServer(replies)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def test_real_core_judge_round_trip(tmp_path: Path) -> None:
    verdict = {
        "candidate_id": 0,
        "path": "shop/cart.py",
        "verdict": "match",
        "line": 10,
        "end_line": 13,
        "rationale": "applies the bulk discount for large totals",
    }
    reply = openai_response(message={"role": "assistant", "content": json.dumps({"verdicts": [verdict]})}, finish="stop")
    workspace = _tree(tmp_path / "workspace")
    with _loopback([reply]) as server:
        (workspace / "voidcode.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "model": {"model": "endpoint/exact-model"},
                    "policy": {"approval_mode": "yolo", "read_only": False},
                    "providers": {
                        "endpoint": {
                            "api_key": "loopback-secret-not-for-storage",
                            "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                        }
                    },
                    "plugins": {"files": {"enabled": False}},
                }
            ),
            encoding="utf-8",
        )
        core = open_core(workspace, state_dir=tmp_path / "state")
        try:
            session_id = core.create_session("sess-find").session_id
            services = _CoreJudgeServices()
            services.core = core
            services.core_session_id = session_id
            body = _invoke(workspace, services, {"query": "applies discount at checkout"})
            facts = [event for event in core.events_after(session_id) if event.event_type == "core.model_query"]
        finally:
            core.close()
    assert body.status == "ok"
    (location,) = body.locations
    assert (location.path, location.start_line, location.end_line) == ("shop/cart.py", 10, 13)
    (request,) = server.requests
    assert not request.get("tools")
    raw_messages = request.get("messages")
    assert isinstance(raw_messages, list)
    messages = [message for message in raw_messages if isinstance(message, Mapping)]
    assert len(messages) == 1
    assert str(messages[0].get("content", "")).endswith("Reply with exactly one JSON object and nothing else.")
    assert facts and facts[0].payload["model"] == "exact-model" and "usage" in facts[0].payload
