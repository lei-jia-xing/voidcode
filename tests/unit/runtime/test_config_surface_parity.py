"""Loader-vs-artifact parity for the runtime configuration boundary.

Scope note (an accepted limitation, not an oversight): the **user-level**
`~/.config/voidcode/config.json` (`UserConfigPayload`) is *not* part of the shipped
schema, so it has no artifact and therefore no corpus rows here. Its loader
behaviour stays pinned by `tests/unit/runtime/test_config_loader_parity.py` (and
the XDG/user-config tests in `test_runtime_config.py`); generating a user-config
artifact is a separate decision.

The whole corpus runs in the default unit loop (`mise run test`, `mise run check`)
and in the coverage CI job: it takes ~7 s, so no marker split or sampling is needed.

`tests/unit/runtime/test_config_schema.py` proves the shipped
`schema/voidcode.config.schema.json` is what `runtime/config_models.py`
generates, and that the models describe the same fields the loader reads. That is
not enough on its own: a *model* can accept or reject something the **loader**
does not, and every gate would still pass.

This module drives one corpus through both surfaces and asserts they agree on
accept/reject for every case:

* the loader: `load_runtime_config()` on a workspace `.voidcode.json` (the same
  path the CLI and the runtime use),
* the artifact: `jsonschema.validate()` against the generated document.

A divergence fails here with the case id, so the artifact can only be as strict
as the loader really is. Cases where the corpus legitimately cannot be compared
are listed in `_SCHEMA_EXEMPT_KEYS` with the reason.
"""

from __future__ import annotations

import json
import typing
from pathlib import Path
from typing import Any

import jsonschema
from pydantic import BaseModel

from voidcode.runtime.config import load_runtime_config
from voidcode.runtime.config_models import RuntimeConfigPayload
from voidcode.runtime.config_schema import runtime_config_json_schema

#: ``$schema`` is deliberately absent from the corpus: it is an unread editor
#: hint whose published ``type: string`` no corpus row exercises. Its loader
#: tolerance is pinned by
#: ``test_config_loader_parity.py::test_non_string_schema_reference_is_ignored_in_the_workspace_config``.

#: A minimal *valid* payload per top-level section, so a generated variant probes
#: one leaf instead of tripping over a missing sibling.
_SECTION_BASELINES: dict[str, object] = {
    "config_schema_version": 1,
    "approval_mode": "ask",
    "permission": {"external_directory_read": {"*": "allow"}},
    "policy": {"version": "v1", "tool_policy": {"allow": ["read"]}},
    "model": "opencode-go/x",
    "execution_engine": "provider",
    "fallback_models": ["opencode-go/y"],
    "tool_timeout_seconds": 60,
    "reasoning_effort": "low",
    "hooks": {"enabled": True, "pre_tool": [["echo"]]},
    "formatter": {"languages": {"python": {"command": ["ruff"], "extensions": [".py"]}}},
    "tools": {"builtin": {"enabled": True}, "local": {"enabled": False, "path": ".voidcode/tools"}, "allowlist": ["read"]},
    "skills": {"enabled": True, "paths": [".voidcode/skills"]},
    "context_window": {"default_tool_result_chars": 6000, "per_tool_result_chars": {"read": 1000}},
    "lsp": {"enabled": True, "servers": {"python": {"command": ["pyright"]}}},
    "mcp": {"enabled": True, "servers": {"s": {"command": ["echo"]}}, "request_timeout_seconds": 30},
    "tui": {"keymap": {"a": "session_new"}, "preferences": {"theme": {"mode": "dark"}}},
    "providers": {"openai": {"timeout_seconds": 10}},
    "background_task": {"default_concurrency": 2, "provider_concurrency": {"openai": 1}},
    "agent": {"preset": "leader", "model": "opencode-go/x", "tools": {"allowlist": ["read"]}},
    "agents": {"worker": {"model": "opencode-go/x"}},
}

_DROP = object()
_WRONG_TYPES: tuple[object, ...] = (5, "x", [], {}, True)


def _set_path(payload: dict[str, object], path: tuple[str, ...], value: object) -> bool:
    """Set ``path`` in ``payload``; return ``False`` when the shape cannot host it.

    A leaf inside a list (``permission.rules[i].decision``) has no addressable slot
    in a JSON payload, so such rows are skipped instead of producing a malformed
    structure the loader would reject with a non-``ValueError``.
    """
    node = payload
    for part in path[:-1]:
        if part not in node:
            node[part] = {}
        child = node[part]
        if not isinstance(child, dict):
            return False
        node = child
    if value is _DROP:
        node.pop(path[-1], None)
    else:
        node[path[-1]] = value
    return True


