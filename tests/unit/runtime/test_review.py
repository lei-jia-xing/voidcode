from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from pathlib import Path
from typing import cast

import pytest

from tests.provider_loopback import OpenAILoopbackServer, configure_openai_provider, openai_response
from voidcode.core.turns import FinalTurn, ModelTurnFact, ToolTurn, TurnPlan, TurnRequest
from voidcode.runtime.active_session import ACTIVE_SESSION_REGISTRY
from voidcode.runtime.contracts import GitStatusSnapshot, RuntimeRequest
from voidcode.runtime.review import WorkspaceReviewService
from voidcode.runtime.service import VoidCodeRuntime
from voidcode.runtime.storage import SqliteSessionStore
from voidcode.tools.contracts import ToolCall


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "--quiet")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "--quiet", "-m", "base")
    return root


def _git_status(root: Path) -> GitStatusSnapshot:
    return GitStatusSnapshot(state="git_ready", root=str(root))


def test_diff_includes_staged_and_unstaged_changes(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    tracked = root / "tracked.txt"
    tracked.write_text("base\nstaged\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    tracked.write_text("base\nstaged\nunstaged\n", encoding="utf-8")

    result = WorkspaceReviewService(workspace=root).diff(path="tracked.txt", git=_git_status(root))

    assert result.state == "changed"
    assert result.diff is not None
    assert "+staged" in result.diff
    assert "+unstaged" in result.diff


def test_diff_all_includes_untracked_and_binary_files(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "space ü.txt").write_text("new text\n", encoding="utf-8")
    (root / "binary.bin").write_bytes(b"\x00\x01")
    tracked = root / "tracked.txt"
    tracked.write_text("changed\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")

    result = WorkspaceReviewService(workspace=root).diff_all(git=_git_status(root))

    assert result.path == "."
    assert result.state == "changed"
    assert result.diff is not None
    assert "space ü.txt" in result.diff
    assert "binary.bin" in result.diff
    assert "GIT binary patch" in result.diff
    assert "+changed" in result.diff


def test_snapshot_decodes_renames_and_unusual_names(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _git(root, "mv", "tracked.txt", "old name.txt")
    _git(root, "mv", "old name.txt", "new ü.txt")
    (root / "space name.txt").write_text("untracked\n", encoding="utf-8")

    snapshot = WorkspaceReviewService(workspace=root).snapshot(git=_git_status(root))

    changed = {item.path: item for item in snapshot.changed_files}
    assert changed["new ü.txt"].old_path == "tracked.txt"
    assert changed["space name.txt"].change_type == "untracked"


def test_diff_reports_clean_and_non_repository_states(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    service = WorkspaceReviewService(workspace=root)

    clean = service.diff_all(git=_git_status(root))
    outside = service.diff_all(git=GitStatusSnapshot(state="not_git_repo"))

    assert clean.state == "clean"
    assert clean.diff is None
    assert outside.state == "not_git_repo"
    assert outside.diff is None


def test_snapshot_preserves_literal_path_whitespace_quotes_and_backslashes(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    names = (" leading.txt", "trailing.txt ", '"quoted".txt', "back\\slash.txt")
    for name in names:
        (root / name).write_text("untracked\n", encoding="utf-8")

    snapshot = WorkspaceReviewService(workspace=root).snapshot(git=_git_status(root))

    assert {item.path for item in snapshot.changed_files} == set(names)


def test_diff_does_not_turn_git_failures_into_clean(tmp_path: Path) -> None:
    root = tmp_path / "not-a-repository"
    root.mkdir()

    with pytest.raises(ValueError, match="git diff failed"):
        WorkspaceReviewService(workspace=root).diff_all(git=GitStatusSnapshot(state="git_ready", root=str(root)))


def test_diff_all_reports_symlink_targets_without_following_them(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside secret\n", encoding="utf-8")
    (root / "outside-link").symlink_to(outside)
    (root / "broken-link").symlink_to(root / "missing-target")

    result = WorkspaceReviewService(workspace=root).diff_all(git=_git_status(root))

    assert result.diff is not None
    assert "+outside secret" not in result.diff
    assert str(outside) in result.diff
    assert "missing-target" in result.diff


class _ReviewStepProducer:
    def __init__(self, child_calls: list[ToolCall] | None = None) -> None:
        self.child_calls = child_calls or [
            ToolCall(
                tool_name="yield",
                arguments={"summary": "Review complete: all checks passed", "data": {"findings": []}},
            )
        ]
        self.captured_prompts: list[str] = []

    def produce(self, request: TurnRequest, tool_results: tuple[object, ...], *, session: object) -> TurnPlan:
        self.captured_prompts.append(request.prompt)
        if getattr(session, "session_id", None) == "parent-session":
            return FinalTurn(facts=(ModelTurnFact(request.run_step, "deterministic", request.prompt),), output="parent done")
        if request.run_step <= len(self.child_calls):
            return ToolTurn(
                facts=(ModelTurnFact(request.run_step, "deterministic", request.prompt),),
                calls=(self.child_calls[request.run_step - 1],),
            )
        return FinalTurn(facts=(ModelTurnFact(request.run_step, "deterministic", request.prompt),), output="review completed")


def test_review_session_governed_child_yield(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "tracked.txt").write_text("modified uncommitted\n")
    (root / "new.txt").write_text("untracked file\n")

    producer = _ReviewStepProducer()
    with VoidCodeRuntime(workspace=root, turn_producer=producer) as runtime:
        parent_resp = runtime.run(RuntimeRequest(session_id="parent-session", prompt="parent work"))
        assert parent_resp.session.status == "completed"

        review_resp = runtime.review_session(session_id="parent-session", focus="regression prevention")
        assert review_resp.session.status == "completed"
        assert review_resp.session.session.parent_id == "parent-session"
        policy = review_resp.session.metadata.get("runtime_policy")
        assert isinstance(policy, dict)
        assert policy.get("agent_preset") == "review"
        assert review_resp.session.metadata.get("read_only") is True
        assert review_resp.output == "Review complete: all checks passed"

        child_prompt = producer.captured_prompts[-1]
        assert "Reviewer focus: regression prevention" in child_prompt
        assert "modified uncommitted" in child_prompt
        assert "untracked file" in child_prompt
        parent_result = runtime.session_result(session_id="parent-session")
        assert parent_result.output == "parent done"
        assert parent_result.session.status == "completed"


def test_review_session_parent_and_repo_governance(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "tracked.txt").write_text("modified\n")
    producer = _ReviewStepProducer()

    with VoidCodeRuntime(workspace=root, turn_producer=producer) as runtime:
        runtime.run(RuntimeRequest(session_id="parent-session", prompt="parent work"))
        # Active session running rejection
        ACTIVE_SESSION_REGISTRY.register(workspace=root, session_id="parent-session", run_id="active-run", metadata={})
        try:
            with pytest.raises(ValueError, match="is running"):
                runtime.review_session(session_id="parent-session")
        finally:
            ACTIVE_SESSION_REGISTRY.unregister(workspace=root, session_id="parent-session", run_id="active-run")

        # Waiting parent rejection
        store = cast(SqliteSessionStore, runtime._repositories.sessions)
        with store._write_connect(root) as c:
            c.execute("UPDATE sessions SET status = 'waiting' WHERE workspace_id = ? AND session_id = ?", (str(root), "parent-session"))
            c.commit()
        with pytest.raises(ValueError, match="status waiting"):
            runtime.review_session(session_id="parent-session")
        with store._write_connect(root) as c:
            c.execute("UPDATE sessions SET status = 'completed' WHERE workspace_id = ? AND session_id = ?", (str(root), "parent-session"))
            c.commit()

        # Clean workspace rejection
        _git(root, "checkout", "--", "tracked.txt")
        with pytest.raises(ValueError, match="no workspace changes"):
            runtime.review_session(session_id="parent-session")

        # Diff exceeding limit rejection (>256 KiB)
        (root / "huge.txt").write_text("x" * (265 * 1024))
        with pytest.raises(ValueError, match="too large"):
            runtime.review_session(session_id="parent-session")
        (root / "huge.txt").unlink()

    # Not a git repo rejection
    non_repo = tmp_path / "not-git"
    non_repo.mkdir()
    with VoidCodeRuntime(workspace=non_repo, turn_producer=producer) as non_repo_runtime:
        non_repo_runtime.run(RuntimeRequest(session_id="parent-session", prompt="parent work"))
        with pytest.raises(ValueError, match="Git repository"):
            non_repo_runtime.review_session(session_id="parent-session")


def test_review_session_mutation_denied(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "tracked.txt").write_text("modified\n")

    h = hashlib.sha256(b"modified\n").hexdigest()
    producer = _ReviewStepProducer(
        child_calls=[
            ToolCall(
                tool_name="edit",
                arguments={"path": "tracked.txt", "oldString": "modified", "newString": "tampered", "expectedHash": h},
            )
        ]
    )
    with VoidCodeRuntime(workspace=root, turn_producer=producer) as runtime:
        runtime.run(RuntimeRequest(session_id="parent-session", prompt="parent work"))
        with pytest.raises(ValueError) as excinfo:
            runtime.review_session(session_id="parent-session")
        assert "delegation policy denied tool" in str(excinfo.value) or "read-only access is active" in str(excinfo.value)
        assert (root / "tracked.txt").read_text() == "modified\n"


def test_review_session_loopback_yield(tmp_path: Path) -> None:
    replies = [
        openai_response(message={"role": "assistant", "content": "parent finished"}, finish="stop"),
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-yield-review",
                        "type": "function",
                        "function": {
                            "name": "yield",
                            "arguments": json.dumps(
                                {
                                    "summary": "Review complete: 0 vulnerabilities found, diff verified clean.",
                                    "data": {"findings": []},
                                }
                            ),
                        },
                    }
                ],
            },
            finish="tool_calls",
        ),
        openai_response(
            message={"role": "assistant", "content": "Review terminalization complete."},
            finish="stop",
        ),
    ]

    server = OpenAILoopbackServer(replies)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        root = _repo(tmp_path)
        (root / "tracked.txt").write_text("modified uncommitted line\n")
        (root / "new_file.py").write_text("# new untracked python file\n")

        configure_openai_provider(root, f"http://127.0.0.1:{server.server_address[1]}/v1")

        with VoidCodeRuntime(workspace=root) as runtime:
            parent_resp = runtime.run(RuntimeRequest(session_id="parent-session", prompt="do parent work"))
            assert parent_resp.session.status == "completed"
            assert parent_resp.output == "parent finished"

            review_resp = runtime.review_session(session_id="parent-session", focus="security & logic bugs")
            assert review_resp.session.status == "completed"
            assert review_resp.session.session.parent_id == "parent-session"
            policy = review_resp.session.metadata.get("runtime_policy")
            assert isinstance(policy, dict)
            assert policy.get("agent_preset") == "review"
            assert review_resp.session.metadata.get("read_only") is True
            assert review_resp.output == "Review complete: 0 vulnerabilities found, diff verified clean."

            assert len(server.requests) >= 2
            review_req_body = server.requests[1]
            review_messages = review_req_body.get("messages", [])
            messages_text = json.dumps(review_messages)
            assert "Reviewer focus: security & logic bugs" in messages_text
            assert "modified uncommitted line" in messages_text
            assert "new_file.py" in messages_text

            parent_result = runtime.session_result(session_id="parent-session")
            assert parent_result.session.status == "completed"
            assert parent_result.output == "parent finished"
    finally:
        server.shutdown()
