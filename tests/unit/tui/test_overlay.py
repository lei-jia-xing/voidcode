from __future__ import annotations

import re
from types import SimpleNamespace

from voidcode.runtime.question import PendingQuestionOption, PendingQuestionPrompt, QuestionResponse
from voidcode.tools.question import QuestionTool
from voidcode.tui.keys import Key
from voidcode.tui.overlay import (
    ApprovalOverlay,
    OverlayOutcome,
    OverlayOutcomeKind,
    QuestionOverlay,
    SessionPickerOverlay,
    TreeSelectorOverlay,
)
from voidcode.tui.term import visible_width

from .conftest import SGR, plain, theme

WIDTH = 100


def text(rows: list[str] | tuple[str, ...]) -> str:
    return "\n".join(SGR.sub("", row) for row in rows)


def type_text(overlay, value: str) -> None:
    for char in value:
        overlay.handle_key(Key(char, char))


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


def approval(**kwargs: object) -> ApprovalOverlay:
    defaults: dict[str, object] = {
        "tool": "bash",
        "target": "rm -rf /tmp/x",
        "reason": "destructive command",
        "arguments": '{\n  "command": "rm -rf /tmp/x"\n}',
        "theme": theme(),
    }
    defaults.update(kwargs)
    return ApprovalOverlay(**defaults)  # type: ignore[arg-type]


def test_approval_key_mapping() -> None:
    assert approval().handle_key(Key("y")) == OverlayOutcome(OverlayOutcomeKind.DONE, "allow")
    assert approval().handle_key(Key("n")) == OverlayOutcome(OverlayOutcomeKind.DONE, "deny")
    assert approval().handle_key(Key("escape")) == OverlayOutcome(OverlayOutcomeKind.DONE, "deny")
    assert approval().handle_key(Key("tab")) == OverlayOutcome(OverlayOutcomeKind.PENDING, None)


def test_approval_cancel_path_is_deny_not_cancelled() -> None:
    # the old modal bound escape to ``deny``; a dismissal (None) was mapped to
    # deny by the app. The overlay surfaces that as an explicit deny payload.
    outcome = approval().handle_key(Key("escape"))
    assert outcome.kind is OverlayOutcomeKind.DONE
    assert outcome.payload == "deny"


def test_approval_renders_header_reason_and_arguments() -> None:
    rows = plain(approval().render(WIDTH))
    assert "Approve bash for rm -rf /tmp/x?" in rows[1]
    assert "Why: destructive command" in rows[2]
    assert any('"command": "rm -rf /tmp/x"' in row for row in rows)


def test_approval_omits_reason_and_target_when_absent() -> None:
    rows = text(approval(reason="", target="").render(WIDTH))
    assert "Approve bash?" in rows
    assert "Why:" not in rows


# ---------------------------------------------------------------------------
# Question wizard
# ---------------------------------------------------------------------------

THREE = [
    {"header": "Shell", "question": "Which shell?", "options": [{"label": "bash", "description": "GNU"}, {"label": "zsh"}]},
    {"header": "Extras", "question": "Pick extras", "multiple": True, "options": [{"label": "vim"}, {"label": "git"}]},
    {"header": "Name", "question": "Project name?", "options": [{"label": "myproj"}]},
]


def wizard(questions=THREE) -> QuestionOverlay:
    return QuestionOverlay(questions=questions, theme=theme())


def test_single_question_single_select_submits_on_enter() -> None:
    overlay = QuestionOverlay(
        questions=[{"header": "H", "question": "Q?", "options": [{"label": "yes"}, {"label": "no"}]}],
        theme=theme(),
    )
    assert overlay.handle_key(Key("enter")) == OverlayOutcome(OverlayOutcomeKind.DONE, (("H", ("yes",)),))


def test_page_navigation_review_and_submit_payload() -> None:
    overlay = wizard()
    overlay.handle_key(Key("enter"))  # q1: bash
    assert "Question 2 of 3" in text(overlay.render(WIDTH))
    overlay.handle_key(Key("space"))  # q2: toggle vim
    overlay.handle_key(Key("right"))  # -> q3
    assert "Question 3 of 3" in text(overlay.render(WIDTH))
    overlay.handle_key(Key("enter"))  # q3: myproj -> review
    assert "Review answers" in text(overlay.render(WIDTH))
    assert overlay.handle_key(Key("enter")) == OverlayOutcome(
        OverlayOutcomeKind.DONE,
        (("Shell", ("bash",)), ("Extras", ("vim",)), ("Name", ("myproj",))),
    )