def _payload_model_of(annotation: object) -> type[BaseModel] | None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in typing.get_args(annotation):
        found = _payload_model_of(arg)
        if found is not None:
            return found
    return None


def _leaf_variants(annotation: object) -> list[object]:
    """The variant set for one leaf: null, empty, and wrong types."""
    variants: list[object] = [None]
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin in (list, tuple, set, frozenset) or any(typing.get_origin(arg) in (list, tuple) for arg in args):
        variants.append([])
    elif origin is dict:
        variants.extend([{}, {"k": None}, {"k": 5}])
    elif annotation is str or str(annotation).startswith("typing.Literal"):
        variants.append("")
    variants.extend(item for item in _WRONG_TYPES if item not in (None, []))
    return variants


_MAP_ENTRY = "__map_entry__"


def _mapped_model_of(annotation: object) -> type[BaseModel] | None:
    for arg in typing.get_args(annotation):
        if typing.get_origin(arg) is dict:
            args = typing.get_args(arg)
            return _payload_model_of(args[1] if args else None)
    return None


def _model_leaves(annotation: object, path: tuple[str, ...], depth: int = 0) -> list[tuple[tuple[str, ...], object]]:
    model = _payload_model_of(annotation)
    if model is None or depth > 5:
        return [(path, annotation)]
    leaves: list[tuple[tuple[str, ...], object]] = []
    for name, field in model.model_fields.items():
        mapped = _mapped_model_of(field.annotation)
        if mapped is not None:
            # a map of objects (mcp.servers, lsp.servers, languages, ...): continue
            # through one representative entry instead of the map itself
            leaves.extend(_model_leaves(mapped, (*path, name, _MAP_ENTRY), depth + 1))
            continue
        leaves.extend(_model_leaves(field.annotation, (*path, name), depth + 1))
    return leaves


def _model_objects(annotation: object, path: tuple[str, ...], depth: int = 0) -> list[tuple[type[BaseModel], tuple[str, ...]]]:
    model = _payload_model_of(annotation)
    if model is None or depth > 4:
        return []
    found = [(model, path)]
    for name, field in model.model_fields.items():
        found.extend(_model_objects(field.annotation, (*path, name), depth + 1))
    return found


def _resolve_map_entries(payload: dict[str, object], path: tuple[str, ...]) -> tuple[str, ...] | None:
    """Rewrite ``__map_entry__`` segments to the key the baseline actually uses."""
    resolved: list[str] = []
    node: object = payload
    for part in path:
        if part == _MAP_ENTRY:
            if not isinstance(node, dict) or not node:
                return None
            entry = next(iter(node))
            if not isinstance(entry, str):
                return None
            resolved.append(entry)
            node = node[entry]
            continue
        resolved.append(part)
        if isinstance(node, dict):
            node = node.get(part)
    return tuple(resolved)


def _generated_corpus_rows() -> list[tuple[str, dict[str, object]]]:
    """Build probe payloads by walking the payload models.

    Every leaf of the model tree gets :func:`_leaf_variants` applied to a valid
    baseline payload for its section, so a row isolates one leaf instead of failing
    on an unrelated missing sibling. Object-valued fields additionally get an
    unknown key and a removed required child.
    """
    rows: list[tuple[str, dict[str, object]]] = []
    for section, field in RuntimeConfigPayload.model_fields.items():
        scalar_baseline = _SECTION_BASELINES.get(section)
        if section not in _SECTION_BASELINES:
            continue
        section_value = scalar_baseline if not isinstance(scalar_baseline, dict) else _SECTION_BASELINES[section]
        section_value = _SECTION_BASELINES[section]
        for leaf_path, annotation in _model_leaves(field.annotation, (section,)):
            for variant in _leaf_variants(annotation):
                payload = json.loads(json.dumps({section: section_value}))
                resolved = _resolve_map_entries(payload, leaf_path)
                if resolved is None or not _set_path(payload, resolved, variant):
                    continue
                rows.append((f"gen:{'.'.join(leaf_path)}={variant!r}", payload))
        for model_type, model_path in _model_objects(field.annotation, (section,)):
            payload = json.loads(json.dumps({section: section_value}))
            if _set_path(payload, (*model_path, "nope"), 1):
                rows.append((f"gen:unknown_key:{'.'.join(model_path)}", payload))
            for required_name, required_field in model_type.model_fields.items():
                if not required_field.is_required():
                    continue
                payload = json.loads(json.dumps({section: section_value}))
                if _set_path(payload, (*model_path, required_name), _DROP):
                    rows.append((f"gen:missing:{'.'.join((*model_path, required_name))}", payload))
    return rows


