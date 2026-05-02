# Build Verification - VoidCode Local Build System Verification Guide

Use this skill when asked to create or modify a buildable project, or when the user requests verification of a generated build system. It is a local VoidCode adaptation for ensuring that generated build configurations actually work before reporting completion.

## When to Apply

- The user asked you to create a CMake, Make, Meson, or similar build system.
- You have generated or modified build configuration files (`CMakeLists.txt`, `Makefile`, `meson.build`, `BUILD`, etc.).
- The user asked to "build", "compile", "configure", or "verify" a project.
- You are about to report task completion for a task that involves creating buildable code.

## Capability Model

- Treat build verification as a local capability. Use `shell_exec` to run build commands within the workspace.
- Do not claim that a build tool is available until you verify it exists (e.g., `which cmake` or `command -v cmake`).
- Keep build verification bounded: one configure + one build attempt is sufficient for a minimal sample.
- If the build tool is not available, state that limitation clearly and skip verification rather than silently claiming success.

## Verification Flow

### CMake Projects

1. **Check availability**: run `cmake --version` to confirm CMake is installed.
2. **Configure**: run `cmake -S <source-dir> -B <build-dir> -DCMAKE_BUILD_TYPE=Release` (or `Debug` for debug-focused samples).
3. **Fix configure errors**: if `cmake` reports errors, read the error output, fix `CMakeLists.txt`, and retry configure. Common issues:
   - Typos in command names (e.g., `target_add_dependencies` → `target_link_libraries` or `target_include_directories`).
   - Missing `find_package` calls for required dependencies.
   - Incorrect variable names or target references.
4. **Build**: run `cmake --build <build-dir>` after successful configure.
5. **Fix build errors**: if compilation fails, read the compiler output, fix source or CMake files, and retry.
6. **Report**: include the configure and build commands run, their output (or a summary), and whether they succeeded.

### Make Projects

1. **Check availability**: run `make --version` or `command -v make`.
2. **Build**: run `make` in the project directory.
3. **Fix errors**: read compiler/linker output, fix source or Makefile, and retry.
4. **Report**: include the command run and result.

### Other Build Systems

Apply the same pattern: check tool availability, run the configure/build step, fix errors, and report evidence.

## Guardrails

- Do not install system packages unless the user explicitly asked.
- Do not run build commands outside the workspace directory.
- Do not claim "completed" if verification was skipped without stating why.
- If verification fails and you cannot fix it after reasonable attempts, report the failure and the remaining issues clearly.
- Keep the verification scope minimal: configure + build is sufficient. Running the resulting binary is optional and only when relevant.

## Output Format

When verification is performed, include a summary in your final response:

```text
<build-verification>
Tool: cmake/make/etc.
Configure: <command> — success/failure
Build: <command> — success/failure
Notes: any issues found and fixed, or reason for skipping
</build-verification>
```

If no build tool was available or the task did not involve a build system, state:

```text
<build-verification>
Skipped: <reason>
</build-verification>
```
