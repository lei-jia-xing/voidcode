"""Unit tests for the delegation-owned profile registry: builtins, discovery, rendering."""

from __future__ import annotations

from collections.abc import Mapping
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Literal, cast

import pytest

from voidcode.plugins.delegation.delegation_plugin import create_plugin
from voidcode.plugins.delegation.profiles.builtin import (
    DEFAULT_AGENT_MANIFEST,
    EXECUTE_AGENT_MANIFEST,
    EXPLORE_AGENT_MANIFEST,
    PLAN_AGENT_MANIFEST,
    RESEARCH_AGENT_MANIFEST,
    REVIEW_AGENT_MANIFEST,
    get_builtin_agent_manifest,
    list_builtin_agent_manifests,
    validate_builtin_agent_manifests,
)
from voidcode.plugins.delegation.profiles.commands import AgentsCommand, AgentShowCommand
from voidcode.plugins.delegation.profiles.models import AgentManifest, AgentPromptMaterialization
from voidcode.plugins.delegation.profiles.prompts import (
    is_builtin_prompt_profile,
    render_agent_prompt,
    render_builtin_prompt_profile,
)
from voidcode.plugins.delegation.profiles.registry import (
    agent_manifest_id_from_name,
    is_valid_agent_manifest_id,
    load_agent_manifest_registry,
    manifest_from_markdown,
    project_agent_manifest_dir,
    user_agent_manifest_dir,
)
from voidcode.sdk.commands import CommandInvocation
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import PluginServices

_WORKSPACE_DISCOVERY_TOOLS = ("read", "glob", "grep", "ast_grep", "lsp")
_USER_APPEND_HEADING = "User-append: user-authored text is authoritative over generated blocks."

_EXPECTED_TOOL_ALLOWLISTS: dict[str, tuple[str, ...]] = {
    "default": (
        *_WORKSPACE_DISCOVERY_TOOLS,
        "write",
        "edit",
        "background_process",
        "shell_exec",
        "task",
        "task_batch",
        "todo",
        "get_goal",
        "create_goal",
        "update_goal",
        "skill",
        "web_search",
        "web_fetch",
        "mcp",
    ),
    "execute": (
        *_WORKSPACE_DISCOVERY_TOOLS,
        "write",
        "edit",
        "background_process",
        "shell_exec",
        "todo",
        "mcp",
    ),
    "review": _WORKSPACE_DISCOVERY_TOOLS,
    "explore": _WORKSPACE_DISCOVERY_TOOLS,
    "research": ("web_search", "web_fetch"),
    "plan": (
        *_WORKSPACE_DISCOVERY_TOOLS,
        "skill",
        "web_search",
        "web_fetch",
    ),
}

#: Names from the legacy world that no admitted tool answers to.
_DEAD_TOOL_NAMES = ("yield", "question", "invoke_tool", "mcp/*")


def _write_agent_file(directory: Path, filename: str, frontmatter: str, body: str = "Custom agent body.") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(f"---\n{frontmatter}---\n\n{body}\n", encoding="utf-8")
    return path


def _custom_frontmatter(
    *,
    name: str = "Custom Agent",
    description: str = "Custom profile description.",
    mode: str = "subagent",
    extra: str = "",
) -> str:
    return f"name: {name}\ndescription: {description}\nmode: {mode}\n{extra}"


def _load_registry(
    workspace: Path,
    *,
    xdg_config_home: Path,
    installed: tuple[AgentManifest, ...] = (),
):
    return load_agent_manifest_registry(
        workspace,
        env={"XDG_CONFIG_HOME": str(xdg_config_home)},
        installed_manifests=installed,
    )