_HAND_WRITTEN_CORPUS: list[tuple[str, dict[str, object]]] = [
    # --- mcp servers -------------------------------------------------------
    ("mcp.stdio.command_empty", {"mcp": {"servers": {"s": {"transport": "stdio", "command": []}}}}),
    ("mcp.stdio.command_empty_string", {"mcp": {"servers": {"s": {"command": [""]}}}}),
    ("mcp.stdio.command_missing", {"mcp": {"servers": {"s": {"transport": "stdio"}}}}),
    ("mcp.stdio.command_ok", {"mcp": {"servers": {"s": {"command": ["echo"]}}}}),
    ("mcp.stdio.command_string", {"mcp": {"servers": {"s": {"command": "echo"}}}}),
    ("mcp.remote.url_empty", {"mcp": {"servers": {"s": {"transport": "remote-http", "url": ""}}}}),
    ("mcp.remote.url_missing", {"mcp": {"servers": {"s": {"transport": "remote-http"}}}}),
    ("mcp.remote.url_ok", {"mcp": {"servers": {"s": {"transport": "remote-http", "url": "https://x/y"}}}}),
    ("mcp.transport_bad", {"mcp": {"servers": {"s": {"transport": "carrier-pigeon", "command": ["x"]}}}}),
    ("mcp.env_bad_value", {"mcp": {"servers": {"s": {"command": ["x"], "env": {"A": 1}}}}}),
    ("mcp.scope_bad", {"mcp": {"servers": {"s": {"command": ["x"], "scope": "nope"}}}}),
    ("mcp.timeout_zero", {"mcp": {"request_timeout_seconds": 0}}),
    ("mcp.timeout_ok", {"mcp": {"request_timeout_seconds": 1.5}}),
    ("mcp.unknown_key", {"mcp": {"nope": 1}}),
    ("mcp.server_unknown_key", {"mcp": {"servers": {"s": {"command": ["x"], "nope": 1}}}}),
    ("mcp.servers_null", {"mcp": {"servers": None}}),
    ("mcp.enabled_string", {"mcp": {"enabled": "yes"}}),
    ("mcp.builtin_shorthand", {"mcp": {"servers": {"grep_app": {}}}}),
    ("mcp.empty_object", {"mcp": {}}),
    ("mcp.server_not_object", {"mcp": {"servers": {"s": 5}}}),
    # --- hooks -------------------------------------------------------------
    ("hooks.enabled_null", {"hooks": {"enabled": None}}),
    ("hooks.enabled_false", {"hooks": {"enabled": False}}),
    ("hooks.failure_mode_bad", {"hooks": {"failure_mode": "nope"}}),
    ("hooks.failure_mode_ok", {"hooks": {"failure_mode": "fail"}}),
    ("hooks.timeout_zero", {"hooks": {"timeout_seconds": 0}}),
    ("hooks.timeout_ok", {"hooks": {"timeout_seconds": 1}}),
    ("hooks.pre_tool_empty_command", {"hooks": {"pre_tool": [[]]}}),
    ("hooks.pre_tool_empty_item", {"hooks": {"pre_tool": [[""]]}}),
    ("hooks.pre_tool_not_array", {"hooks": {"pre_tool": "x"}}),
    ("hooks.pre_tool_item_not_array", {"hooks": {"pre_tool": ["x"]}}),
    ("hooks.pre_tool_ok", {"hooks": {"pre_tool": [["python", "x.py"]]}}),
    ("hooks.unknown_key", {"hooks": {"nope": 1}}),
    ("hooks.preset_override", {"hooks": {"formatter_presets": {"ruff": {"command": ["my-fmt"]}}}}),
    ("hooks.preset_custom_without_command", {"hooks": {"formatter_presets": {"custom": {"extensions": [".x"]}}}}),
    ("hooks.preset_command_empty", {"hooks": {"formatter_presets": {"custom": {"command": [], "extensions": [".x"]}}}}),
    ("hooks.preset_unknown_key", {"hooks": {"formatter_presets": {"ruff": {"nope": 1}}}}),
    ("hooks.preset_cwd_bad", {"hooks": {"formatter_presets": {"ruff": {"cwd_policy": "nope"}}}}),
    ("hooks.preset_not_object", {"hooks": {"formatter_presets": {"ruff": 5}}}),
    # --- formatter ---------------------------------------------------------
    ("formatter.empty_object", {"formatter": {}}),
    ("formatter.languages_empty", {"formatter": {"languages": {}}}),
    ("formatter.languages_ok", {"formatter": {"languages": {"py": {"command": ["ruff"], "extensions": [".py"]}}}}),
    ("formatter.languages_bad_preset", {"formatter": {"languages": {"py": {"command": []}}}}),
    ("formatter.enabled_nonbool", {"formatter": {"enabled": "x"}}),
    ("formatter.unknown_key", {"formatter": {"nope": 1}}),
    # --- tools / skills ----------------------------------------------------
    ("tools.local_absolute", {"tools": {"local": {"path": "/abs"}}}),
    ("tools.local_dotdot", {"tools": {"local": {"path": "../x"}}}),
    ("tools.local_empty_path", {"tools": {"local": {"path": ""}}}),
    ("tools.local_ok", {"tools": {"local": {"enabled": True, "path": ".voidcode/tools"}}}),
    ("tools.builtin_not_object", {"tools": {"builtin": 5}}),
    ("tools.builtin_ok", {"tools": {"builtin": {"enabled": False}}}),
    ("tools.allowlist_bad_item", {"tools": {"allowlist": [5]}}),
    ("tools.allowlist_empty_string", {"tools": {"allowlist": [""]}}),
    ("tools.allowlist_null", {"tools": {"allowlist": None}}),
    ("tools.essential_only_string", {"tools": {"essential_only": "yes"}}),
    ("tools.unknown_key", {"tools": {"nope": 1}}),
    ("skills.paths_bad_item", {"skills": {"paths": [1]}}),
    ("skills.enabled_int", {"skills": {"enabled": 1}}),
    ("skills.enabled_null", {"skills": {"enabled": None}}),
    ("skills.unknown_key", {"skills": {"nope": 1}}),
    # --- context window ----------------------------------------------------
    ("context.default_chars_zero", {"context_window": {"default_tool_result_chars": 0}}),
    ("context.default_chars_null", {"context_window": {"default_tool_result_chars": None}}),
    ("context.default_chars_ok", {"context_window": {"default_tool_result_chars": 100}}),
    ("context.per_tool_empty_key", {"context_window": {"per_tool_result_chars": {"": 1}}}),
    ("context.per_tool_zero", {"context_window": {"per_tool_result_chars": {"read": 0}}}),
    ("context.per_tool_ok", {"context_window": {"per_tool_result_chars": {"read": 10}}}),
    ("context.per_tool_not_object", {"context_window": {"per_tool_result_chars": 5}}),
    ("context.version_bad", {"context_window": {"version": 3}}),
    ("context.version_null", {"context_window": {"version": None}}),
    ("context.version_ok", {"context_window": {"version": 2}}),
    ("context.summary_bad", {"context_window": {"summary_strategy": "x"}}),
    ("context.diagnostics_bad", {"context_window": {"provider_context_diagnostics": "x"}}),
    ("context.transform_policy_bad", {"context_window": {"context_transform_failure_policy": "x"}}),
    ("context.threshold_zero", {"context_window": {"provider_context_oversized_feedback_chars": 0}}),
    ("context.unknown_key", {"context_window": {"nope": 1}}),
    ("context.compaction_threshold_zero", {"context_window": {"compaction": {"threshold_tokens": 0}}}),
    (
        "context.compaction_ok",
        {"context_window": {"compaction": {"threshold_tokens": 9000, "keep_recent_tool_tokens": 500}}},
    ),
    ("context.compaction_unknown_key", {"context_window": {"compaction": {"nope": 1}}}),
    # --- lsp ---------------------------------------------------------------
    ("lsp.server_unknown_preset", {"lsp": {"servers": {"s": {"preset": "nope"}}}}),
    ("lsp.server_command_empty", {"lsp": {"servers": {"s": {"command": []}}}}),
    ("lsp.server_command_empty_string", {"lsp": {"servers": {"s": {"command": [""]}}}}),
    ("lsp.server_ok", {"lsp": {"servers": {"s": {"command": ["x"]}}}}),
    ("lsp.server_settings_not_object", {"lsp": {"servers": {"s": {"command": ["x"], "settings": 5}}}}),
    ("lsp.server_unknown_key", {"lsp": {"servers": {"s": {"command": ["x"], "nope": 1}}}}),
    ("lsp.server_preset_not_string", {"lsp": {"servers": {"s": {"preset": 5}}}}),
    ("lsp.servers_not_object", {"lsp": {"servers": 5}}),
    ("lsp.diagnostics_string", {"lsp": {"diagnostics_on_write": "yes"}}),
    ("lsp.unknown_key", {"lsp": {"nope": 1}}),
    # --- tui ---------------------------------------------------------------
    ("tui.keymap_bad_value", {"tui": {"keymap": {"a": "nope"}}}),
    ("tui.keymap_bad_item", {"tui": {"keymap": {"a": 1}}}),
    ("tui.keymap_ok", {"tui": {"keymap": {"a": "session_new"}}}),
    ("tui.theme_mode_bad", {"tui": {"preferences": {"theme": {"mode": "nope"}}}}),
    ("tui.theme_name_int", {"tui": {"preferences": {"theme": {"name": 5}}}}),
    ("tui.unknown_key", {"tui": {"nope": 1}}),
    # --- background tasks --------------------------------------------------
    ("background.concurrency_zero", {"background_task": {"default_concurrency": 0}}),
    ("background.concurrency_ok", {"background_task": {"default_concurrency": 1}}),
    ("background.provider_concurrency_zero", {"background_task": {"provider_concurrency": {"a": 0}}}),
    ("background.model_concurrency_ok", {"background_task": {"model_concurrency": {"a": 2}}}),
    ("background.reminders_string", {"background_task": {"delegated_reminders_enabled": "yes"}}),
    ("background.unknown_key", {"background_task": {"nope": 1}}),
    # --- reminders ---------------------------------------------------------
    ("reminders.enabled_string", {"reminders": {"enabled": "yes"}}),
    ("reminders.enabled_ok", {"reminders": {"enabled": False}}),
    ("reminders.max_per_cycle_zero", {"reminders": {"todo": {"max_per_cycle": 0}}}),
    ("reminders.max_per_cycle_string", {"reminders": {"todo": {"max_per_cycle": "3"}}}),
    ("reminders.ok", {"reminders": {"enabled": True, "todo": {"max_per_cycle": 2}}}),
    ("reminders.todo_not_object", {"reminders": {"todo": 5}}),
    ("reminders.unknown_key", {"reminders": {"nope": 1}}),
    # --- permission --------------------------------------------------------
    ("permission.rule_missing_decision", {"permission": {"rules": [{"tool": "write"}]}}),
    ("permission.rule_bad_decision", {"permission": {"rules": [{"decision": "nope"}]}}),
    ("permission.rule_empty_tool", {"permission": {"rules": [{"tool": "", "decision": "ask"}]}}),
    ("permission.rule_missing_tool", {"permission": {"rules": [{"decision": "ask"}]}}),
    ("permission.rule_unknown_key", {"permission": {"rules": [{"decision": "ask", "nope": 1}]}}),
    ("permission.rule_not_object", {"permission": {"rules": [5]}}),
    ("permission.rule_ok", {"permission": {"rules": [{"tool": "write", "path": "src/**", "decision": "ask"}]}}),
    ("permission.map_bad_decision", {"permission": {"external_directory_read": {"*": "nope"}}}),
    ("permission.map_empty_key", {"permission": {"external_directory_read": {"": "ask"}}}),
    ("permission.map_ok", {"permission": {"external_directory_write": {"*": "ask"}}}),
    ("permission.unknown_key", {"permission": {"nope": 1}}),
    # --- policy ------------------------------------------------------------
    ("policy.allow_empty_item", {"policy": {"version": "v1", "tool_policy": {"allow": [""]}}}),
    ("policy.deny_empty_item", {"policy": {"version": "v1", "tool_policy": {"deny": [""]}}}),
    ("policy.profile_refs_empty", {"policy": {"version": "v1", "prompt_activation": {"profile_refs": [""]}}}),
    ("policy.default_key", {"policy": {"version": "v1", "tool_policy": {"default": "allow"}}}),
    ("policy.bad_scope", {"policy": {"version": "v1", "hook_policy": {"allowed_event_scopes": ["nope"]}}}),
    ("policy.action_unknown", {"policy": {"version": "v1", "hook_policy": {"actions": ["nope"]}}}),
    ("policy.bad_version", {"policy": {"version": "v2"}}),
    ("policy.unknown_key", {"policy": {"metadata": {}}}),
    ("policy.enabled_string", {"policy": {"enabled": "yes"}}),
    ("policy.nested_unknown_key", {"policy": {"version": "v1", "tool_policy": {"nope": 1}}}),
    ("policy.ok", {"policy": {"version": "v1", "tool_policy": {"allow": ["read"]}}}),
    # --- providers ---------------------------------------------------------
    ("providers.endpoint_timeout_zero", {"providers": {"endpoint": {"timeout_seconds": 0}}}),
    ("providers.endpoint_api_key_empty", {"providers": {"endpoint": {"api_key": ""}}}),
    ("providers.openai_api_key_empty", {"providers": {"openai": {"api_key": ""}}}),
    ("providers.openai_timeout_zero", {"providers": {"openai": {"timeout_seconds": 0}}}),
    ("providers.openai_timeout_ok", {"providers": {"openai": {"timeout_seconds": 1}}}),
    ("providers.google_method_bad", {"providers": {"google": {"auth": {"method": "nope"}}}}),
    ("providers.google_method_missing", {"providers": {"google": {"auth": {}}}}),
    ("providers.google_method_ok", {"providers": {"google": {"auth": {"method": "oauth"}}}}),
    ("providers.github-copilot_method_bad", {"providers": {"github-copilot": {"auth": {"method": "nope"}}}}),
    ("providers.github-copilot_method_ok", {"providers": {"github-copilot": {"auth": {"method": "token"}}}}),
    ("providers.github-copilot_refresh_zero", {"providers": {"github-copilot": {"auth": {"method": "token", "refresh_leeway_seconds": 0}}}}),
    ("providers.model_map_empty_value", {"providers": {"endpoint": {"model_map": {"a": ""}}}}),
    ("providers.model_map_ok", {"providers": {"endpoint": {"model_map": {"a": "b"}}}}),
    ("providers.custom_reserved", {"providers": {"custom": {"openai": {}}}}),
    ("providers.custom_ok", {"providers": {"custom": {"mine": {}}}}),
    ("providers.unknown", {"providers": {"nope": {}}}),
    ("providers.beta_headers_empty_item", {"providers": {"anthropic": {"beta_headers": [""]}}}),
    ("providers.retry_negative", {"providers": {"openai": {"transient_retry": {"max_retries": -1}}}}),
    ("providers.retry_ok", {"providers": {"openai": {"transient_retry": {"max_retries": 1}}}}),
    ("providers.null_block", {"providers": {"openai": None}}),
    # --- agent / agents ----------------------------------------------------
    ("agent.model_empty", {"agent": {"preset": "leader", "model": ""}}),
    ("agent.model_int", {"agent": {"preset": "leader", "model": 5}}),
    ("agent.prompt_empty", {"agent": {"preset": "leader", "prompt": ""}}),
    ("agent.prompt_blank", {"agent": {"preset": "leader", "prompt": "   "}}),
    ("agent.prompt_int", {"agent": {"preset": "leader", "prompt": 5}}),
    ("agent.prompt_append_empty", {"agent": {"preset": "leader", "prompt_append": ""}}),
    ("agent.prompt_profile_empty", {"agent": {"preset": "leader", "prompt_profile": ""}}),
    ("agent.prompt_profile_int", {"agent": {"preset": "leader", "prompt_profile": 5}}),
    ("agent.fallback_item_type", {"agent": {"preset": "leader", "model": "opencode-go/x", "fallback_models": [1]}}),
    ("agent.fallback_empty_string", {"agent": {"preset": "leader", "model": "opencode-go/x", "fallback_models": [""]}}),
    ("agent.fallback_duplicates", {"agent": {"preset": "leader", "model": "opencode-go/x", "fallback_models": ["opencode-go/y", "opencode-go/y"]}}),
    ("agent.fallback_no_model", {"agent": {"preset": "leader", "fallback_models": ["opencode-go/y"]}}),
    ("agent.fallback_ok", {"agent": {"preset": "leader", "model": "opencode-go/x", "fallback_models": ["opencode-go/y"]}}),
    ("agent.preset_missing", {"agent": {"model": "opencode-go/x"}}),
    ("agent.preset_int", {"agent": {"preset": 5}}),
    ("agent.preset_unknown", {"agent": {"preset": "nope"}}),
    ("agent.hook_refs_unknown", {"agent": {"preset": "leader", "hook_refs": ["nope"]}}),
    ("agent.hook_refs_bad_item", {"agent": {"preset": "leader", "hook_refs": [5]}}),
    ("agent.context_transform_bad_item", {"agent": {"preset": "leader", "context_transform_refs": [5]}}),
    ("agent.context_transform_duplicate", {"agent": {"preset": "leader", "context_transform_refs": ["a", "a"]}}),
    ("agent.tools_local", {"agent": {"preset": "leader", "tools": {"local": {}}}}),
    ("agent.tools_allowlist_bad_item", {"agent": {"preset": "leader", "tools": {"allowlist": [5]}}}),
    ("agent.tools_ok", {"agent": {"preset": "leader", "tools": {"allowlist": ["read"]}}}),
    ("agent.skills_enabled_int", {"agent": {"preset": "leader", "skills": {"enabled": 1}}}),
    ("agent.mcp_binding_profile_empty", {"agent": {"preset": "leader", "mcp_binding": {"profile": ""}}}),
    ("agent.mcp_binding_server_empty", {"agent": {"preset": "leader", "mcp_binding": {"servers": [""]}}}),
    ("agent.mcp_binding_server_duplicate", {"agent": {"preset": "leader", "mcp_binding": {"servers": ["a", "a"]}}}),
    ("agent.mcp_binding_ok", {"agent": {"preset": "leader", "mcp_binding": {"profile": "p", "servers": ["a"]}}}),
    ("agent.unknown_key", {"agent": {"preset": "leader", "nope": 1}}),
    ("agent.unknown_key_execution_engine", {"agent": {"preset": "leader", "execution_engine": "provider"}}),
    ("agents.builtin_without_preset", {"agents": {"worker": {"model": "opencode-go/x"}}}),
    ("agents.builtin_preset_int", {"agents": {"worker": {"preset": 5}}}),
    ("agents.custom_key_no_preset", {"agents": {"mine": {"model": "opencode-go/x"}}}),
    ("agents.custom_key_with_preset", {"agents": {"mine": {"preset": "worker", "model": "opencode-go/x"}}}),
    ("agents.bad_key", {"agents": {"Bad Key": {}}}),
    ("agents.entry_not_object", {"agents": {"worker": 5}}),
    ("agents.model_empty", {"agents": {"worker": {"model": ""}}}),
    ("agents.unknown_key", {"agents": {"worker": {"nope": 1}}}),
    ("agents.empty_map", {"agents": {}}),
    # --- top level scalars -------------------------------------------------
    ("top.model_int", {"model": 5}),
    ("top.model_empty", {"model": ""}),
    ("top.model_ok", {"model": "opencode-go/x"}),
    ("top.approval_bad", {"approval_mode": "sometimes"}),
    ("top.approval_null", {"approval_mode": None}),
    ("top.approval_ok", {"approval_mode": "deny"}),
    ("top.execution_engine_bad", {"execution_engine": "nope"}),
    ("top.execution_engine_ok", {"execution_engine": "provider"}),
    ("top.tool_timeout_zero", {"tool_timeout_seconds": 0}),
    ("top.tool_timeout_null", {"tool_timeout_seconds": None}),
    ("top.tool_timeout_ok", {"tool_timeout_seconds": 1}),
    ("top.reasoning_effort_bad", {"reasoning_effort": "none"}),
    ("top.reasoning_effort_ok", {"reasoning_effort": "low"}),
    ("top.fallback_ok", {"model": "opencode-go/x", "fallback_models": ["opencode-go/y"]}),
    ("top.fallback_bad_item", {"model": "opencode-go/x", "fallback_models": [1]}),
    ("top.fallback_empty_string", {"model": "opencode-go/x", "fallback_models": [""]}),
    ("top.fallback_no_model", {"fallback_models": ["opencode-go/y"]}),
    ("top.unknown_key", {"nope": 1}),
    ("top.empty_object", {}),
    ("top.section_not_object", {"tools": 5}),
    ("top.all_null", {"approval_mode": None, "tools": None, "lsp": None, "mcp": None, "agent": None, "agents": None}),
]

