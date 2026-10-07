from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import voidcode.tools.output as output_module
from voidcode.core.tool_context import ArtifactInvalid, ArtifactPage, ArtifactUnavailable, NextPage
from voidcode.tools.contracts import TextOutput, ToolFailure, ToolSuccess
from voidcode.tools.output import (
    cap_tool_result_output,
    read_tool_output_artifact,
    resolve_tool_output_artifact,
    sanitize_tool_arguments,
    sanitize_tool_result_data,
    search_tool_output_artifact,
    strip_redaction_sentinels,
    tool_output_artifact_temp_root,
)


def _metadata(result: object) -> dict[str, object]:
    output = result.output
    assert isinstance(output, TextOutput)
    reference = output.bounds.reference
    assert reference is not None and reference.artifact is not None
    return dict(reference.artifact)


def test_cap_tool_result_output_noops_under_limits(tmp_path: Path) -> None:
    result = ToolSuccess(tool_name="sample", output=TextOutput("small output"))

    capped = cap_tool_result_output(result)

    assert capped is result
    assert not (tmp_path / ".voidcode" / "tool-output").exists()


def test_cap_tool_result_output_caps_by_line_count_and_saves_full_output(tmp_path: Path) -> None:
    content = "".join(f"line-{index}\n" for index in range(6))
    result = ToolSuccess(tool_name="sample", output=TextOutput(content))

    capped = cap_tool_result_output(result, max_lines=3, max_bytes=10_000)

    assert isinstance(capped.output, TextOutput)
    assert "line-0" in capped.output.text
    assert "line-3" not in capped.output.text
    assert "Tool output truncated" in capped.output.text
    assert capped.output.bounds.truncated is True
    assert capped.output.bounds.partial is True
    assert capped.output.bounds.reference is not None
    assert capped.output.bounds.reference is not None
    assert capped.output.bounds.reference.uri.startswith("voidcode://artifact/")
    assert not (tmp_path / ".voidcode" / "tool-output").exists()
    raw_artifact = _metadata(capped)["artifact"]
    assert isinstance(raw_artifact, dict)
    artifact = cast(dict[str, object], raw_artifact)
    assert artifact["status"] == "available"
    assert artifact["producer"] == "voidcode.tool_output.v1"
    assert _metadata(capped)["artifact_id"] == artifact["artifact_id"]
    normalization = cast(dict[str, object], _metadata(capped)["normalization"])
    assert normalization["kind"] == "tool_output_truncated"
    assert normalization["artifact_id"] == artifact["artifact_id"]
    assert "retry_guidance" in _metadata(capped)
    diagnostics = cast(list[dict[str, object]], _metadata(capped)["diagnostics"])
    assert diagnostics[-1]["reason"] == "tool_output_truncated"
    artifact_path = Path(cast(str, artifact["path"]))
    assert artifact_path.read_text(encoding="utf-8") == content
    assert artifact_path.stat().st_mode & 0o777 == 0o600
    assert tool_output_artifact_temp_root().stat().st_mode & 0o777 == 0o700
    assert _metadata(capped)["original_line_count"] == 6


def test_tool_output_artifact_temp_root_uses_xdg_cache_home(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(output_module.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))

    root = tool_output_artifact_temp_root()

    assert root == tmp_path / "xdg-cache" / "voidcode" / "tool-output"
    assert root.stat().st_mode & 0o777 == 0o700


def test_tool_output_artifact_temp_root_uses_windows_local_app_data(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(output_module.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "RoamingAppData"))

    root = tool_output_artifact_temp_root()

    assert root == tmp_path / "LocalAppData" / "voidcode" / "tool-output"
    assert root.stat().st_mode & 0o777 == 0o700


def test_tool_output_artifact_retrieval_supports_offsets_and_search(tmp_path: Path) -> None:
    content = "alpha\nbeta\ngamma\nbeta-two\n"
    result = ToolSuccess(tool_name="sample", output=TextOutput(content))

    capped = cap_tool_result_output(
        result,
        session_id="session-1",
        tool_call_id="call-1",
        max_lines=2,
        max_bytes=10_000,
    )

    raw_artifact = _metadata(capped)["artifact"]
    assert isinstance(raw_artifact, dict)
    artifact = cast(dict[str, object], raw_artifact)
    read_result = read_tool_output_artifact(artifact, offset=1, limit=2)
    assert isinstance(read_result, ArtifactPage)
    assert read_result.text == "beta\ngamma\n"
    assert isinstance(read_result.page, NextPage)
    assert read_result.page.offset == 3

    search_result = search_tool_output_artifact(artifact, pattern="beta")
    assert search_result["artifact_missing"] is False
    assert search_result["match_count"] == 2
    matches = search_result["matches"]
    assert isinstance(matches, list)
    assert matches[0] == {"line_number": 2, "line": "beta"}


