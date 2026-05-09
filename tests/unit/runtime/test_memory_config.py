from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from voidcode.runtime import config as runtime_config
from voidcode.runtime.config import RUNTIME_CONFIG_FILE_NAME, load_runtime_config
from voidcode.runtime.config_schema import (
    generate_starter_runtime_config,
    runtime_config_json_schema,
)


def _write_repo_config(workspace: Path, payload: dict[str, object]) -> None:
    (workspace / RUNTIME_CONFIG_FILE_NAME).write_text(json.dumps(payload), encoding="utf-8")


def test_runtime_memory_config_defaults_to_workspace_enabled_without_prompt_recall(
    tmp_path: Path,
) -> None:
    config = load_runtime_config(tmp_path, env={})

    memory = cast(Any, config).memory
    assert memory.enabled is True
    assert memory.scope == "workspace"
    assert memory.semantic_search == "auto"
    assert memory.sqlite_vec.enabled == "auto"
    assert memory.recall.enabled is False
    assert memory.recall.limit == 5
    assert memory.recall.max_chars == 2000


@pytest.mark.parametrize("semantic_search", ["off", "auto", "required"])
@pytest.mark.parametrize("sqlite_vec_enabled", ["auto", "off", "required"])
def test_runtime_memory_config_accepts_search_and_sqlite_vec_modes(
    tmp_path: Path,
    semantic_search: str,
    sqlite_vec_enabled: str,
) -> None:
    _write_repo_config(
        tmp_path,
        {
            "memory": {
                "enabled": False,
                "scope": "workspace",
                "semantic_search": semantic_search,
                "sqlite_vec": {"enabled": sqlite_vec_enabled},
                "recall": {"enabled": True, "limit": 8, "max_chars": 4096},
            }
        },
    )

    config = load_runtime_config(tmp_path, env={})

    memory = cast(Any, config).memory
    assert memory.enabled is False
    assert memory.scope == "workspace"
    assert memory.semantic_search == semantic_search
    assert memory.sqlite_vec.enabled == sqlite_vec_enabled
    assert memory.recall.enabled is True
    assert memory.recall.limit == 8
    assert memory.recall.max_chars == 4096


@pytest.mark.parametrize(
    ("memory_payload", "match"),
    [
        pytest.param(
            {"semantic_search": "always"},
            "memory.semantic_search.*off, auto, required",
            id="semantic-search-mode",
        ),
        pytest.param(
            {"sqlite_vec": {"enabled": "on"}},
            "memory.sqlite_vec.enabled.*auto, off, required",
            id="sqlite-vec-mode",
        ),
        pytest.param(
            {"scope": "user"},
            "memory.scope.*workspace",
            id="workspace-only-scope",
        ),
        pytest.param(
            {"recall": {"enabled": "yes"}},
            "memory.recall.enabled",
            id="recall-enabled-type",
        ),
        pytest.param(
            {"recall": {"limit": 0}},
            "memory.recall.limit.*greater than or equal to 1",
            id="recall-limit-minimum",
        ),
        pytest.param(
            {"recall": {"max_chars": 0}},
            "memory.recall.max_chars.*greater than or equal to 1",
            id="recall-max-chars-minimum",
        ),
    ],
)
def test_runtime_memory_config_rejects_invalid_values_with_clear_errors(
    tmp_path: Path,
    memory_payload: dict[str, object],
    match: str,
) -> None:
    _write_repo_config(tmp_path, {"memory": memory_payload})

    with pytest.raises(ValueError, match=match):
        _ = load_runtime_config(tmp_path, env={})


def test_runtime_memory_config_is_repo_local_not_user_level(tmp_path: Path) -> None:
    repo_config_keys = cast(frozenset[str], runtime_config.__dict__["_REPO_CONFIG_KEYS"])
    user_config_keys = cast(frozenset[str], runtime_config.__dict__["_USER_CONFIG_KEYS"])
    assert "memory" in repo_config_keys
    assert "memory" not in user_config_keys

    config_home = tmp_path / "config-home"
    user_config_path = config_home / "voidcode" / "config.json"
    user_config_path.parent.mkdir(parents=True)
    user_config_path.write_text(json.dumps({"memory": {"enabled": False}}), encoding="utf-8")

    with pytest.raises(ValueError, match="runtime config field 'memory' is not supported"):
        _ = load_runtime_config(tmp_path, env={"XDG_CONFIG_HOME": str(config_home)})


def test_runtime_memory_config_json_schema_documents_repo_local_memory() -> None:
    schema = runtime_config_json_schema()
    properties = cast(dict[str, object], schema["properties"])
    defs = cast(dict[str, object], schema["$defs"])

    assert properties["memory"] == {"$ref": "#/$defs/memoryConfig"}
    memory_config = cast(dict[str, object], defs["memoryConfig"])
    assert memory_config["additionalProperties"] is False
    assert memory_config["description"] == (
        "Workspace-local explicit memory storage. Prompt recall and semantic search remain opt-in. "
    )

    memory_properties = cast(dict[str, object], memory_config["properties"])
    assert memory_properties["enabled"] == {
        "type": "boolean",
        "default": True,
        "description": "Enable workspace memory storage.",
    }
    assert memory_properties["scope"] == {
        "type": "string",
        "enum": ["workspace"],
        "description": "MVP scope is workspace only.",
    }
    assert memory_properties["semantic_search"] == {
        "type": "string",
        "enum": ["off", "auto", "required"],
        "default": "auto",
        "description": (
            "Semantic search mode for memory lookup. auto defers until embeddings are available."
        ),
    }
    assert memory_properties["recall"] == {
        "$ref": "#/$defs/memoryRecallConfig",
        "description": "Optional recent-memory recall used for prompt context.",
    }
    assert memory_properties["sqlite_vec"] == {
        "$ref": "#/$defs/memorySqliteVecConfig",
        "description": "Optional sqlite-vec integration used for embeddings-backed lookup.",
    }

    recall_config = cast(dict[str, object], defs["memoryRecallConfig"])
    recall_properties = cast(dict[str, object], recall_config["properties"])
    assert recall_config["additionalProperties"] is False
    assert recall_config["description"] == (
        "Recent-memory recall for prompt context. Disabled by default."
    )
    assert recall_properties["enabled"] == {
        "type": "boolean",
        "default": False,
        "description": "Enable prompt recall from recent memories.",
    }
    assert recall_properties["limit"] == {
        "type": "integer",
        "minimum": 1,
        "default": 5,
        "description": "Maximum recalled memories.",
    }
    assert recall_properties["max_chars"] == {
        "type": "integer",
        "minimum": 1,
        "default": 2000,
        "description": "Character cap applied after recall is selected.",
    }

    sqlite_vec_config = cast(dict[str, object], defs["memorySqliteVecConfig"])
    sqlite_vec_properties = cast(dict[str, object], sqlite_vec_config["properties"])
    assert sqlite_vec_config["additionalProperties"] is False
    assert sqlite_vec_config["description"] == (
        "sqlite-vec integration for embeddings-backed memory lookup."
    )
    assert sqlite_vec_properties["enabled"] == {
        "type": "string",
        "enum": ["auto", "off", "required"],
        "default": "auto",
        "description": "Auto keeps sqlite-vec optional until embeddings are needed.",
    }


def test_starter_runtime_config_includes_memory_with_recall_disabled() -> None:
    payload = generate_starter_runtime_config(include_examples=True)

    assert payload["memory"] == {
        "enabled": True,
        "recall": {"enabled": False},
        "semantic_search": "auto",
        "sqlite_vec": {"enabled": "auto"},
    }
