"""Compaction plugin: window projection over the core's verified seam."""

from collections.abc import Mapping
from functools import partial

from voidcode.sdk.plugin import Plugin, StateSchema

from .compaction_plugin import SCHEMA_VERSION, CompactCommand, compaction_observer, parse_compaction_config, prepare_compaction_deactivation


def create_plugin(config: Mapping[str, object] | None = None) -> Plugin:
    resolved = parse_compaction_config(config)
    return Plugin(
        commands=(CompactCommand(resolved),),
        observer=partial(compaction_observer, config=resolved),
        observed_facts=("usage", "run_finished"),
        state=StateSchema(
            version=SCHEMA_VERSION,
            schema={
                "type": "object",
                "required": ["schema_version", "cursor_sequence", "last_projection"],
                "additionalProperties": False,
                "properties": {
                    "schema_version": {"type": "integer", "enum": [SCHEMA_VERSION]},
                    "cursor_sequence": {"type": "integer", "minimum": 0},
                    "last_projection": {
                        "type": ["object", "null"],
                        "required": ["covered_from_sequence", "covered_to_sequence", "covered_bytes", "dropped_bytes", "summary_kind"],
                        "additionalProperties": False,
                        "properties": {
                            "covered_from_sequence": {"type": "integer", "minimum": 0},
                            "covered_to_sequence": {"type": "integer", "minimum": 0},
                            "covered_bytes": {"type": "integer", "minimum": 0},
                            "dropped_bytes": {"type": "integer", "minimum": 0},
                            "summary_kind": {"type": "string", "enum": ["deterministic", "model"]},
                        },
                    },
                },
            },
        ),
        prepare_deactivate=prepare_compaction_deactivation,
    )