#: Hand-written rows for the concrete regressions plus one model-driven row per
#: leaf/variant, so a new field is exercised the moment it exists.
CORPUS: list[tuple[str, dict[str, object]]] = [*_HAND_WRITTEN_CORPUS, *_generated_corpus_rows()]


#: Cases where the *loader* enforces a rule a JSON Schema cannot express, so the
#: artifact accepts what the loader rejects. Each entry states the rule; a case
#: that starts diverging the other way (the artifact rejecting something the
#: loader accepts) is still a failure, which is the harm this gate exists for.
LOADER_ONLY_RULES: dict[str, str] = {
    "mcp.stdio.command_missing": "a builtin server name may omit `command` (its descriptor supplies it), so the requirement is not a static rule",
    "mcp.remote.url_missing": "same, for `url` on remote-http builtins",
    "hooks.preset_override": "a builtin preset name merges over the builtin preset instead of replacing it",
    "hooks.preset_custom_without_command": "`command` is required only for a preset that is not a builtin",
    "tools.local_absolute": "the local tools path must be workspace-relative",
    "tools.local_dotdot": "the local tools path may not contain `..`",
    "context.per_tool_empty_key": "per-tool result limits require non-blank tool names",
    "lsp.server_unknown_preset": "the preset must exist in the LSP preset catalog",
    "providers.google_method_ok": "the chosen auth method must also carry usable credential material",
    "providers.github-copilot_method_ok": "same, for the Copilot auth methods",
    "agent.prompt_blank": "agent text fields must be non-blank, not merely non-empty",
    "agent.fallback_duplicates": "a fallback chain may not repeat a model",
    "agent.preset_unknown": "the preset must exist in the agent manifest registry",
    "agent.hook_refs_unknown": "hook refs must exist in the hook preset catalog",
    "agent.context_transform_duplicate": "context transform refs are deduplicated/rejected by the transform registry",
    "agent.mcp_binding_server_duplicate": "MCP binding servers may not repeat",
    "gen:lsp.servers.__map_entry__.command=None": (
        "an LSP server named after a builtin preset may omit `command`, so an empty argv is only rejected for other names"
    ),
    "gen:lsp.servers.__map_entry__.command=[]": "same conditional rule for an empty argv",
    "gen:mcp.servers.__map_entry__.command=None": (
        "a builtin MCP server shorthand may omit `command` (its descriptor supplies it), so the requirement is conditional"
    ),
    "gen:mcp.servers.__map_entry__.command=[]": "same conditional rule for an empty argv",
    "lsp.server_command_empty": "an LSP server named after a builtin preset may omit `command`, so an empty argv is only rejected for other names",
}


