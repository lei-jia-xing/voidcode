"""Debug traces must stay bounded and must not fail a provider call."""

import json

import pytest

from voidcode.capabilities.providers.native import trace


@pytest.fixture
def trace_path(tmp_path, monkeypatch):
    path = tmp_path / "trace.jsonl"
    monkeypatch.setenv("VOIDCODE_PROVIDER_TRACE", "1")
    monkeypatch.setenv("VOIDCODE_PROVIDER_TRACE_LOG", str(path))
    return path


def test_trace_retains_small_records_and_restricts_existing_permissions(trace_path):
    trace_path.touch(mode=0o644)
    trace.write_provider_trace(request={"prompt": "你好"}, response={"text": "answer"}, metadata={"provider": "test"})
    record = json.loads(trace_path.read_text())
    assert record["request"] == {"prompt": "你好"}
    assert record["response"] == {"text": "answer"}
    assert trace_path.stat().st_mode & 0o777 == 0o600


def test_trace_bounds_the_whole_record_not_each_field(trace_path):
    trace.write_provider_trace(request={"messages": ['你好\\"' * 10000] * 200}, response={}, metadata={})
    raw = trace_path.read_bytes()
    assert len(raw) < trace._MAX_BYTES * 2 + 512
    record = json.loads(raw)
    assert record["truncated"] is True
    assert len(record["preview"].encode("utf-8")) <= trace._MAX_BYTES


@pytest.mark.parametrize("failure", ["directory", "serialization", "symlink"])
def test_trace_failure_warns_without_raising_or_printing_payload(trace_path, caplog, failure):
    if failure == "directory":
        trace_path.mkdir()
    elif failure == "symlink":
        target = trace_path.with_name("user-file")
        target.write_bytes(b"unchanged")
        trace_path.symlink_to(target)
    trace.write_provider_trace(request={"secret": "do-not-log-this"}, response=object() if failure == "serialization" else {}, metadata={})
    assert "Provider trace could not be written" in caplog.text
    assert "do-not-log-this" not in caplog.text
    if failure == "symlink":
        assert target.read_bytes() == b"unchanged"


def test_disabled_trace_does_not_create_files_or_serialize_requests(trace_path, monkeypatch):
    monkeypatch.delenv("VOIDCODE_PROVIDER_TRACE")
    trace.write_provider_trace(request={}, response=object(), metadata={})
    assert not trace_path.exists()
