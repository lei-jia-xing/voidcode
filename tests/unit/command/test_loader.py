from pathlib import Path

import pytest

from voidcode.command.loader import load_markdown_commands


def test_markdown_commands_reject_runtime_mode_frontmatter(tmp_path: Path) -> None:
    command = tmp_path / "review.md"
    command.write_text(
        "---\nname: review\nmode: plan\n---\nReview the changes.\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported frontmatter field: mode"):
        load_markdown_commands(tmp_path, source="user")
