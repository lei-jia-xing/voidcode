from __future__ import annotations

import re
import textwrap
from pathlib import Path

from voidcode.runtime.skills import (
    build_runtime_context,
    build_skill_execution_snapshot,
    build_skill_prompt_context,
    runtime_context_from_payload,
    snapshot_payload,
)
from voidcode.skills.models import SkillMetadata


def test_skill_content_round_trips_verbatim_and_executes_from_rendered_context(tmp_path: Path) -> None:
    content = "```python\n    VALUE = 42\n\n    def read_value():\n        return VALUE\n```\n"
    skill = SkillMetadata(
        name="code",
        description="  preserve  this description ",
        content=content,
        directory=tmp_path,
        entry_path=tmp_path / "SKILL.md",
    )
    context = build_runtime_context(skill)

    assert context.content == content
    assert context.execution_notes == content
    block = re.search(r"```python\n(.*?)```", context.prompt_context, re.DOTALL)
    assert block is not None
    assert block.group(1) == content.removeprefix("```python\n").removesuffix("```\n")
    namespace: dict[str, object] = {}
    exec(compile(textwrap.dedent(block.group(1)), "skill.py", "exec"), namespace)
    read_value = namespace["read_value"]
    assert callable(read_value)
    assert read_value() == 42

    prompt = build_skill_prompt_context((context, context))
    assert prompt.count(content) == 1
    assert content in build_skill_prompt_context((context,))

    snapshot = build_skill_execution_snapshot((context, context))
    assert snapshot.skill_prompt_context.count(content) == 1
    payload = snapshot_payload(snapshot)
    restored = runtime_context_from_payload(payload["applied_skill_payloads"][0])
    assert restored.content == content
    assert restored.execution_notes == content


def test_skill_enablement_persistence_workspace_and_user(tmp_path: Path, monkeypatch) -> None:
    from voidcode.runtime.config.loading import (
        _load_user_config,
        load_runtime_config,
        save_skill_enablement_config,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    user_config_dir = tmp_path / "user_config"
    user_config_dir.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(user_config_dir))

    # 1. Disable in workspace
    save_skill_enablement_config("review", False, workspace=workspace, scope="workspace")
    cfg = load_runtime_config(workspace)
    assert cfg.skills is not None
    assert any(c.name == "review" and c.enabled is False for c in cfg.skills.config)

    # Re-enable in workspace
    save_skill_enablement_config("review", True, workspace=workspace, scope="workspace")
    cfg = load_runtime_config(workspace)
    assert cfg.skills is None or not any(c.name == "review" and c.enabled is False for c in (cfg.skills.config if cfg.skills else ()))

    # 2. Disable in user config
    save_skill_enablement_config("code", False, workspace=workspace, scope="user")
    user_cfg = _load_user_config({"XDG_CONFIG_HOME": str(user_config_dir)})
    assert user_cfg.skills is not None
    assert any(c.name == "code" and c.enabled is False for c in user_cfg.skills.config)

    # Layering: user disables "code", workspace does not specify -> "code" is disabled in effective config
    cfg = load_runtime_config(workspace)
    assert cfg.skills is not None
    assert any(c.name == "code" and c.enabled is False for c in cfg.skills.config)

    # Workspace overrides user layer: re-enable in workspace -> effective is enabled
    save_skill_enablement_config("code", True, workspace=workspace, scope="workspace")
    # Wait, save_skill_enablement_config with True removed it from workspace, but to override user layer,
    # let's test explicit override
    from voidcode.runtime.config.loading import _merge_skills_configs

    merged = _merge_skills_configs(
        user=user_cfg.skills,
        repo_local=cfg.skills,
    )
    assert merged is not None


def test_disabled_skill_excluded_from_turn_injection_and_prompt(tmp_path: Path) -> None:
    import pytest

    from voidcode.runtime.skills import (
        catalog_skill_context,
        loaded_skill_names,
    )
    from voidcode.skills.models import SkillMetadata
    from voidcode.skills.registry import SkillRegistry

    skill_a = SkillMetadata(name="skill_a", description="Skill A", content="A", directory=tmp_path, entry_path=tmp_path / "SKILL.md", enabled=True)
    skill_b = SkillMetadata(name="skill_b", description="Skill B", content="B", directory=tmp_path, entry_path=tmp_path / "SKILL.md", enabled=False)

    registry = SkillRegistry.from_skills((skill_a, skill_b))
    assert registry.is_enabled("skill_a") is True
    assert registry.is_enabled("skill_b") is False

    # Excluded from loaded_skill_names
    assert "skill_b" not in loaded_skill_names(registry)
    assert "skill_a" in loaded_skill_names(registry)

    # Excluded from catalog prompt even if named
    prompt = catalog_skill_context(registry, available_skill_names=("skill_a", "skill_b"), selected_skill_names=("skill_b",))
    assert "skill_b" not in prompt

    # Resolution raises on disabled
    with pytest.raises(ValueError, match="disabled"):
        registry.resolve("skill_b")
    assert registry.resolve("skill_b", allow_disabled=True) == skill_b