def test_tool_output_artifact_reference_metadata_is_bounded_and_safe(tmp_path: Path) -> None:
    raw_content = "".join(f"artifact-line-{index}\n" for index in range(40))
    result = ToolSuccess(tool_name="shell_exec", output=TextOutput(raw_content))

    capped = cap_tool_result_output(
        result,
        session_id="session-1",
        tool_call_id="call-1",
        max_lines=2,
        max_bytes=80,
    )

    assert isinstance(capped.output, TextOutput)
    assert "artifact-line-0" in capped.output.text
    assert "artifact-line-10" not in capped.output.text
    assert capped.output.bounds.reference is not None
    assert capped.output.bounds.reference.uri == f"voidcode://artifact/{_metadata(capped)['artifact_id']}"
    assert "read(path=" in capped.output.text
    assert 'task(operation="output", full_session=true)' not in capped.output.text
    assert _metadata(capped)["retry_guidance"] == f'Read the full output with read(path="{capped.output.bounds.reference.uri}").'
    raw_artifact = _metadata(capped)["artifact"]
    assert isinstance(raw_artifact, dict)
    artifact = cast(dict[str, object], raw_artifact)
    assert artifact["artifact_id"] == _metadata(capped)["artifact_id"]
    assert artifact["tool_call_id"] == "call-1"
    assert artifact["byte_count"] == len(raw_content.encode("utf-8"))
    assert artifact["line_count"] == 40
    assert _metadata(capped)["output_path"] == artifact["path"]
    assert str(tmp_path / ".voidcode") not in cast(str, artifact["path"])
    assert ".xdg-cache" in cast(str, artifact["path"])
    retrieved = read_tool_output_artifact(artifact, offset=0, limit=10)

    assert isinstance(retrieved, ArtifactPage)
    assert retrieved.text == "".join(f"artifact-line-{index}\n" for index in range(10))
    assert isinstance(retrieved.page, NextPage)
    assert retrieved.page.offset == 10


def test_tool_output_artifact_resolves_from_events_by_id_or_tool_call(tmp_path: Path) -> None:
    result = ToolSuccess(tool_name="sample", output=TextOutput("one\ntwo\nthree\n"))
    capped = cap_tool_result_output(
        result,
        session_id="session-1",
        tool_call_id="call-1",
        max_lines=1,
        max_bytes=10_000,
    )
    artifact_id = _metadata(capped)["artifact_id"]
    assert isinstance(artifact_id, str)
    events = [{"payload": {"artifact": _metadata(capped)["artifact"]}}]

    by_artifact_id = resolve_tool_output_artifact(events, artifact_id=artifact_id)
    by_tool_call_id = resolve_tool_output_artifact(events, tool_call_id="call-1")

    assert by_artifact_id is not None
    assert by_artifact_id["artifact_id"] == artifact_id
    assert by_tool_call_id is not None
    assert by_tool_call_id["tool_call_id"] == "call-1"


def test_tool_output_artifact_resolver_skips_invalid_candidate_for_same_tool_call(
    tmp_path: Path,
) -> None:
    result = ToolSuccess(tool_name="sample", output=TextOutput("one\ntwo\nthree\n"))
    capped = cap_tool_result_output(
        result,
        session_id="session-1",
        tool_call_id="call-1",
        max_lines=1,
        max_bytes=10_000,
    )
    valid_artifact = _metadata(capped)["artifact"]
    assert isinstance(valid_artifact, dict)
    forged_artifact = {
        **cast(dict[str, object], valid_artifact),
        "artifact_id": "artifact_",
    }
    events: list[object] = [
        {"payload": {"artifact": forged_artifact}},
        {"payload": {"artifact": valid_artifact}},
    ]

    resolved = resolve_tool_output_artifact(events, tool_call_id="call-1")

    assert resolved is not None
    assert resolved["artifact_id"] == _metadata(capped)["artifact_id"]