def test_builtin_manifests_expose_exact_allowlists_and_refs() -> None:
    manifests = list_builtin_agent_manifests()

    assert tuple(manifest.id for manifest in manifests) == ("default", "execute", "review", "explore", "research", "plan")
    for manifest in manifests:
        assert manifest.tool_allowlist == _EXPECTED_TOOL_ALLOWLISTS[manifest.id]
        assert manifest.source_scope == "builtin"
        assert manifest.skill_refs == ()
        assert manifest.fallback_models == ()
        assert manifest.mcp_binding is None
        assert manifest.prompt_materialization is not None
        assert manifest.prompt_materialization.source == "builtin"
        assert manifest.prompt_materialization.format == "text"
        assert manifest.prompt_materialization.version == 3
        assert manifest.prompt_materialization.profile == manifest.prompt_profile

    assert get_builtin_agent_manifest("default") is DEFAULT_AGENT_MANIFEST
    assert get_builtin_agent_manifest("execute") is EXECUTE_AGENT_MANIFEST
    assert get_builtin_agent_manifest("review") is REVIEW_AGENT_MANIFEST
    assert get_builtin_agent_manifest("explore") is EXPLORE_AGENT_MANIFEST
    assert get_builtin_agent_manifest("research") is RESEARCH_AGENT_MANIFEST
    assert get_builtin_agent_manifest("plan") is PLAN_AGENT_MANIFEST
    assert get_builtin_agent_manifest("missing") is None


def test_builtin_allowlists_name_only_admitted_tools() -> None:
    """The legacy handoff, ask-user, nested-invocation and ``mcp/*`` names are gone."""
    for manifest in list_builtin_agent_manifests():
        assert not set(_DEAD_TOOL_NAMES).intersection(manifest.tool_allowlist)
    assert "mcp" in DEFAULT_AGENT_MANIFEST.tool_allowlist
    assert "mcp" in EXECUTE_AGENT_MANIFEST.tool_allowlist


def test_builtin_readonly_stance_lives_on_the_manifest() -> None:
    """Review/explore/research/plan children stay read-only; execute and default do not."""
    assert EXECUTE_AGENT_MANIFEST.readonly is False
    assert DEFAULT_AGENT_MANIFEST.readonly is False
    for manifest in (REVIEW_AGENT_MANIFEST, EXPLORE_AGENT_MANIFEST, RESEARCH_AGENT_MANIFEST, PLAN_AGENT_MANIFEST):
        assert manifest.readonly is True


def test_builtin_manifest_names_descriptions_and_modes() -> None:
    assert (DEFAULT_AGENT_MANIFEST.name, DEFAULT_AGENT_MANIFEST.mode, DEFAULT_AGENT_MANIFEST.top_level_selectable) == (
        "Default",
        "primary",
        True,
    )
    assert DEFAULT_AGENT_MANIFEST.description == "General user-facing assistant; runtime owns policy and tool scope."
    assert (EXECUTE_AGENT_MANIFEST.name, EXECUTE_AGENT_MANIFEST.description) == (
        "Execute",
        "Delegated execution of an assigned task; runtime enforces the active tool scope.",
    )
    assert (REVIEW_AGENT_MANIFEST.name, REVIEW_AGENT_MANIFEST.description) == (
        "Review",
        "Delegated review of evidence, risks, and options.",
    )
    assert (EXPLORE_AGENT_MANIFEST.name, EXPLORE_AGENT_MANIFEST.description) == (
        "Explore",
        "Delegated read-only discovery of relevant workspace evidence.",
    )
    assert (RESEARCH_AGENT_MANIFEST.name, RESEARCH_AGENT_MANIFEST.description) == (
        "Research",
        "Delegated research using public sources and external evidence.",
    )
    assert (PLAN_AGENT_MANIFEST.name, PLAN_AGENT_MANIFEST.description) == (
        "Planning",
        "Delegated read-only planning grounded in the request and relevant evidence.",
    )
    for manifest in (EXECUTE_AGENT_MANIFEST, REVIEW_AGENT_MANIFEST, EXPLORE_AGENT_MANIFEST, RESEARCH_AGENT_MANIFEST, PLAN_AGENT_MANIFEST):
        assert manifest.mode == "subagent"
        assert manifest.top_level_selectable is False


