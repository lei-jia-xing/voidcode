from __future__ import annotations

import hashlib
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from voidcode.formatter import RuntimeFormatterPresetConfig
from voidcode.hook.config import RuntimeHooksConfig
from voidcode.runtime.edit_schema_policy import EditSchema
from voidcode.runtime.service import ToolRegistry
from voidcode.tools import EditTool, ToolCall
from voidcode.tools._repair import ToolDiagnosticError
from voidcode.tools.runtime_context import RuntimeToolInvocationContext, bind_runtime_tool_context


def _content_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_edit_tool_replaces_exact_text(tmp_path: Path) -> None:
    file_path = tmp_path / "test.txt"
    file_path.write_text("hello world", encoding="utf-8")

    tool = EditTool()

    result = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={
                "path": "test.txt",
                "oldString": "world",
                "newString": "voidcode",
                "expectedHash": _content_hash(file_path),
            },
        ),
        workspace=tmp_path,
    )

    assert result.tool_name == "edit"
    assert result.status == "ok"
    assert result.content == "Edit applied successfully."
    assert file_path.read_text(encoding="utf-8") == "hello voidcode"
    assert result.data["additions"] == 1
    assert result.data["deletions"] == 1


def test_edit_tool_replaces_all_occurrences(tmp_path: Path) -> None:
    file_path = tmp_path / "test.txt"
    file_path.write_text("foo bar foo baz foo", encoding="utf-8")

    tool = EditTool()

    result = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={
                "path": "test.txt",
                "oldString": "foo",
                "newString": "qux",
                "replaceAll": True,
                "expectedHash": _content_hash(file_path),
            },
        ),
        workspace=tmp_path,
    )

    assert file_path.read_text(encoding="utf-8") == "qux bar qux baz qux"
    assert result.content is not None
    assert "3 occurrences replaced" in result.content


def test_edit_tool_rejects_multiple_exact_matches_without_replace_all(tmp_path: Path) -> None:
    file_path = tmp_path / "test.txt"
    file_path.write_text("foo bar foo", encoding="utf-8")

    tool = EditTool()

    with pytest.raises(ToolDiagnosticError, match="Multiple matches found") as exc_info:
        tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={
                    "path": "test.txt",
                    "oldString": "foo",
                    "newString": "qux",
                    "expectedHash": _content_hash(file_path),
                },
            ),
            workspace=tmp_path,
        )

    diagnostic = exc_info.value
    assert diagnostic.error_kind == "ambiguous_match"
    assert diagnostic.error_details["reason"] == "ambiguous_match"
    assert diagnostic.error_details["match_count"] == 2
    matches = cast(list[dict[str, object]], diagnostic.error_details["matches"])
    assert matches[0]["line_numbers"] == [1, 1]
    assert "foo" in str(matches[0]["preview"])
    assert isinstance(diagnostic.retry_guidance, str)
    assert diagnostic.retry_guidance
    assert "replaceAll" in diagnostic.retry_guidance


def test_edit_tool_rejects_non_string_arguments(tmp_path: Path) -> None:
    tool = EditTool()

    with pytest.raises(ValueError, match="string path"):
        tool.invoke(
            ToolCall(tool_name="edit", arguments={"path": 123, "oldString": "a", "newString": "b"}),
            workspace=tmp_path,
        )

    with pytest.raises(ValueError, match="string oldString"):
        tool.invoke(
            ToolCall(tool_name="edit", arguments={"path": "f.txt", "oldString": 123, "newString": "b"}),
            workspace=tmp_path,
        )

    with pytest.raises(ValueError, match="string newString"):
        tool.invoke(
            ToolCall(tool_name="edit", arguments={"path": "f.txt", "oldString": "a", "newString": 123}),
            workspace=tmp_path,
        )