#: Recurring loader-only rules named by the *part of the case id that carries the
#: rule* (a substring, not a blanket family): each entry is one rule the loader
#: owns and JSON Schema cannot see.
LOADER_ONLY_MATCHES: tuple[tuple[str, str], ...] = (
    (
        ".transient_retry.max_delay_ms=",
        "provider retry delays are cross-checked (max_delay_ms must cover base_delay_ms) in provider/config.py",
    ),
    (
        "__map_entry__.preset='",
        "the preset must exist in the LSP preset catalog (provider of the rule: runtime/config.py + lsp catalog)",
    ),
    (
        "gen:agents.",
        "an agent entry resolves against the manifest registry",
    ),
    (
        "gen:agent.preset",
        "the preset must exist in the agent manifest registry",
    ),
    (
        "gen:policy.",
        "policy semantics (version, section shapes, action filtering) live in runtime/policy.py",
    ),
)


def _declared_loader_only(case_id: str) -> bool:
    if case_id in LOADER_ONLY_RULES:
        return True
    return any(match in case_id for match, _reason in LOADER_ONLY_MATCHES)


def _loader_accepts(workspace: Path, payload: dict[str, object]) -> bool:
    config_path = workspace / ".voidcode.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    try:
        try:
            load_runtime_config(workspace, env={})
        except ValueError:
            return False
    finally:
        config_path.unlink()
    return True