def test_builtin_manifests_validate_and_reject_broken_records() -> None:
    manifests = list_builtin_agent_manifests()
    assert validate_builtin_agent_manifests(manifests) is manifests

    with pytest.raises(ValueError, match="must not be top-level selectable"):
        validate_builtin_agent_manifests(
            (
                AgentManifest(
                    id="broken",
                    name="Broken",
                    mode="subagent",
                    description="d",
                    prompt_profile="plan",
                    top_level_selectable=True,
                ),
            )
        )
    with pytest.raises(ValueError, match="has mode='primary' but is not marked top_level_selectable"):
        validate_builtin_agent_manifests((AgentManifest(id="broken", name="Broken", mode="primary", description="d", prompt_profile="plan"),))
    with pytest.raises(ValueError, match="references unknown prompt profile 'ghost'"):
        validate_builtin_agent_manifests((AgentManifest(id="broken", name="Broken", mode="subagent", description="d", prompt_profile="ghost"),))
    with pytest.raises(ValueError, match="must not contain duplicate tool patterns"):
        validate_builtin_agent_manifests(
            (
                AgentManifest(
                    id="broken",
                    name="Broken",
                    mode="subagent",
                    description="d",
                    prompt_profile="plan",
                    tool_allowlist=("read", "read"),
                ),
            )
        )


def test_manifest_payload_round_trip() -> None:
    for manifest in list_builtin_agent_manifests():
        assert AgentManifest.from_payload(manifest.to_payload()) == manifest

    payload = DEFAULT_AGENT_MANIFEST.prompt_materialization
    assert payload is not None
    assert AgentPromptMaterialization.from_payload(payload.to_payload()) == payload
    assert AgentPromptMaterialization.from_payload(payload.to_payload(profile="custom")) == AgentPromptMaterialization(profile="custom", version=3)
    with pytest.raises(ValueError, match="invalid current prompt materialization fields"):
        AgentPromptMaterialization.from_payload({**payload.to_payload(), "unknown": 1})