def test_edit_tool_allows_path_outside_workspace(tmp_path: Path) -> None:
    tool = EditTool()
    outside = tmp_path.parent / "outside-edit.txt"
    outside.write_text("alpha", encoding="utf-8")

    result = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={
                "path": str(outside),
                "oldString": "alpha",
                "newString": "beta",
                "expectedHash": _content_hash(outside),
            },
        ),
        workspace=tmp_path,
    )
    assert result.status == "ok"
    assert outside.read_text(encoding="utf-8") == "beta"


def test_edit_tool_allows_symlink_escape_when_runtime_permission_allows(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside_edit_escape.txt"
    outside.write_text("alpha", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink is not available on this platform")

    tool = EditTool()
    result = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={
                "path": "link.txt",
                "oldString": "alpha",
                "newString": "beta",
                "expectedHash": _content_hash(outside),
            },
        ),
        workspace=tmp_path,
    )
    assert result.status == "ok"
    assert outside.read_text(encoding="utf-8") == "beta"


def test_edit_tool_rejects_nonexistent_file(tmp_path: Path) -> None:
    tool = EditTool()

    with pytest.raises(ValueError, match="does not exist"):
        tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={"path": "missing.txt", "oldString": "a", "newString": "b"},
            ),
            workspace=tmp_path,
        )


def test_edit_tool_rejects_identical_old_and_new(tmp_path: Path) -> None:
    file_path = tmp_path / "test.txt"
    file_path.write_text("hello", encoding="utf-8")

    tool = EditTool()

    with pytest.raises(ToolDiagnosticError, match="identical") as exc_info:
        tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={
                    "path": "test.txt",
                    "oldString": "hello",
                    "newString": "hello",
                    "expectedHash": _content_hash(file_path),
                },
            ),
            workspace=tmp_path,
        )

    diagnostic = exc_info.value
    assert diagnostic.error_kind == "no_op"
    assert diagnostic.error_details["reason"] == "identical_old_and_new"
    assert diagnostic.error_details["old_string"] == "hello"
    assert diagnostic.error_details["new_string"] == "hello"
    assert isinstance(diagnostic.retry_guidance, str)
    assert diagnostic.retry_guidance


def test_edit_tool_rejects_when_old_string_not_found(tmp_path: Path) -> None:
    file_path = tmp_path / "test.txt"
    file_path.write_text("hello", encoding="utf-8")

    tool = EditTool()

    with pytest.raises(ToolDiagnosticError, match="Could not find oldString") as exc_info:
        tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={
                    "path": "test.txt",
                    "oldString": "missing",
                    "newString": "b",
                    "expectedHash": _content_hash(file_path),
                },
            ),
            workspace=tmp_path,
        )

    diagnostic = exc_info.value
    assert diagnostic.error_kind == "tool_input_mismatch"
    assert diagnostic.error_details["reason"] == "old_string_not_found"
    assert "Replacers attempted:" in str(diagnostic)
    message = str(diagnostic)
    assert "SimpleReplacer" in message
    assert "ContextAwareReplacer" in message
    assert "No nearby text match found" in message
    assert "attempted_replacers" in diagnostic.error_details
    assert diagnostic.error_details["line_number_prefix_suspected"] is False
    assert isinstance(diagnostic.retry_guidance, str)
    assert diagnostic.retry_guidance


def test_edit_tool_no_match_with_unindented_old_string_keeps_diagnostics(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "test.txt"
    file_path.write_text("hello", encoding="utf-8")

    tool = EditTool()

    with pytest.raises(ValueError, match="Could not find oldString") as exc_info:
        tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={
                    "path": "test.txt",
                    "oldString": "missing",
                    "newString": "b",
                    "expectedHash": _content_hash(file_path),
                },
            ),
            workspace=tmp_path,
        )

    assert "Replacers attempted:" in str(exc_info.value)


