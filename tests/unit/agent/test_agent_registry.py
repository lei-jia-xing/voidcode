from __future__ import annotations

import sys
from pathlib import Path

import pytest

from voidcode.agent import (
    AgentMcpBindingIntent,
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
                "tool_allowlist: [read, grep]",
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
    assert manifest.tool_allowlist == ("read", "grep")
    assert manifest.skill_refs == ("code-review",)
    assert manifest.preset_hook_refs == ("role_reminder",)
    assert manifest.prompt_materialization is not None
    assert manifest.prompt_materialization.source == "custom_markdown"
    assert manifest.prompt_materialization.body == "Stay read-only and summarize risks."
    assert not hasattr(manifest, "routing_hints")


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
    assert manifest.prompt_materialization.prompt_append == ("Always include severity.\nInclude exact file paths.")
    rendered = render_agent_prompt({"prompt_materialization": manifest.prompt_materialization})
    assert rendered == ("You are a security-focused review agent.\n\nAlways include severity.\nInclude exact file paths.")


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


def test_manifest_from_markdown_file_supports_flow_sequence_with_quoted_comma(tmp_path: Path) -> None:
    path = tmp_path / "flow.md"
    _write_agent(
        path,
        "\n".join(
            (
                "name: Flow Reviewer",
                "description: Reviews with a quoted comma",
                "mode: subagent",
                'tool_allowlist: [read, "grep, ripgrep"]',
            )
        ),
    )

    manifest = manifest_from_markdown_file(path, scope="project")

    assert manifest.tool_allowlist == ("read", "grep, ripgrep")


def test_manifest_from_markdown_file_supports_flow_mapping_binding(tmp_path: Path) -> None:
    path = tmp_path / "flow-binding.md"
    _write_agent(
        path,
        "name: Flow Binding\ndescription: Reviews with MCP context\nmode: subagent\nmcp_binding: {profile: docs, servers: [repo, context7]}",
    )

    manifest = manifest_from_markdown_file(path, scope="project")

    assert manifest.mcp_binding == AgentMcpBindingIntent(profile="docs", servers=("repo", "context7"))


def test_manifest_from_markdown_file_supports_folded_block_scalar(tmp_path: Path) -> None:
    path = tmp_path / "folded.md"
    _write_agent(
        path,
        "\n".join(
            (
                "name: Folded Reviewer",
                "description: Reviews with folded guidance",
                "mode: subagent",
                "prompt_append: >",
                "  Always include severity",
                "  and exact file paths.",
            )
        ),
    )

    manifest = manifest_from_markdown_file(path, scope="project")

    assert manifest.prompt_materialization is not None
    assert manifest.prompt_materialization.prompt_append == "Always include severity and exact file paths."


def test_manifest_from_markdown_file_treats_unquoted_hash_as_comment(tmp_path: Path) -> None:
    unquoted = tmp_path / "comment.md"
    quoted = tmp_path / "quoted.md"
    _write_agent(unquoted, "name: Comment Reviewer\ndescription: Fix bug #42\nmode: subagent")
    _write_agent(quoted, 'name: Quoted Reviewer\ndescription: "Fix bug #42"\nmode: subagent')

    assert manifest_from_markdown_file(unquoted, scope="project").description == "Fix bug"
    assert manifest_from_markdown_file(quoted, scope="project").description == "Fix bug #42"


def test_manifest_from_markdown_file_rejects_duplicate_frontmatter_key(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.md"
    _write_agent(path, "name: Duplicate\ndescription: first\ndescription: second\nmode: subagent")

    with pytest.raises(ValueError, match="duplicate frontmatter key 'description'"):
        _ = manifest_from_markdown_file(path, scope="project")


def test_manifest_from_markdown_file_rejects_unknown_frontmatter_field(tmp_path: Path) -> None:
    path = tmp_path / "unknown.md"
    _write_agent(path, "name: Unknown\ndescription: nope\nmode: subagent\nrouting_hints: [fast]")

    with pytest.raises(ValueError, match="unsupported frontmatter field 'routing_hints'"):
        _ = manifest_from_markdown_file(path, scope="project")


@pytest.mark.parametrize(
    "frontmatter",
    ("name: 2024-01-01", "name: yes", "name: ''", "name:"),
    ids=("date-typed-string", "boolean-typed-string", "empty-string", "null-value"),
)
def test_manifest_from_markdown_file_rejects_invalid_required_values(tmp_path: Path, frontmatter: str) -> None:
    path = tmp_path / "typed.md"
    _write_agent(path, f"{frontmatter}\ndescription: nope\nmode: subagent")

    with pytest.raises(ValueError, match="frontmatter field 'name' must be a non-empty string"):
        _ = manifest_from_markdown_file(path, scope="project")


def test_manifest_from_markdown_file_rejects_missing_closing_delimiter(tmp_path: Path) -> None:
    path = tmp_path / "unterminated.md"
    path.write_text("---\nname: Unterminated\ndescription: nope\nmode: subagent\n", encoding="utf-8")

    with pytest.raises(ValueError, match="must close the YAML frontmatter block"):
        _ = manifest_from_markdown_file(path, scope="project")


def test_manifest_from_markdown_file_rejects_empty_body(tmp_path: Path) -> None:
    path = tmp_path / "empty-body.md"
    path.write_text("---\nname: Empty Body\ndescription: nope\nmode: subagent\n---\n\n", encoding="utf-8")

    with pytest.raises(ValueError, match="must not have an empty markdown body"):
        _ = manifest_from_markdown_file(path, scope="project")


def test_user_agent_manifest_dir_uses_windows_appdata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from voidcode.agent import user_agent_manifest_dir

    monkeypatch.setattr(sys, "platform", "win32")

    assert user_agent_manifest_dir(env={"APPDATA": str(tmp_path / "Roaming")}) == (tmp_path / "Roaming" / "voidcode" / "agents")


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