def _schema_accepts(validator: jsonschema.Draft202012Validator, payload: dict[str, object]) -> bool:
    return not list(validator.iter_errors(payload))


def _check_corpus(cases: list[tuple[str, dict[str, object]]], workspace: Path) -> list[str]:
    """Assert artifact/loader agreement for ``cases``; return the faults.

    Two properties are asserted for every row:

    * if the artifact rejects a payload, the loader must reject it too (no false
      alarms for editors), and
    * if the loader rejects it while the artifact accepts it, the case must be a
      declared :data:`LOADER_ONLY_RULES` / :data:`LOADER_ONLY_MATCHES` entry that
      states the rule and its owner.
    """
    validator = jsonschema.Draft202012Validator(runtime_config_json_schema())
    faults: list[str] = []
    for case_id, payload in cases:
        loader_ok = _loader_accepts(workspace, payload)
        schema_ok = _schema_accepts(validator, payload)
        if schema_ok == loader_ok:
            # both reject (or both accept): no divergence to report
            continue
        if schema_ok and not loader_ok:
            if not _declared_loader_only(case_id):
                faults.append(f"{case_id}: loader rejects but the artifact accepts; declare it in LOADER_ONLY_RULES with a reason")
            continue
        faults.append(f"{case_id}: the artifact rejects a payload the loader accepts (schema must not be stricter)")
    return faults


