from __future__ import annotations

from pathlib import Path
from typing import cast

from voidcode.tools import (
    ToolResult,
    cap_tool_result_output,
    read_tool_output_artifact,
    resolve_tool_output_artifact,
    sanitize_tool_arguments,
    sanitize_tool_result_data,
    search_tool_output_artifact,
    strip_redaction_sentinels,
    tool_output_artifact_temp_root,
)


def test_cap_tool_result_output_noops_under_limits(tmp_path: Path) -> None:
    result = ToolResult(tool_name="sample", status="ok", content="small output")

    capped = cap_tool_result_output(result, workspace=tmp_path)

    assert capped is result
    assert not (tmp_path / ".voidcode" / "tool-output").exists()


def test_cap_tool_result_output_caps_by_line_count_and_saves_full_output(tmp_path: Path) -> None:
    content = "".join(f"line-{index}\n" for index in range(6))
    result = ToolResult(tool_name="sample", status="ok", content=content)

    capped = cap_tool_result_output(result, workspace=tmp_path, max_lines=3, max_bytes=10_000)

    assert capped.content is not None
    assert "line-0" in capped.content
    assert "line-3" not in capped.content
    assert "Tool output truncated" in capped.content
    assert capped.truncated is True
    assert capped.partial is True
    assert isinstance(capped.reference, str)
    assert capped.reference.startswith("artifact:")
    assert not (tmp_path / ".voidcode" / "tool-output").exists()
    raw_artifact = capped.data["artifact"]
    assert isinstance(raw_artifact, dict)
    artifact = cast(dict[str, object], raw_artifact)
    assert artifact["status"] == "available"
    assert artifact["producer"] == "voidcode.tool_output.v1"
    assert capped.data["artifact_id"] == artifact["artifact_id"]
    artifact_path = Path(cast(str, artifact["path"]))
    assert artifact_path.read_text(encoding="utf-8") == content
    assert artifact_path.stat().st_mode & 0o777 == 0o600
    assert tool_output_artifact_temp_root().stat().st_mode & 0o777 == 0o700
    assert capped.data["original_line_count"] == 6


def test_tool_output_artifact_retrieval_supports_offsets_and_search(tmp_path: Path) -> None:
    content = "alpha\nbeta\ngamma\nbeta-two\n"
    result = ToolResult(tool_name="sample", status="ok", content=content)

    capped = cap_tool_result_output(
        result,
        workspace=tmp_path,
        session_id="session-1",
        tool_call_id="call-1",
        max_lines=2,
        max_bytes=10_000,
    )

    raw_artifact = capped.data["artifact"]
    assert isinstance(raw_artifact, dict)
    artifact = cast(dict[str, object], raw_artifact)
    read_result = read_tool_output_artifact(artifact, offset=1, limit=2)
    assert read_result["artifact_missing"] is False
    assert read_result["content"] == "beta\ngamma\n"
    assert read_result["next_offset"] == 3

    search_result = search_tool_output_artifact(artifact, pattern="beta")
    assert search_result["artifact_missing"] is False
    assert search_result["match_count"] == 2
    matches = search_result["matches"]
    assert isinstance(matches, list)
    assert matches[0] == {"line_number": 2, "line": "beta"}


def test_tool_output_artifact_resolves_from_events_by_id_or_tool_call(tmp_path: Path) -> None:
    result = ToolResult(tool_name="sample", status="ok", content="one\ntwo\nthree\n")
    capped = cap_tool_result_output(
        result,
        workspace=tmp_path,
        session_id="session-1",
        tool_call_id="call-1",
        max_lines=1,
        max_bytes=10_000,
    )
    artifact_id = capped.data["artifact_id"]
    assert isinstance(artifact_id, str)
    events = [{"payload": {"artifact": capped.data["artifact"]}}]

    by_artifact_id = resolve_tool_output_artifact(events, artifact_id=artifact_id)
    by_tool_call_id = resolve_tool_output_artifact(events, tool_call_id="call-1")

    assert by_artifact_id is not None
    assert by_artifact_id["artifact_id"] == artifact_id
    assert by_tool_call_id is not None
    assert by_tool_call_id["tool_call_id"] == "call-1"


def test_tool_output_artifact_rejects_untrusted_paths(tmp_path: Path) -> None:
    outside_file = tmp_path / "secret.txt"
    outside_file.write_text("must not read", encoding="utf-8")
    forged_artifact: dict[str, object] = {
        "producer": "voidcode.tool_output.v1",
        "artifact_id": "artifact_forged",
        "path": str(outside_file),
    }

    read_result = read_tool_output_artifact(forged_artifact)
    search_result = search_tool_output_artifact(forged_artifact, pattern="must")
    resolved = resolve_tool_output_artifact(
        [{"payload": {"artifact": forged_artifact}}], artifact_id="artifact_forged"
    )

    assert read_result["status"] == "invalid"
    assert "content" not in read_result
    assert search_result["status"] == "invalid"
    assert resolved is None


def test_tool_output_artifact_retrieval_reports_missing(tmp_path: Path) -> None:
    result = ToolResult(tool_name="sample", status="ok", content="one\ntwo\nthree\n")
    capped = cap_tool_result_output(result, workspace=tmp_path, max_lines=1, max_bytes=10_000)
    raw_artifact = capped.data["artifact"]
    assert isinstance(raw_artifact, dict)
    artifact = cast(dict[str, object], raw_artifact)
    Path(cast(str, artifact["path"])).unlink()

    missing = read_tool_output_artifact(artifact)

    assert missing["status"] == "missing"
    assert missing["artifact_missing"] is True