def test_user_and_project_discovery_precedence(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    xdg_config_home = tmp_path / "config"
    user_dir = user_agent_manifest_dir({"XDG_CONFIG_HOME": str(xdg_config_home)})
    assert user_dir == xdg_config_home / "voidcode" / "agents"
    assert project_agent_manifest_dir(workspace) == workspace.resolve() / ".voidcode" / "agents"

    _write_agent_file(user_dir, "shared.md", _custom_frontmatter(name="Shared", description="user copy"))
    _write_agent_file(user_dir, "user-only.md", _custom_frontmatter(name="User Only"))
    _write_agent_file(
        project_agent_manifest_dir(workspace),
        "shared.md",
        _custom_frontmatter(name="Shared", description="project copy", mode="primary"),
    )

    registry = _load_registry(workspace, xdg_config_home=xdg_config_home)

    shared = registry.get("shared")
    assert shared is not None
    assert shared.description == "project copy"
    assert shared.source_scope == "project"
    assert shared.mode == "primary"
    assert shared.top_level_selectable is True
    user_only = registry.get("user-only")
    assert user_only is not None
    assert user_only.source_scope == "user"
    assert user_only.mode == "subagent"
    assert user_only.top_level_selectable is False
    assert [manifest.id for manifest in registry.list_manifests()][:6] == ["default", "execute", "review", "explore", "research", "plan"]
    assert registry.executable_primary_ids() == frozenset({"default", "shared"})
    assert registry.executable_subagent_ids() == frozenset({"execute", "review", "explore", "research", "plan", "user-only"})


def test_duplicate_id_in_same_scope_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    xdg_config_home = tmp_path / "config"
    user_dir = user_agent_manifest_dir({"XDG_CONFIG_HOME": str(xdg_config_home)})
    _write_agent_file(user_dir, "a.md", _custom_frontmatter(name="Dupe"))
    _write_agent_file(user_dir, "b.md", _custom_frontmatter(name="Dupe"))

    with pytest.raises(ValueError, match="duplicate custom agent manifest id 'dupe'"):
        _load_registry(workspace, xdg_config_home=xdg_config_home)


def test_builtin_id_cannot_be_replaced(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    xdg_config_home = tmp_path / "config"
    _write_agent_file(
        project_agent_manifest_dir(workspace),
        "default.md",
        _custom_frontmatter(name="Default Impostor", extra="id: default\n"),
    )

    with pytest.raises(ValueError, match="uses builtin id 'default'; builtin agent manifests cannot be replaced"):
        _load_registry(workspace, xdg_config_home=xdg_config_home)


def test_frontmatter_whitelist_and_required_fields(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    xdg_config_home = tmp_path / "config"
    user_dir = user_agent_manifest_dir({"XDG_CONFIG_HOME": str(xdg_config_home)})

    _write_agent_file(user_dir, "unknown-key.md", _custom_frontmatter(extra="tools: [read]\n"))
    with pytest.raises(ValueError, match="unsupported frontmatter field 'tools'; supported fields are: "):
        _load_registry(workspace, xdg_config_home=xdg_config_home)

    (user_dir / "unknown-key.md").unlink()
    _write_agent_file(user_dir, "missing-mode.md", "name: Missing Mode\ndescription: no mode here\n")
    with pytest.raises(ValueError, match="missing required frontmatter field") as missing:
        _load_registry(workspace, xdg_config_home=xdg_config_home)
    assert "mode" in str(missing.value)

    (user_dir / "missing-mode.md").unlink()
    _write_agent_file(user_dir, "bad-mode.md", _custom_frontmatter(mode="all"))
    with pytest.raises(ValueError, match="frontmatter field 'mode' must be 'primary' or 'subagent'"):
        _load_registry(workspace, xdg_config_home=xdg_config_home)

    (user_dir / "bad-mode.md").unlink()
    _write_agent_file(user_dir, "numeric-name.md", "name: 7\ndescription: numeric name\nmode: subagent\n")
    with pytest.raises(ValueError, match="frontmatter field 'name' must be a non-empty string"):
        _load_registry(workspace, xdg_config_home=xdg_config_home)


def test_frontmatter_framing_and_value_rejections(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    xdg_config_home = tmp_path / "config"
    user_dir = user_agent_manifest_dir({"XDG_CONFIG_HOME": str(xdg_config_home)})
    user_dir.mkdir(parents=True)

    (user_dir / "no-block.md").write_text(f"{_custom_frontmatter()}\nbody\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must start with a YAML frontmatter delimiter"):
        _load_registry(workspace, xdg_config_home=xdg_config_home)

    (user_dir / "no-block.md").write_text(f"---\n{_custom_frontmatter()}---\n\n   \n", encoding="utf-8")
    with pytest.raises(ValueError, match="must not have an empty markdown body"):
        _load_registry(workspace, xdg_config_home=xdg_config_home)

    (user_dir / "no-block.md").write_text("---\nname: Dupe\nname: Dupe\ndescription: d\nmode: subagent\n---\n\nbody\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate frontmatter key 'name'"):
        _load_registry(workspace, xdg_config_home=xdg_config_home)

    (user_dir / "no-block.md").write_text(f"---\n{_custom_frontmatter(extra='preset_hook_refs: [ghost_guard]\n')}---\n\nbody\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported frontmatter field 'preset_hook_refs'"):
        _load_registry(workspace, xdg_config_home=xdg_config_home)


def test_installed_package_scope_requires_source_id(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    xdg_config_home = tmp_path / "config"
    packaging = AgentManifest(
        id="packaged",
        name="Packaged",
        mode="subagent",
        description="Installed package profile.",
        source_scope="package",
        source_id="voidcode-pack",
    )

    registry = _load_registry(workspace, xdg_config_home=xdg_config_home, installed=(packaging,))
    assert registry.get("packaged") is packaging
    assert registry.executable_subagent_ids() == frozenset({"execute", "review", "explore", "research", "plan", "packaged"})

    with pytest.raises(ValueError, match="installed agent manifests require actual package origin/source_id"):
        _load_registry(
            workspace,
            xdg_config_home=xdg_config_home,
            installed=(AgentManifest(id="loose", name="Loose", mode="subagent", description="d"),),
        )
    with pytest.raises(ValueError, match="invalid installed agent manifest id: Bad Id"):
        _load_registry(
            workspace,
            xdg_config_home=xdg_config_home,
            installed=(AgentManifest(id="Bad Id", name="Bad", mode="subagent", description="d", source_scope="package", source_id="pkg"),),
        )
    with pytest.raises(ValueError, match="package prompt materialization requires source_id"):
        AgentPromptMaterialization(profile="packaged", source_scope="package")


def test_manifest_id_derivation_and_validation() -> None:
    assert agent_manifest_id_from_name("  My Fancy_Agent!! ") == "my-fancy-agent"
    assert agent_manifest_id_from_name("A  B") == "a-b"
    with pytest.raises(ValueError, match="must contain at least one alphanumeric character"):
        agent_manifest_id_from_name("---")
    assert is_valid_agent_manifest_id("plan2") is True
    assert is_valid_agent_manifest_id("2plan") is False
    assert is_valid_agent_manifest_id("Plan") is False

    manifest = manifest_from_markdown(
        "---\nname: My Fancy_Agent\ndescription: derived id\nmode: primary\n---\n\nBody text.\n",
        scope="project",
        source_path="/tmp/my.md",
    )
    assert manifest.id == "my-fancy-agent"
    assert manifest.prompt_profile == "my-fancy-agent"
    assert manifest.source_path == "/tmp/my.md"
    assert manifest.top_level_selectable is True


def test_mode_maps_to_top_level_selectable() -> None:
    primary = manifest_from_markdown(
        "---\nname: Primary One\ndescription: selectable\nmode: primary\n---\n\nBody text.\n",
        scope="user",
    )
    subagent = manifest_from_markdown(
        "---\nname: Sub One\ndescription: not selectable\nmode: subagent\n---\n\nBody text.\n",
        scope="user",
    )

    assert (primary.mode, primary.top_level_selectable) == ("primary", True)
    assert (subagent.mode, subagent.top_level_selectable) == ("subagent", False)


def test_builtin_prompt_renders_exact_resource_text() -> None:
    assert is_builtin_prompt_profile("default") is True
    assert is_builtin_prompt_profile("ghost") is False
    assert render_builtin_prompt_profile("ghost") is None

    profiles_dir = resource_files("voidcode.plugins.delegation.profiles").joinpath("texts")
    for manifest in list_builtin_agent_manifests():
        profile = cast(str, manifest.prompt_profile)
        expected = profiles_dir.joinpath(f"{profile}.txt").read_text(encoding="utf-8").strip()
        assert expected
        assert render_builtin_prompt_profile(profile) == expected
        assert render_agent_prompt(manifest.prompt_materialization) == expected

    assert render_agent_prompt(None) is None
    with pytest.raises(ValueError, match="unknown builtin agent prompt profile: ghost"):
        render_agent_prompt(AgentPromptMaterialization(profile="ghost"))


def test_custom_markdown_prompt_rendering_with_append(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    xdg_config_home = tmp_path / "config"
    user_dir = user_agent_manifest_dir({"XDG_CONFIG_HOME": str(xdg_config_home)})
    body = "You are the custom body.\n\nKeep it short."
    _write_agent_file(
        user_dir,
        "custom.md",
        _custom_frontmatter(name="Custom", extra="prompt_append: Prefer terse answers.\n"),
        body=body,
    )

    registry = _load_registry(workspace, xdg_config_home=xdg_config_home)
    manifest = registry.get("custom")
    assert manifest is not None
    materialization = manifest.prompt_materialization
    assert materialization is not None
    assert materialization.source == "custom_markdown"
    assert materialization.format == "markdown"
    assert materialization.profile == "custom"
    assert materialization.version == 1
    assert materialization.body == body
    assert materialization.prompt_append == "Prefer terse answers."
    assert materialization.source_scope == "user"
    assert materialization.source_path == str(user_dir / "custom.md")
    assert render_agent_prompt(materialization) == f"{body}\n\n{_USER_APPEND_HEADING}\nPrefer terse answers."

    (user_dir / "custom.md").unlink()
    _write_agent_file(user_dir, "plain.md", _custom_frontmatter(name="Plain"), body=body)
    plain = _load_registry(workspace, xdg_config_home=xdg_config_home).get("plain")
    assert plain is not None
    assert render_agent_prompt(plain.prompt_materialization) == body


# --- Plugin command surface ---


def _command_context(workspace: Path) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        session_id="session-1",
        run_id="run-1",
        invocation_id="inv-1",
        revision_id="rev-1",
        actor="user",
        logical_turn="turn-1",
    )


class _NoServices(PluginServices):
    def get_state(self, scope: Literal["session", "workspace"] = "session"):
        raise AssertionError("agents reads no plugin state")

    def get_state_schema_version(self, scope: Literal["session", "workspace"] = "session") -> int:
        raise AssertionError("agents reads no plugin state")

    def replace_state(
        self,
        state: Mapping[str, object],
        expected_version: int,
        scope: Literal["session", "workspace"] = "session",
    ) -> int:
        raise AssertionError("agents writes no plugin state")

    def stage_event(self, event_type: str, payload: Mapping[str, object]) -> None:
        raise AssertionError("agents stages no events")

    def enqueue_continuation(self, payload: Mapping[str, object], *, idempotency_key: str | None = None) -> str:
        raise AssertionError("agents enqueues no continuations")

    def cancel_continuations(self, *, continuation_id: str | None = None) -> None:
        raise AssertionError("agents cancels no continuations")


def test_agents_command_lists_builtin_and_project_profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    project_dir = workspace / ".voidcode" / "agents"
    _write_agent_file(
        project_dir, "custom.md", _custom_frontmatter(name="Custom", description="Local helper", mode="subagent"), body="Do local work."
    )

    result = AgentsCommand().invoke(
        CommandInvocation(context=_command_context(workspace), name="agents", arguments={}),
        _NoServices(),
    )
    assert result.status == "ok"
    assert result.output is not None
    assert "[subagent] review —" in result.output
    assert "[primary] default —" in result.output
    assert "[subagent] custom — Local helper (project, tools: 0)" in result.output
    rows = {row["id"]: row for row in result.data["profiles"]}  # type: ignore[index]
    assert rows["custom"]["source_scope"] == "project"
    assert rows["execute"]["mode"] == "subagent"


def test_agents_show_renders_builtin_prompt_and_rejects_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    review_prompt_resource = Path(__file__).resolve().parents[3] / "src" / "voidcode" / "plugins" / "delegation" / "profiles" / "texts" / "review.txt"
    expected_prompt = review_prompt_resource.read_text(encoding="utf-8").strip()
    command = AgentShowCommand()

    shown = command.invoke(
        CommandInvocation(context=_command_context(workspace), name="agents.show", arguments={"id": "review"}),
        _NoServices(),
    )
    assert shown.status == "ok"
    assert shown.output == expected_prompt
    assert shown.data is not None
    assert shown.data["tool_allowlist"] == ("read", "glob", "grep", "ast_grep", "lsp")
    assert "write" not in shown.data["tool_allowlist"]
    assert "yield" not in shown.data["tool_allowlist"]

    unknown = command.invoke(
        CommandInvocation(context=_command_context(workspace), name="agents.show", arguments={"id": "nope"}),
        _NoServices(),
    )
    assert unknown.status == "error"
    assert unknown.error == "unknown agent profile: nope"

    missing = command.invoke(
        CommandInvocation(context=_command_context(workspace), name="agents.show", arguments={}),
        _NoServices(),
    )
    assert missing.status == "error"
    assert missing.error == "agents.show requires a profile id"


def test_create_plugin_registers_commands_and_tools() -> None:
    plugin = create_plugin({})
    assert [command.definition.name for command in plugin.commands] == ["agents", "agents.show"]
    assert [tool.definition.name for tool in plugin.tools] == ["task", "task_batch"]
    assert plugin.context is None
