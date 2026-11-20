"""Per-dispatch model projection headroom: adaptive shrink under a fixed ceiling.

Acceptance A-G: a tight-window model gets smaller selected pages with intact
result URIs (A); unknown limits (B), unknown usage (C), and image-carrying
windows (D) keep the exact current bytes; committed-history rate pairing is
same-model only and skips image-polluted dispatches (E, F); a budget below the
identity minimum keeps the ceiling rendering per tool without refusing the
dispatch (G).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from voidcode.bootstrap import bundled_sources, open_core
from voidcode.capabilities.providers.dispatch import build_registered_providers
from voidcode.capabilities.providers.native.anthropic_native import AnthropicMessagesProvider
from voidcode.capabilities.providers.native.google_native import GoogleGenAIProvider
from voidcode.capabilities.providers.native.model_catalog import catalog_model_limits
from voidcode.capabilities.providers.native.model_routing import RoutedTurnProvider, WireRouting
from voidcode.capabilities.providers.native.openai_native import OpenAIChatCompletionsProvider
from voidcode.core.coverage import (
    allocate_tool_text_budget,
    effective_input_limit_tokens,
    measured_tokens_per_character,
    project_tool_result,
    read_artifact_content,
)
from voidcode.core.engine import Core, _Cursor, _Execution
from voidcode.core.storage.store import StoredEvent
from voidcode.sdk.codec import decode_tool_result, encode_provider_result
from voidcode.sdk.hooks import RequestWindow, WindowSegment
from voidcode.sdk.provider import ModelContextLimits, ProviderTokenUsage, ProviderTurnResult
from voidcode.sdk.tools import TOOL_OUTPUT_BYTES
from voidcode.sdk.transcript import TranscriptDispatch, decode_transcript_content, encode_transcript_content

PLUGIN_ID = "headroom-fixture"
PROVIDER_NAME = "headroom"
MODEL = f"{PROVIDER_NAME}/tight"

_FIXTURE_MODULE = '''"""Test-only headroom provider + tools (evidence file, no network)."""

from __future__ import annotations

import json
from pathlib import Path

from voidcode.sdk import (
    AttachmentOutput,
    ModelContextLimits,
    Plugin,
    ProviderTokenUsage,
    ProviderTurnResult,
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolSuccess,
)


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class _PlainProvider:
    name = "headroom"

    def __init__(self, evidence, script):
        self._evidence = evidence
        self._script = script
        self._turns = 0

    def propose_turn(self, request):
        record = {
            "model": request.model_name,
            "messages": [
                {
                    "role": message.role,
                    "content": message.content,
                    "tool_call_id": message.tool_call_id,
                    "images": [image.data for image in message.images],
                    "tool_calls": [
                        {"tool_name": call.tool_name, "tool_call_id": call.tool_call_id, "arguments": dict(call.arguments)}
                        for call in message.tool_calls
                    ],
                }
                for message in request.messages
            ],
            "tools": [tool.name for tool in request.available_tools],
        }
        with open(self._evidence, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\\n")
        script = _read_json(self._script)
        if self._turns >= len(script):
            raise AssertionError(f"unexpected dispatch {self._turns}")
        reply = script[self._turns]
        self._turns += 1
        calls = tuple(
            ToolCall(item["tool_name"], item.get("arguments", {}), item["tool_call_id"]) for item in reply.get("tool_calls", [])
        )
        usage = reply.get("usage")
        return ProviderTurnResult(
            tool_calls=calls,
            output=reply.get("output"),
            usage=ProviderTokenUsage(input_tokens=usage["input_tokens"], output_tokens=usage.get("output_tokens"))
            if usage is not None
            else None,
            done_reason="tool_calls" if calls else "stop",
            finish_reason_reported=True,
        )


class _LimitsProvider(_PlainProvider):
    def __init__(self, evidence, script, limits_file):
        super().__init__(evidence, script)
        self._limits_file = limits_file

    def model_limits(self, model_name):
        raw = _read_json(self._limits_file)
        if raw is None:
            return None
        return ModelContextLimits(
            context_window=raw.get("context_window"),
            max_input_tokens=raw.get("max_input_tokens"),
            max_output_tokens=raw.get("max_output_tokens"),
        )


class _BigText:
    definition = ToolDefinition("bigtext", "Return size filler bytes of model text")

    def invoke(self, call, *, context, services):
        return ToolSuccess(call.tool_name, TextOutput("B" * int(call.arguments.get("size", 60000))))


class _Shot:
    definition = ToolDefinition("shot", "Return one inline test image")

    def __init__(self, data):
        self._data = data

    def invoke(self, call, *, context, services):
        return ToolSuccess(call.tool_name, AttachmentOutput("image/png", f"data:image/png;base64,{self._data}"))


def create_plugin(config=None):
    cfg = dict(config or {})
    if cfg.get("advertise_limits", True):
        provider = _LimitsProvider(cfg["evidence"], cfg["script"], cfg["limits_file"])
    else:
        provider = _PlainProvider(cfg["evidence"], cfg["script"])
    return Plugin(providers=(provider,), tools=(_BigText(), _Shot(cfg["image_data"])))
'''


def _write_fixture(source: Path) -> Path:
    source.mkdir(parents=True, exist_ok=True)
    (source / "__init__.py").write_text(_FIXTURE_MODULE, encoding="utf-8")
    return source


def _configure(
    root: Path,
    source: Path,
    *,
    script: list[dict],
    limits: dict | None,
    advertise_limits: bool = True,
    image_data: str = "",
) -> tuple[Path, Path, Path]:
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    evidence = root / "evidence.jsonl"
    evidence.write_text("", encoding="utf-8")
    script_path = root / "script.json"
    script_path.write_text(json.dumps(script), encoding="utf-8")
    limits_path = root / "limits.json"
    limits_path.write_text(json.dumps(limits), encoding="utf-8")
    (workspace / "voidcode.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model": {"model": MODEL},
                "policy": {"approval_mode": "yolo"},
                "providers": {PROVIDER_NAME: {"api_key": "headroom-fixture-not-a-secret", "base_url": "http://127.0.0.1:1/"}},
                "plugins": {
                    **{plugin_id: {"enabled": False} for plugin_id in bundled_sources()},
                    PLUGIN_ID: {
                        "path": str(source),
                        "config": {
                            "evidence": str(evidence),
                            "script": str(script_path),
                            "limits_file": str(limits_path),
                            "advertise_limits": advertise_limits,
                            "image_data": image_data,
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return workspace, evidence, limits_path


def _evidence_turns(evidence: Path) -> list[dict]:
    return [json.loads(line) for line in evidence.read_text(encoding="utf-8").splitlines() if line.strip()]


def _intents(core: Core, session_id: str) -> list:
    return [event for event in core.store.events(session_id) if event.event_type == "core.provider_intent"]


def _tool_call_reply(tool_name: str, call_id: str, arguments: dict, input_tokens: int | None) -> dict:
    reply: dict = {"tool_calls": [{"tool_name": tool_name, "tool_call_id": call_id, "arguments": arguments}]}
    if input_tokens is not None:
        reply["usage"] = {"input_tokens": input_tokens, "output_tokens": 10}
    return reply


def _output_reply(text: str, input_tokens: int | None) -> dict:
    reply: dict = {"output": text}
    if input_tokens is not None:
        reply["usage"] = {"input_tokens": input_tokens, "output_tokens": 10}
    return reply


# --- Unit: effective input law ---


def test_effective_input_limit_prefers_max_input_and_derives_window_minus_output() -> None:
    assert effective_input_limit_tokens(None) == (None, None)
    assert effective_input_limit_tokens(ModelContextLimits()) == (None, None)
    assert effective_input_limit_tokens(ModelContextLimits(context_window=10000, max_output_tokens=2000)) == (
        8000,
        "context_window_minus_max_output",
    )
    # The input law wins over the derived window remainder when both are advertised.
    assert effective_input_limit_tokens(ModelContextLimits(context_window=10000, max_input_tokens=5000, max_output_tokens=2000)) == (
        5000,
        "max_input_tokens",
    )
    # A window without an advertised output maximum leaves the reserve unknowable.
    assert effective_input_limit_tokens(ModelContextLimits(context_window=10000)) == (None, None)
    assert effective_input_limit_tokens(ModelContextLimits(max_output_tokens=2000)) == (None, None)
    # A degenerate remainder is unknown, never zero or negative.
    assert effective_input_limit_tokens(ModelContextLimits(context_window=2000, max_output_tokens=2000)) == (None, None)
    assert effective_input_limit_tokens(ModelContextLimits(context_window=1000, max_output_tokens=2000)) == (None, None)


# --- Unit: allocation matrix ---


def test_allocate_tool_text_budget_unknown_matrix() -> None:
    limits = ModelContextLimits(context_window=10000, max_output_tokens=2000)
    base = {"tool_count": 1, "fixed_characters": 100, "tool_schema_characters": 50, "ceiling_bytes": 51200}
    assert allocate_tool_text_budget(**base, images_present=False, limits=limits, rate_tokens_per_char=0.25).policy == "adaptive"
    assert (
        allocate_tool_text_budget(**{**base, "tool_count": 0}, images_present=False, limits=limits, rate_tokens_per_char=0.25).reason
        == "no_shrinkable"
    )
    assert allocate_tool_text_budget(**base, images_present=True, limits=limits, rate_tokens_per_char=0.25).reason == "images_present"
    assert allocate_tool_text_budget(**base, images_present=False, limits=None, rate_tokens_per_char=0.25).reason == "unknown_limits"
    assert (
        allocate_tool_text_budget(**base, images_present=False, limits=ModelContextLimits(context_window=10000), rate_tokens_per_char=0.25).reason
        == "unknown_limits"
    )
    for bad_rate in (None, 0, 0.0, -1.0, float("nan"), float("inf"), True, "0.25"):
        allocation = allocate_tool_text_budget(**base, images_present=False, limits=limits, rate_tokens_per_char=bad_rate)
        assert allocation.policy == "fixed" and allocation.reason == "unknown_usage", bad_rate
        assert allocation.per_tool_max_bytes is None


def test_allocate_tool_text_budget_math_and_ceiling() -> None:
    limits = ModelContextLimits(max_input_tokens=8000)
    # 8000 tokens at 0.25/char = 32000 chars; minus 1000 fixed and 500 schemas = 30500 over 2 tools.
    allocation = allocate_tool_text_budget(
        tool_count=2,
        fixed_characters=1000,
        tool_schema_characters=500,
        images_present=False,
        limits=limits,
        rate_tokens_per_char=0.25,
        ceiling_bytes=51200,
    )
    assert allocation.policy == "adaptive" and allocation.reason == "ok"
    assert allocation.per_tool_max_bytes == 15250
    assert allocation.limit_tokens == 8000 and allocation.limit_source == "max_input_tokens"
    # A share at or above the ceiling keeps the fixed host policy: shrink-only, never growth.
    assert (
        allocate_tool_text_budget(
            tool_count=1,
            fixed_characters=100,
            tool_schema_characters=0,
            images_present=False,
            limits=ModelContextLimits(max_input_tokens=100000),
            rate_tokens_per_char=0.25,
            ceiling_bytes=51200,
        ).reason
        == "within_ceiling"
    )
    # Less than one character per tool cannot even be attempted.
    assert (
        allocate_tool_text_budget(
            tool_count=2,
            fixed_characters=7999,
            tool_schema_characters=0,
            images_present=False,
            limits=limits,
            rate_tokens_per_char=1.0,
            ceiling_bytes=51200,
        ).reason
        == "insufficient_budget"
    )


def test_allocate_tool_text_budget_rejects_degenerate_inputs() -> None:
    for field in ("tool_count", "fixed_characters", "tool_schema_characters"):
        with_weights = {"tool_count": 1, "fixed_characters": 0, "tool_schema_characters": 0, field: -1}
        try:
            allocate_tool_text_budget(**with_weights, images_present=False, limits=None, rate_tokens_per_char=None, ceiling_bytes=51200)
        except ValueError:
            pass
        else:
            raise AssertionError(field)
    try:
        allocate_tool_text_budget(
            tool_count=1,
            fixed_characters=0,
            tool_schema_characters=0,
            images_present=False,
            limits=None,
            rate_tokens_per_char=None,
            ceiling_bytes=0,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("ceiling_bytes")


# --- Unit: committed-history rate pairing (E, F) ---


def _stored(sequence: int, event_type: str, payload: dict) -> StoredEvent:
    return StoredEvent(
        session_id="sess",
        sequence=sequence,
        run_id="run",
        event_type=event_type,
        source="core",
        actor="core",
        revision_id="rev",
        payload=payload,
    )


def _intent(sequence: int, provider: str, model: str, window: RequestWindow) -> StoredEvent:
    return _stored(sequence, "core.provider_intent", {"provider": provider, "model": model, "request_window": window.as_payload()})


def _turn(sequence: int, provider: str, model: str, dispatch: int, input_tokens: int | None) -> StoredEvent:
    result = ProviderTurnResult(output="done", usage=None if input_tokens is None else ProviderTokenUsage(input_tokens=input_tokens))
    return _stored(
        sequence,
        "core.model_turn",
        {"provider": provider, "model": model, "dispatch_sequence": dispatch, "result": encode_provider_result(result)},
    )


def _window(characters: int, *, images: int | None = 0) -> RequestWindow:
    segment = {"role": "user", "characters": characters, "sha256": "0" * 64, "tool_calls": 0, "tool_name": None, "reasoning_characters": 0}
    if images is not None:
        segment["image_count"] = images
        segment["image_bytes"] = images * 100
    payload = {"message_characters": characters, "tool_schema_characters": 0, "tool_count": 0, "segments": [segment]}
    return RequestWindow.from_payload(payload)


def test_rate_pairs_newest_same_model_dispatch_and_skips_other_models() -> None:
    events = [
        _intent(1, "headroom", "tight", _window(1000)),
        _turn(2, "headroom", "tight", 1, 250),
        _intent(3, "headroom", "other", _window(1000)),
        _turn(4, "headroom", "other", 3, 500),
        _intent(5, "headroom", "tight", _window(2000)),
        _turn(6, "headroom", "tight", 5, 1000),
    ]
    assert measured_tokens_per_character(events, provider_name="headroom", model_name="tight") == 0.5
    assert measured_tokens_per_character(events, provider_name="headroom", model_name="other") == 0.5
    assert measured_tokens_per_character(events, provider_name="headroom", model_name="missing") is None


def test_rate_skips_image_polluted_and_pre_inventory_windows() -> None:
    clean = [
        _intent(1, "headroom", "tight", _window(1000)),
        _turn(2, "headroom", "tight", 1, 250),
    ]
    assert measured_tokens_per_character(clean, provider_name="headroom", model_name="tight") == 0.25
    polluted = clean + [
        _intent(3, "headroom", "tight", _window(1000, images=2)),
        _turn(4, "headroom", "tight", 3, 250),
    ]
    # The newer image-carrying dispatch cannot pollute the rate; the older clean one still qualifies.
    assert measured_tokens_per_character(polluted, provider_name="headroom", model_name="tight") == 0.25
    legacy = [
        _intent(1, "headroom", "tight", _window(1000, images=None)),
        _turn(2, "headroom", "tight", 1, 250),
    ]
    # Pre-inventory facts claim unknown images, never zero: no rate until a measured dispatch lands.
    assert measured_tokens_per_character(legacy, provider_name="headroom", model_name="tight") is None


def test_rate_skips_unusable_measurements_without_guessing() -> None:
    assert measured_tokens_per_character([], provider_name="headroom", model_name="tight") is None
    assert measured_tokens_per_character([_intent(1, "h", "m", _window(10))], provider_name="", model_name="m") is None
    no_usage = [_intent(1, "headroom", "tight", _window(1000)), _turn(2, "headroom", "tight", 1, None)]
    assert measured_tokens_per_character(no_usage, provider_name="headroom", model_name="tight") is None
    zero_tokens = [_intent(1, "headroom", "tight", _window(1000)), _turn(2, "headroom", "tight", 1, 0)]
    assert measured_tokens_per_character(zero_tokens, provider_name="headroom", model_name="tight") is None
    orphan = [_turn(1, "headroom", "tight", 99, 250)]
    assert measured_tokens_per_character(orphan, provider_name="headroom", model_name="tight") is None
    empty_window = [_intent(1, "headroom", "tight", _window(0)), _turn(2, "headroom", "tight", 1, 250)]
    assert measured_tokens_per_character(empty_window, provider_name="headroom", model_name="tight") is None
    malformed = [
        _stored(1, "core.provider_intent", {"request_window": {"message_characters": -1}}),
        _turn(2, "headroom", "tight", 1, 250),
    ]
    assert measured_tokens_per_character(malformed, provider_name="headroom", model_name="tight") is None


# --- Unit: image inventory round-trips ---


def test_window_image_inventory_round_trip_and_unknown_poisoning() -> None:
    segment = WindowSegment(
        role="tool",
        characters=10,
        sha256="0" * 64,
        tool_calls=0,
        tool_name="bigtext",
        reasoning_characters=0,
        image_count=2,
        image_bytes=300,
    )
    assert WindowSegment.from_payload(segment.as_payload()) == segment
    window = RequestWindow(message_characters=10, tool_schema_characters=5, segments=(segment,), tool_count=1)
    assert RequestWindow.from_payload(window.as_payload()) == window
    assert window.total_image_count == 2
    legacy = WindowSegment(role="user", characters=3, sha256="1" * 64)
    assert legacy.image_count is None and legacy.image_bytes is None
    assert RequestWindow(message_characters=3, tool_schema_characters=0, segments=(legacy,)).total_image_count is None
    content = TranscriptDispatch("headroom", "tight", window)
    assert decode_transcript_content(encode_transcript_content(content)) == content
    legacy_content = TranscriptDispatch("headroom", "tight", RequestWindow(message_characters=1, tool_schema_characters=0))
    assert decode_transcript_content(encode_transcript_content(legacy_content)) == legacy_content


# --- Unit: limits probes ---


def test_catalog_and_adapter_limits_probes() -> None:
    known = catalog_model_limits("anthropic", "claude-sonnet-4-5")
    assert known is not None and known.context_window == 200000 and known.max_output_tokens == 64000
    assert catalog_model_limits("endpoint", "exact-model") is None
    assert catalog_model_limits("anthropic", "  ") is None
    assert AnthropicMessagesProvider().model_limits("claude-sonnet-4-5") == known
    assert AnthropicMessagesProvider().model_limits("missing-model") is None
    assert AnthropicMessagesProvider().model_limits("") is None
    assert GoogleGenAIProvider().model_limits("gemini-2.5-flash") == catalog_model_limits("google", "gemini-2.5-flash")
    assert OpenAIChatCompletionsProvider().model_limits("gpt-4o") == catalog_model_limits("openai", "gpt-4o")
    assert OpenAIChatCompletionsProvider().model_limits("missing-model") is None
    routed = RoutedTurnProvider(
        name="gateway",
        routing=WireRouting("anthropic", {}, "chat"),
        build=lambda route: (_ for _ in ()).throw(AssertionError("limits never build a wire")),
        model_map={"alias": "claude-sonnet-4-5"},
    )
    assert routed.model_limits("alias") == known
    assert routed.model_limits("missing") is None


def test_registered_provider_delegates_limits_without_network() -> None:
    providers = {provider.name: provider for provider in build_registered_providers({})}
    assert providers["anthropic"].model_limits("claude-sonnet-4-5") == catalog_model_limits("anthropic", "claude-sonnet-4-5")
    assert providers["anthropic"].model_limits("missing-model") is None
    assert providers["openai"].model_limits("gpt-4o") == catalog_model_limits("openai", "gpt-4o")
    assert providers["endpoint"].model_limits("exact-model") is None


# --- Dispatch: adaptive shrink with intact identities (A) + first-dispatch fixed (C) ---


def test_tight_window_dispatch_shrinks_tool_text_with_intact_identities(tmp_path: Path) -> None:
    source = _write_fixture(tmp_path / "plugin")
    script = [
        _tool_call_reply("bigtext", "call-1", {"size": 60000}, 2000),
        _output_reply("done", 3000),
    ]
    workspace, evidence, _ = _configure(tmp_path, source, script=script, limits={"context_window": 6000, "max_output_tokens": 1000})
    with open_core(workspace, state_dir=tmp_path / "state") as core:
        result = core.run("summarize the filler", session_id="sess-tight")
        assert result.status == "completed", result.error
        intents = _intents(core, result.session_id)
        assert len(intents) == 2
        first = intents[0].payload["projection_allocation"]
        assert first["policy"] == "fixed" and first["tool_count"] == 0 and first["reason"] == "no_shrinkable"
        second = intents[1].payload["projection_allocation"]
        assert second["policy"] == "adaptive" and second["reason"] == "ok"
        assert second["limit_tokens"] == 5000 and second["limit_source"] == "context_window_minus_max_output"
        budget = second["per_tool_max_bytes"]
        assert isinstance(budget, int) and budget < TOOL_OUTPUT_BYTES
        assert second["ineffective_tools"] == 0

        turns = _evidence_turns(evidence)
        assert len(turns) == 2
        tool_messages = [message for message in turns[1]["messages"] if message["role"] == "tool"]
        assert len(tool_messages) == 1
        sent = tool_messages[0]["content"]
        document = json.loads(sent)
        assert document["result_uri"].startswith("artifact://") and document.get("projection_note") is not None

        completed = next(event for event in core.store.events(result.session_id) if event.event_type == "core.tool_completed")
        stored = decode_tool_result(completed.payload["result"])
        assert stored.result_reference is not None
        ceiling, _, _ = project_tool_result(stored, max_bytes=TOOL_OUTPUT_BYTES, artifact_reader=core._artifact_page)
        assert len(sent.encode("utf-8")) < len(ceiling.encode("utf-8")) <= TOOL_OUTPUT_BYTES
        # The complete result is untouched: all 60000 bytes recoverable through the committed reference.
        original = json.loads(read_artifact_content(stored.result_reference, core._artifact_page).decode("utf-8"))
        assert original["output"]["text"] == "B" * 60000


def test_unknown_usage_keeps_ceiling_bytes_exactly(tmp_path: Path) -> None:
    source = _write_fixture(tmp_path / "plugin")
    script = [
        _tool_call_reply("bigtext", "call-1", {"size": 60000}, None),
        _output_reply("done", None),
    ]
    workspace, evidence, _ = _configure(tmp_path, source, script=script, limits={"context_window": 6000, "max_output_tokens": 1000})
    with open_core(workspace, state_dir=tmp_path / "state") as core:
        result = core.run("summarize the filler", session_id="sess-unknown-usage")
        assert result.status == "completed", result.error
        intents = _intents(core, result.session_id)
        assert len(intents) == 2
        second = intents[1].payload["projection_allocation"]
        assert second["policy"] == "fixed" and second["reason"] == "unknown_usage"
        assert second["per_tool_max_bytes"] is None and second["limit_tokens"] == 5000

        turns = _evidence_turns(evidence)
        tool_messages = [message for message in turns[1]["messages"] if message["role"] == "tool"]
        assert len(tool_messages) == 1
        completed = next(event for event in core.store.events(result.session_id) if event.event_type == "core.tool_completed")
        stored = decode_tool_result(completed.payload["result"])
        ceiling, _, _ = project_tool_result(stored, max_bytes=TOOL_OUTPUT_BYTES, artifact_reader=core._artifact_page)
        assert tool_messages[0]["content"] == ceiling


def test_unknown_limits_keeps_ceiling_bytes_exactly(tmp_path: Path) -> None:
    source = _write_fixture(tmp_path / "plugin")
    script = [
        _tool_call_reply("bigtext", "call-1", {"size": 60000}, 2000),
        _output_reply("done", 3000),
    ]
    workspace, evidence, _ = _configure(tmp_path, source, script=script, limits=None, advertise_limits=False)
    with open_core(workspace, state_dir=tmp_path / "state") as core:
        result = core.run("summarize the filler", session_id="sess-unknown-limits")
        assert result.status == "completed", result.error
        intents = _intents(core, result.session_id)
        assert len(intents) == 2
        for intent in intents:
            allocation = intent.payload["projection_allocation"]
            assert allocation["policy"] == "fixed" and allocation["limit_tokens"] is None
        assert intents[1].payload["projection_allocation"]["reason"] == "unknown_limits"

        turns = _evidence_turns(evidence)
        tool_messages = [message for message in turns[1]["messages"] if message["role"] == "tool"]
        assert len(tool_messages) == 1
        completed = next(event for event in core.store.events(result.session_id) if event.event_type == "core.tool_completed")
        stored = decode_tool_result(completed.payload["result"])
        ceiling, _, _ = project_tool_result(stored, max_bytes=TOOL_OUTPUT_BYTES, artifact_reader=core._artifact_page)
        assert tool_messages[0]["content"] == ceiling


def test_image_window_keeps_fixed_policy_and_forwards_image_intact(tmp_path: Path) -> None:
    source = _write_fixture(tmp_path / "plugin")
    raw = base64.b64encode(b"headroom-test-image-bytes").decode("ascii")
    script = [
        _tool_call_reply("shot", "call-shot", {}, 2000),
        _output_reply("seen", 3000),
    ]
    workspace, evidence, _ = _configure(tmp_path, source, script=script, limits={"context_window": 6000, "max_output_tokens": 1000}, image_data=raw)
    with open_core(workspace, state_dir=tmp_path / "state") as core:
        result = core.run("look at the shot", session_id="sess-image")
        assert result.status == "completed", result.error
        intents = _intents(core, result.session_id)
        assert len(intents) == 2
        second = intents[1].payload["projection_allocation"]
        assert second["policy"] == "fixed" and second["reason"] == "images_present"

        turns = _evidence_turns(evidence)
        tool_messages = [message for message in turns[1]["messages"] if message["role"] == "tool"]
        assert len(tool_messages) == 1 and tool_messages[0]["images"] == [raw]
        window = RequestWindow.from_payload(intents[1].payload["request_window"])
        assert window.total_image_count == 1


def test_budget_below_identity_minimum_keeps_ceiling_without_refusing_dispatch(tmp_path: Path) -> None:
    source = _write_fixture(tmp_path / "plugin")
    script = [
        _tool_call_reply("bigtext", "call-1", {"size": 60000}, 2000),
        _output_reply("done", 3000),
        _output_reply("again done", 100),
    ]
    workspace, evidence, limits_path = _configure(tmp_path, source, script=script, limits={"max_input_tokens": 10**9})
    prompt2 = "again"
    with open_core(workspace, state_dir=tmp_path / "state") as core:
        first = core.run("summarize the filler", session_id="sess-floor")
        assert first.status == "completed", first.error
        session_id = first.session_id
        events = list(core.store.events(session_id))
        rate = measured_tokens_per_character(events, provider_name=PROVIDER_NAME, model_name="tight")
        assert rate is not None and rate > 0
        schemas = int(RequestWindow.from_payload(_intents(core, session_id)[0].payload["request_window"]).tool_schema_characters)
        session = core.store.session(session_id)
        fixed_messages = core._messages(_Execution(session, _Cursor(), list(events), "measure"))
        tool_characters = sum(len(message.content or "") for message in fixed_messages if message.role == "tool")
        fixed = sum(len(message.content or "") + len(message.reasoning or "") for message in fixed_messages) - tool_characters
        assert sum(1 for message in fixed_messages if message.role == "tool") == 1
        # Aim the only tool's share at ~50 bytes: adaptive, but far below any identity fit.
        limits_path.write_text(json.dumps({"max_input_tokens": int((fixed + len(prompt2) + schemas + 50) * rate) + 1}), encoding="utf-8")

        second = core.run(prompt2, session_id=session_id)
        assert second.status == "completed", second.error
        intents = _intents(core, session_id)
        assert len(intents) == 3
        floor = intents[2].payload["projection_allocation"]
        assert floor["policy"] == "adaptive" and floor["reason"] == "ok"
        assert isinstance(floor["per_tool_max_bytes"], int) and floor["per_tool_max_bytes"] < 512
        assert floor["ineffective_tools"] == 1

        turns = _evidence_turns(evidence)
        tool_messages = [message for message in turns[2]["messages"] if message["role"] == "tool"]
        assert len(tool_messages) == 1
        completed = next(event for event in core.store.events(session_id) if event.event_type == "core.tool_completed")
        stored = decode_tool_result(completed.payload["result"])
        ceiling, _, _ = project_tool_result(stored, max_bytes=TOOL_OUTPUT_BYTES, artifact_reader=core._artifact_page)
        assert tool_messages[0]["content"] == ceiling
