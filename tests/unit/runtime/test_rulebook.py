from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.runtime.context_rules import (
    build_rule_catalog,
    read_rule_uri,
    rulebook_prompt_context,
    rulebook_snapshot_from_payload,
    rulebook_snapshot_payload,
)
from voidcode.runtime.context_transforms import RuntimeContextTransformRequest, RuntimeFileRulesTransformProvider
from voidcode.tools import ReadTool, ToolCall


def _write_rules(workspace: Path) -> None:
    always = workspace / ".voidcode" / "rules" / "always" / "safety.md"
    discoverable = workspace / ".voidcode" / "rules" / "discoverable" / "deploy.md"
    always.parent.mkdir(parents=True)
    discoverable.parent.mkdir(parents=True)
    always.write_text(
        "---\n"
        "name: safety\ndescription: Safety boundary\napplication: always_apply\n"
        "scope: workspace\nprecedence: 100\n---\nNever bypass runtime policy.\n",
        encoding="utf-8",
    )
    discoverable.write_text(
        "---\nname: deploy\ndescription: Deployment notes\napplication: discoverable\nscope: repo\nprecedence: 20\n---\nDeploy only after review.\n",
        encoding="utf-8",
    )


def test_rulebook_prompt_injects_always_body_but_discoverable_metadata_only(tmp_path: Path) -> None:
    _write_rules(tmp_path)
    catalog = build_rule_catalog(tmp_path)
    prompt = rulebook_prompt_context(catalog)
    assert "Never bypass runtime policy." in prompt
    assert "deploy: Deployment notes" in prompt
    assert "Deploy only after review." not in prompt
    assert "scope=repo" in prompt
    assert "voidcode://rule/deploy" in prompt


def test_rulebook_transform_reuses_reactive_file_rule_provider(tmp_path: Path) -> None:
    _write_rules(tmp_path)
    result = RuntimeFileRulesTransformProvider().build_result(
        RuntimeContextTransformRequest(workspace=tmp_path, tool_results=(), hook_preset_context="")
    )
    contents = "\n".join(injection.content for injection in result.injections)
    assert "Never bypass runtime policy." in contents
    assert "Deploy only after review." not in contents
    assert "voidcode://rule/deploy" in contents


def test_rule_uri_success_unknown_traversal_and_bounded_read(tmp_path: Path) -> None:
    _write_rules(tmp_path)
    result = read_rule_uri("voidcode://rule/deploy", workspace=tmp_path, limit=1)
    assert result["raw_content"] == "Deploy only after review."
    assert result["content_hash"] == build_rule_catalog(tmp_path).resolve("deploy").metadata.content_hash
    with pytest.raises(ValueError, match="unknown rule"):
        read_rule_uri("voidcode://rule/missing", workspace=tmp_path)
    with pytest.raises(ValueError, match="invalid rule URI"):
        read_rule_uri("voidcode://rule/%2e%2e%2fsafety", workspace=tmp_path)
    with pytest.raises(ValueError, match="positive"):
        read_rule_uri("voidcode://rule/deploy", workspace=tmp_path, offset=0)
    with pytest.raises(ValueError, match="unsupported legacy rule URI"):
        read_rule_uri("rule://deploy", workspace=tmp_path)


def test_rulebook_snapshot_hash_and_changed_files_are_replay_stable(tmp_path: Path) -> None:
    _write_rules(tmp_path)
    first = build_rule_catalog(tmp_path).snapshot
    payload = rulebook_snapshot_payload(first)
    assert rulebook_snapshot_from_payload(payload).snapshot_hash == first.snapshot_hash
    assert payload["snapshot_version"] == 2
    with pytest.raises(ValueError, match="version must be 2"):
        rulebook_snapshot_from_payload({**payload, "snapshot_version": 1})
    (tmp_path / ".voidcode" / "rules" / "always" / "safety.md").write_text(
        "---\nname: safety\ndescription: Changed\napplication: always_apply\n---\nChanged body.\n",
        encoding="utf-8",
    )
    changed = build_rule_catalog(tmp_path)
    request = RuntimeContextTransformRequest(workspace=tmp_path, tool_results=(), hook_preset_context="", rulebook_snapshot=payload)
    result = RuntimeFileRulesTransformProvider().build_result(request)
    contents = "\n".join(injection.content for injection in result.injections)
    assert "Changed body." not in contents
    assert "deploy: Deployment notes" in contents
    assert "Deploy only after review." not in contents
    with pytest.raises(ValueError, match="hash"):
        rulebook_snapshot_from_payload({**payload, "snapshot_hash": "0" * 64})
    assert changed.snapshot.snapshot_hash != first.snapshot_hash


def test_read_tool_serves_rule_uri_and_never_falls_through_to_external_path(tmp_path: Path) -> None:
    _write_rules(tmp_path)
    result = ReadTool().invoke(ToolCall(tool_name="read", arguments={"path": "voidcode://rule/safety"}), workspace=tmp_path)
    assert result.status == "ok"
    assert result.data["type"] == "rule"
    assert "Never bypass runtime policy." in result.data["raw_content"]
    with pytest.raises(ValueError, match="unsupported legacy rule URI"):
        ReadTool().invoke(ToolCall(tool_name="read", arguments={"path": "rule://safety"}), workspace=tmp_path)