def test_multiple_select_toggles_and_does_not_advance() -> None:
    overlay = QuestionOverlay(questions=[THREE[1]], theme=theme())
    assert overlay.handle_key(Key("space")) == OverlayOutcome(OverlayOutcomeKind.PENDING, None)  # vim on
    overlay.handle_key(Key("down"))
    overlay.handle_key(Key("space"))  # git on
    overlay.handle_key(Key("up"))
    overlay.handle_key(Key("space"))  # vim off
    overlay.handle_key(Key("space"))  # vim back on
    overlay.handle_key(Key("right"))  # -> review
    outcome = overlay.handle_key(Key("enter"))
    assert outcome == OverlayOutcome(OverlayOutcomeKind.DONE, (("Extras", ("vim", "git")),))


def test_only_declared_labels_can_be_answered() -> None:
    # The runtime (``QuestionTool.validate_responses``) accepts only declared
    # option labels; highlight movement clamps to them and the ``Other…``
    # free-text row is gone, so the wizard cannot emit anything else.
    overlay = QuestionOverlay(
        questions=[{"header": "H", "question": "Q?", "options": [{"label": "a"}, {"label": "b"}]}],
        theme=theme(),
    )
    rendered = text(overlay.render(WIDTH))
    assert "Other" not in rendered
    for _ in range(6):
        overlay.handle_key(Key("down"))  # clamped to the last declared option
    assert overlay.handle_key(Key("enter")) == OverlayOutcome(OverlayOutcomeKind.DONE, (("H", ("b",)),))


def test_wizard_output_is_accepted_by_runtime_validation() -> None:
    """Whatever the wizard emits must pass the runtime's own check.

    The defect this guards: the removed ``Other…`` row let the wizard produce an
    answer that ``QuestionTool.validate_responses`` rejected.
    """
    overlay = wizard()
    overlay.handle_key(Key("enter"))  # q1: bash
    overlay.handle_key(Key("space"))  # q2: vim
    overlay.handle_key(Key("right"))  # q3
    overlay.handle_key(Key("enter"))  # q3: myproj -> review
    outcome = overlay.handle_key(Key("enter"))
    assert outcome.kind is OverlayOutcomeKind.DONE
    assert isinstance(outcome.payload, tuple)
    prompts = tuple(
        PendingQuestionPrompt(
            question=page["question"],
            header=page["header"],
            options=tuple(PendingQuestionOption(label=option["label"]) for option in page["options"]),
            multiple=page.get("multiple", False),
        )
        for page in THREE
    )
    responses = tuple(QuestionResponse(header=header, answers=answers) for header, answers in outcome.payload)
    assert QuestionTool.validate_responses(prompts, responses) == responses


def test_submit_is_refused_while_unanswered() -> None:
    overlay = wizard()
    overlay.handle_key(Key("enter"))  # q1 answered
    # q2 unanswered: right (and enter) must not advance to review
    assert overlay.handle_key(Key("right")) == OverlayOutcome(OverlayOutcomeKind.PENDING, None)
    assert "Question 2 of 3" in text(overlay.render(WIDTH))
    assert overlay.handle_key(Key("enter")) == OverlayOutcome(OverlayOutcomeKind.PENDING, None)
    assert "Review answers" not in text(overlay.render(WIDTH))


def test_back_to_unanswered_question_is_marked() -> None:
    overlay = wizard()
    overlay.handle_key(Key("enter"))  # q1
    overlay.handle_key(Key("space"))
    overlay.handle_key(Key("right"))  # q3
    overlay.handle_key(Key("enter"))  # q3: myproj -> review, all answered
    assert "Review answers" in text(overlay.render(WIDTH))
    assert "Unanswered" not in text(overlay.render(WIDTH))


def test_left_right_navigation_between_questions() -> None:
    overlay = wizard()
    overlay.handle_key(Key("enter"))  # q1 -> q2
    overlay.handle_key(Key("left"))  # back to q1
    assert "Question 1 of 3" in text(overlay.render(WIDTH))
    overlay.handle_key(Key("right"))  # q1 answered -> q2
    assert "Question 2 of 3" in text(overlay.render(WIDTH))


def test_escape_cancels() -> None:
    assert wizard().handle_key(Key("escape")) == OverlayOutcome(OverlayOutcomeKind.CANCELLED, None)


def test_empty_questions_cancel() -> None:
    overlay = QuestionOverlay(questions=[], theme=theme())
    assert "No questions" in text(overlay.render(WIDTH))
    assert overlay.handle_key(Key("enter")) == OverlayOutcome(OverlayOutcomeKind.CANCELLED, None)


# ---------------------------------------------------------------------------
# Session picker
# ---------------------------------------------------------------------------