def test_edit_tool_reports_near_match_context_when_old_string_is_stale(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "test.py"
    file_path.write_text(
        "def greet():\n    message = 'hello'\n    return message\n",
        encoding="utf-8",
    )

    tool = EditTool()

    with pytest.raises(ValueError, match="Near-match hints") as exc_info:
        tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={
                    "path": "test.py",
                    "oldString": "def greet():\n    message = 'hullo'\n    return value",
                    "newString": "def greet():\n    message = 'hi'\n    return message",
                    "expectedHash": _content_hash(file_path),
                },
            ),
            workspace=tmp_path,
        )

    message = str(exc_info.value)
    assert "Replacers attempted:" in message
    assert "BlockAnchorReplacer" in message
    assert "L1" in message
    assert "message = 'hello'" in message
    assert "Diff (- oldString, + current):" in message
    assert "-    message = 'hullo'" in message
    assert "+    message = 'hello'" in message
    assert " def greet():" in message
    assert "+    return message" in message
    assert "first block anchor is close" in message
    assert "retry with exact current text" in message


def test_edit_tool_warns_when_old_string_includes_read_line_prefixes(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "test.py"
    file_path.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    tool = EditTool()

    with pytest.raises(ValueError, match="line prefixes") as exc_info:
        tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={
                    "path": "test.py",
                    "oldString": "1: alpha\n2: beta",
                    "newString": "alpha\nupdated",
                    "expectedHash": _content_hash(file_path),
                },
            ),
            workspace=tmp_path,
        )

    message = str(exc_info.value)
    assert "oldString appears to include read output line prefixes" in message
    assert "remove those prefixes" in message


def test_edit_tool_preserves_line_endings(tmp_path: Path) -> None:
    file_path = tmp_path / "test.txt"
    file_path.write_bytes(b"line1\r\nline2\r\nline3")

    tool = EditTool()

    tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={
                "path": "test.txt",
                "oldString": "line2",
                "newString": "modified",
                "expectedHash": _content_hash(file_path),
            },
        ),
        workspace=tmp_path,
    )

    content = file_path.read_bytes()
    assert content == b"line1\r\nmodified\r\nline3"


def test_edit_tool_matches_block_anchors_with_small_typos(tmp_path: Path) -> None:
    file_path = tmp_path / "test.txt"
    file_path.write_text(
        "alpha\nstart block\nkeep middle\nend block\nomega\n",
        encoding="utf-8",
    )

    tool = EditTool()

    result = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={
                "path": "test.txt",
                "oldString": "start blok\nkeep middle\nend block",
                "newString": "start block\nupdated middle\nend block",
                "expectedHash": _content_hash(file_path),
            },
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert file_path.read_text(encoding="utf-8") == ("alpha\nstart block\nupdated middle\nend block\nomega\n")


def test_edit_tool_skips_formatter_when_no_matching_preset(tmp_path: Path) -> None:
    file_path = tmp_path / "note.txt"
    file_path.write_text("hello world\n", encoding="utf-8")

    tool = EditTool(
        hooks_config=RuntimeHooksConfig(
            formatter_presets={
                "python": RuntimeFormatterPresetConfig(
                    command=("missing-formatter",),
                    extensions=(".py",),
                )
            }
        )
    )

    result = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={"path": "note.txt", "oldString": "world", "newString": "voidcode", "expectedHash": _content_hash(file_path)},
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert result.content == "Edit applied successfully."
    assert "diagnostics" not in result.data
    assert "formatter" not in result.data
    assert file_path.read_text(encoding="utf-8") == "hello voidcode\n"


def test_edit_tool_skips_formatter_when_hooks_are_disabled(tmp_path: Path) -> None:
    file_path = tmp_path / "main.py"
    file_path.write_text("print('hi')\n", encoding="utf-8")

    tool = EditTool(
        hooks_config=RuntimeHooksConfig(
            enabled=False,
            formatter_presets={
                "python": RuntimeFormatterPresetConfig(
                    command=("missing-formatter-binary",),
                    extensions=(".py",),
                )
            },
        )
    )

    result = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={"path": "main.py", "oldString": "'hi'", "newString": "'bye'", "expectedHash": _content_hash(file_path)},
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert result.content == "Edit applied successfully."
    assert "diagnostics" not in result.data
    assert "formatter" not in result.data
    assert file_path.read_text(encoding="utf-8") == "print('bye')\n"


