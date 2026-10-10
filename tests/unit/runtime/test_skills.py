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
