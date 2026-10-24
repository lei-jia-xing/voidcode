"""The catalog is reproducible from official models.dev fields, without local overrides."""

import importlib.util
import json
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts/generate_model_catalog.py"
_spec = importlib.util.spec_from_file_location("catalog_generator", _SCRIPT)
assert _spec is not None and _spec.loader is not None
generator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(generator)


def test_official_snapshot_adds_new_models_and_preserves_unknowns(tmp_path):
    source = tmp_path / "api.json"
    output = tmp_path / "catalog.json"
    model = {
        "name": "New Model",
        "tool_call": True,
        "reasoning": True,
        "structured_output": True,
        "limit": {"context": 128000, "output": 8000},
        "cost": {"input": 2, "output": 8},
        "reasoning_options": [{"type": "effort", "values": ["low", "high"]}],
    }
    source.write_text(json.dumps({"openai": {"models": {"new-model": model}}}), encoding="utf-8")
    generator.main(["--source", str(source), "--output", str(output)])
    result = json.loads(output.read_text())["openai"]["new-model"]
    assert result["context_window"] == 128000
    assert result["cost_per_input_token"] == 2 / 1_000_000
    assert result["supports_json_mode"] is True
    assert result["supported_effort_levels"] == ["low", "high"]
    assert "cost_per_cache_read_token" not in result
    assert "default_reasoning_effort" not in result
    assert "tokenizer" not in result
    first = output.read_bytes()
    generator.main(["--source", str(source), "--output", str(output)])
    assert output.read_bytes() == first