def test_edit_tool_rejects_without_prior_read_before_formatter_execution(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "main.py"
    file_path.write_text("print('hi')\n", encoding="utf-8")
    formatter_marker = tmp_path / "formatter-ran.txt"
    formatter_script = tmp_path / "formatter.py"
    formatter_script.write_text(
        textwrap.dedent(
            f"""
            import pathlib
            import sys

            pathlib.Path({str(formatter_marker)!r}).write_text("ran", encoding="utf-8")
            pathlib.Path(sys.argv[-1]).write_text("print( 'formatted' )\\n", encoding="utf-8")
            """
        ),
        encoding="utf-8",
    )
    tool = EditTool(
        hooks_config=RuntimeHooksConfig(
            formatter_presets={
                "python": RuntimeFormatterPresetConfig(
                    command=(sys.executable, str(formatter_script)),
                    extensions=(".py",),
                )
            }
        )
    )

    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="test")):
        with pytest.raises(ValueError, match="requires reading the current file before modifying it"):
            tool.invoke(
                ToolCall(
                    tool_name="edit",
                    arguments={"path": "main.py", "oldString": "'hi'", "newString": "'bye'", "expectedHash": _content_hash(file_path)},
                ),
                workspace=tmp_path,
            )

    assert not formatter_marker.exists()
    assert file_path.read_text(encoding="utf-8") == "print('hi')\n"


def test_edit_tool_runs_formatter_after_prior_read_and_write(tmp_path: Path) -> None:
    file_path = tmp_path / "main.py"
    file_path.write_text("print('hi')\n", encoding="utf-8")
    formatter_script = tmp_path / "formatter.py"
    formatter_script.write_text(
        textwrap.dedent(
            """
            import pathlib
            import sys

            path = pathlib.Path(sys.argv[-1])
            observed = path.read_text(encoding="utf-8")
            if observed != "print('bye')\\n":
                raise SystemExit(f"formatter saw unexpected content: {observed!r}")
            path.write_text("print( 'bye' )\\n", encoding="utf-8")
            """
        ),
        encoding="utf-8",
    )
    read_paths = frozenset({file_path.resolve().as_posix()})
    read_lines = {file_path.resolve().as_posix(): frozenset({1})}
    tool = EditTool(
        hooks_config=RuntimeHooksConfig(
            formatter_presets={
                "python": RuntimeFormatterPresetConfig(
                    command=(sys.executable, str(formatter_script)),
                    extensions=(".py",),
                )
            }
        )
    )

    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="test", read_paths=read_paths, read_lines=read_lines)):
        result = tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={"path": "main.py", "oldString": "'hi'", "newString": "'bye'", "expectedHash": _content_hash(file_path)},
            ),
            workspace=tmp_path,
        )

    assert result.status == "ok"
    assert file_path.read_text(encoding="utf-8") == "print( 'bye' )\n"
    assert cast(dict[str, object], result.data["formatter"])["status"] == "formatted"


