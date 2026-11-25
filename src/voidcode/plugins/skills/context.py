"""Per-request provider context: skill catalog, file rules and rulebook."""

from __future__ import annotations

from voidcode.sdk.hooks import ContextContribution
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import PluginServices

from .catalog import catalog_skill_context
from .config import SkillsConfig
from .limits import MAX_SKILL_CONTEXT_BYTES, truncate_utf8
from .registry import build_registry
from .rules import build_rule_catalog, render_file_rules, render_rule_diagnostics, rulebook_prompt_context, runtime_file_rule_result
from .state import load_rules_state


class SkillsContextProvider:
    """Per-dispatch contribution: skill catalog, file rules, then the rulebook.

    Rule context is independent of the skills config, exactly like the legacy runtime: the
    file-rule transform shipped the workspace ``AGENTS.md`` hierarchy and the rulebook whether
    or not skills were enabled. The skills catalog stays gated on ``enabled``.
    """

    def __init__(self, config: SkillsConfig) -> None:
        self._config = config

    def __call__(self, context: ToolContext, services: PluginServices) -> ContextContribution | None:
        config = self._config
        workspace = context.require_workspace()
        parts: list[str] = []
        if config.enabled:
            registry = build_registry(workspace=workspace, config=config)
            tool_lookup = getattr(services, "tool_definition", None)
            read_tool_available = None if tool_lookup is None else tool_lookup("read") is not None
            catalog = catalog_skill_context(registry, read_tool_available=read_tool_available)
            if catalog:
                parts.append(catalog)
        state, _version = load_rules_state(services)
        file_rules = runtime_file_rule_result(workspace=workspace, touched_paths=state.touched_paths)
        rendered_file_rules = render_file_rules(file_rules.contexts)
        if rendered_file_rules:
            parts.append(rendered_file_rules)
        rule_diagnostics = render_rule_diagnostics(file_rules.diagnostics)
        if rule_diagnostics:
            parts.append(rule_diagnostics)
        rulebook_catalog = build_rule_catalog(workspace)
        if rulebook_catalog.entries or rulebook_catalog.diagnostics:
            rulebook = rulebook_prompt_context(rulebook_catalog)
            if rulebook:
                parts.append(rulebook)
        if not parts:
            return None
        content = truncate_utf8(
            "\n\n".join(parts),
            limit=MAX_SKILL_CONTEXT_BYTES,
            marker="\n[skills context truncated by runtime context policy]",
        )
        return ContextContribution(content=content)
