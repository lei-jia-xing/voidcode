"""CLI contract tests for the memory command group."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

from .._paths import with_src_pythonpath

_MEMORY_CONTENT = "Prefer pytest over unittest"
_MEMORY_KIND = "preference"
_MEMORY_TAG = "python"


def _run_memory_cli(
    tmp_path: Path,
    *args: str,
    workspace: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    database_path = _external_database_path(tmp_path)
    effective_workspace = workspace or tmp_path / "workspace"
    effective_workspace.mkdir(parents=True, exist_ok=True)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    env = with_src_pythonpath(os.environ.copy())
    env.setdefault("VOIDCODE_EXECUTION_ENGINE", "deterministic")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "voidcode",
            "--db-path",
            str(database_path),
            "memory",
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=effective_workspace,
    )


def _external_database_path(tmp_path: Path) -> Path:
    return Path("/tmp/opencode") / "voidcode-memory-cli-tests" / tmp_path.name / "memory.sqlite3"


def _json_payload(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)


def _write_memory_disabled_config(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / ".voidcode.json").write_text(
        json.dumps({"memory": {"enabled": False}}),
        encoding="utf-8",
    )


def _add_memory(tmp_path: Path) -> dict[str, Any]:
    result = _run_memory_cli(
        tmp_path,
        "add",
        "--workspace",
        ".",
        "--kind",
        _MEMORY_KIND,
        "--tag",
        _MEMORY_TAG,
        _MEMORY_CONTENT,
        "--json",
    )
    payload = _json_payload(result)
    memory = payload["memory"]
    assert isinstance(memory, dict)
    return cast(dict[str, Any], memory)


def _assert_no_workspace_storage_files(workspace: Path) -> None:
    forbidden_suffixes = {".db", ".sqlite", ".sqlite3"}
    forbidden_name_fragments = ("sqlite-vec", "vector-index")
    created = [
        path.relative_to(workspace)
        for path in workspace.rglob("*")
        if path.is_file()
        and (
            path.suffix in forbidden_suffixes
            or any(fragment in path.name for fragment in forbidden_name_fragments)
        )
    ]
    assert created == []


def test_memory_add_outputs_json_record_and_human_summary(tmp_path: Path) -> None:
    json_result = _run_memory_cli(
        tmp_path,
        "add",
        "--workspace",
        ".",
        "--kind",
        _MEMORY_KIND,
        "--tag",
        _MEMORY_TAG,
        _MEMORY_CONTENT,
        "--json",
    )
    payload = _json_payload(json_result)

    assert payload["memory"] == {
        "id": payload["memory"]["id"],
        "workspace_id": str(tmp_path / "workspace"),
        "kind": _MEMORY_KIND,
        "content": _MEMORY_CONTENT,
        "tags": [_MEMORY_TAG],
        "created_at": payload["memory"]["created_at"],
    }
    assert isinstance(payload["memory"]["id"], str)
    assert payload["memory"]["id"]
    assert isinstance(payload["memory"]["created_at"], int)

    human_result = _run_memory_cli(
        tmp_path,
        "add",
        "--workspace",
        ".",
        "--kind",
        _MEMORY_KIND,
        "--tag",
        _MEMORY_TAG,
        _MEMORY_CONTENT,
    )

    assert human_result.returncode == 0
    assert "Added memory" in human_result.stdout
    assert _MEMORY_KIND in human_result.stdout
    assert _MEMORY_TAG in human_result.stdout


def test_memory_list_outputs_json_and_human_results(tmp_path: Path) -> None:
    memory = _add_memory(tmp_path)

    json_result = _run_memory_cli(tmp_path, "list", "--workspace", ".", "--json")
    payload = _json_payload(json_result)

    assert payload["memories"] == [memory]
    assert payload["count"] == 1

    human_result = _run_memory_cli(tmp_path, "list", "--workspace", ".")

    assert human_result.returncode == 0
    assert memory["id"] in human_result.stdout
    assert _MEMORY_CONTENT in human_result.stdout
    assert _MEMORY_KIND in human_result.stdout


def test_memory_search_outputs_json_and_no_results_contract(tmp_path: Path) -> None:
    memory = _add_memory(tmp_path)

    match_result = _run_memory_cli(
        tmp_path,
        "search",
        "--workspace",
        ".",
        "pytest",
        "--json",
    )
    match_payload = _json_payload(match_result)

    assert match_payload["query"] == "pytest"
    assert match_payload["memories"] == [memory]
    assert match_payload["count"] == 1

    no_match_result = _run_memory_cli(
        tmp_path,
        "search",
        "--workspace",
        ".",
        "golang",
        "--json",
    )
    no_match_payload = _json_payload(no_match_result)

    assert no_match_payload == {"query": "golang", "memories": [], "count": 0}

    human_result = _run_memory_cli(tmp_path, "search", "--workspace", ".", "golang")

    assert human_result.returncode == 0
    assert "No memories found" in human_result.stdout


def test_memory_show_outputs_record_and_unknown_id_error(tmp_path: Path) -> None:
    memory = _add_memory(tmp_path)

    json_result = _run_memory_cli(
        tmp_path,
        "show",
        str(memory["id"]),
        "--workspace",
        ".",
        "--json",
    )
    payload = _json_payload(json_result)

    assert payload["memory"] == memory

    human_result = _run_memory_cli(tmp_path, "show", str(memory["id"]), "--workspace", ".")

    assert human_result.returncode == 0
    assert str(memory["id"]) in human_result.stdout
    assert _MEMORY_CONTENT in human_result.stdout

    missing_result = _run_memory_cli(
        tmp_path,
        "show",
        "mem_missing",
        "--workspace",
        ".",
        "--json",
    )

    assert missing_result.returncode != 0
    assert "mem_missing" in missing_result.stderr
    assert "not found" in missing_result.stderr.lower()


def test_memory_delete_tombstones_record_and_hides_it_by_default(tmp_path: Path) -> None:
    memory = _add_memory(tmp_path)

    delete_result = _run_memory_cli(
        tmp_path,
        "delete",
        str(memory["id"]),
        "--workspace",
        ".",
        "--json",
    )
    delete_payload = _json_payload(delete_result)

    assert delete_payload == {"deleted": True, "id": memory["id"]}

    for args in (
        ("list", "--workspace", ".", "--json"),
        ("search", "--workspace", ".", "pytest", "--json"),
    ):
        payload = _json_payload(_run_memory_cli(tmp_path, *args))
        assert payload["memories"] == []
        assert payload["count"] == 0

    show_result = _run_memory_cli(
        tmp_path,
        "show",
        str(memory["id"]),
        "--workspace",
        ".",
        "--json",
    )

    assert show_result.returncode != 0
    assert "not found" in show_result.stderr.lower()


def test_memory_status_reports_storage_scope_without_active_session(tmp_path: Path) -> None:
    result = _run_memory_cli(tmp_path, "status", "--workspace", ".", "--json")
    payload = _json_payload(result)

    assert payload["workspace_id"] == str(tmp_path / "workspace")
    assert payload["database_path"] == str(_external_database_path(tmp_path))
    assert payload["requires_active_session"] is False
    assert payload["enabled"] is True
    assert payload["scope"] == "workspace"
    assert payload["total_memories"] == 0
    assert payload["active_memories"] == 0
    assert payload["deleted_memories"] == 0
    assert payload["recall_enabled"] is False
    assert payload["semantic_search"] == "auto"
    assert payload["sqlite_vec"] == "auto"
    assert payload["keyword_search_available"] is True
    assert payload["semantic_search_available"] is False
    assert payload["sqlite_vec_status"] in {
        "available",
        "not_installed",
        "extension_loading_unavailable",
        "sqlite_version_unsupported",
    }

    human_result = _run_memory_cli(tmp_path, "status", "--workspace", ".")

    assert human_result.returncode == 0
    assert "Memory store" in human_result.stdout
    assert str(_external_database_path(tmp_path)) in human_result.stdout
    assert "active session: no" in human_result.stdout.lower()
    assert "keyword_search=true" in human_result.stdout
    assert "semantic_search=false" in human_result.stdout
    assert "sqlite_vec_status=" in human_result.stdout


def test_memory_disabled_config_blocks_active_cli_operations_but_allows_status(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    _write_memory_disabled_config(workspace)

    status_payload = _json_payload(
        _run_memory_cli(tmp_path, "status", "--workspace", ".", "--json", workspace=workspace)
    )

    assert status_payload["enabled"] is False
    assert status_payload["keyword_search_available"] is False
    assert status_payload["semantic_search_available"] is False
    assert status_payload["sqlite_vec_status"] == "disabled"

    for args in (
        ("add", "--workspace", ".", "disabled memory", "--json"),
        ("list", "--workspace", ".", "--json"),
        ("search", "--workspace", ".", "disabled", "--json"),
        ("show", "mem_missing", "--workspace", ".", "--json"),
        ("delete", "mem_missing", "--workspace", ".", "--json"),
    ):
        result = _run_memory_cli(tmp_path, *args, workspace=workspace)

        assert result.returncode != 0
        assert "memory is disabled" in result.stderr.lower()

    status_after_payload = _json_payload(
        _run_memory_cli(tmp_path, "status", "--workspace", ".", "--json", workspace=workspace)
    )
    assert status_after_payload["total_memories"] == 0
    assert status_after_payload["active_memories"] == 0


def test_memory_rejects_invalid_kind_and_empty_content(tmp_path: Path) -> None:
    invalid_kind = _run_memory_cli(
        tmp_path,
        "add",
        "--workspace",
        ".",
        "--kind",
        "invalid",
        "bad kind",
        "--json",
    )

    assert invalid_kind.returncode == 2
    assert "invalid" in invalid_kind.stderr.lower()
    assert "kind" in invalid_kind.stderr.lower()

    empty_content = _run_memory_cli(
        tmp_path,
        "add",
        "--workspace",
        ".",
        "--kind",
        _MEMORY_KIND,
        "",
        "--json",
    )

    assert empty_content.returncode == 2
    assert "content" in empty_content.stderr.lower()
    assert "empty" in empty_content.stderr.lower()


def test_default_memory_commands_do_not_create_workspace_local_storage(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    result = _run_memory_cli(
        tmp_path,
        "add",
        "--workspace",
        ".",
        "--kind",
        _MEMORY_KIND,
        _MEMORY_CONTENT,
        workspace=workspace,
    )

    assert result.returncode == 0
    _assert_no_workspace_storage_files(workspace)