def test_edit_tool_surfaces_warning_when_formatter_executable_is_missing(tmp_path: Path) -> None:
    file_path = tmp_path / "main.py"
    file_path.write_text("print('hi')\n", encoding="utf-8")

    tool = EditTool(
        hooks_config=RuntimeHooksConfig(
            formatter_presets={
                "python": RuntimeFormatterPresetConfig(
                    command=("missing-formatter-binary",),
                    extensions=(".py",),
                    fallback_commands=(("also-missing",),),
                )
            }
        )
    )

    result = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={"path": "main.py", "oldString": "'hi'", "newString": "'bye'", "expectedHash": _content_hash(file_path)},
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert "Formatter warning:" in (result.content or "")
    assert file_path.read_text(encoding="utf-8") == "print('bye')\n"
    diagnostics = result.data["diagnostics"]
    assert diagnostics == [
        {
            "source": "formatter",
            "severity": "warning",
            "message": (
                "No formatter executable was available for preset 'python'. "
                "Tried: missing-formatter-binary, also-missing. Install one of them or override "
                "hooks.formatter_presets.python.command in .voidcode.json."
            ),
            "language": "python",
            "cwd": str(tmp_path),
            "attempted_commands": [
                ["missing-formatter-binary", str(file_path)],
                ["also-missing", str(file_path)],
            ],
        }
    ]


def test_edit_tool_re_reads_after_successful_formatter_rewrite(tmp_path: Path) -> None:
    file_path = tmp_path / "main.py"
    file_path.write_text("print('hi')\n", encoding="utf-8")
    formatter_script = tmp_path / "formatter.py"
    formatter_script.write_text(
        textwrap.dedent(
            """
            import pathlib
            import sys

            pathlib.Path(sys.argv[-1]).write_text("print( 'bye' )\\n", encoding="utf-8")
            """
        ),
        encoding="utf-8",
    )

    tool = EditTool(
        hooks_config=RuntimeHooksConfig(
            formatter_presets={
                "python": RuntimeFormatterPresetConfig(
                    command=(sys.executable, str(formatter_script)),
                    extensions=(".py",),
                )
            }
        )
    )

    result = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={"path": "main.py", "oldString": "'hi'", "newString": "'bye'", "expectedHash": _content_hash(file_path)},
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert file_path.read_text(encoding="utf-8") == "print( 'bye' )\n"
    assert result.data["formatter"] == {
        "status": "formatted",
        "language": "python",
        "cwd": str(tmp_path),
        "command": [sys.executable, str(formatter_script), str(file_path)],
        "attempted_commands": [[sys.executable, str(formatter_script), str(file_path)]],
    }
    assert "print( 'bye' )" in str(result.data["diff"])
    assert "diagnostics" not in result.data


def test_edit_tool_keeps_edit_successful_when_formatter_returns_non_zero(tmp_path: Path) -> None:
    file_path = tmp_path / "main.py"
    file_path.write_text("print('hi')\n", encoding="utf-8")
    formatter_script = tmp_path / "broken_formatter.py"
    formatter_script.write_text(
        textwrap.dedent(
            """
            import pathlib
            import sys

            pathlib.Path(sys.argv[-1]).write_text("print('partial')\\n", encoding="utf-8")
            raise SystemExit(1)
            """
        ),
        encoding="utf-8",
    )

    tool = EditTool(
        hooks_config=RuntimeHooksConfig(
            formatter_presets={
                "python": RuntimeFormatterPresetConfig(
                    command=(sys.executable, str(formatter_script)),
                    extensions=(".py",),
                )
            }
        )
    )

    result = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={"path": "main.py", "oldString": "'hi'", "newString": "'bye'", "expectedHash": _content_hash(file_path)},
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert "Formatter warning:" in (result.content or "")
    assert file_path.read_text(encoding="utf-8") == "print('partial')\n"
    diagnostics = result.data["diagnostics"]
    assert isinstance(diagnostics, list)
    first_diagnostic = cast(dict[str, object], diagnostics[0])
    assert first_diagnostic["source"] == "formatter"
    assert "Format failed for main.py" in str(first_diagnostic["message"])
    assert "print('partial')" in str(result.data["diff"])