def test_generated_schema_and_loader_agree_on_every_corpus_case(tmp_path: Path) -> None:
    """The gate: every generated corpus row, in both directions.

    The sweep costs ~7 s for ~2100 rows because it reuses one workspace file and
    compiles the artifact validator once (an earlier version created a temporary
    directory and recompiled the schema per row, which cost 7 minutes).
    """
    faults = _check_corpus(CORPUS, tmp_path)

    assert faults == [], "artifact/loader divergence:\n" + "\n".join(faults)


def test_loader_only_rules_all_come_from_the_corpus() -> None:
    """A declared loader-only rule must still be exercised by the corpus."""
    corpus_ids = {case_id for case_id, _payload in CORPUS}

    assert set(LOADER_ONLY_RULES) <= corpus_ids
    matches = tuple(match for match, _reason in LOADER_ONLY_MATCHES)
    assert all(any(match in case_id for case_id in corpus_ids) for match in matches)


def test_corpus_covers_every_top_level_section() -> None:
    """Guard against a corpus that silently stops covering a section."""
    covered = {key for _case_id, payload in CORPUS for key in payload}
    schema = runtime_config_json_schema()
    sections = set(cast_dict(schema["properties"])) - {"$schema"}

    assert sections <= covered


def cast_dict(value: Any) -> dict[str, Any]:
    assert isinstance(value, dict)
    return value
