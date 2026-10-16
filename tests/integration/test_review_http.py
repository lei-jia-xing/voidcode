from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from voidcode.runtime import create_runtime_app
from voidcode.runtime.contracts import RuntimeRequest
from voidcode.runtime.service import VoidCodeRuntime


@dataclass(frozen=True, slots=True)
class _TransportResponse:
    status: int
    headers: dict[str, str]
    body_parts: list[bytes]

    @property
    def body(self) -> bytes:
        return b"".join(self.body_parts)

    def json(self) -> object:
        return json.loads(self.body.decode("utf-8"))


def _run_app(
    app: object,
    *,
    method: str,
    path: str,
    body: bytes = b"",
    query_string: bytes = b"",
) -> _TransportResponse:
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
        "method": method,
        "path": path,
        "query_string": query_string,
    }
    asyncio.run(app(scope, _receive, _send))

    start_message = next(message for message in sent if cast(str, message["type"]) == "http.response.start")
    headers = {key.decode("utf-8").lower(): value.decode("utf-8") for key, value in cast(list[tuple[bytes, bytes]], start_message["headers"])}
    body_parts = [cast(bytes, message.get("body", b"")) for message in sent if cast(str, message["type"]) == "http.response.body"]
    return _TransportResponse(
        status=cast(int, start_message["status"]),
        headers=headers,
        body_parts=body_parts,
    )


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def _setup_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "--quiet")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "--quiet", "-m", "initial commit")
    return root


def test_http_get_review_branches_and_commits(tmp_path: Path) -> None:
    root = _setup_repo(tmp_path)
    _git(root, "checkout", "-b", "feature")
    (root / "feat.txt").write_text("feature\n", encoding="utf-8")
    _git(root, "add", "feat.txt")
    _git(root, "commit", "--quiet", "-m", "feature commit")

    app = create_runtime_app(workspace=root)

    # GET /api/review/branches
    branches_resp = _run_app(app, method="GET", path="/api/review/branches")
    assert branches_resp.status == 200
    branches_data = cast(dict[str, object], branches_resp.json())
    assert "branches" in branches_data
    assert "master" in cast(list[str], branches_data["branches"])
    assert "feature" in cast(list[str], branches_data["branches"])
    assert branches_data["current"] == "feature"

    # GET /api/review/commits
    commits_resp = _run_app(app, method="GET", path="/api/review/commits")
    assert commits_resp.status == 200
    commits_data = cast(dict[str, object], commits_resp.json())
    assert "commits" in commits_data
    commits_list = cast(list[dict[str, object]], commits_data["commits"])
    assert len(commits_list) >= 2
    assert commits_list[0]["subject"] == "feature commit"


def test_http_post_review_session_targets(tmp_path: Path) -> None:
    root = _setup_repo(tmp_path)
    (root / "initial.txt").write_text("initial\n")
    _git(root, "add", "initial.txt")
    _git(root, "commit", "-m", "second commit")
    commit_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True).stdout.strip()

    _git(root, "checkout", "-b", "dev")
    (root / "dev.txt").write_text("dev change\n")
    _git(root, "add", "dev.txt")
    _git(root, "commit", "-m", "dev commit")

    from voidcode.core.turns import FinalTurn, ModelTurnFact, ToolTurn, TurnPlan, TurnRequest
    from voidcode.tools.contracts import ToolCall

    class _TestProducer:
        def produce(self, request: TurnRequest, tool_results: tuple[object, ...], *, session: object) -> TurnPlan:
            if getattr(session, "session_id", None) == "parent-session":
                return FinalTurn(facts=(ModelTurnFact(request.run_step, "deterministic", request.prompt),), output="parent done")
            if request.run_step == 1:
                return ToolTurn(
                    facts=(ModelTurnFact(request.run_step, "deterministic", request.prompt),),
                    calls=(
                        ToolCall(
                            tool_name="yield",
                            tool_call_id="call-1",
                            arguments={"summary": "Review complete: all checks passed", "data": {"findings": []}},
                        ),
                    ),
                )
            return FinalTurn(facts=(ModelTurnFact(request.run_step, "deterministic", request.prompt),), output="review completed")

    producer = _TestProducer()
    with VoidCodeRuntime(workspace=root, turn_producer=producer) as runtime:
        runtime.run(RuntimeRequest(session_id="parent-session", prompt="parent prompt"))

    app = create_runtime_app(workspace=root, runtime_factory=lambda: VoidCodeRuntime(workspace=root, turn_producer=producer))
    payload_base = json.dumps({"target": {"type": "base_branch", "branch": "master"}}).encode("utf-8")
    resp_base = _run_app(app, method="POST", path="/api/sessions/parent-session/review", body=payload_base)
    assert resp_base.status == 200
    base_json = cast(dict[str, object], resp_base.json())
    assert "session" in base_json

    # 2. Commit target
    payload_commit = json.dumps({"target": {"type": "commit", "sha": commit_sha, "title": "second commit"}}).encode("utf-8")
    resp_commit = _run_app(app, method="POST", path="/api/sessions/parent-session/review", body=payload_commit)
    assert resp_commit.status == 200
    commit_json = cast(dict[str, object], resp_commit.json())
    assert "session" in commit_json

    # 3. Uncommitted target (modify working tree)
    (root / "tracked.txt").write_text("uncommitted change\n")
    payload_uncommitted = json.dumps({"target": {"type": "uncommitted"}}).encode("utf-8")
    resp_uncommitted = _run_app(app, method="POST", path="/api/sessions/parent-session/review", body=payload_uncommitted)
    assert resp_uncommitted.status == 200

    # 4. Backward compat focus-only payload -> Custom
    payload_focus = json.dumps({"focus": "check correctness"}).encode("utf-8")
    resp_focus = _run_app(app, method="POST", path="/api/sessions/parent-session/review", body=payload_focus)
    assert resp_focus.status == 200

    # 5. Invalid target type rejection
    payload_invalid = json.dumps({"target": {"type": "invalid_target"}}).encode("utf-8")
    resp_invalid = _run_app(app, method="POST", path="/api/sessions/parent-session/review", body=payload_invalid)
    assert resp_invalid.status == 400
