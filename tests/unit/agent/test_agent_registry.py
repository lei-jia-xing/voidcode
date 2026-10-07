from __future__ import annotations

import subprocess
import zipfile
from importlib import metadata
from pathlib import Path

import pytest

from voidcode.agent import (
    AgentPromptMaterialization,
    load_agent_manifest_registry,
    manifest_from_markdown_file,
    render_agent_prompt,
)


def _write_agent(path: Path, frontmatter: str, body: str = "Custom prompt body.") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")


def test_manifest_from_markdown_file_parses_frontmatter_and_body(tmp_path: Path) -> None:
    path = tmp_path / "reviewer.md"
    _write_agent(
        path,
        "\n".join(
            (
                "name: Review Helper",
                "description: Focused reviewer",
                "mode: subagent",
                "model: opencode-zen/test-model",
                "fallback_models: [opencode-zen/fallback]",
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
    assert manifest.model_preference == "opencode-zen/test-model"
    assert manifest.fallback_models == ("opencode-zen/fallback",)
    assert manifest.tool_allowlist == ("read", "grep")
    assert manifest.skill_refs == ("code-review",)
    assert manifest.preset_hook_refs == ("role_reminder",)
    assert manifest.prompt_materialization is not None
    assert manifest.prompt_materialization.source == "custom_markdown"
    assert manifest.prompt_materialization.body == "Stay read-only and summarize risks."


def test_manifest_from_markdown_file_rejects_missing_required_fields(tmp_path: Path) -> None:
    path = tmp_path / "bad.md"
    _write_agent(path, "name: Missing Mode\ndescription: nope")

    with pytest.raises(ValueError, match="bad.md.*missing required.*mode"):
        _ = manifest_from_markdown_file(path, scope="project")


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


def test_registry_rejects_custom_builtin_id(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_agent(
        workspace / ".voidcode" / "agents" / "leader.md",
        "id: leader\nname: Fake Leader\ndescription: nope\nmode: primary",
    )

    with pytest.raises(ValueError, match="builtin id 'leader'.*cannot be replaced"):
        _ = load_agent_manifest_registry(workspace, env={})


def test_actual_installed_markdown_layer_and_reserved_collisions(tmp_path: Path) -> None:
    package_name = "p5-agent-consumer-fixture"
    dist_info = "p5_agent_consumer_fixture-1.0.0.dist-info"
    body = "---\nid: helper\nname: Package Helper\ndescription: Package persona\nmode: subagent\nprompt_append: package append\n---\npackage body\n"
    contents = {
        "agent_consumer_fixture/__init__.py": "",
        "agent_consumer_fixture/agent.md": body,
        "agent_consumer_fixture/collision.md": body.replace("id: helper", "id: leader"),
        f"{dist_info}/METADATA": f"Metadata-Version: 2.1\nName: {package_name}\nVersion: 1.0.0\n",
        f"{dist_info}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    wheel = tmp_path / "p5_agent_consumer_fixture-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in contents.items():
            archive.writestr(name, content)
        archive.writestr(f"{dist_info}/RECORD", "".join(f"{name},,\n" for name in contents) + f"{dist_info}/RECORD,,\n")
    site = tmp_path / "installed"
    subprocess.run(["uv", "pip", "install", "--no-deps", "--target", str(site), str(wheel)], check=True, capture_output=True)
    distribution = next(item for item in metadata.distributions(path=[str(site)]) if item.metadata["Name"] == package_name)
    installed = manifest_from_markdown_file(
        site / "agent_consumer_fixture" / "agent.md",
        scope="package",
        source_id=f"{distribution.metadata['Name']}/agent/helper",
    )
    workspace = tmp_path / "workspace"
    config_home = tmp_path / "config"
    env = {"XDG_CONFIG_HOME": str(config_home)}
    registry = load_agent_manifest_registry(workspace, env=env, installed_manifests=(installed,))
    selected = registry.get("helper")
    assert selected is not None and selected.prompt_materialization is not None
    rendered = render_agent_prompt(selected.prompt_materialization)
    assert rendered is not None and rendered.index("package body") < rendered.index("package append")
    assert selected.source_scope == "package"
    assert selected.source_id == f"{distribution.metadata['Name']}/agent/helper"
    assert selected.prompt_materialization.source_id == selected.source_id
    _write_agent(config_home / "voidcode" / "agents" / "helper.md", "id: helper\nname: User Helper\ndescription: user\nmode: subagent", "user body")
    user = load_agent_manifest_registry(workspace, env=env, installed_manifests=(installed,)).get("helper")
    assert user is not None and user.source_scope == "user" and user.source_id is None
    _write_agent(
        workspace / ".voidcode" / "agents" / "helper.md",
        "id: helper\nname: Workspace Helper\ndescription: project\nmode: subagent",
        "workspace body",
    )
    project = load_agent_manifest_registry(workspace, env=env, installed_manifests=(installed,)).get("helper")
    assert project is not None and project.prompt_materialization is not None
    assert render_agent_prompt(project.prompt_materialization) == "workspace body"
    with pytest.raises(ValueError):
        load_agent_manifest_registry(workspace, env=env, installed_manifests=(installed, installed))
    collision = manifest_from_markdown_file(
        site / "agent_consumer_fixture" / "collision.md",
        scope="package",
        source_id=f"{distribution.metadata['Name']}/agent/leader",
    )
    with pytest.raises(ValueError):
        load_agent_manifest_registry(workspace, env=env, installed_manifests=(collision,))


@pytest.mark.parametrize("version", [True, 1.0, 0, "", None])
def test_prompt_payload_refuses_noninteger_or_missing_version(version: object) -> None:
    with pytest.raises(ValueError):
        AgentPromptMaterialization.from_payload({"profile": "leader", "source": "builtin", "format": "text", "version": version})
    with pytest.raises(ValueError):
        AgentPromptMaterialization.from_payload({"profile": "leader", "source": "builtin", "format": "text"})