def test_edit_tool_keeps_edit_successful_when_formatter_times_out(tmp_path: Path) -> None:
    file_path = tmp_path / "main.py"
    file_path.write_text("print('hi')\n", encoding="utf-8")

    tool = EditTool(
        hooks_config=RuntimeHooksConfig(
            formatter_presets={
                "python": RuntimeFormatterPresetConfig(
                    command=("slow-formatter",),
                    extensions=(".py",),
                )
            }
        )
    )

    with patch(
        "voidcode.formatter.executor.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["slow-formatter"], timeout=10.0),
    ):
        result = tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={"path": "main.py", "oldString": "'hi'", "newString": "'bye'", "expectedHash": _content_hash(file_path)},
            ),
            workspace=tmp_path,
        )

    assert result.status == "ok"
    assert "Formatter warning:" in (result.content or "")
    diagnostics = result.data["diagnostics"]
    assert isinstance(diagnostics, list)
    first_diagnostic = cast(dict[str, object], diagnostics[0])
    assert "timed out after 30.0s" in str(first_diagnostic["message"])
    assert file_path.read_text(encoding="utf-8") == "print('bye')\n"


def test_edit_tool_rejects_missing_expected_hash_with_structured_diagnostic(tmp_path: Path) -> None:
    file_path = tmp_path / "test.txt"
    file_path.write_text("hello world", encoding="utf-8")

    tool = EditTool()

    with pytest.raises(ToolDiagnosticError, match="expectedHash") as exc_info:
        tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={"path": "test.txt", "oldString": "world", "newString": "voidcode"},
            ),
            workspace=tmp_path,
        )

    diagnostic = exc_info.value
    assert diagnostic.error_kind == "tool_input_mismatch"
    assert diagnostic.error_details["reason"] == "missing_expected_hash"
    assert diagnostic.error_details["path"] == "test.txt"
    assert "read_file" in (diagnostic.retry_guidance or "")
    assert file_path.read_text(encoding="utf-8") == "hello world"


def test_edit_tool_rejects_stale_expected_hash_with_structured_diagnostic(tmp_path: Path) -> None:
    file_path = tmp_path / "test.txt"
    file_path.write_text("hello world", encoding="utf-8")

    tool = EditTool()

    with pytest.raises(ToolDiagnosticError, match="stale edit") as exc_info:
        tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={
                    "path": "test.txt",
                    "oldString": "world",
                    "newString": "voidcode",
                    "expectedHash": "0" * 64,
                },
            ),
            workspace=tmp_path,
        )

    diagnostic = exc_info.value
    assert diagnostic.error_kind == "stale_edit"
    assert diagnostic.error_details["reason"] == "content_hash_mismatch"
    assert diagnostic.error_details["expected_hash"] == "0" * 64
    assert diagnostic.error_details["actual_hash"] == _content_hash(file_path)
    assert diagnostic.error_details["path"] == "test.txt"
    assert "data.content_hash" in (diagnostic.retry_guidance or "")
    assert file_path.read_text(encoding="utf-8") == "hello world"


def test_edit_tool_schema_requires_expected_hash() -> None:
    schema = EditTool.definition.input_schema
    assert "expectedHash" in schema
    assert schema["required"] == ["path", "oldString", "newString", "expectedHash"]
    assert "data.content_hash" in str(schema["expectedHash"]["description"])


def test_tools_package_and_default_registry_export_edit_tool() -> None:
    registry = ToolRegistry.with_defaults()

    assert "EditTool" in __import__("voidcode.tools", fromlist=["__all__"]).__all__
    assert registry.resolve("edit").definition.name == "edit"
    assert registry.resolve("edit").definition.read_only is False


def _read_lines_context(path: Path, lines: set[int]) -> RuntimeToolInvocationContext:
    resolved = path.resolve().as_posix()
    return RuntimeToolInvocationContext(
        session_id="test",
        read_paths=frozenset({resolved}),
        read_lines={resolved: frozenset(lines)},
    )


def _read_model_context(path: Path, model: str) -> RuntimeToolInvocationContext:
    resolved = path.resolve().as_posix()
    return RuntimeToolInvocationContext(
        session_id="test",
        model=model,
        read_paths=frozenset({resolved}),
        read_lines={resolved: frozenset({1})},
    )


