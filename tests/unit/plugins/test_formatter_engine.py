"""Behavior tests for the ported formatter plugin (presets + subprocess chain)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from voidcode.plugins.formatter import (
    FORMATTER_TIMEOUT_SECONDS,
    FormatterExecutor,
    FormatterRuntimeConfig,
    RuntimeFormatterPresetConfig,
    apply_formatter_overrides,
    default_formatter_presets,
    formatter_diagnostics,
    formatter_payload,
    resolve_formatter_preset,
)

MISSING_BINARY = "voidcode-definitely-missing-formatter"


def _python(code: str) -> tuple[str, ...]:
    return (sys.executable, "-c", code)


def _config(
    presets: dict[str, RuntimeFormatterPresetConfig],
    *,
    enabled: bool | None = True,
    format_on_write: bool = True,
) -> FormatterRuntimeConfig:
    return FormatterRuntimeConfig(enabled=enabled, format_on_write=format_on_write, formatter_presets=presets)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("main.py", "python"),
        ("stubs.pyi", "python"),
        ("a.tsx", "typescript"),
        ("a.mts", "typescript"),
        ("a.cjs", "javascript"),
        ("package.json", "json"),
        ("tsconfig.jsonc", "json"),
        ("README.md", "markdown"),
        ("page.mdx", "markdown"),
        ("config.yml", "yaml"),
        ("index.htm", "html"),
        ("app.css", "css"),
        ("app.scss", "scss"),
        ("app.less", "less"),
        ("App.vue", "vue"),
        ("App.svelte", "svelte"),
        ("Page.astro", "astro"),
        ("schema.gql", "graphql"),
        ("view.hbs", "handlebars"),
        ("view.handlebars", "handlebars"),
        ("Cargo.toml", "toml"),
        ("run.zsh", "shell"),
        ("Dockerfile", "dockerfile"),
        ("flake.nix", "nix"),
        ("schema.sql", "sql"),
        ("lib.rs", "rust"),
        ("main.go", "go"),
        ("lib.h", "c"),
        ("lib.hpp", "cpp"),
        ("lib.cxx", "cpp"),
        ("Main.java", "java"),
        ("Main.kts", "kotlin"),
        ("feed.xml", "xml"),
    ],
)
def test_default_presets_resolve_declared_languages(name: str, expected: str) -> None:
    resolved = resolve_formatter_preset(default_formatter_presets(), Path("/tmp") / name)
    assert resolved is not None
    assert resolved[0] == expected


@pytest.mark.parametrize("name", ["notes.txt", "archive.tar.gz", "Makefile"])
def test_default_presets_ignore_unknown_suffixes(name: str) -> None:
    assert resolve_formatter_preset(default_formatter_presets(), Path("/tmp") / name) is None


def test_resolution_follows_mapping_order_not_extension_specificity() -> None:
    broad = RuntimeFormatterPresetConfig(command=("broad",), extensions=(".foo",))
    narrow = RuntimeFormatterPresetConfig(command=("narrow",), extensions=("special.foo",))
    target = Path("/tmp/special.foo")

    first, _ = resolve_formatter_preset({"first": broad, "second": narrow}, target) or ("", None)
    assert first == "first"

    swapped, _ = resolve_formatter_preset({"first": narrow, "second": broad}, target) or ("", None)
    assert swapped == "first"


def test_matching_is_case_insensitive_on_the_file_name() -> None:
    presets = {"dockerfile": RuntimeFormatterPresetConfig(command=("dockerfmt", "--write"), extensions=("Dockerfile",))}
    assert resolve_formatter_preset(presets, Path("/tmp/dockerfile")) is not None


def test_unconfigured_suffix_yields_not_configured(tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("x", encoding="utf-8")
    executor = FormatterExecutor(_config({"python": RuntimeFormatterPresetConfig(command=(MISSING_BINARY,), extensions=(".py",))}), tmp_path)

    result = executor.run(target)

    assert result.status == "not_configured"
    assert result.command is None
    assert result.error is None
    assert formatter_diagnostics(result) == []


def test_disabled_and_opt_out_configs_skip_execution(tmp_path: Path) -> None:
    marker = tmp_path / "ran.txt"
    target = tmp_path / "code.py"
    target.write_text("x", encoding="utf-8")
    presets = {
        "python": RuntimeFormatterPresetConfig(
            command=_python(f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ran')"),
            extensions=(".py",),
        )
    }

    for config in (
        _config(presets, enabled=False, format_on_write=True),
        _config(presets, enabled=None, format_on_write=False),
    ):
        result = FormatterExecutor(config, tmp_path).run(target)
        assert result.status == "not_configured"
        assert not marker.exists()


def test_enabled_alias_arms_formatting_and_explicit_format_on_write_wins() -> None:
    base = FormatterRuntimeConfig(format_on_write=False)
    assert apply_formatter_overrides(base, enabled=True).format_on_write is True
    assert apply_formatter_overrides(base, enabled=True, format_on_write=False).format_on_write is False
    assert apply_formatter_overrides(base, format_on_write=True).format_on_write is True

    override = RuntimeFormatterPresetConfig(command=("taplo", "fmt"), extensions=(".toml",))
    merged = apply_formatter_overrides(base, languages={"toml": override})
    assert merged.formatter_presets["toml"] is override
    assert merged.formatter_presets["python"].command == ("ruff", "format")


def test_missing_binary_falls_through_to_the_next_candidate(tmp_path: Path) -> None:
    target = tmp_path / "code.py"
    target.write_text("x", encoding="utf-8")
    primary = (MISSING_BINARY, "--write")
    fallback = _python("import sys; sys.exit(0)")
    presets = {
        "python": RuntimeFormatterPresetConfig(command=primary, extensions=(".py",), fallback_commands=(fallback,)),
    }

    result = FormatterExecutor(_config(presets), tmp_path).run(target)

    assert result.status == "formatted"
    assert result.language == "python"
    assert result.command == (*fallback, str(target))
    assert result.attempted_commands == ((*primary, str(target)), (*fallback, str(target)))


def test_first_success_wins_and_later_candidates_never_run(tmp_path: Path) -> None:
    marker = tmp_path / "later-ran.txt"
    target = tmp_path / "code.py"
    target.write_text("x", encoding="utf-8")
    winning = _python("import sys; sys.exit(0)")
    presets = {
        "python": RuntimeFormatterPresetConfig(
            command=winning,
            extensions=(".py",),
            fallback_commands=(_python(f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ran')"),),
        )
    }

    result = FormatterExecutor(_config(presets), tmp_path).run(target)

    assert result.status == "formatted"
    assert result.command == (*winning, str(target))
    assert result.attempted_commands == ((*winning, str(target)),)
    assert not marker.exists()


def test_first_non_zero_exit_falls_through_to_a_succeeding_candidate(tmp_path: Path) -> None:
    target = tmp_path / "code.py"
    target.write_text("x", encoding="utf-8")
    failing = _python("import sys; print('boom', file=sys.stderr); sys.exit(3)")
    succeeding = _python("import sys; sys.exit(0)")
    presets = {
        "python": RuntimeFormatterPresetConfig(command=failing, extensions=(".py",), fallback_commands=(succeeding,)),
    }

    result = FormatterExecutor(_config(presets), tmp_path).run(target)

    assert result.status == "formatted"
    assert result.command == (*succeeding, str(target))
    assert result.attempted_commands == ((*failing, str(target)), (*succeeding, str(target)))


def test_non_zero_failure_returns_warning_payload_instead_of_raising(tmp_path: Path) -> None:
    target = tmp_path / "code.py"
    target.write_text("x", encoding="utf-8")
    first = _python("import sys; print('first boom', file=sys.stderr); sys.exit(3)")
    second = _python("import sys; print('second boom', file=sys.stderr); sys.exit(2)")
    presets = {"python": RuntimeFormatterPresetConfig(command=first, extensions=(".py",), fallback_commands=(second,))}

    result = FormatterExecutor(_config(presets), tmp_path).run(target)

    assert result.status == "failed"
    assert result.command == (*second, str(target))
    assert result.cwd == tmp_path.resolve()
    assert result.stderr == "second boom\n"
    assert result.stdout == ""
    assert result.error == (f"Format failed for code.py using preset 'python' from {tmp_path.resolve()}: second boom")

    payload = formatter_payload(result)
    assert payload["status"] == "failed"
    assert payload["language"] == "python"
    assert payload["cwd"] == str(tmp_path.resolve())
    assert payload["command"] == [*second, str(target)]
    assert payload["attempted_commands"] == [list((*first, str(target))), list((*second, str(target)))]
    assert payload["error"] == result.error

    diagnostics = formatter_diagnostics(result)
    assert diagnostics == [
        {
            "source": "formatter",
            "severity": "warning",
            "message": result.error,
            "language": "python",
            "cwd": str(tmp_path.resolve()),
            "command": [*second, str(target)],
            "attempted_commands": [
                [*first, str(target)],
                [*second, str(target)],
            ],
        }
    ]
    assert formatter_diagnostics(None) == []


def test_failure_uses_stdout_when_stderr_is_empty(tmp_path: Path) -> None:
    target = tmp_path / "code.py"
    target.write_text("x", encoding="utf-8")
    presets = {
        "python": RuntimeFormatterPresetConfig(command=_python("import sys; print('only stdout'); sys.exit(1)"), extensions=(".py",)),
    }

    result = FormatterExecutor(_config(presets), tmp_path).run(target)

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.endswith(": only stdout")


def test_failure_with_no_output_uses_the_generic_reason(tmp_path: Path) -> None:
    target = tmp_path / "code.py"
    target.write_text("x", encoding="utf-8")
    presets = {"python": RuntimeFormatterPresetConfig(command=_python("import sys; sys.exit(1)"), extensions=(".py",))}

    result = FormatterExecutor(_config(presets), tmp_path).run(target)

    assert result.error is not None
    assert result.error.endswith(": formatter exited with a non-zero status")


def test_failure_message_truncates_stderr_to_300_characters(tmp_path: Path) -> None:
    target = tmp_path / "code.py"
    target.write_text("x", encoding="utf-8")
    noisy = _python("import sys; sys.stderr.write('x' * 400); sys.exit(1)")
    presets = {"python": RuntimeFormatterPresetConfig(command=noisy, extensions=(".py",))}

    result = FormatterExecutor(_config(presets), tmp_path).run(target)

    assert result.stderr == "x" * 400
    assert result.error is not None
    detail = result.error.split(f"using preset 'python' from {tmp_path.resolve()}: ", 1)[1]
    assert detail == "x" * 300


def test_non_enoent_oserror_is_recorded_as_a_failed_attempt(tmp_path: Path) -> None:
    target = tmp_path / "code.py"
    target.write_text("x", encoding="utf-8")
    not_executable = tmp_path / "shim"
    not_executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    presets = {"python": RuntimeFormatterPresetConfig(command=(str(not_executable),), extensions=(".py",))}

    result = FormatterExecutor(_config(presets), tmp_path).run(target)

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.startswith(f"Format failed for code.py using preset 'python' from {tmp_path.resolve()}: ")


def test_all_candidates_missing_yields_missing_executable(tmp_path: Path) -> None:
    target = tmp_path / "code.py"
    target.write_text("x", encoding="utf-8")
    presets = {
        "python": RuntimeFormatterPresetConfig(
            command=(MISSING_BINARY, "--write"),
            extensions=(".py",),
            fallback_commands=(("voidcode-also-missing",), (MISSING_BINARY, "-i")),
        ),
    }

    result = FormatterExecutor(_config(presets), tmp_path).run(target)

    assert result.status == "missing_executable"
    assert result.command is None
    assert result.stdout is None
    assert result.error == (
        f"No formatter executable was available for preset 'python'. Tried: {MISSING_BINARY}, voidcode-also-missing. "
        f"Install one of them or override hooks.formatter_presets.python.command in .voidcode.json."
    )
    assert formatter_diagnostics(result)[0]["severity"] == "warning"


def test_timeout_is_reported_as_failure_with_timeout_message(tmp_path: Path) -> None:
    target = tmp_path / "code.py"
    target.write_text("x", encoding="utf-8")
    slow = _python("import time; time.sleep(10)")
    presets = {"python": RuntimeFormatterPresetConfig(command=slow, extensions=(".py",))}

    result = FormatterExecutor(_config(presets), tmp_path, timeout_seconds=0.2).run(target)

    assert result.status == "failed"
    assert result.command == (*slow, str(target))
    assert result.attempted_commands == ((*slow, str(target)),)
    assert result.stderr == "formatter timed out after 0.2s"
    assert result.error is not None
    assert result.error.endswith(": formatter timed out after 0.2s")
    assert formatter_diagnostics(result)[0]["message"] == result.error


def test_timeout_beats_a_later_missing_binary_candidate(tmp_path: Path) -> None:
    target = tmp_path / "code.py"
    target.write_text("x", encoding="utf-8")
    slow = _python("import time; time.sleep(10)")
    presets = {
        "python": RuntimeFormatterPresetConfig(command=slow, extensions=(".py",), fallback_commands=((MISSING_BINARY,),)),
    }

    result = FormatterExecutor(_config(presets), tmp_path, timeout_seconds=0.2).run(target)

    assert result.status == "failed"
    assert result.stderr == "formatter timed out after 0.2s"
    assert len(result.attempted_commands) == 2


def test_default_timeout_matches_the_module_constant() -> None:
    assert FORMATTER_TIMEOUT_SECONDS == 30.0


def test_workspace_cwd_policy_runs_the_formatter_at_the_workspace_root(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    nested = workspace / "src" / "deep"
    nested.mkdir(parents=True)
    marker = tmp_path / "cwd.txt"
    target = nested / "code.py"
    target.write_text("x", encoding="utf-8")
    presets = {
        "python": RuntimeFormatterPresetConfig(
            command=_python(f"import os, pathlib; pathlib.Path({str(marker)!r}).write_text(os.getcwd())"),
            extensions=(".py",),
            root_markers=("pyproject.toml",),
            cwd_policy="workspace",
        ),
    }
    (workspace / "pyproject.toml").write_text("", encoding="utf-8")

    result = FormatterExecutor(_config(presets), workspace).run(target)

    assert result.status == "formatted"
    assert result.cwd == workspace.resolve()
    assert Path(marker.read_text(encoding="utf-8")) == workspace.resolve()


def test_file_directory_cwd_policy_runs_the_formatter_beside_the_file(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    nested = workspace / "src" / "deep"
    nested.mkdir(parents=True)
    marker = tmp_path / "cwd.txt"
    target = nested / "code.py"
    target.write_text("x", encoding="utf-8")
    presets = {
        "python": RuntimeFormatterPresetConfig(
            command=_python(f"import os, pathlib; pathlib.Path({str(marker)!r}).write_text(os.getcwd())"),
            extensions=(".py",),
            root_markers=("pyproject.toml",),
            cwd_policy="file_directory",
        ),
    }

    result = FormatterExecutor(_config(presets), workspace).run(target)

    assert result.cwd == nested
    assert Path(marker.read_text(encoding="utf-8")) == nested


def test_nearest_root_policy_walks_up_to_the_closest_marker(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    deep = workspace / "packages" / "app" / "src"
    deep.mkdir(parents=True)
    (workspace / "pyproject.toml").write_text("", encoding="utf-8")
    (workspace / "packages" / "app" / "ruff.toml").write_text("", encoding="utf-8")
    target = deep / "code.py"
    target.write_text("x", encoding="utf-8")
    presets = {
        "python": RuntimeFormatterPresetConfig(
            command=_python("import sys; sys.exit(0)"),
            extensions=(".py",),
            root_markers=("pyproject.toml", "ruff.toml"),
            cwd_policy="nearest_root",
        ),
    }

    result = FormatterExecutor(_config(presets), workspace).run(target)

    assert result.status == "formatted"
    assert result.cwd == workspace / "packages" / "app"


def test_nearest_root_policy_uses_the_marker_in_the_file_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    nested = workspace / "src"
    nested.mkdir(parents=True)
    (nested / "pyproject.toml").write_text("", encoding="utf-8")
    target = nested / "code.py"
    target.write_text("x", encoding="utf-8")
    presets = {
        "python": RuntimeFormatterPresetConfig(
            command=_python("import sys; sys.exit(0)"),
            extensions=(".py",),
            root_markers=("pyproject.toml",),
            cwd_policy="nearest_root",
        ),
    }

    assert FormatterExecutor(_config(presets), workspace).run(target).cwd == nested


def test_nearest_root_policy_falls_back_to_workspace_without_markers(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    nested = workspace / "src"
    nested.mkdir(parents=True)
    target = nested / "code.py"
    target.write_text("x", encoding="utf-8")
    presets = {
        "python": RuntimeFormatterPresetConfig(
            command=_python("import sys; sys.exit(0)"),
            extensions=(".py",),
            root_markers=(),
            cwd_policy="nearest_root",
        ),
        "rust": RuntimeFormatterPresetConfig(
            command=_python("import sys; sys.exit(0)"),
            extensions=(".rs",),
            root_markers=("Cargo.toml",),
            cwd_policy="nearest_root",
        ),
    }
    rust_target = nested / "lib.rs"
    rust_target.write_text("x", encoding="utf-8")

    executor = FormatterExecutor(_config(presets), workspace)

    assert executor.run(target).cwd == workspace.resolve()
    assert executor.run(rust_target).cwd == workspace.resolve()


def test_nearest_root_stops_at_workspace_boundary(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    (workspace / "pyproject.toml").write_text("", encoding="utf-8")
    target = outside / "code.py"
    target.write_text("x", encoding="utf-8")
    presets = {
        "python": RuntimeFormatterPresetConfig(
            command=_python("import sys; sys.exit(0)"),
            extensions=(".py",),
            root_markers=("pyproject.toml",),
            cwd_policy="nearest_root",
        ),
    }

    result = FormatterExecutor(_config(presets), workspace).run(target)

    assert result.status == "formatted"
    assert result.cwd == workspace.resolve()
    assert result.command is not None
    assert result.command[-1] == str(target)


def test_executor_never_raises_for_a_missing_file_argument(tmp_path: Path) -> None:
    workspace = tmp_path
    (workspace / "pyproject.toml").write_text("", encoding="utf-8")
    missing_target = workspace / "src" / "ghost.py"
    presets = {
        "python": RuntimeFormatterPresetConfig(command=_python("import sys; sys.exit(0)"), extensions=(".py",), root_markers=("pyproject.toml",)),
    }

    result = FormatterExecutor(_config(presets), workspace).run(missing_target)

    assert result.status == "formatted"
    assert result.path == missing_target


def test_env_shim_command_is_executed_with_the_file_argument(tmp_path: Path) -> None:
    workspace = tmp_path
    shim = workspace / "fmt-shim.sh"
    shim.write_text('#!/bin/sh\nprintf "%s" "$1" > "$(dirname "$1")/shim-arg.txt"\n', encoding="utf-8")
    os.chmod(shim, 0o755)
    target = workspace / "code.py"
    target.write_text("x", encoding="utf-8")
    presets = {"python": RuntimeFormatterPresetConfig(command=(str(shim),), extensions=(".py",), cwd_policy="workspace")}

    result = FormatterExecutor(_config(presets), workspace).run(target)

    assert result.status == "formatted"
    assert result.command == (str(shim), str(target))
    assert (workspace / "shim-arg.txt").read_text(encoding="utf-8") == str(target)


def test_payload_omits_unset_optional_fields(tmp_path: Path) -> None:
    result = FormatterExecutor(_config({}), tmp_path).run(tmp_path / "code.py")

    assert formatter_payload(result) == {"status": "not_configured"}
    assert formatter_diagnostics(result) == []