def test_tool_output_artifact_resolver_skips_invalid_candidate_for_same_artifact_id(
    tmp_path: Path,
) -> None:
    result = ToolSuccess(tool_name="sample", output=TextOutput("one\ntwo\nthree\n"))
    capped = cap_tool_result_output(
        result,
        session_id="session-1",
        tool_call_id="call-1",
        max_lines=1,
        max_bytes=10_000,
    )
    valid_artifact = _metadata(capped)["artifact"]
    assert isinstance(valid_artifact, dict)
    artifact = cast(dict[str, object], valid_artifact)
    raw_artifact_id = artifact["artifact_id"]
    assert isinstance(raw_artifact_id, str)
    artifact_id = raw_artifact_id
    artifact_path = Path(cast(str, artifact["path"]))
    forged_artifact = {
        **artifact,
        "path": str(artifact_path.with_name("tool-call-artifact_000000000000000000000000.txt")),
    }
    events: list[object] = [
        {"payload": {"artifact": forged_artifact}},
        {"payload": {"artifact": valid_artifact}},
    ]

    resolved = resolve_tool_output_artifact(events, artifact_id=artifact_id)

    assert resolved is not None
    assert resolved["artifact_id"] == artifact_id


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
    resolved = resolve_tool_output_artifact([{"payload": {"artifact": forged_artifact}}], artifact_id="artifact_forged")

    assert isinstance(read_result, ArtifactInvalid)
    assert search_result["status"] == "invalid"
    assert resolved is None


def test_tool_output_artifact_rejects_traversal_reference(tmp_path: Path) -> None:
    outside_file = tmp_path / "secret.txt"
    outside_file.write_text("must not read", encoding="utf-8")
    forged_artifact: dict[str, object] = {
        "producer": "voidcode.tool_output.v1",
        "artifact_id": "artifact_000000000000000000000000",
        "path": str(outside_file.parent / ".." / outside_file.name),
        "tool_call_id": "call-1",
    }

    read_result = read_tool_output_artifact(forged_artifact)
    search_result = search_tool_output_artifact(forged_artifact, pattern="must")
    resolved = resolve_tool_output_artifact(
        [{"payload": {"artifact": forged_artifact}}],
        tool_call_id="call-1",
    )

    assert isinstance(read_result, ArtifactInvalid)
    assert search_result["status"] == "invalid"
    assert resolved is None


def test_tool_output_artifact_rejects_short_id_for_another_artifact_path(
    tmp_path: Path,
) -> None:
    result = ToolSuccess(tool_name="sample", output=TextOutput("one\ntwo\nthree\n"))
    capped = cap_tool_result_output(result, max_lines=1, max_bytes=10_000)
    real_artifact = _metadata(capped)["artifact"]
    assert isinstance(real_artifact, dict)
    forged_artifact = {
        **cast(dict[str, object], real_artifact),
        "artifact_id": "artifact_",
    }

    read_result = read_tool_output_artifact(forged_artifact)
    search_result = search_tool_output_artifact(forged_artifact, pattern="three")
    resolved = resolve_tool_output_artifact([{"payload": {"artifact": forged_artifact}}], artifact_id="artifact_")

    assert isinstance(read_result, ArtifactInvalid)
    assert search_result["status"] == "invalid"
    assert resolved is None


def test_tool_output_artifact_rejects_valid_shaped_id_for_another_artifact_path(
    tmp_path: Path,
) -> None:
    result = ToolSuccess(tool_name="sample", output=TextOutput("one\ntwo\nthree\n"))
    capped = cap_tool_result_output(result, max_lines=1, max_bytes=10_000)
    real_artifact = _metadata(capped)["artifact"]
    assert isinstance(real_artifact, dict)
    forged_id = "artifact_000000000000000000000000"
    forged_artifact = {
        **cast(dict[str, object], real_artifact),
        "artifact_id": forged_id,
    }

    read_result = read_tool_output_artifact(forged_artifact)
    search_result = search_tool_output_artifact(forged_artifact, pattern="three")
    resolved = resolve_tool_output_artifact([{"payload": {"artifact": forged_artifact}}], artifact_id=forged_id)

    assert isinstance(read_result, ArtifactInvalid)
    assert search_result["status"] == "invalid"
    assert resolved is None


def test_tool_output_artifact_retrieval_reports_missing(tmp_path: Path) -> None:
    result = ToolSuccess(tool_name="sample", output=TextOutput("one\ntwo\nthree\n"))
    capped = cap_tool_result_output(result, max_lines=1, max_bytes=10_000)
    raw_artifact = _metadata(capped)["artifact"]
    assert isinstance(raw_artifact, dict)
    artifact = cast(dict[str, object], raw_artifact)
    Path(cast(str, artifact["path"])).unlink()

    missing = read_tool_output_artifact(artifact)

    assert isinstance(missing, ArtifactUnavailable)


