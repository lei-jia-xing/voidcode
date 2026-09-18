"""Generate the shipped runtime-config JSON Schema from the payload models.

Dev-time generator only; never imported by the runtime. Writes
`schema/voidcode.config.schema.json` from
`voidcode.runtime.config_schema.runtime_config_json_schema()`, which is itself
generated from the payload models in `voidcode.runtime.config_models` (plus the
provider boundary models in `voidcode.provider.config`). Adding a config field
therefore means regenerating this artifact:

    uv run python scripts/generate_config_schema.py
    uv run python scripts/generate_config_schema.py --check

`--check` exits non-zero when the checked-in artifact differs, which is what
`mise run schema:check` runs.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from voidcode.runtime.config_schema import format_runtime_config_schema_json  # noqa: E402

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "schema" / "voidcode.config.schema.json"


def main(argv: list[str]) -> int:
    schema_text = format_runtime_config_schema_json()
    if "--check" in argv:
        current = OUTPUT_PATH.read_text(encoding="utf-8") if OUTPUT_PATH.exists() else ""
        if current == schema_text:
            print(f"{OUTPUT_PATH} matches the generated runtime config schema")
            return 0
        print(
            f"{OUTPUT_PATH} is stale: run `uv run python scripts/generate_config_schema.py` to regenerate it from the payload models",
            file=sys.stderr,
        )
        return 1

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(schema_text, encoding="utf-8")
    print(f"wrote {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
