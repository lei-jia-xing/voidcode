"""Property tests for the question-answer request contract.

The payload contract is asserted through the transport itself — raw ASGI, exactly
like the HTTP integration tests — so the tests pin what a client observes: the
answers delivered to the runtime on success, and the 400 message on rejection.
"""

from __future__ import annotations

import asyncio
import json
from typing import cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from voidcode.runtime.contracts import RuntimeResponse
from voidcode.runtime.http import RuntimeTransport, RuntimeTransportApp
from voidcode.runtime.question import QuestionResponse
from voidcode.runtime.session import SessionRef, SessionState

pytestmark = pytest.mark.filterwarnings("ignore:unclosed database in <sqlite3.Connection object.*:ResourceWarning")

CI_SETTINGS = settings(derandomize=True, database=None, deadline=None, max_examples=200)

_text_chars = st.characters(
    blacklist_categories=["Cs"],
    blacklist_characters=["\x00", "\n", "\r"],
)
_non_blank_text = st.text(alphabet=_text_chars, min_size=1, max_size=20).filter(lambda text: text.strip() != "" and text == text.strip())
_blank_text = st.sampled_from(("", " ", "  ", "\t", " \t "))
_json_scalar = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
)
_json_like = st.recursive(
    _json_scalar | _non_blank_text,
    lambda children: st.lists(children, max_size=3) | st.dictionaries(_non_blank_text, children, max_size=3),
    max_leaves=6,
)
_invalid_text_value = st.one_of(_blank_text, _json_scalar, st.lists(_json_like, max_size=3))
_invalid_request_id = st.one_of(
    st.just(""),
    _json_scalar,
    st.lists(_json_like, max_size=3),
    st.dictionaries(_non_blank_text, _json_like, max_size=3),
)

_QUESTION_PATH = "/api/sessions/question-session/question"


class _RecordingRuntime:
    """Runtime double recording the question answers the transport delivers."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple[QuestionResponse, ...]]] = []

    def answer_question(
        self,
        session_id: str,
        *,
        question_request_id: str,
        responses: tuple[QuestionResponse, ...],
    ) -> RuntimeResponse:
        self.calls.append((session_id, question_request_id, responses))
        return RuntimeResponse(
            session=SessionState(
                session=SessionRef(id=session_id),
                status="running",
                turn=1,
                metadata={},
            ),
            events=(),
            output=None,
        )


_current_runtime: _RecordingRuntime | None = None


def _runtime_factory() -> RuntimeTransport:
    assert _current_runtime is not None, "the request runner installs a runtime before driving the app"
    return cast(RuntimeTransport, _current_runtime)


# One app for the whole module: building the transport is the expensive part of
# a property run, and the runtime is supplied per request.
_app = RuntimeTransportApp(runtime_factory=_runtime_factory)


def _post_question(body: bytes) -> tuple[int, dict[str, object], _RecordingRuntime]:
    global _current_runtime
    runtime = _RecordingRuntime()
    _current_runtime = runtime
    messages: list[dict[str, object]] = [{"type": "http.request", "body": body, "more_body": False}]
    sent: list[dict[str, object]] = []

    async def _receive() -> dict[str, object]:
        if messages:
            return messages.pop(0)
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def _send(message: dict[str, object]) -> None:
        sent.append(message)

    scope: dict[str, object] = {
        "type": "http",
        "method": "POST",
        "path": _QUESTION_PATH,
        "query_string": b"",
        "headers": [],
    }
    asyncio.run(_app(scope, _receive, _send))

    start_message = next(message for message in sent if message["type"] == "http.response.start")
    raw_body = b"".join(cast(bytes, message.get("body", b"")) for message in sent if message["type"] == "http.response.body")
    return cast(int, start_message["status"]), cast(dict[str, object], json.loads(raw_body)), runtime


@CI_SETTINGS
@given(
    request_id=_non_blank_text,
    header=_non_blank_text,
    answers=st.lists(_non_blank_text, min_size=1, max_size=4),
)
def test_question_answer_endpoint_accepts_valid_payloads(
    request_id: str,
    header: str,
    answers: list[str],
) -> None:
    status, _payload, runtime = _post_question(
        json.dumps(
            {
                "request_id": request_id,
                "responses": [{"header": header, "answers": answers}],
            }
        ).encode("utf-8")
    )

    assert status == 200
    assert runtime.calls == [
        (  # one resolved question, delivered intact
            "question-session",
            request_id,
            (QuestionResponse(header=header, answers=tuple(answers)),),
        )
    ]


@CI_SETTINGS
@given(request_id=_invalid_request_id)
def test_question_answer_endpoint_rejects_invalid_request_ids(request_id: object) -> None:
    status, payload, runtime = _post_question(
        json.dumps(
            {
                "request_id": request_id,
                "responses": [{"header": "Runtime path", "answers": ["Reuse existing"]}],
            }
        ).encode("utf-8")
    )

    assert status == 400
    assert payload == {"error": "request_id must be a non-empty string", "code": None}
    assert runtime.calls == []


@CI_SETTINGS
@given(
    responses=st.one_of(
        _json_scalar,
        st.just([]),
        st.lists(
            st.one_of(
                _json_scalar,
                st.fixed_dictionaries(
                    {
                        "header": _invalid_text_value,
                        "answers": st.just(["Reuse existing"]),
                    }
                ),
                st.fixed_dictionaries(
                    {
                        "header": _non_blank_text,
                        "answers": st.one_of(
                            _json_scalar,
                            st.just([]),
                            st.lists(_invalid_text_value, min_size=1, max_size=3),
                        ),
                    }
                ),
            ),
            min_size=1,
            max_size=3,
        ),
    )
)
def test_question_answer_endpoint_rejects_invalid_response_payloads(responses: object) -> None:
    status, payload, runtime = _post_question(json.dumps({"request_id": "question-1", "responses": responses}).encode("utf-8"))

    assert status == 400
    assert isinstance(payload["error"], str)
    assert payload["error"]
    assert runtime.calls == []


def test_question_answer_endpoint_rejects_non_json_payloads() -> None:
    status, payload, runtime = _post_question(b"{not-json")

    assert status == 400
    assert payload == {"error": "request body must be valid JSON", "code": None}
    assert runtime.calls == []