def test_tool_output_artifact_search_reports_missing(tmp_path: Path) -> None:
    result = ToolSuccess(tool_name="sample", output=TextOutput("one\ntwo\nthree\n"))
    capped = cap_tool_result_output(result, max_lines=1, max_bytes=10_000)
    raw_artifact = _metadata(capped)["artifact"]
    assert isinstance(raw_artifact, dict)
    artifact = cast(dict[str, object], raw_artifact)
    Path(cast(str, artifact["path"])).unlink()

    missing = search_tool_output_artifact(artifact, pattern="two")

    assert missing["status"] == "missing"
    assert missing["artifact_missing"] is True
    assert "matches" not in missing


def test_cap_tool_result_output_caps_by_utf8_byte_count_safely(tmp_path: Path) -> None:
    content = "π" * 100
    result = ToolSuccess(tool_name="unicode", output=TextOutput(content))

    capped = cap_tool_result_output(result, max_lines=2000, max_bytes=51)

    assert isinstance(capped.output, TextOutput)
    assert "�" not in capped.output.text
    assert "Tool output truncated" in capped.output.text
    assert capped.output.bounds.reference is not None
    raw_artifact = _metadata(capped)["artifact"]
    assert isinstance(raw_artifact, dict)
    artifact = cast(dict[str, object], raw_artifact)
    assert Path(cast(str, artifact["path"])).read_text(encoding="utf-8") == content


def test_cap_tool_result_output_skips_errors_and_empty_content(tmp_path: Path) -> None:
    empty = ToolSuccess(tool_name="sample", output=TextOutput(""))

    assert cap_tool_result_output(empty) is empty


def test_cap_tool_result_output_caps_large_errors(tmp_path: Path) -> None:
    error_text = "".join(f"error-{index}\n" for index in range(10))
    result = ToolFailure(tool_name="sample", error=error_text, output=TextOutput(error_text))

    capped = cap_tool_result_output(result, max_lines=3, max_bytes=10_000)

    assert capped.error is not None
    assert "error-0" in capped.error
    assert "error-4" not in capped.error
    assert "Tool error truncated" in capped.error
    assert capped.output.bounds.truncated is True
    diagnostics = cast(list[dict[str, object]], _metadata(capped)["diagnostics"])
    assert diagnostics[-1]["reason"] == "tool_error_truncated"
    retry_guidance = cast(str, diagnostics[-1]["retry_guidance"])
    assert 'task(operation="output", full_session=true)' not in retry_guidance
    assert "full error" in retry_guidance
    assert capped.output.bounds.reference is not None
    normalization = cast(dict[str, object], _metadata(capped)["normalization"])
    assert normalization["kind"] == "tool_error_truncated"
    raw_artifact = _metadata(capped)["artifact"]
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
    query_summary = cast(dict[str, object], query)
    assert query_summary["omitted"] is True
    assert query_summary["byte_count"] == len(large_query.encode("utf-8"))
    assert query_summary["line_count"] == 1
    assert query_summary["preview"] == large_query[:4000]
    assert query_summary["omitted_chars"] == len(large_query) - 4000


def test_sanitize_tool_result_data_strips_inline_blobs_and_nested_arguments() -> None:
    data_uri = "data:image/png;base64," + "A" * 100
    raw_blocker = "secret blocked reason"
    sanitized = sanitize_tool_result_data(
        {
            "arguments": {"content": "raw file body", "path": "out.txt"},
            "attachment": {"mime": "image/png", "data_uri": data_uri},
            "todos": [{"content": "raw todo text", "status": "pending"}],
            "phases": [{"name": "Tasks", "tasks": [{"content": "blocked", "status": "blocked", "blocker": raw_blocker}]}],
        }
    )

    assert sanitized["arguments"] == {
        "content": {"omitted": True, "byte_count": 13, "line_count": 1},
        "path": "out.txt",
    }
    attachment = sanitized["attachment"]
    assert isinstance(attachment, dict)
    attachment_payload = cast(dict[str, object], attachment)
    assert attachment_payload["mime"] == "image/png"
    assert attachment_payload["data_uri"] == {
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
    phases = cast(list[dict[str, object]], sanitized["phases"])
    task = cast(dict[str, object], cast(list[object], phases[0]["tasks"])[0])
    assert task["blocker"] == {"omitted": True, "byte_count": len(raw_blocker.encode("utf-8")), "line_count": 1}


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
                }
            ]
        },
        redacted_keys=frozenset({"content"}),
    )

    assert stripped == {"todos": [{"content": "", "status": "pending"}]}


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