def test_edit_tool_edits_seen_line_when_read_window_covers_it(tmp_path: Path) -> None:
    file_path = tmp_path / "sample.txt"
    file_path.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    tool = EditTool()

    with bind_runtime_tool_context(_read_lines_context(file_path, {1, 2, 3})):
        result = tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={"path": "sample.txt", "oldString": "beta", "newString": "BETA", "expectedHash": _content_hash(file_path)},
            ),
            workspace=tmp_path,
        )

    assert result.status == "ok"
    assert file_path.read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"


def test_edit_tool_rejects_edit_of_line_outside_read_window(tmp_path: Path) -> None:
    file_path = tmp_path / "sample.txt"
    file_path.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    tool = EditTool()

    with bind_runtime_tool_context(_read_lines_context(file_path, {1})):
        with pytest.raises(ToolDiagnosticError, match="never revealed by read_file") as exc_info:
            tool.invoke(
                ToolCall(
                    tool_name="edit",
                    arguments={"path": "sample.txt", "oldString": "gamma", "newString": "GAMMA", "expectedHash": _content_hash(file_path)},
                ),
                workspace=tmp_path,
            )

    diagnostic = exc_info.value
    assert diagnostic.error_kind == "tool_input_mismatch"
    assert diagnostic.error_details["reason"] == "unseen_range"
    assert diagnostic.error_details["path"] == "sample.txt"
    assert diagnostic.error_details["unseen_line_ranges"] == [{"start": 3, "end": 3}]
    assert "read_file" in (diagnostic.retry_guidance or "")
    assert file_path.read_text(encoding="utf-8") == "alpha\nbeta\ngamma\n"


def test_edit_tool_allows_edit_covered_by_union_of_multiple_reads(tmp_path: Path) -> None:
    file_path = tmp_path / "sample.txt"
    file_path.write_text("\n".join(f"line-{index}" for index in range(1, 7)), encoding="utf-8")
    tool = EditTool()

    with bind_runtime_tool_context(_read_lines_context(file_path, {1, 2, 3, 4, 5, 6})):
        result = tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={"path": "sample.txt", "oldString": "line-5", "newString": "FIVE", "expectedHash": _content_hash(file_path)},
            ),
            workspace=tmp_path,
        )

    assert result.status == "ok"
    assert "FIVE" in file_path.read_text(encoding="utf-8")


def test_edit_tool_rejects_replace_all_when_any_occurrence_is_unseen(tmp_path: Path) -> None:
    file_path = tmp_path / "sample.txt"
    file_path.write_text("foo\nfoo\n", encoding="utf-8")
    tool = EditTool()

    with bind_runtime_tool_context(_read_lines_context(file_path, {1})):
        with pytest.raises(ToolDiagnosticError, match="never revealed by read_file") as exc_info:
            tool.invoke(
                ToolCall(
                    tool_name="edit",
                    arguments={
                        "path": "sample.txt",
                        "oldString": "foo",
                        "newString": "bar",
                        "replaceAll": True,
                        "expectedHash": _content_hash(file_path),
                    },
                ),
                workspace=tmp_path,
            )

    diagnostic = exc_info.value
    assert diagnostic.error_kind == "tool_input_mismatch"
    assert diagnostic.error_details["reason"] == "unseen_range"
    assert diagnostic.error_details["unseen_line_ranges"] == [{"start": 2, "end": 2}]
    assert file_path.read_text(encoding="utf-8") == "foo\nfoo\n"


def test_edit_tool_strict_schema_applies_exact_match(tmp_path: Path) -> None:
    file_path = tmp_path / "test.txt"
    file_path.write_text("hello world", encoding="utf-8")

    tool = EditTool(edit_schema_resolver=lambda _model: EditSchema.STRICT)

    result = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={
                "path": "test.txt",
                "oldString": "world",
                "newString": "voidcode",
                "expectedHash": _content_hash(file_path),
            },
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert file_path.read_text(encoding="utf-8") == "hello voidcode"


