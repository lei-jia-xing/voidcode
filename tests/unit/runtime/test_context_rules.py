from __future__ import annotations

from pathlib import Path

from voidcode.runtime.context.rules import (
    LOCAL_RULE_FILE_NAME,
    MAX_RULE_FILES,
    RULE_FILE_NAME,
    _applicable_rule_paths,
    _find_project_root,
    _project_hierarchy_rule_paths,
    runtime_file_rule_contexts,
)
from voidcode.runtime.context.transforms import build_provider_context_transform_result


def test_aggregate_rule_byte_budget_and_truncation_metadata(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / RULE_FILE_NAME).write_text("éé", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / RULE_FILE_NAME).write_text("later", encoding="utf-8")

    exact = runtime_file_rule_contexts(workspace=sub, tool_results=(), max_total_rule_bytes=4)
    assert len(exact) == 1
    assert exact[0].content == "éé"
    assert exact[0].truncated is True
    assert len(exact[0].content.encode("utf-8")) == 4

    partial = runtime_file_rule_contexts(workspace=sub, tool_results=(), max_total_rule_bytes=3)
    assert len(partial) == 1
    assert partial[0].content == "é"
    assert len(partial[0].content.encode("utf-8")) <= 3
    assert partial[0].truncated is True

    tiny = runtime_file_rule_contexts(workspace=sub, tool_results=(), max_total_rule_bytes=1)
    assert len(tiny) == 1
    assert len(tiny[0].content.encode("utf-8")) <= 1
    assert tiny[0].truncated is True

    zero = runtime_file_rule_contexts(workspace=sub, tool_results=(), max_total_rule_bytes=0)
    assert len(zero) == 1
    assert zero[0].content == ""
    assert zero[0].truncated is True


def test_aggregate_rule_byte_budget_bounds_ascii_truncation_marker(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / RULE_FILE_NAME).write_text("A" * 100, encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / RULE_FILE_NAME).write_text("B" * 100, encoding="utf-8")
    contexts = runtime_file_rule_contexts(workspace=sub, tool_results=(), max_total_rule_bytes=80)
    assert len(contexts) == 1
    assert sum(len(context.content.encode("utf-8")) for context in contexts) <= 80
    assert contexts[0].content.startswith("A")
    assert contexts[0].truncated is True


def test_local_override_takes_precedence_over_agents_md(tmp_path: Path) -> None:
    (tmp_path / RULE_FILE_NAME).write_text("standard agents", encoding="utf-8")
    (tmp_path / LOCAL_RULE_FILE_NAME).write_text("local override agents", encoding="utf-8")

    contexts = runtime_file_rule_contexts(workspace=tmp_path, tool_results=())
    assert len(contexts) == 1
    assert contexts[0].path == LOCAL_RULE_FILE_NAME
    assert contexts[0].content == "local override agents"


def test_project_hierarchy_discovered_root_to_workspace_on_turn_one(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()
    (repo_root / RULE_FILE_NAME).write_text("root guidance", encoding="utf-8")

    subproject = repo_root / "packages" / "frontend"
    subproject.mkdir(parents=True)
    (subproject / LOCAL_RULE_FILE_NAME).write_text("subproject override", encoding="utf-8")

    contexts = runtime_file_rule_contexts(workspace=subproject, tool_results=())
    assert len(contexts) == 2
    assert contexts[0].path == "../../AGENTS.md"
    assert contexts[0].content == "root guidance"
    assert contexts[1].path == LOCAL_RULE_FILE_NAME
    assert contexts[1].content == "subproject override"


def test_git_worktree_file_marker_recognized_as_project_root(tmp_path: Path) -> None:
    repo_root = tmp_path / "worktree"
    repo_root.mkdir()
    (repo_root / ".git").write_text("gitdir: /path/to/main/.git/worktrees/wt\n", encoding="utf-8")
    (repo_root / RULE_FILE_NAME).write_text("worktree root rules", encoding="utf-8")

    sub_dir = repo_root / "sub"
    sub_dir.mkdir()
    (sub_dir / RULE_FILE_NAME).write_text("sub rules", encoding="utf-8")

    assert _find_project_root(sub_dir) == repo_root
    hierarchy = _project_hierarchy_rule_paths(workspace_root=sub_dir)
    assert len(hierarchy) == 2
    assert hierarchy[0] == repo_root / RULE_FILE_NAME
    assert hierarchy[1] == sub_dir / RULE_FILE_NAME


def test_no_git_marker_stops_at_workspace_boundary(tmp_path: Path) -> None:
    parent_dir = tmp_path / "outside"
    parent_dir.mkdir()
    (parent_dir / RULE_FILE_NAME).write_text("outside leaked rules", encoding="utf-8")

    workspace = parent_dir / "isolated_workspace"
    workspace.mkdir()
    (workspace / RULE_FILE_NAME).write_text("workspace rules", encoding="utf-8")

    # When no .git marker is found above, _find_project_root stops at workspace
    assert _find_project_root(workspace) == workspace
    hierarchy = _project_hierarchy_rule_paths(workspace_root=workspace)
    assert len(hierarchy) == 1
    assert hierarchy[0] == workspace / RULE_FILE_NAME


def test_baseline_rules_not_evicted_by_max_rule_files_limit(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / RULE_FILE_NAME).write_text("baseline root", encoding="utf-8")

    # Create 12 subdirectories with rule files
    touched_paths: list[str] = []
    for i in range(12):
        sub = tmp_path / f"pkg_{i:02d}"
        sub.mkdir()
        (sub / RULE_FILE_NAME).write_text(f"rule {i}", encoding="utf-8")
        target = sub / "file.txt"
        target.write_text("data", encoding="utf-8")
        touched_paths.append(str(target))

    paths = _applicable_rule_paths(
        workspace_root=tmp_path,
        touched_paths=tuple(touched_paths),
        max_rule_files=MAX_RULE_FILES,
        include_workspace_root=True,
    )
    assert len(paths) == MAX_RULE_FILES
    # Root baseline rule MUST be preserved as the first entry, never evicted
    assert paths[0] == tmp_path / RULE_FILE_NAME


def test_first_turn_context_transform_injects_root_and_subproject_hierarchy(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()
    (repo_root / RULE_FILE_NAME).write_text("Repository architecture and security policy", encoding="utf-8")

    frontend = repo_root / "apps" / "frontend"
    frontend.mkdir(parents=True)
    (frontend / LOCAL_RULE_FILE_NAME).write_text("Frontend React guidelines and local override", encoding="utf-8")

    transform_res = build_provider_context_transform_result(
        workspace=frontend,
        tool_results=(),
        hook_preset_context="",
    )
    injections = transform_res.injections
    assert len(injections) == 2
    assert injections[0].metadata.get("path") == "../../AGENTS.md"
    assert "Repository architecture and security policy" in injections[0].content
    assert injections[1].metadata.get("path") == LOCAL_RULE_FILE_NAME
    assert "Frontend React guidelines and local override" in injections[1].content
