"""Unit tests for the Skills plugin: discovery, registry rules, catalog text, reader and commands."""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import CancelledError
from pathlib import Path

import pytest

from tests.fixtures.fake_services import FakeServices
from voidcode.capabilities.resource_pages import MAX_RESOURCE_PAGE_BYTES
from voidcode.plugins.skills import create_plugin
from voidcode.plugins.skills.skills_plugin import (
    DEFAULT_SKILL_SEARCH_PATHS,
    MAX_SKILL_BODY_BYTES,
    MAX_SKILL_CATALOG_BYTES,
    MAX_SKILL_DESCRIPTION_CHARS,
    MAX_SKILL_FILE_BYTES,
    MAX_SKILL_NAME_CHARS,
    MAX_SKILLS,
    SkillEnableRule,
    SkillManifestParseError,
    SkillReader,
    SkillRegistry,
    SkillsCommand,
    SkillsConfig,
    SkillsContextProvider,
    SkillShowCommand,
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


def test_discovery_is_recursive_sorted_and_isolates_duplicate_names(tmp_path: Path) -> None:
    first = _write_skill(tmp_path, name="alpha", description="a", body="A")
    _write_skill(tmp_path, name="beta", description="b", body="B", folder="nested/deeper")
    duplicate = _write_skill(tmp_path, name="alpha", description="dup", body="A2", folder="other")

    registry = SkillRegistry.discover(workspace=tmp_path)
    assert loaded_skill_names(registry) == ("alpha", "beta")
    assert registry.skills["alpha"].entry_path == first.resolve()
    assert len(registry.failures) == 1
    failure = registry.failures[0]
    assert failure.name == "alpha"
    assert failure.label == "alpha (duplicate 1)"
    assert str(first.resolve()) in failure.message
    assert str(duplicate.resolve()) in failure.message
    assert failure.display_name == "alpha (duplicate 1)"

    contribution = SkillsContextProvider(SkillsConfig(enabled=True))(_context(tmp_path), FakeServices())  # type: ignore[arg-type]
    assert contribution is not None
    assert "<name>alpha</name>" in contribution.content
    assert "<skill_diagnostics>" in contribution.content
    assert "alpha (duplicate 1)" in contribution.content

    listed = SkillsCommand(SkillsConfig(enabled=True)).invoke(CommandInvocation(_context(tmp_path), "skills"), FakeServices())
    assert listed.status == "ok"
    assert "[broken] alpha (duplicate 1)" in str(listed.output)
    options = listed.presentation.data["options"]  # type: ignore[index]
    assert len({option["id"] for option in options}) == len(options)

    shown = SkillShowCommand(SkillsConfig(enabled=True)).invoke(
        CommandInvocation(_context(tmp_path), "skills.show", {"name": "alpha (duplicate 1)"}),
        FakeServices(),
    )
    assert shown.status == "error" and "is broken" in str(shown.error)

    healthy = _read(SkillsConfig(enabled=True), tmp_path, "skill://alpha")
    assert isinstance(healthy, ResourcePage)
    assert "A" in str(healthy.body["raw_content"])
    unrelated = _read(SkillsConfig(enabled=True), tmp_path, "skill://beta")
    assert isinstance(unrelated, ResourcePage)
    assert "B" in str(unrelated.body["raw_content"])


def test_missing_search_root_and_escaping_path(tmp_path: Path) -> None:
    assert SkillRegistry.discover(workspace=tmp_path).all() == ()
    registry = SkillRegistry.discover(workspace=tmp_path, search_paths=("../outside",))
    assert registry.all() == ()
    assert any("escapes workspace" in error for error in registry.config_errors)


def test_unreadable_skill_file_is_a_per_skill_failure(tmp_path: Path) -> None:
    root = tmp_path / ".voidcode" / "skills" / "boom"
    root.mkdir(parents=True)
    entry = root / "SKILL.md"
    entry.write_text("---\nname: boom\ndescription: d\n---\nbody\n", encoding="utf-8")
    entry.chmod(0o000)
    try:
        registry = SkillRegistry.discover(workspace=tmp_path)
    finally:
        entry.chmod(0o600)
    assert registry.all() == ()
    assert [failure.name for failure in registry.failures] == ["boom"]
    assert "failed to read" in registry.failures[0].message


# --- Registry rules ---


def test_enable_rules_by_name_and_path_and_resolution_errors(tmp_path: Path) -> None:
    entry = _write_skill(tmp_path, name="alpha", description="a", body="A")
    _write_skill(tmp_path, name="beta", description="b", body="B")
    registry = SkillRegistry.discover(workspace=tmp_path)
    registry.apply_config_rules((SkillEnableRule(name="alpha", enabled=False),))
    assert registry.skills["alpha"].enabled is False
    assert [skill.name for skill in registry.skills.values() if skill.enabled] == ["beta"]

    by_path = SkillRegistry.discover(workspace=tmp_path)
    by_path.apply_config_rules((SkillEnableRule(path=str(entry.parent), enabled=False),))
    assert by_path.skills["alpha"].enabled is False
    assert by_path.skills["beta"].enabled is True

    with pytest.raises(ValueError, match="unknown skill: gamma"):
        by_path.resolve("gamma")
    with pytest.raises(ValueError, match="skill 'alpha' is disabled"):
        by_path.resolve("alpha")
    assert by_path.resolve("alpha", allow_disabled=True).name == "alpha"


# --- Prompt context texts ---


def test_catalog_text_lists_metadata_only_and_skill_uri_targets(tmp_path: Path) -> None:
    entry = _write_skill(tmp_path, name="alpha", description="a", body="A")
    registry = SkillRegistry.discover(workspace=tmp_path)
    catalog = catalog_skill_context(registry)
    assert catalog.startswith(
        "Runtime skills catalog (recommended/visible).\n"
        "Each entry is metadata only; the full SKILL.md body is NOT in your\n"
        "current context.\n"
        "Load a skill body ONLY when its description matches the active task.\n"
        'Use the read tool: read(path="skill://<name>"). Do NOT speculatively load every\n'
        "skill; lazy loading keeps the harness lightweight and the model focused.\n"
        "\n"
        "<available_skills>"
    )
    assert "    <name>alpha</name>" in catalog
    assert "    <description>a</description>" in catalog
    assert "    <location>skill://alpha</location>" in catalog
    assert f"    <sourcePath>{entry.resolve().as_uri()}</sourcePath>" in catalog
    assert catalog.endswith("</available_skills>")
    assert "A" not in catalog.split("<available_skills>")[0]

    empty = SkillRegistry.from_skills(())
    assert catalog_skill_context(empty) == ""

    unavailable = catalog_skill_context(registry, read_tool_available=False)
    assert "Skill bodies cannot be loaded" in unavailable
    assert "the read tool is unavailable" in unavailable
    assert "Use the read tool" not in unavailable
    assert "Skill bodies cannot be loaded" not in catalog

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


def test_skill_reader_byte_pages_and_rejects_empty_names(tmp_path: Path) -> None:
    _write_skill(tmp_path, name="alpha", description="a", body="A")
    config = SkillsConfig(enabled=True)
    page = _read(config, tmp_path, "skill://alpha", byte_offset=0, byte_limit=16)
    assert isinstance(page, ResourcePage)
    assert page.body["offset_unit"] == "bytes"
    assert page.body["next_byte_offset"] == 16
    assert page.body["truncated"] is True
    with pytest.raises(ValueError, match="out of range"):
        _read(config, tmp_path, "skill://alpha", byte_offset=100_000)
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


def test_skill_reader_reports_malformed_skill_without_poisoning_siblings(tmp_path: Path) -> None:
    root = tmp_path / ".voidcode" / "skills" / "boom"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("---\nname: boom\n---\nbody\n", encoding="utf-8")
    _write_skill(tmp_path, name="good", description="ok", body="healthy")
    config = SkillsConfig(enabled=True)
    with pytest.raises(ValueError, match="skill 'boom' is unavailable: .*description"):
        _read(config, tmp_path, "skill://boom")
    healthy = _read(config, tmp_path, "skill://good")
    assert isinstance(healthy, ResourcePage)
    assert "healthy" in str(healthy.body["raw_content"])


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


# --- Fault isolation, bounds and containment ---


def _write_raw_skill(workspace: Path, folder: str, contents: bytes) -> Path:
    root = workspace / ".voidcode" / "skills" / folder
    root.mkdir(parents=True, exist_ok=True)
    entry = root / "SKILL.md"
    entry.write_bytes(contents)
    return entry


@pytest.mark.parametrize(
    ("folder", "contents", "message"),
    [
        ("yaml", b"---\nname: [unclosed\n---\nbody\n", "invalid YAML"),
        ("unclosed", b"---\nname: broken\nbody\n", "must close"),
        ("bodyless", b"---\nname: broken\ndescription: d\n---\n\n", "non-empty"),
        ("utf8", b"---\nname: broken\ndescription: d\n---\n\xff\n", "not valid UTF-8"),
    ],
)
def test_malformed_entries_are_isolated(tmp_path: Path, folder: str, contents: bytes, message: str) -> None:
    _write_raw_skill(tmp_path, folder, contents)
    _write_skill(tmp_path, name="good", description="healthy", body="healthy body")
    plugin = create_plugin({"enabled": True})
    registry = build_registry(workspace=tmp_path, config=SkillsConfig(enabled=True))
    assert [skill.name for skill in registry.all()] == ["good"]
    assert len(registry.failures) == 1
    assert message in registry.failures[0].message

    contribution = plugin.context(_context(tmp_path), FakeServices())  # type: ignore[arg-type]
    assert contribution is not None
    assert "<name>good</name>" in contribution.content
    assert "<skill_diagnostics>" in contribution.content
    assert registry.failures[0].name in contribution.content

    listed = SkillsCommand(SkillsConfig(enabled=True)).invoke(CommandInvocation(_context(tmp_path), "skills"), FakeServices())
    assert listed.status == "ok"
    assert "[broken]" in str(listed.output)
    assert "fix " in str(listed.output)

    selector = SkillShowCommand(SkillsConfig(enabled=True)).prepare_input(CommandInvocation(_context(tmp_path), "skills.show"), FakeServices())
    options = selector.data["fields"][0]["options"]  # type: ignore[index]
    assert any(option["id"] == registry.failures[0].name for option in options)

    broken = SkillShowCommand(SkillsConfig(enabled=True)).invoke(
        CommandInvocation(_context(tmp_path), "skills.show", {"name": registry.failures[0].name}),
        FakeServices(),
    )
    assert broken.status == "error" and "is broken" in str(broken.error) and "fix " in str(broken.error)

    healthy = _read(SkillsConfig(enabled=True), tmp_path, "skill://good")
    assert isinstance(healthy, ResourcePage)
    assert "healthy body" in str(healthy.body["raw_content"])


def test_disabled_broken_skill_is_quarantined(tmp_path: Path) -> None:
    entry = _write_raw_skill(tmp_path, "broken", b"---\nname: [unclosed\n---\nbody\n")
    config = SkillsConfig(
        enabled=True,
        rules=(SkillEnableRule(path=str(entry.parent), enabled=False),),
    )
    registry = build_registry(workspace=tmp_path, config=config)
    assert registry.all() == ()
    assert registry.failures == ()
    assert registry.config_errors == []
    assert _read(config, tmp_path, "skill://broken") is None
    catalog = catalog_skill_context(registry)
    assert catalog == ""


def test_skill_file_and_metadata_caps_are_per_skill_diagnostics(tmp_path: Path) -> None:
    _write_raw_skill(tmp_path, "oversize-file", b"x" * (MAX_SKILL_FILE_BYTES + 1))
    _write_raw_skill(
        tmp_path,
        "oversize-body",
        f"---\nname: big\ndescription: d\n---\n\n{'x' * (MAX_SKILL_BODY_BYTES + 1)}\n".encode(),
    )
    _write_raw_skill(
        tmp_path,
        "oversize-name",
        f"---\nname: {'n' * 129}\ndescription: d\n---\nbody\n".encode(),
    )
    _write_raw_skill(
        tmp_path,
        "oversize-description",
        f"---\nname: long\ndescription: {'d' * (MAX_SKILL_DESCRIPTION_CHARS + 1)}\n---\nbody\n".encode(),
    )
    registry = build_registry(workspace=tmp_path, config=SkillsConfig(enabled=True))
    messages = " | ".join(failure.message for failure in registry.failures)
    assert "exceeds the" in messages and "byte cap" in messages
    assert f"body must not exceed {MAX_SKILL_BODY_BYTES} bytes" in messages
    assert "skill name must not exceed" in messages
    assert "skill description must not exceed" in messages
    assert len(registry.failures) == 4


def test_discovery_count_cap_emits_a_bounded_cap_diagnostic(tmp_path: Path) -> None:
    for index in range(MAX_SKILLS + 2):
        _write_skill(tmp_path, name=f"skill-{index:03d}", description="d", body="body")
    registry = build_registry(workspace=tmp_path, config=SkillsConfig(enabled=True))
    assert len(registry.all()) == MAX_SKILLS
    assert registry.failures == ()
    assert any("discovery cap" in error for error in registry.config_errors)


def test_skill_discovery_does_not_materialize_glob_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for index in range(MAX_SKILLS + 2):
        _write_skill(tmp_path, name=f"skill-{index:03d}", description="d", body="body")

    def forbidden_glob(*args: object, **kwargs: object) -> object:
        raise AssertionError("skill discovery must not materialize every glob path")

    monkeypatch.setattr(Path, "glob", forbidden_glob)
    registry = build_registry(workspace=tmp_path, config=SkillsConfig(enabled=True))
    assert len(registry.all()) == MAX_SKILLS
    assert any("discovery cap" in error for error in registry.config_errors)


def test_exact_cap_values_are_accepted(tmp_path: Path) -> None:
    name = "n" * MAX_SKILL_NAME_CHARS
    description = "d" * MAX_SKILL_DESCRIPTION_CHARS
    body = "x" * MAX_SKILL_BODY_BYTES
    _write_raw_skill(
        tmp_path,
        "boundary",
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n".encode(),
    )
    registry = build_registry(workspace=tmp_path, config=SkillsConfig(enabled=True))
    assert registry.failures == ()
    skill = registry.skills[name]
    assert len(skill.description) == MAX_SKILL_DESCRIPTION_CHARS
    assert len(skill.content.encode("utf-8")) == MAX_SKILL_BODY_BYTES


def test_count_cap_boundary_loads_exactly_the_maximum(tmp_path: Path) -> None:
    for index in range(MAX_SKILLS):
        _write_skill(tmp_path, name=f"skill-{index:03d}", description="d", body="body")
    registry = build_registry(workspace=tmp_path, config=SkillsConfig(enabled=True))
    assert len(registry.all()) == MAX_SKILLS
    assert registry.failures == ()


def test_catalog_byte_cap_omits_entries_with_a_visible_notice(tmp_path: Path) -> None:
    for index in range(20):
        _write_skill(tmp_path, name=f"skill-{index:02d}", description="d" * 900, body="body")
    registry = build_registry(workspace=tmp_path, config=SkillsConfig(enabled=True))
    catalog = catalog_skill_context(registry)
    assert len(catalog.encode("utf-8")) <= MAX_SKILL_CATALOG_BYTES
    assert "catalog byte cap" in catalog
    assert "</available_skills>" in catalog
    assert catalog.count("<skill>") < len(registry.all())


def test_skill_page_byte_bounds_a_single_huge_line(tmp_path: Path) -> None:
    _write_skill(tmp_path, name="huge", description="huge", body="x" * (MAX_RESOURCE_PAGE_BYTES + 500))
    config = SkillsConfig(enabled=True)
    page = _read(config, tmp_path, "skill://huge", offset=6, limit=1)
    assert isinstance(page, ResourcePage)
    assert page.body["offset_unit"] == "bytes"
    assert page.body["truncated"] is True
    assert page.body["next_byte_offset"] is not None
    assert len(str(page.body["raw_content"]).encode("utf-8")) <= MAX_RESOURCE_PAGE_BYTES
    final = page
    for _ in range(10):
        next_byte_offset = final.body["next_byte_offset"]
        if next_byte_offset is None:
            break
        final = _read(config, tmp_path, "skill://huge", byte_offset=int(next_byte_offset))
        assert isinstance(final, ResourcePage)
        assert len(str(final.body["raw_content"]).encode("utf-8")) <= MAX_RESOURCE_PAGE_BYTES
    assert final.body["next_byte_offset"] is None


def test_skill_read_honors_abort_signal(tmp_path: Path) -> None:
    class AbortSignal:
        cancelled = True
        reason = "cancelled by test"

        def set_cancelled(self, value: bool, *, reason: str | None = None) -> None:
            _ = (value, reason)

    _write_skill(tmp_path, name="alpha", description="a", body="A")
    caller = ResourceReadCaller(
        session_id="session-1",
        workspace=tmp_path,
        revision_id="rev-1",
        run_id="run-1",
        abort_signal=AbortSignal(),  # type: ignore[arg-type]
    )
    with pytest.raises(CancelledError):
        SkillReader(SkillsConfig(enabled=True)).read(
            ResourceReadRequest(uri="skill://alpha"),
            caller,
            FakeServices(),  # type: ignore[arg-type]
        )


def test_config_rule_paths_resolve_against_the_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_skill(tmp_path, name="alpha", description="a", body="A")
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)
    config = SkillsConfig(
        enabled=True,
        rules=(SkillEnableRule(path=".voidcode/skills/alpha", enabled=False),),
    )
    registry = build_registry(workspace=tmp_path, config=config)
    assert registry.skills["alpha"].enabled is False
    assert registry.config_errors == []


