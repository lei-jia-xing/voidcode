"""Unit tests for the skills plugin's rule context: AGENTS.md hierarchy, rulebook, touched paths."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import pytest

from voidcode.plugins.skills import create_plugin
from voidcode.plugins.skills.rules import (
    DEFAULT_RULE_LIMITS,
    LOCAL_RULE_FILE_NAME,
    RULE_FILE_NAME,
    RuleCatalog,
    RuleCatalogEntry,
    RuleLimits,
    build_rule_catalog,
    render_file_rule_context,
    render_file_rules,
    rulebook_prompt_context,
    runtime_file_rule_contexts,
)
from voidcode.plugins.skills.skills_plugin import (
    TOUCHED_PATHS_LIMIT,
    RulesState,
    SkillsConfig,
    SkillsContextProvider,
    skills_observer,
)
from voidcode.sdk.hooks import FactPage, InvocationFinishedFact, UsageFact
from voidcode.sdk.invocation import ToolContext

TRUNCATION_MARKER = "\n[Rule file truncated by runtime context policy]"
FILE_RULE_PREFIX = "Runtime file rules are active for touched workspace paths."


@dataclass
class FakeServices:
    """Session state plus a committed event log, standing in for the scoped services."""

    state: Mapping[str, object] | None = None
    version: int = 0
    events: tuple[InvocationFinishedFact, ...] = ()
    writes: list[tuple[Mapping[str, object], int]] = field(default_factory=list)

    def get_state(self, scope: Literal["session", "workspace"] = "session") -> tuple[Mapping[str, object] | None, int]:
        assert scope == "session"
        return self.state, self.version

    def read_facts(self, *, after_sequence: int = 0, limit: int = 100) -> FactPage:
        remaining = tuple(event for event in self.events if event.event_sequence > after_sequence)
        page = remaining[:limit]
        return FactPage(page, page[-1].event_sequence if page else after_sequence, len(remaining) > limit)

    def replace_state(self, state: Mapping[str, object], expected_version: int, scope: Literal["session", "workspace"] = "session") -> int:
        assert scope == "session"
        self.writes.append((state, expected_version))
        self.state = state
        self.version = expected_version + 1
        return self.version


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


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _invocation_fact(*, event_sequence: int) -> InvocationFinishedFact:
    return InvocationFinishedFact(
        session_id="session-1",
        run_id="run-1",
        revision_id="rev-1",
        logical_turn="turn-1",
        event_sequence=event_sequence,
        tool_name="write",
        tool_call_id="call-1",
        status="ok",
    )


# --- Rule file hierarchy ---


def test_override_file_wins_over_agents_md(tmp_path: Path) -> None:
    _write(tmp_path / RULE_FILE_NAME, "standard")
    assert [context.path for context in runtime_file_rule_contexts(workspace=tmp_path)] == [RULE_FILE_NAME]

    _write(tmp_path / LOCAL_RULE_FILE_NAME, "override")
    contexts = runtime_file_rule_contexts(workspace=tmp_path)
    assert [context.path for context in contexts] == [LOCAL_RULE_FILE_NAME]
    assert contexts[0].content == "override"


def test_hierarchy_is_ordered_project_root_then_workspace(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    _write(project / RULE_FILE_NAME, "root rules")
    _write(project / "src" / RULE_FILE_NAME, "src rules")
    workspace = project / "src" / "app"
    workspace.mkdir(parents=True)
    _write(workspace / RULE_FILE_NAME, "app rules")

    contexts = runtime_file_rule_contexts(workspace=workspace)
    assert [context.path for context in contexts] == ["../../AGENTS.md", "../AGENTS.md", "AGENTS.md"]
    assert [context.content for context in contexts] == ["root rules", "src rules", "app rules"]


def test_touched_paths_add_dynamic_rules_after_the_baseline(tmp_path: Path) -> None:
    _write(tmp_path / RULE_FILE_NAME, "root rules")
    _write(tmp_path / "sub" / RULE_FILE_NAME, "sub rules")

    contexts = runtime_file_rule_contexts(workspace=tmp_path, touched_paths=("sub/file.txt",))
    assert [context.path for context in contexts] == ["AGENTS.md", "sub/AGENTS.md"]

    assert [context.path for context in runtime_file_rule_contexts(workspace=tmp_path)] == ["AGENTS.md"]


def test_dynamic_rules_keep_the_most_recent_and_respect_the_file_cap(tmp_path: Path) -> None:
    _write(tmp_path / RULE_FILE_NAME, "root rules")
    for name in ("alpha", "beta", "gamma"):
        _write(tmp_path / name / RULE_FILE_NAME, f"{name} rules")
    touched = ("alpha/file.txt", "beta/file.txt", "gamma/file.txt")

    contexts = runtime_file_rule_contexts(workspace=tmp_path, touched_paths=touched, limits=RuleLimits(max_rule_files=3))
    assert [context.path for context in contexts] == ["AGENTS.md", "beta/AGENTS.md", "gamma/AGENTS.md"]

    assert len(runtime_file_rule_contexts(workspace=tmp_path, touched_paths=touched)) == 4


def test_default_file_cap_keeps_the_baseline_plus_the_last_seven_dynamic_rules(tmp_path: Path) -> None:
    _write(tmp_path / RULE_FILE_NAME, "root rules")
    names = [f"dir-{index}" for index in range(DEFAULT_RULE_LIMITS.max_rule_files + 2)]
    for name in names:
        _write(tmp_path / name / RULE_FILE_NAME, f"{name} rules")

    contexts = runtime_file_rule_contexts(workspace=tmp_path, touched_paths=tuple(f"{name}/file.txt" for name in names))
    assert len(contexts) == DEFAULT_RULE_LIMITS.max_rule_files == 8
    expected = ["AGENTS.md"] + [f"{name}/AGENTS.md" for name in names[-(DEFAULT_RULE_LIMITS.max_rule_files - 1) :]]
    assert [context.path for context in contexts] == expected


def test_default_total_budget_stops_at_thirty_six_kilobytes(tmp_path: Path) -> None:
    _write(tmp_path / RULE_FILE_NAME, "x" * 12_000)
    names = ["one", "two", "three", "four"]
    for name in names:
        _write(tmp_path / name / RULE_FILE_NAME, "y" * 12_000)

    contexts = runtime_file_rule_contexts(workspace=tmp_path, touched_paths=tuple(f"{name}/file.txt" for name in names))
    assert [context.path for context in contexts] == ["AGENTS.md", "one/AGENTS.md", "two/AGENTS.md"]
    assert sum(len(context.content.encode("utf-8")) for context in contexts) == 36_000


def test_baseline_alone_saturates_the_file_cap(tmp_path: Path) -> None:
    _write(tmp_path / RULE_FILE_NAME, "root rules")
    for name in ("one", "two", "three"):
        _write(tmp_path / name / RULE_FILE_NAME, name)

    contexts = runtime_file_rule_contexts(workspace=tmp_path, touched_paths=("one/file.txt",), limits=RuleLimits(max_rule_files=1))
    assert [context.path for context in contexts] == ["AGENTS.md"]


def test_touched_path_outside_the_workspace_is_ignored(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    _write(project / RULE_FILE_NAME, "root rules")
    workspace = project / "app"
    workspace.mkdir(parents=True)
    _write(workspace / RULE_FILE_NAME, "app rules")
    _write(tmp_path / "outside" / RULE_FILE_NAME, "outside rules")

    contexts = runtime_file_rule_contexts(workspace=workspace, touched_paths=("../../outside/file.txt",))
    assert [context.path for context in contexts] == ["../AGENTS.md", "AGENTS.md"]


# --- Truncation and budget math ---


def test_per_file_truncation_appends_the_exact_marker(tmp_path: Path) -> None:
    _write(tmp_path / RULE_FILE_NAME, "a" * (DEFAULT_RULE_LIMITS.max_rule_file_chars + 50))

    contexts = runtime_file_rule_contexts(workspace=tmp_path)
    assert len(contexts) == 1
    context = contexts[0]
    assert context.path == RULE_FILE_NAME
    assert context.content == "a" * DEFAULT_RULE_LIMITS.max_rule_file_chars + TRUNCATION_MARKER


def test_total_budget_truncates_the_file_that_overruns_it(tmp_path: Path) -> None:
    _write(tmp_path / RULE_FILE_NAME, "a" * 120)
    _write(tmp_path / "sub" / RULE_FILE_NAME, "b" * 120)

    contexts = runtime_file_rule_contexts(workspace=tmp_path, touched_paths=("sub/file.txt",), limits=RuleLimits(max_total_rule_bytes=200))
    assert [context.path for context in contexts] == ["AGENTS.md", "sub/AGENTS.md"]
    assert contexts[0].content == "a" * 120
    assert contexts[1].content == "b" * (200 - 120 - len(TRUNCATION_MARKER)) + TRUNCATION_MARKER
    assert len(contexts[1].content.encode("utf-8")) == 200 - 120


def test_budget_exhausted_by_an_exact_file_stops_there(tmp_path: Path) -> None:
    _write(tmp_path / RULE_FILE_NAME, "a" * 100)
    _write(tmp_path / "sub" / RULE_FILE_NAME, "b" * 100)

    contexts = runtime_file_rule_contexts(workspace=tmp_path, touched_paths=("sub/file.txt",), limits=RuleLimits(max_total_rule_bytes=100))
    assert [context.path for context in contexts] == ["AGENTS.md"]
    assert contexts[0].content == "a" * 100


def test_zero_budget_reports_the_first_rule_path_with_empty_content(tmp_path: Path) -> None:
    _write(tmp_path / RULE_FILE_NAME, "root rules")

    contexts = runtime_file_rule_contexts(workspace=tmp_path, limits=RuleLimits(max_total_rule_bytes=0))
    assert [context.path for context in contexts] == ["AGENTS.md"]
    assert contexts[0].content == ""

    assert runtime_file_rule_contexts(workspace=tmp_path, limits=RuleLimits(max_total_rule_bytes=0), include_workspace_root=False) == ()
    assert runtime_file_rule_contexts(workspace=None) == ()
    assert runtime_file_rule_contexts(workspace=tmp_path, limits=RuleLimits(max_rule_files=0)) == ()
    assert runtime_file_rule_contexts(workspace=tmp_path, limits=RuleLimits(max_total_rule_bytes=-1)) == ()


def test_unreadable_rule_file_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path / RULE_FILE_NAME, "root rules")
    sub_rule = _write(tmp_path / "sub" / RULE_FILE_NAME, "sub rules")
    original = Path.read_text

    def flaky(self: Path, *args: object, **kwargs: object) -> str:
        if self == sub_rule:
            raise OSError("unreadable")
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", flaky)
    contexts = runtime_file_rule_contexts(workspace=tmp_path, touched_paths=("sub/file.txt",))
    assert [context.path for context in contexts] == ["AGENTS.md"]


def test_render_file_rules_uses_the_legacy_framing(tmp_path: Path) -> None:
    _write(tmp_path / RULE_FILE_NAME, "root rules")

    contexts = runtime_file_rule_contexts(workspace=tmp_path)
    assert render_file_rule_context(contexts[0]) == f"{FILE_RULE_PREFIX}\nRule file: AGENTS.md\nroot rules"
    assert render_file_rules(contexts) == f"{FILE_RULE_PREFIX}\nRule file: AGENTS.md\nroot rules"
    assert render_file_rules(()) == ""


# --- Rulebook ---


def _rule_document(*, name: str, body: str, application: str = "always_apply", **fields: str) -> str:
    lines = ["---", f"name: {name}", f"application: {application}"]
    lines.extend(f"{key}: {value}" for key, value in fields.items())
    lines.append("---")
    return "\n".join(lines) + f"\n\n{body}\n"


def test_rulebook_parses_valid_documents_and_skips_invalid_ones(tmp_path: Path) -> None:
    root = tmp_path / ".voidcode" / "rules"
    _write(root / "style.md", _rule_document(name="style", body="# Style\n\nUse tabs.", scope="repo", precedence="5", description="House style"))
    _write(root / "notes.md", _rule_document(name="notes", body="Read me", application="discoverable"))
    _write(root / "missing-application.md", "---\nname: broken\n---\n\nbody")
    _write(root / "bad-name.md", _rule_document(name="Bad Name", body="body"))
    _write(root / "bodyless.md", _rule_document(name="empty", body="   "))
    _write(root / "not-a-rule.txt", "ignored")

    catalog = build_rule_catalog(tmp_path)
    assert [entry.metadata.name for entry in catalog.entries] == ["notes", "style"]
    style = catalog.always_apply[0].metadata
    assert (style.scope, style.precedence, style.description) == ("repo", 5, "House style")
    notes = catalog.discoverable[0].metadata
    assert (notes.precedence, notes.description) == (0, "Read me")
    assert build_rule_catalog(None).entries == ()


def test_rulebook_duplicate_names_keep_the_entry_first_in_sort_order(tmp_path: Path) -> None:
    root = tmp_path / ".voidcode" / "rules"
    _write(root / "aaa-first.md", _rule_document(name="dupe", body="first", scope="workspace", precedence="1"))
    _write(root / "zzz-last.md", _rule_document(name="dupe", body="last", scope="workspace", precedence="2"))

    # Same scope: the greater precedence wins regardless of file name.
    assert [entry.content for entry in build_rule_catalog(tmp_path).entries] == ["last"]

    _write(root / "zzz-last.md", _rule_document(name="dupe", body="weaker", scope="workspace", precedence="0"))
    assert [entry.content for entry in build_rule_catalog(tmp_path).entries] == ["first"]

    # A repo-scoped rule sorts after workspace rules, so it wins the collision.
    _write(root / "zzz-last.md", _rule_document(name="dupe", body="repo wins", scope="repo", precedence="0"))
    assert [entry.content for entry in build_rule_catalog(tmp_path).entries] == ["repo wins"]


def test_rulebook_caps_files_content_and_metadata(tmp_path: Path) -> None:
    root = tmp_path / ".voidcode" / "rules"
    for index in range(70):
        _write(root / f"rule-{index:03d}.md", _rule_document(name=f"rule-{index:03d}", body=f"body {index}"))
    assert len(build_rule_catalog(tmp_path).entries) == 64

    other = tmp_path / "single"
    _write(
        other / ".voidcode" / "rules" / "long.md",
        _rule_document(name="long", body="x" * (DEFAULT_RULE_LIMITS.max_rulebook_content_chars + 100), description="d" * 400),
    )
    entry = build_rule_catalog(other).entries[0]
    assert entry.content == "x" * DEFAULT_RULE_LIMITS.max_rulebook_content_chars
    assert len(entry.metadata.description) == DEFAULT_RULE_LIMITS.max_rulebook_metadata_chars


def test_rulebook_prompt_context_is_verbatim_and_capped(tmp_path: Path) -> None:
    root = tmp_path / ".voidcode" / "rules"
    _write(root / "style.md", _rule_document(name="style", body="Use tabs.", scope="repo", precedence="5"))
    _write(root / "notes.md", _rule_document(name="notes", body="Read me", application="discoverable"))

    catalog = build_rule_catalog(tmp_path)
    style = catalog.always_apply[0].metadata
    notes = catalog.discoverable[0].metadata
    assert rulebook_prompt_context(catalog) == (
        "Runtime rulebook always-apply rules are workspace guidance only; runtime policy remains authoritative.\n"
        "\n"
        "Rule style (scope=repo, precedence=5):\n"
        "Use tabs.\n"
        "\n"
        "Discoverable runtime rules (read the listed files when needed):\n"
        f"- notes: Read me (scope=workspace, precedence=0, path={notes.path})"
    )
    assert style.payload() == {
        "name": "style",
        "description": "Use tabs.",
        "application": "always_apply",
        "scope": "repo",
        "precedence": 5,
        "path": (root / "style.md").as_posix(),
    }

    huge = RuleCatalog((RuleCatalogEntry(metadata=style, content="z" * (DEFAULT_RULE_LIMITS.max_rulebook_prompt_chars * 2)),))
    assert len(rulebook_prompt_context(huge)) == DEFAULT_RULE_LIMITS.max_rulebook_prompt_chars
    assert rulebook_prompt_context(RuleCatalog(())) == ""


# --- Touched-path extraction and state ---


def test_rules_state_round_trips_and_rejects_unknown_keys_and_bad_types() -> None:
    assert RulesState.from_payload(None) == RulesState()
    assert RulesState(touched_paths=("a.txt",), cursor_sequence=3).to_payload() == {
        "schema_version": 1,
        "cursor_sequence": 3,
        "touched_paths": ["a.txt"],
    }
    assert RulesState.from_payload({"schema_version": 1, "cursor_sequence": 2, "touched_paths": ["a.txt"]}) == RulesState(
        touched_paths=("a.txt",),
        cursor_sequence=2,
    )
    for payload in (
        {"schema_version": 1, "cursor_sequence": 0, "touched_paths": [], "extra": True},
        {"schema_version": 2, "cursor_sequence": 0, "touched_paths": []},
        {"schema_version": 1, "cursor_sequence": -1, "touched_paths": []},
        {"schema_version": 1, "cursor_sequence": 0, "touched_paths": "a.txt"},
        {"schema_version": 1, "cursor_sequence": 0, "touched_paths": [1]},
    ):
        with pytest.raises(ValueError):
            RulesState.from_payload(payload)


def _resource_fact(sequence: int, arguments: Mapping[str, object]) -> InvocationFinishedFact:
    return InvocationFinishedFact(
        session_id="session-1",
        run_id="run-1",
        revision_id="rev-1",
        logical_turn="turn",
        event_sequence=sequence,
        tool_name="custom_tool",
        tool_call_id=None,
        status="ok",
        resource_paths=(str(arguments["path"]),) if "path" in arguments else (),
    )


def test_observer_records_workspace_relative_paths_once_per_event(tmp_path: Path) -> None:
    services = FakeServices(
        events=(
            _resource_fact(1, {}),
            _resource_fact(2, {"path": "sub/file.txt"}),
            _resource_fact(3, {"path": str(tmp_path / "absolute.txt")}),
            _resource_fact(4, {"path": "../outside.txt"}),
            _resource_fact(5, {}),
        )
    )
    skills_observer(_invocation_fact(event_sequence=5), _context(tmp_path), services)  # type: ignore[arg-type]

    assert services.state == {"schema_version": 1, "cursor_sequence": 5, "touched_paths": ["sub/file.txt", "absolute.txt"]}
    assert services.writes[0][1] == 0

    # A later notification consumes only what is new, and keeps recency order without duplicates.
    services.events = services.events + (_resource_fact(6, {"path": "sub/file.txt"}),)
    skills_observer(_invocation_fact(event_sequence=6), _context(tmp_path), services)  # type: ignore[arg-type]
    assert services.state == {"schema_version": 1, "cursor_sequence": 6, "touched_paths": ["absolute.txt", "sub/file.txt"]}
    assert len(services.writes) == 2

    # Nothing new: no write at all.
    skills_observer(_invocation_fact(event_sequence=6), _context(tmp_path), services)  # type: ignore[arg-type]
    assert len(services.writes) == 2


def test_observer_ignores_other_facts_and_keeps_the_most_recent_paths(tmp_path: Path) -> None:
    services = FakeServices(events=tuple(_resource_fact(index + 1, {"path": f"touched/{index}.txt"}) for index in range(TOUCHED_PATHS_LIMIT + 5)))
    skills_observer(
        UsageFact(
            session_id="session-1",
            run_id="run-1",
            revision_id="rev-1",
            logical_turn="turn-1",
            event_sequence=1,
            dispatch_sequence=1,
            provider_name="endpoint",
            model_name="test-model",
            usage=None,
        ),
        _context(tmp_path),  # type: ignore[arg-type]
        services,
    )
    assert services.writes == []

    # Existing paths share the same bounded recency list.
    services.state = {"schema_version": 1, "cursor_sequence": 0, "touched_paths": ["stale.txt"]}
    services.version = 4
    skills_observer(_invocation_fact(event_sequence=TOUCHED_PATHS_LIMIT + 5), _context(tmp_path), services)  # type: ignore[arg-type]

    payload = services.state
    assert payload is not None
    touched = payload["touched_paths"]
    assert isinstance(touched, list)
    assert len(touched) == TOUCHED_PATHS_LIMIT
    assert touched[-1] == f"touched/{TOUCHED_PATHS_LIMIT + 4}.txt"
    assert "stale.txt" not in touched
    assert services.writes[0][1] == 4


# --- Context assembly ---


def test_context_contribution_orders_skills_then_file_rules_then_rulebook(tmp_path: Path) -> None:
    _write(tmp_path / RULE_FILE_NAME, "root rules")
    _write(tmp_path / ".voidcode" / "rules" / "style.md", _rule_document(name="style", body="Use tabs."))
    skill = tmp_path / ".voidcode" / "skills" / "alpha"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: alpha\ndescription: a\n---\n\nA\n", encoding="utf-8")

    contribution = SkillsContextProvider(SkillsConfig(enabled=True))(_context(tmp_path), FakeServices())  # type: ignore[arg-type]
    assert contribution is not None
    assert contribution.role == "system"
    assert (
        contribution.content.index("<available_skills>")
        < contribution.content.index(FILE_RULE_PREFIX)
        < contribution.content.index("Runtime rulebook always-apply rules")
    )
    assert f"{FILE_RULE_PREFIX}\nRule file: AGENTS.md\nroot rules" in contribution.content
    assert "Rule style (scope=workspace, precedence=100):\nUse tabs." in contribution.content


def test_context_contribution_keeps_rules_when_skills_are_disabled(tmp_path: Path) -> None:
    plugin = create_plugin(None)
    assert plugin.context is not None
    assert plugin.context(_context(tmp_path), FakeServices()) is None  # type: ignore[arg-type]

    _write(tmp_path / RULE_FILE_NAME, "root rules")
    _write(tmp_path / "sub" / RULE_FILE_NAME, "sub rules")
    _write(tmp_path / ".voidcode" / "rules" / "style.md", _rule_document(name="style", body="Use tabs."))
    state = RulesState(touched_paths=("sub/file.txt",)).to_payload()

    contribution = plugin.context(_context(tmp_path), FakeServices(state=state))  # type: ignore[arg-type]
    assert contribution is not None
    assert "<available_skills>" not in contribution.content
    assert contribution.content == (
        f"{FILE_RULE_PREFIX}\n"
        "Rule file: AGENTS.md\n"
        "root rules\n"
        "\n"
        f"{FILE_RULE_PREFIX}\n"
        "Rule file: sub/AGENTS.md\n"
        "sub rules\n"
        "\n"
        "Runtime rulebook always-apply rules are workspace guidance only; runtime policy remains authoritative.\n"
        "\n"
        "Rule style (scope=workspace, precedence=100):\n"
        "Use tabs."
    )
    assert plugin.observer is skills_observer
    assert plugin.tools == ()
    assert [reader.scheme for reader in plugin.resource_readers] == ["skill"]
    assert [command.definition.name for command in plugin.commands] == ["skills", "skills.show"]
