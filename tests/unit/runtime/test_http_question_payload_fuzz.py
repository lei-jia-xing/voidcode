from __future__ import annotations

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from voidcode.runtime.http import RuntimeTransportApp

CI_SETTINGS = settings(derandomize=True, database=None, deadline=None, max_examples=200)

_text_chars = st.characters(
    blacklist_categories=["Cs"],
    blacklist_characters=["\x00", "\n", "\r"],
)
_non_blank_text = st.text(alphabet=_text_chars, min_size=1, max_size=20).filter(
    lambda text: text.strip() != "" and text == text.strip()
)
_blank_text = st.sampled_from(("", " ", "  ", "\t", " \t "))
_json_scalar = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
)
_json_like = st.recursive(
    _json_scalar | _non_blank_text,
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(_non_blank_text, children, max_size=3)
    ),
    max_leaves=6,
)
_invalid_text_value = st.one_of(_blank_text, _json_scalar, st.lists(_json_like, max_size=3))
_invalid_request_id = st.one_of(
    st.just(""),
    _json_scalar,
    st.lists(_json_like, max_size=3),
    st.dictionaries(_non_blank_text, _json_like, max_size=3),
)


def _app() -> RuntimeTransportApp:
    return RuntimeTransportApp(runtime_factory=lambda: None)


@CI_SETTINGS
@given(
    request_id=_non_blank_text,
    header=_non_blank_text,
    answers=st.lists(_non_blank_text, min_size=1, max_size=4),
)
def test_parse_question_answer_request_accepts_valid_payloads(
    request_id: str,
    header: str,
    answers: list[str],
) -> None:
    parsed_request_id, responses = _app()._parse_question_answer_request(
        json.dumps(
            {
                "request_id": request_id,
                "responses": [{"header": header, "answers": answers}],
            }
        ).encode("utf-8")
    )

    assert parsed_request_id == request_id
    assert len(responses) == 1
    assert responses[0].header == header
    assert responses[0].answers == tuple(answers)


@CI_SETTINGS
@given(request_id=_invalid_request_id)
def test_parse_question_answer_request_rejects_invalid_request_ids(request_id: object) -> None:
    with pytest.raises(ValueError, match="request_id must be a non-empty string"):
        _app()._parse_question_answer_request(
            json.dumps(
                {
                    "request_id": request_id,
                    "responses": [{"header": "Runtime path", "answers": ["Reuse existing"]}],
                }
            ).encode("utf-8")
        )


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
def test_parse_question_answer_request_rejects_invalid_response_payloads(
    responses: object,
) -> None:
    with pytest.raises(ValueError):
        _app()._parse_question_answer_request(
            json.dumps({"request_id": "question-1", "responses": responses}).encode("utf-8")
        )


def test_parse_question_answer_request_rejects_non_json_payloads() -> None:
    with pytest.raises(ValueError, match="request body must be valid JSON"):
        _app()._parse_question_answer_request(b"{not-json")