def test_config_rule_requires_exactly_one_selector_and_isolates_escapes(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly one of 'name' or 'path'"):
        parse_skills_config({"config": [{"name": "alpha", "path": "skills/alpha"}]})
    with pytest.raises(ValueError, match="exactly one of 'name' or 'path'"):
        parse_skills_config({"config": [{"enabled": False}]})

    _write_skill(tmp_path, name="alpha", description="a", body="A")
    config = SkillsConfig(
        enabled=True,
        rules=(SkillEnableRule(path="../outside", enabled=False),),
    )
    registry = build_registry(workspace=tmp_path, config=config)
    assert registry.skills["alpha"].enabled is True
    assert any("escapes the workspace" in error for error in registry.config_errors)
    catalog = catalog_skill_context(registry)
    assert "<config_errors>" not in catalog
    assert "escapes the workspace" in catalog
    assert "<name>alpha</name>" in catalog


def test_symlinked_skill_entry_escaping_a_relative_root_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("---\nname: evil\ndescription: d\n---\nsecret\n", encoding="utf-8")
    link_dir = workspace / ".voidcode" / "skills" / "evil"
    link_dir.mkdir(parents=True)
    (link_dir / "SKILL.md").symlink_to(outside / "SKILL.md")

    registry = build_registry(workspace=workspace, config=SkillsConfig(enabled=True))
    assert registry.all() == ()
    assert [failure.name for failure in registry.failures] == ["evil"]
    assert "outside the workspace" in registry.failures[0].message
    with pytest.raises(ValueError, match="outside the workspace"):
        _read(SkillsConfig(enabled=True), workspace, "skill://evil")


def test_literal_tilde_directory_still_checks_entry_containment(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("---\nname: evil\ndescription: d\n---\nsecret\n", encoding="utf-8")
    link_dir = workspace / "~" / "skills" / "evil"
    link_dir.mkdir(parents=True)
    (link_dir / "SKILL.md").symlink_to(outside / "SKILL.md")

    registry = SkillRegistry.discover(workspace=workspace, search_paths=(str(workspace / "~" / "skills"),))
    assert registry.all() == ()
    assert [failure.name for failure in registry.failures] == ["evil"]
    assert "outside the workspace" in registry.failures[0].message


def test_duplicate_entries_reached_through_overlapping_roots_load_once(tmp_path: Path) -> None:
    _write_skill(tmp_path, name="alpha", description="a", body="A", folder="group/alpha")
    registry = SkillRegistry.discover(
        workspace=tmp_path,
        search_paths=(".voidcode/skills", ".voidcode/skills/group"),
    )
    assert [skill.name for skill in registry.all()] == ["alpha"]
    assert registry.failures == ()


def test_skills_command_keeps_healthy_output_shape(tmp_path: Path) -> None:
    _write_skill(tmp_path, name="alpha", description="a", body="A")
    result = SkillsCommand(SkillsConfig(enabled=True)).invoke(CommandInvocation(_context(tmp_path), "skills"), FakeServices())
    assert result.output == f"- [enabled] alpha — a ({tmp_path / '.voidcode/skills/alpha/SKILL.md'})"
