from __future__ import annotations


def extract_paths_from_patch(patch_text: str) -> tuple[str, ...]:
    paths: list[str] = []
    for line in patch_text.splitlines():
        if line.startswith("*** Add File: "):
            paths.append(line.removeprefix("*** Add File: ").strip())
        elif line.startswith("*** Update File: "):
            paths.append(line.removeprefix("*** Update File: ").strip())
        elif line.startswith("*** Delete File: "):
            paths.append(line.removeprefix("*** Delete File: ").strip())
        elif line.startswith("*** Move to: "):
            paths.append(line.removeprefix("*** Move to: ").strip())
    return tuple(path for path in paths if path)
