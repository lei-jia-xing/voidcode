from __future__ import annotations

import json

from voidcode.provider.trace import write_provider_trace


def test_provider_trace_redacts_credential_keys_and_inline_secrets(tmp_path, monkeypatch) -> None:
    trace_path = tmp_path / "trace.jsonl"
    monkeypatch.setenv("VOIDCODE_PROVIDER_TRACE", "1")
    monkeypatch.setenv("VOIDCODE_PROVIDER_TRACE_LOG", str(trace_path))

    write_provider_trace(
        request={
            "headers": {"x-api-key": "secret-header", "Cookie": "session=secret-cookie"},
            "messages": [{"content": 'Authorization: Bearer secret-token {"api_key": "json-secret"}'}],
        },
        response={"debug": "token=secret-value"},
        metadata={},
    )

    record = json.loads(trace_path.read_text(encoding="utf-8"))
    serialized = json.dumps(record, sort_keys=True)
    assert "secret-header" not in serialized
    assert "secret-cookie" not in serialized
    assert "secret-token" not in serialized
    assert "secret-value" not in serialized
    assert "json-secret" not in serialized
    assert "<redacted>" in serialized
