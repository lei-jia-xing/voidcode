from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.agent import (
    agent_manifest_id_from_name,
    load_agent_manifest_registry,
    manifest_from_markdown_file,
    render_agent_prompt,
)


def _write_agent(path: Path, frontmatter: str, body: str = "Custom prompt body.") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")


def test_agent_manifest_id_from_name_normalizes_to_kebab() -> None:
    assert agent_manifest_id_from_name("Review Helper") == "review-helper"
    assert agent_manifest_id_from_name("  QA: Deep_Check!! ") == "qa-deep-check"


def test_manifest_from_markdown_file_parses_frontmatter_and_body(tmp_path: Path) -> None:
    path = tmp_path / "reviewer.md"
    _write_agent(
        path,
        "\n".join(
            (
                "name: Review Helper",
                "description: Focused reviewer",
                "mode: subagent",
                "model: opencode/test-model",
                "fallback_models: [opencode/fallback]",
                "tool_allowlist: [read_file, grep]",
                "skill_refs: [code-review]",
                "preset_hook_refs: [role_reminder]",
            )
        ),
        body="Stay read-only and summarize risks.",
    )

    manifest = manifest_from_markdown_file(path, scope="project")

    assert manifest.id == "review-helper"
    assert manifest.mode == "subagent"
    assert manifest.source_scope == "project"
    assert manifest.source_path == str(path)
    assert manifest.model_preference == "opencode/test-model"
    assert manifest.fallback_models == ("opencode/fallback",)
    assert manifest.tool_allowlist == ("read_file", "grep")
    assert manifest.skill_refs == ("code-review",)
    assert manifest.preset_hook_refs == ("role_reminder",)
    assert manifest.prompt_materialization is not None
    assert manifest.prompt_materialization.source == "custom_markdown"
    assert manifest.prompt_materialization.body == "Stay read-only and summarize risks."


def test_manifest_from_markdown_file_parses_prompt_append_literal_block(
    tmp_path: Path,
) -> None:
    path = tmp_path / "security.md"
    _write_agent(
        path,
        "\n".join(
            (
                "name: security-reviewer",
                "description: Reviews code for security issues",
                "mode: subagent",
                "prompt_append: |",
                "  Always include severity.",
                "  Include exact file paths.",
            )
        ),
        body="You are a security-focused review agent.",
    )

    manifest = manifest_from_markdown_file(path, scope="project")

    assert manifest.prompt_materialization is not None
    assert manifest.prompt_materialization.body == "You are a security-focused review agent."
    assert manifest.prompt_materialization.prompt_append == (
        "Always include severity.\nInclude exact file paths."
    )
    rendered = render_agent_prompt({"prompt_materialization": manifest.prompt_materialization})
    assert rendered == (
        "You are a security-focused review agent.\n\n"
        "Always include severity.\nInclude exact file paths."
    )


def test_manifest_from_markdown_file_parses_nested_block_mapping_lists(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mcp-reviewer.md"
    _write_agent(
        path,
        "\n".join(
            (
                "name: MCP Reviewer",
                "description: Reviews with MCP context",
                "mode: subagent",
                "mcp_binding:",
                "  profile: docs",
                "  servers:",
                "    - repo",
                "    - context7",
            )
        ),
    )

    manifest = manifest_from_markdown_file(path, scope="project")

    assert manifest.mcp_binding is not None
    assert manifest.mcp_binding.profile == "docs"
    assert manifest.mcp_binding.servers == ("repo", "context7")


def test_manifest_from_markdown_file_rejects_missing_required_fields(tmp_path: Path) -> None:
    path = tmp_path / "bad.md"
    _write_agent(path, "name: Missing Mode\ndescription: nope")

    with pytest.raises(ValueError, match="bad.md.*missing required.*mode"):
        _ = manifest_from_markdown_file(path, scope="project")


def test_registry_project_scope_overrides_user_scope(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    config_home = tmp_path / "xdg"
    _write_agent(
        config_home / "voidcode" / "agents" / "helper.md",
        "id: helper\nname: User Helper\ndescription: user\nmode: primary",
        body="user prompt",
    )
    _write_agent(
        workspace / ".voidcode" / "agents" / "helper.md",
        "id: helper\nname: Project Helper\ndescription: project\nmode: primary",
        body="project prompt",
    )

    registry = load_agent_manifest_registry(
        workspace,
        env={"XDG_CONFIG_HOME": str(config_home)},
    )

    manifest = registry.get("helper")
    assert manifest is not None
    assert manifest.name == "Project Helper"
    assert manifest.source_scope == "project"
    assert manifest.prompt_materialization is not None
    assert manifest.prompt_materialization.body == "project prompt"


def test_registry_rejects_duplicate_custom_ids_in_same_scope(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_agent(
        workspace / ".voidcode" / "agents" / "one.md",
        "id: helper\nname: Helper One\ndescription: one\nmode: primary",
    )
    _write_agent(
        workspace / ".voidcode" / "agents" / "two.md",
        "id: helper\nname: Helper Two\ndescription: two\nmode: primary",
    )

    with pytest.raises(ValueError, match="duplicate custom agent manifest id 'helper'"):
        _ = load_agent_manifest_registry(workspace, env={})


def test_registry_rejects_custom_builtin_id(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_agent(
        workspace / ".voidcode" / "agents" / "leader.md",
        "id: leader\nname: Fake Leader\ndescription: nope\nmode: primary",
    )

    with pytest.raises(ValueError, match="builtin id 'leader'.*cannot be replaced"):
        _ = load_agent_manifest_registry(workspace, env={})