SESSIONS = [
    ("session-aaaa1111", "Refactor the overlay module"),
    ("session-bbbb2222", "Fix CJK width bug"),
    ("session-cccc3333", "Add session picker filter"),
]


def picker(sessions=SESSIONS, *, depths: dict[str, int] | None = None) -> SessionPickerOverlay:
    return SessionPickerOverlay(sessions=sessions, depths=depths, theme=theme())


def test_picker_lists_prompt_titles_and_selects_session_id() -> None:
    overlay = picker()
    assert "Refactor the overlay module" in text(overlay.render(WIDTH))
    overlay.handle_key(Key("down"))
    assert overlay.handle_key(Key("enter")) == OverlayOutcome(OverlayOutcomeKind.DONE, "session-bbbb2222")


def test_picker_type_to_filter() -> None:
    overlay = picker()
    type_text(overlay, "cjk")
    rendered = text(overlay.render(WIDTH))
    assert "Fix CJK width bug" in rendered
    assert "overlay module" not in rendered
    assert overlay.handle_key(Key("enter")) == OverlayOutcome(OverlayOutcomeKind.DONE, "session-bbbb2222")


def test_picker_filter_tokens_match_any_order() -> None:
    overlay = picker()
    type_text(overlay, "width fix")
    assert overlay.handle_key(Key("enter")) == OverlayOutcome(OverlayOutcomeKind.DONE, "session-bbbb2222")


def test_picker_backspace_restores_rows() -> None:
    overlay = picker()
    type_text(overlay, "cjk")
    overlay.handle_key(Key("backspace"))
    overlay.handle_key(Key("backspace"))
    overlay.handle_key(Key("backspace"))
    rendered = text(overlay.render(WIDTH))
    for _, title in SESSIONS:
        assert title in rendered


def test_picker_escape_and_empty_cancel() -> None:
    assert picker().handle_key(Key("escape")) == OverlayOutcome(OverlayOutcomeKind.CANCELLED, None)
    assert picker(sessions=[]).handle_key(Key("enter")) == OverlayOutcome(OverlayOutcomeKind.CANCELLED, None)


def test_picker_is_always_fullscreen() -> None:
    assert picker().wants_fullscreen(WIDTH, 1000) is True


FOREST = [
    ("session-aaaa1111", "Refactor the overlay module"),
    ("session-dddd4444", "Fork of refactor"),
    ("session-cccc3333", "Grandchild of refactor"),
    ("session-bbbb2222", "Fix CJK width bug"),
]
# aaaa is a root, dddd forks it, cccc forks dddd; bbbb is an unrelated root.
DEPTHS = {
    "session-aaaa1111": 0,
    "session-dddd4444": 1,
    "session-cccc3333": 2,
    "session-bbbb2222": 0,
}


def _row_text(overlay: SessionPickerOverlay, needle: str) -> str:
    return next(row for row in plain(overlay.render(WIDTH)) if needle in row)


def test_picker_indents_rows_by_fork_depth() -> None:
    overlay = picker(FOREST, depths=DEPTHS)
    root = _row_text(overlay, "Refactor the overlay module")
    child = _row_text(overlay, "Fork of refactor")
    grandchild = _row_text(overlay, "Grandchild of refactor")
    assert root.index("Refactor the overlay module") == 4
    assert child.index("Fork of refactor") == root.index("Refactor the overlay module") + 2
    assert grandchild.index("Grandchild of refactor") == root.index("Refactor the overlay module") + 4


def test_picker_same_depth_siblings_share_an_indent() -> None:
    overlay = picker(FOREST, depths=DEPTHS)
    # Two unrelated roots (aaaa and bbbb) share the depth-0 column.
    roots = {_row_text(overlay, "Refactor the overlay module").index("Refactor"), _row_text(overlay, "Fix CJK width bug").index("Fix")}
    assert len(roots) == 1


def test_picker_missing_forest_row_defaults_to_depth_zero() -> None:
    overlay = picker(FOREST, depths={"session-bbbb2222": 3})
    assert _row_text(overlay, "Refactor the overlay module").index("Refactor") == 4


def test_picker_filtering_matches_a_title_at_non_zero_depth() -> None:
    overlay = picker(FOREST, depths=DEPTHS)
    type_text(overlay, "grandchild")
    rendered = text(overlay.render(WIDTH))
    assert "Grandchild of refactor" in rendered
    assert "Fix CJK width bug" not in rendered
    assert overlay.handle_key(Key("enter")) == OverlayOutcome(OverlayOutcomeKind.DONE, "session-cccc3333")


# ---------------------------------------------------------------------------
# Checkout tree selector
# ---------------------------------------------------------------------------


