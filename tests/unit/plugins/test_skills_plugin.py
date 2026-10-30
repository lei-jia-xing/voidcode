"""Unit tests for the Skills plugin: discovery, registry rules, catalog text, reader and commands."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.fixtures.fake_services import FakeServices
from voidcode.plugins.skills import create_plugin
from voidcode.plugins.skills.skills_plugin import (
    DEFAULT_SKILL_SEARCH_PATHS,
    SkillEnableRule,
    SkillLoadError,
    SkillManifestParseError,
    SkillReader,
    SkillRegistry,
    SkillsCommand,
    SkillsConfig,
    SkillsContextProvider,
    build_registry,
    catalog_skill_context,
    loaded_skill_names,
    parse_skill_manifest,
    parse_skills_config,
)
from voidcode.sdk.commands import CommandInvocation
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.resources import ResourcePage, ResourceReadCaller, ResourceReadRequest
from voidcode.sdk.tools import TextOutput


def _context(workspace: Path) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        session_id="session-1",
        run_id="run-1",
        invocation_id="inv-1",
        revision_id="rev-1",
        actor="model",
        logical_turn="turn-1",
    )


def _write_skill(workspace: Path, *, name: str, description: str, body: str, folder: str | None = None) -> Path:
    root = workspace / ".voidcode" / "skills" / (folder or name)
    root.mkdir(parents=True, exist_ok=True)
    entry = root / "SKILL.md"
    entry.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n", encoding="utf-8")
    return entry


# --- Manifest parsing and discovery ---


def test_parse_skill_manifest_returns_body_and_paths(tmp_path: Path) -> None:
    entry = _write_skill(tmp_path, name="release", description="Cut a release", body="# Steps\n\n1. bump")
    skill = parse_skill_manifest(entry.read_text(encoding="utf-8"), entry_path=entry)
    assert skill.name == "release"
    assert skill.description == "Cut a release"
    assert skill.content == "# Steps\n\n1. bump"
    assert skill.directory == entry.parent
    assert skill.enabled is True


@pytest.mark.parametrize(
    "contents",
    [
        "# no frontmatter\n",
        "---\nname: only\n---\nbody\n",
        "---\nname: x\ndescription: y\nextra: z\n---\nbody\n",
        "---\nname: x\ndescription: y\n---\n\n",
        '---\nname: ""\ndescription: y\n---\nbody\n',
    ],
)
def test_parse_skill_manifest_rejects_invalid_files(tmp_path: Path, contents: str) -> None:
    entry = tmp_path / "SKILL.md"
    with pytest.raises(SkillManifestParseError):
        parse_skill_manifest(contents, entry_path=entry)


def test_discovery_is_recursive_sorted_and_rejects_duplicates(tmp_path: Path) -> None:
    _write_skill(tmp_path, name="alpha", description="a", body="A")
    _write_skill(tmp_path, name="beta", description="b", body="B", folder="nested/deeper")
    registry = SkillRegistry.discover(workspace=tmp_path)
    assert loaded_skill_names(registry) == ("alpha", "beta")

    _write_skill(tmp_path, name="alpha", description="dup", body="A2", folder="other")
    with pytest.raises(ValueError, match="duplicate skill name 'alpha'"):
        SkillRegistry.discover(workspace=tmp_path)


def test_missing_search_root_and_escaping_path(tmp_path: Path) -> None:
    assert SkillRegistry.discover(workspace=tmp_path).all() == ()
    with pytest.raises(ValueError, match="escapes workspace"):
        SkillRegistry.discover(workspace=tmp_path, search_paths=("../outside",))


def test_unreadable_skill_file_fails_as_load_error(tmp_path: Path) -> None:
    root = tmp_path / ".voidcode" / "skills" / "boom"
    root.mkdir(parents=True)
    entry = root / "SKILL.md"
    entry.write_text("---\nname: boom\ndescription: d\n---\nbody\n", encoding="utf-8")
    entry.chmod(0o000)
    try:
        with pytest.raises(SkillLoadError):
            SkillRegistry.discover(workspace=tmp_path)
    finally:
        entry.chmod(0o600)


# --- Registry rules ---


def test_enable_rules_by_name_and_path_and_resolution_errors(tmp_path: Path) -> None:
    entry = _write_skill(tmp_path, name="alpha", description="a", body="A")
    _write_skill(tmp_path, name="beta", description="b", body="B")
    registry = SkillRegistry.discover(workspace=tmp_path)
    registry.apply_config_rules((SkillEnableRule(name="alpha", enabled=False),))
    assert registry.is_enabled("alpha") is False
    assert [skill.name for skill in registry.enabled_skills()] == ["beta"]

    by_path = SkillRegistry.discover(workspace=tmp_path)
    by_path.apply_config_rules((SkillEnableRule(path=str(entry.parent), enabled=False),))
    assert by_path.is_enabled("alpha") is False
    assert by_path.is_enabled("beta") is True

    with pytest.raises(ValueError, match="unknown skill: gamma"):
        by_path.resolve("gamma")
    with pytest.raises(ValueError, match="skill 'alpha' is disabled"):
        by_path.resolve("alpha")
    assert by_path.resolve("alpha", allow_disabled=True).name == "alpha"


# --- Prompt context texts ---


def test_catalog_text_lists_metadata_only_with_file_uri(tmp_path: Path) -> None:
    entry = _write_skill(tmp_path, name="alpha", description="a", body="A")
    registry = SkillRegistry.discover(workspace=tmp_path)
    catalog = catalog_skill_context(registry)
    assert catalog.startswith(
        "Runtime skills catalog (recommended/visible).\n"
        "Each entry is metadata only; the full SKILL.md body is NOT in your\n"
        "current context.\n"
        "Load a skill body ONLY when its description matches the active task.\n"
        'Use the read tool: read(path="skill://..."). Do NOT speculatively load every\n'
        "skill; lazy loading keeps the harness lightweight and the model focused.\n"
        "\n"
        "<available_skills>"
    )
    assert "    <name>alpha</name>" in catalog
    assert "    <description>a</description>" in catalog
    assert f"    <location>{entry.resolve().as_uri()}</location>" in catalog
    assert catalog.endswith("</available_skills>")
    assert "A" not in catalog.split("<available_skills>")[0]

    empty = SkillRegistry.from_skills(())
    assert catalog_skill_context(empty) == ""

    registry.apply_config_rules((SkillEnableRule(name="alpha", enabled=False),))
    disabled = catalog_skill_context(registry)
    assert disabled == ""


# --- Plugin configuration ---


def test_skills_config_defaults_and_validation() -> None:
    assert parse_skills_config(None) == SkillsConfig(enabled=False, paths=DEFAULT_SKILL_SEARCH_PATHS, rules=())
    parsed = parse_skills_config(
        {
            "enabled": True,
            "paths": [".voidcode/skills", "skills"],
            "config": [{"name": "alpha", "enabled": False}, {"path": ".voidcode/skills/beta", "enabled": True}],
        }
    )
    assert parsed.enabled is True
    assert parsed.paths == (".voidcode/skills", "skills")
    assert parsed.rules == (
        SkillEnableRule(name="alpha", path=None, enabled=False),
        SkillEnableRule(name=None, path=".voidcode/skills/beta", enabled=True),
    )

    with pytest.raises(ValueError, match="field 'unknown' is not supported"):
        parse_skills_config({"unknown": 1})
    with pytest.raises(ValueError, match="field 'paths' must be an array"):
        parse_skills_config({"paths": "skills"})
    with pytest.raises(ValueError, match="field 'enabled' must be a boolean"):
        parse_skills_config({"enabled": "yes"})
    with pytest.raises(ValueError, match="rule field 'extra' is not supported"):
        parse_skills_config({"config": [{"extra": True}]})


def test_build_registry_requires_explicit_enablement(tmp_path: Path) -> None:
    _write_skill(tmp_path, name="alpha", description="a", body="A")
    assert build_registry(workspace=tmp_path, config=SkillsConfig()).all() == ()
    enabled = build_registry(workspace=tmp_path, config=SkillsConfig(enabled=True))
    assert loaded_skill_names(enabled) == ("alpha",)


# --- Reader ---


def _caller(workspace: Path) -> ResourceReadCaller:
    return ResourceReadCaller(session_id="session-1", workspace=workspace, revision_id="rev-1", run_id="run-1")


def _read(
    config: SkillsConfig,
    workspace: Path,
    uri: str,
    offset: int = 1,
    limit: int | None = None,
    byte_offset: int | None = None,
    byte_limit: int | None = None,
) -> ResourcePage | None:
    return SkillReader(config).read(
        ResourceReadRequest(uri=uri, offset=offset, limit=limit, byte_offset=byte_offset, byte_limit=byte_limit),
        _caller(workspace),
        FakeServices(),  # type: ignore[arg-type]
    )


def test_skill_reader_returns_body_and_metadata(tmp_path: Path) -> None:
    entry = _write_skill(tmp_path, name="alpha", description="a", body="# Body\n\n    indented")
    page = _read(SkillsConfig(enabled=True), tmp_path, "skill://alpha")
    assert isinstance(page, ResourcePage)
    rendered = f"## Skill: alpha\n**Description**: a\nsourcePath: {entry}\nbaseDirectory: {entry.parent}\n\n# Body\n\n    indented"
    assert isinstance(page.output, TextOutput)
    assert page.output.text == f"Read 8 line(s) from skill://alpha.\n\n{rendered}"
    assert page.body["name"] == "alpha"
    assert page.body["description"] == "a"
    assert page.body["sourcePath"] == str(entry)
    assert page.body["baseDirectory"] == str(entry.parent)
    assert page.body["raw_content"] == rendered
    assert page.body["next_offset"] is None
    assert page.body["truncated"] is False


def test_skill_reader_pages_long_renders_by_line(tmp_path: Path) -> None:
    body = "\n".join(f"line {index}" for index in range(30))
    _write_skill(tmp_path, name="alpha", description="a", body=body)
    config = SkillsConfig(enabled=True)
    first = _read(config, tmp_path, "skill://alpha", limit=10)
    assert isinstance(first, ResourcePage)
    assert first.body["next_offset"] == 11
    assert first.body["truncated"] is True
    second = _read(config, tmp_path, "skill://alpha", offset=11, limit=100)
    assert isinstance(second, ResourcePage)
    assert second.body["next_offset"] is None
    assert "line 29" in str(second.body["raw_content"])
    with pytest.raises(ValueError, match="Offset 36 is out of range for this skill"):
        _read(config, tmp_path, "skill://alpha", offset=36)


def test_skill_reader_refuses_byte_args_and_empty_names(tmp_path: Path) -> None:
    _write_skill(tmp_path, name="alpha", description="a", body="A")
    config = SkillsConfig(enabled=True)
    with pytest.raises(ValueError, match="byte_offset and byte_limit are not supported"):
        _read(config, tmp_path, "skill://alpha", byte_offset=0)
    with pytest.raises(ValueError, match="requires a skill name"):
        _read(config, tmp_path, "skill://")
    with pytest.raises(ValueError, match="requires a skill name"):
        _read(config, tmp_path, "skill://   ")


def test_skill_reader_unknown_is_absence_while_disabled_fails(tmp_path: Path) -> None:
    _write_skill(tmp_path, name="alpha", description="a", body="A")
    enabled = SkillsConfig(enabled=True)
    assert _read(enabled, tmp_path, "skill://missing") is None
    assert _read(SkillsConfig(), tmp_path, "skill://alpha") is None
    by_name = SkillsConfig(enabled=True, rules=(SkillEnableRule(name="alpha", enabled=False),))
    with pytest.raises(ValueError, match="skill 'alpha' is disabled"):
        _read(by_name, tmp_path, "skill://alpha")


def test_skill_reader_propagates_malformed_skills(tmp_path: Path) -> None:
    root = tmp_path / ".voidcode" / "skills" / "boom"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("---\nname: boom\n---\nbody\n", encoding="utf-8")
    with pytest.raises(SkillLoadError):
        _read(SkillsConfig(enabled=True), tmp_path, "skill://boom")


# --- Command and context ---


def test_skills_command_lists_enabled_state_and_reports_absence(tmp_path: Path) -> None:
    _write_skill(tmp_path, name="alpha", description="a", body="A")
    _write_skill(tmp_path, name="beta", description="b", body="B")
    config = SkillsConfig(enabled=True, rules=(SkillEnableRule(name="beta", enabled=False),))
    invocation = CommandInvocation(context=_context(tmp_path), name="skills", arguments={})
    res = SkillsCommand(config).invoke(invocation, FakeServices())
    assert res.status == "ok"
    assert res.output == (
        f"- [enabled] alpha — a ({tmp_path / '.voidcode/skills/alpha/SKILL.md'})\n"
        f"- [disabled] beta — b ({tmp_path / '.voidcode/skills/beta/SKILL.md'})"
    )
    assert res.data is not None
    assert [row["name"] for row in res.data["skills"]] == ["alpha", "beta"]

    disabled = SkillsCommand(SkillsConfig()).invoke(invocation, FakeServices())
    assert disabled.output == "Skills are disabled."
    assert disabled.data == {"enabled": False, "skills": ()}

    empty_workspace = tmp_path / "empty"
    empty_workspace.mkdir()
    empty_invocation = CommandInvocation(context=_context(empty_workspace), name="skills", arguments={})
    empty = SkillsCommand(SkillsConfig(enabled=True)).invoke(empty_invocation, FakeServices())
    assert empty.output == "No skills discovered."


def test_context_provider_emits_catalog_only_when_enabled_and_present(tmp_path: Path) -> None:
    plugin = create_plugin({"enabled": True})
    assert plugin.context is not None
    assert plugin.context(_context(tmp_path), FakeServices()) is None

    _write_skill(tmp_path, name="alpha", description="a", body="A")
    contribution = plugin.context(_context(tmp_path), FakeServices())
    assert contribution is not None
    assert "<name>alpha</name>" in contribution.content

    disabled_plugin = create_plugin(None)
    assert disabled_plugin.context is not None
    assert disabled_plugin.context(_context(tmp_path), FakeServices()) is None


def test_create_plugin_registers_declared_reader_and_command() -> None:
    plugin = create_plugin({"enabled": True})
    assert plugin.tools == ()
    assert [reader.scheme for reader in plugin.resource_readers] == ["skill"]
    assert [command.definition.name for command in plugin.commands] == ["skills", "skills.show"]


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 9, "cursor_sequence": 42, "touched_paths": []},
        {"schema_version": True, "cursor_sequence": 42, "touched_paths": []},
        {"schema_version": 1, "touched_paths": []},
        {"schema_version": 1, "cursor_sequence": 42},
        {"schema_version": 1, "cursor_sequence": -1, "touched_paths": []},
    ],
)
def test_invalid_rule_state_is_not_silently_reset(tmp_path: Path, payload: Mapping[str, object]) -> None:
    class InvalidStateServices(FakeServices):
        def get_state(self, scope="session"):
            return payload, 3

    with pytest.raises(ValueError):
        SkillsContextProvider(SkillsConfig())(_context(tmp_path), InvalidStateServices())


def test_catalog_escapes_skill_metadata_in_xml(tmp_path: Path) -> None:
    from xml.etree import ElementTree

    description = "</description><skill>unexpected & extra"
    _write_skill(tmp_path, name="alpha", description=repr(description), body="body")
    catalog = catalog_skill_context(SkillRegistry.discover(workspace=tmp_path))
    document = ElementTree.fromstring(catalog[catalog.index("<available_skills>") :])
    assert len(document.findall("skill")) == 1
    assert document.findtext("skill/description") == description


def test_skill_browser_selects_disabled_metadata_and_pages_unicode_without_loss(tmp_path: Path) -> None:
    import json

    from voidcode.plugins.skills.skills_plugin import SkillShowCommand
    from voidcode.sdk.views import ChoicesViewData, MarkdownViewData, choices_dispatch

    body = "技能🙂" * 4000
    _write_skill(tmp_path, name="long", description="Long guide", body=body)
    config = SkillsConfig(enabled=True, rules=(SkillEnableRule(name="long", enabled=False),))
    context = _context(tmp_path)
    listed = SkillsCommand(config).invoke(CommandInvocation(context, "skills"), FakeServices())
    action, arguments = choices_dispatch(ChoicesViewData.from_payload(listed.presentation.data), "long")
    assert action == "skills.show"
    result = SkillShowCommand(config).invoke(CommandInvocation(context, action, {**arguments, "limit": 500}), FakeServices())
    assert result.status == "ok" and result.data["enabled"] is False
    detail = MarkdownViewData.from_payload(result.presentation.data)
    assert "Status: disabled" in detail.content and "SKILL.md" in detail.content
    next_page = next(action for action in detail.actions if action.label == "Next page")
    assert next_page.arguments["offset"] == 500
    second = SkillShowCommand(config).invoke(CommandInvocation(context, next_page.action, next_page.arguments), FakeServices())
    assert second.data["offset"] == 500 and second.data["next_offset"] == 1000
    assert "Previous page" in [action.label for action in MarkdownViewData.from_payload(second.presentation.data).actions]
    assert len(json.dumps(result.as_payload(), ensure_ascii=False).encode()) < 64 * 1024


def test_skill_browser_reports_empty_disabled_and_bad_offsets(tmp_path: Path) -> None:
    from voidcode.plugins.skills.skills_plugin import SkillShowCommand

    context = _context(tmp_path)
    assert SkillsCommand(SkillsConfig()).invoke(CommandInvocation(context, "skills"), FakeServices()).presentation.view_type == "notice"
    command = SkillShowCommand(SkillsConfig(enabled=True))
    assert command.prepare_input(CommandInvocation(context, "skills.show"), FakeServices()).view_type == "notice"
    _write_skill(tmp_path, name="tiny", description="Tiny", body="hello")
    result = command.invoke(CommandInvocation(context, "skills.show", {"name": "tiny", "offset": 999}), FakeServices())
    assert result.status == "error" and "beyond" in result.error
    missing = command.invoke(CommandInvocation(context, "skills.show", {"name": "missing"}), FakeServices())
    assert missing.status == "error" and "Unknown skill" in missing.error


def test_skill_browser_adapts_emoji_pages_to_serialized_view_limit(tmp_path: Path) -> None:
    from voidcode.plugins.skills.skills_plugin import SkillShowCommand
    from voidcode.sdk.views import MarkdownViewData

    _write_skill(tmp_path, name="emoji", description="Guide", body="🙂" * 9000)
    command = SkillShowCommand(SkillsConfig(enabled=True))
    arguments = {"name": "emoji", "limit": 6000}
    offset = 0
    while True:
        result = command.invoke(CommandInvocation(_context(tmp_path), "skills.show", arguments), FakeServices())
        assert result.status == "ok"
        assert result.data["offset"] == offset
        detail = MarkdownViewData.from_payload(result.presentation.data)
        next_page = next((action for action in detail.actions if action.label == "Next page"), None)
        if next_page is None:
            assert result.data["next_offset"] is None
            break
        assert offset < next_page.arguments["offset"] <= offset + 6000
        offset = next_page.arguments["offset"]
        arguments = dict(next_page.arguments)
    assert offset > 0
