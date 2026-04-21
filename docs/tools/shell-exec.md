# `shell_exec`

`shell_exec` runs a command inside the current workspace. It is a high-power, high-risk tool and should not replace dedicated read/search/edit tools.

## When to use it

Good fit:

- you truly need command execution,
- you need build / test / lint / git / package-manager / script behavior,
- you need a local CLI that is already installed on the machine.

Bad fit:

- reading files (prefer `read_file`),
- finding files (prefer `list` or `glob`),
- searching file contents (prefer `grep`),
- simple file creation or editing (prefer `write_file`, `edit`, or `apply_patch`).

## Input

```json
{
  "command": "pytest tests/unit",
  "timeout": 30
}
```

## Key semantics

- the command string is parsed with `shlex.split(...)`,
- it runs through `subprocess.run(..., shell=False)`,
- default timeout is 30 seconds, maximum is 120,
- large output is truncated.

That means:

- this is **not** “send the whole string to a shell and hope for the best”,
- complex shell pipelines and shell-specific syntax are not a safe default assumption,
- agents should not treat `shell_exec` as a universal text interface.

## Return value

On success, `status` is still `ok` even if the command exits non-zero. The real fields that matter are:

- `data.exit_code`
- `data.stdout`
- `data.stderr`
- `data.timeout`
- `data.truncated`

The key rule is:

> For `shell_exec`, the agent must inspect `exit_code`. `status="ok"` only means the tool itself ran, not that the command succeeded.

## Common failures

- `shell_exec requires a string command argument`
- `shell_exec command must not be empty`
- `shell_exec command timed out after ...`
- `shell_exec failed to execute command: ...`

## Usage guidance

- Build / test / lint: appropriate
- File reading: not appropriate
- Directory discovery: not appropriate
- Content search: not appropriate
- Small edit / patch operations: not appropriate