def entry(sequence: int, event_type: str, preview: str, *, on_path: bool = True) -> SimpleNamespace:
    """One ``SessionEntrySummary`` row (the runtime owns the real type)."""
    return SimpleNamespace(sequence=sequence, event_type=event_type, parent_sequence=sequence - 1, on_current_path=on_path, preview=preview)


# A checked-out session: 1-2 are the path the leaf sits on; 3-4 are appended
# after 2 and are the abandoned continuation.
CHECKED_OUT = [
    entry(1, "runtime.request_received", "fix the parser bug"),
    entry(2, "graph.response_ready", "Looking at parser.py…"),
    entry(3, "runtime.tool_started", "bash: pytest -q", on_path=False),
    entry(4, "graph.response_ready", "Reverted the wrong guard", on_path=False),
]


def tree(entries=CHECKED_OUT) -> TreeSelectorOverlay:
    return TreeSelectorOverlay(entries=entries, theme=theme())


def test_tree_rows_keep_ascending_sequence_order_with_path_and_abandoned_markers() -> None:
    overlay = tree()
    rows = plain(overlay.render(WIDTH))
    assert isinstance(rows, list)
    listed = [row for row in rows if re.search(r"\d", row)]
    sequences = [int(re.search(r"(\d+)", row).group(1)) for row in listed]  # type: ignore[union-attr]
    assert sequences == [1, 2, 3, 4]  # the listing order, never re-sorted
    assert "* abandoned" not in listed[0]
    assert "* abandoned" not in listed[1]
    assert "* abandoned" in listed[2]
    assert "* abandoned" in listed[3]


def test_tree_row_shows_sequence_type_and_preview() -> None:
    row = next(row for row in plain(tree().render(WIDTH)) if "fix the parser bug" in row)
    assert re.search(r"1\s+user\s+fix the parser bug", row)  # type: ignore[union-attr]


def test_tree_marks_the_current_leaf_on_the_path() -> None:
    rows = plain(tree().render(WIDTH))
    assert isinstance(rows, list)
    cursor = theme().symbol("nav.cursor")
    leaf_row = next(row for row in rows if "Looking at parser.py" in row)
    assert cursor in leaf_row
    # Exactly one row carries the leaf marker.
    assert sum(cursor in row for row in rows) == 1


def test_tree_filter_is_token_and_and_enter_returns_the_sequence() -> None:
    overlay = tree()
    type_text(overlay, "guard")
    rendered = text(overlay.render(WIDTH))
    assert "Reverted the wrong guard" in rendered
    assert "fix the parser bug" not in rendered
    assert overlay.handle_key(Key("enter")) == OverlayOutcome(OverlayOutcomeKind.DONE, 4)


def test_tree_backspace_restores_rows_and_escape_cancels() -> None:
    overlay = tree()
    type_text(overlay, "guard")
    for _ in "guard":
        overlay.handle_key(Key("backspace"))
    assert "fix the parser bug" in text(overlay.render(WIDTH))
    assert overlay.handle_key(Key("escape")) == OverlayOutcome(OverlayOutcomeKind.CANCELLED, None)
    assert tree(entries=[]).handle_key(Key("enter")) == OverlayOutcome(OverlayOutcomeKind.CANCELLED, None)


def test_tree_is_always_fullscreen() -> None:
    assert tree().wants_fullscreen(WIDTH, 1000) is True


# ---------------------------------------------------------------------------
# Fullscreen boundary
# ---------------------------------------------------------------------------


def test_wants_fullscreen_tracks_row_count() -> None:
    overlay = wizard()
    rows = len(overlay.render(WIDTH))
    assert overlay.wants_fullscreen(WIDTH, rows) is False
    assert overlay.wants_fullscreen(WIDTH, rows - 1) is True


# ---------------------------------------------------------------------------
# Width / contract
# ---------------------------------------------------------------------------


def test_every_row_fits_a_wide_char_corpus() -> None:
    overlays = [
        approval(tool="bash", target="你好世界 🎉" * 4, reason="理由 🎉" * 5, arguments='{"cmd": "' + "x" * 90 + '"}'),
        QuestionOverlay(
            questions=[
                {
                    "header": "标题 🎉",
                    "question": "选择一个问题 🎉" * 4,
                    "multiple": True,
                    "options": [{"label": "选项 A 🎉", "description": "描述" * 20}, {"label": "b"}],
                }
            ],
            theme=theme(),
        ),
        SessionPickerOverlay(sessions=[("session-a", "会话标题 🎉" * 8)], theme=theme()),
    ]
    for width in (100, 40, 20, 12, 5, 3, 1):
        for overlay in overlays:
            for row in overlay.render(width):
                assert visible_width(row) <= width, (type(overlay).__name__, width, row)