def test_edit_tool_strict_schema_rejects_non_exact_input_with_ambiguous_match(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "test.txt"
    file_path.write_text("hello world", encoding="utf-8")

    tool = EditTool(edit_schema_resolver=lambda _model: EditSchema.STRICT)

    with pytest.raises(ToolDiagnosticError, match="strict edit matching") as exc_info:
        tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={
                    "path": "test.txt",
                    "oldString": "hello   world",
                    "newString": "goodbye",
                    "expectedHash": _content_hash(file_path),
                },
            ),
            workspace=tmp_path,
        )

    diagnostic = exc_info.value
    assert diagnostic.error_kind == "ambiguous_match"
    assert diagnostic.error_details["reason"] == "strict_no_exact_match"
    assert diagnostic.error_details["edit_schema"] == "strict"
    assert diagnostic.error_details["match_count"] == 0
    assert isinstance(diagnostic.retry_guidance, str)
    assert diagnostic.retry_guidance
    assert "read_file" in diagnostic.retry_guidance
    assert file_path.read_text(encoding="utf-8") == "hello world"


def test_edit_tool_strict_schema_rejects_multiple_exact_matches(tmp_path: Path) -> None:
    file_path = tmp_path / "test.txt"
    file_path.write_text("foo bar foo", encoding="utf-8")

    tool = EditTool(edit_schema_resolver=lambda _model: EditSchema.STRICT)

    with pytest.raises(ToolDiagnosticError, match="Multiple matches found") as exc_info:
        tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={
                    "path": "test.txt",
                    "oldString": "foo",
                    "newString": "qux",
                    "expectedHash": _content_hash(file_path),
                },
            ),
            workspace=tmp_path,
        )

    diagnostic = exc_info.value
    assert diagnostic.error_kind == "ambiguous_match"
    assert diagnostic.error_details["reason"] == "ambiguous_match"
    assert diagnostic.error_details["match_count"] == 2


def test_edit_tool_flexible_schema_preserves_fuzzy_matching(tmp_path: Path) -> None:
    file_path = tmp_path / "test.txt"
    file_path.write_text("hello world", encoding="utf-8")

    tool = EditTool()

    result = tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={
                "path": "test.txt",
                "oldString": "hello   world",
                "newString": "goodbye",
                "expectedHash": _content_hash(file_path),
            },
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert file_path.read_text(encoding="utf-8") == "goodbye"


def test_edit_tool_resolves_schema_from_runtime_context_model(tmp_path: Path) -> None:
    file_path = tmp_path / "test.txt"
    file_path.write_text("hello world", encoding="utf-8")

    tool = EditTool(edit_schema_resolver=lambda model: EditSchema.STRICT if model == "strict-model" else EditSchema.FLEXIBLE)

    with bind_runtime_tool_context(_read_model_context(file_path, "strict-model")):
        with pytest.raises(ToolDiagnosticError, match="strict edit matching") as exc_info:
            tool.invoke(
                ToolCall(
                    tool_name="edit",
                    arguments={
                        "path": "test.txt",
                        "oldString": "hello   world",
                        "newString": "goodbye",
                        "expectedHash": _content_hash(file_path),
                    },
                ),
                workspace=tmp_path,
            )

    assert exc_info.value.error_kind == "ambiguous_match"

    with bind_runtime_tool_context(_read_model_context(file_path, "flexible-model")):
        result = tool.invoke(
            ToolCall(
                tool_name="edit",
                arguments={
                    "path": "test.txt",
                    "oldString": "hello   world",
                    "newString": "goodbye",
                    "expectedHash": _content_hash(file_path),
                },
            ),
            workspace=tmp_path,
        )

    assert result.status == "ok"
    assert file_path.read_text(encoding="utf-8") == "goodbye"