def test_cap_tool_result_output_caps_by_utf8_byte_count_safely(tmp_path: Path) -> None:
    content = "π" * 100
    result = ToolResult(tool_name="unicode", status="ok", content=content)

    capped = cap_tool_result_output(result, workspace=tmp_path, max_lines=2000, max_bytes=51)

    assert capped.content is not None
    assert "�" not in capped.content
    assert "Tool output truncated" in capped.content
    assert isinstance(capped.reference, str)
    raw_artifact = capped.data["artifact"]
    assert isinstance(raw_artifact, dict)
    artifact = cast(dict[str, object], raw_artifact)
    assert Path(cast(str, artifact["path"])).read_text(encoding="utf-8") == content


def test_cap_tool_result_output_skips_errors_and_empty_content(tmp_path: Path) -> None:
    empty = ToolResult(tool_name="sample", status="ok", content="")

    assert cap_tool_result_output(empty, workspace=tmp_path) is empty


def test_cap_tool_result_output_caps_large_errors(tmp_path: Path) -> None:
    error_text = "".join(f"error-{index}\n" for index in range(10))
    result = ToolResult(tool_name="sample", status="error", error=error_text)

    capped = cap_tool_result_output(result, workspace=tmp_path, max_lines=3, max_bytes=10_000)

    assert capped.error is not None
    assert "error-0" in capped.error
    assert "error-4" not in capped.error
    assert "Tool error truncated" in capped.error
    assert capped.truncated is True
    assert isinstance(capped.reference, str)
    raw_artifact = capped.data["artifact"]
    assert isinstance(raw_artifact, dict)
    artifact = cast(dict[str, object], raw_artifact)
    assert Path(cast(str, artifact["path"])).read_text(encoding="utf-8") == error_text


def test_sanitize_tool_arguments_omits_sensitive_text_fields() -> None:
    sanitized = sanitize_tool_arguments(
        {
            "path": "sample.txt",
            "content": "secret contents",
            "edits": [{"oldString": "old", "newString": "new"}],
        }
    )

    assert sanitized["path"] == "sample.txt"
    assert sanitized["content"] == {"omitted": True, "byte_count": 15, "line_count": 1}
    edits = sanitized["edits"]
    assert isinstance(edits, list)
    assert edits[0] == {
        "oldString": {"omitted": True, "byte_count": 3, "line_count": 1},
        "newString": {"omitted": True, "byte_count": 3, "line_count": 1},
    }


def test_sanitize_tool_arguments_preserves_preview_for_oversized_benign_text() -> None:
    large_query = "context prefix " + ("x" * 5000)

    sanitized = sanitize_tool_arguments({"query": large_query})

    query = sanitized["query"]
    assert isinstance(query, dict)
    assert query["omitted"] is True
    assert query["byte_count"] == len(large_query.encode("utf-8"))
    assert query["line_count"] == 1
    assert query["preview"] == large_query[:4000]
    assert query["omitted_chars"] == len(large_query) - 4000


def test_sanitize_tool_result_data_strips_inline_blobs_and_nested_arguments() -> None:
    data_uri = "data:image/png;base64," + "A" * 100
    sanitized = sanitize_tool_result_data(
        {
            "arguments": {"content": "raw file body", "path": "out.txt"},
            "attachment": {"mime": "image/png", "data_uri": data_uri},
            "todos": [{"content": "raw todo text", "status": "pending"}],
        }
    )

    assert sanitized["arguments"] == {
        "content": {"omitted": True, "byte_count": 13, "line_count": 1},
        "path": "out.txt",
    }
    attachment = sanitized["attachment"]
    assert isinstance(attachment, dict)
    assert attachment["mime"] == "image/png"
    assert attachment["data_uri"] == {
        "omitted": True,
        "byte_count": len(data_uri.encode("utf-8")),
        "line_count": 1,
    }
    todos = sanitized["todos"]
    assert isinstance(todos, list)
    assert todos[0] == {
        "content": {"omitted": True, "byte_count": 13, "line_count": 1},
        "status": "pending",
    }


def test_strip_redaction_sentinels_replaces_redaction_metadata_with_empty_strings() -> None:
    stripped = strip_redaction_sentinels(
        {
            "path": "out.txt",
            "content": {"omitted": True, "byte_count": 11, "line_count": 1},
            "edits": [
                {
                    "oldString": {"omitted": True, "byte_count": 3, "line_count": 1},
                    "newString": {"omitted": True, "byte_count": 3, "line_count": 1},
                }
            ],
        },
        redacted_keys=frozenset({"content", "oldString", "newString"}),
    )

    assert stripped == {
        "path": "out.txt",
        "content": "",
        "edits": [{"oldString": "", "newString": ""}],
    }


def test_strip_redaction_sentinels_handles_nested_todo_arguments() -> None:
    stripped = strip_redaction_sentinels(
        {
            "todos": [
                {
                    "content": {"omitted": True, "byte_count": 9, "line_count": 1},
                    "status": "pending",
                    "priority": "high",
                }
            ]
        },
        redacted_keys=frozenset({"content"}),
    )

    assert stripped == {"todos": [{"content": "", "status": "pending", "priority": "high"}]}


def test_strip_redaction_sentinels_preserves_truncation_previews() -> None:
    summary = {
        "omitted": True,
        "byte_count": 5000,
        "line_count": 1,
        "preview": "safe oversized query prefix",
        "omitted_chars": 300,
    }

    stripped = strip_redaction_sentinels({"query": summary})

    assert stripped == {"query": summary}


def test_strip_redaction_sentinels_preserves_matching_custom_metadata_objects() -> None:
    metadata = {"omitted": True, "byte_count": 42, "line_count": 2}

    stripped = strip_redaction_sentinels({"content": metadata})

    assert stripped == {"content": metadata}
