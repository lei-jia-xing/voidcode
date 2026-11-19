"""Sandbox plan framing: one payload build, stable wire bytes."""

import base64
import json

from voidcode.core.activation import SandboxPolicy
from voidcode.core.sandbox import SandboxBackend, build_plan


def _backend() -> SandboxBackend:
    return SandboxBackend(
        policy=SandboxPolicy(mode="workspace-write", writable_roots=("/work",)),
        helper_path="/opt/voidcode/helper",
        protected_paths=("/opt/voidcode/helper", "/etc/secrets"),
        env_baseline=(("PATH", "/usr/bin"),),
    )


def _plan(stdin: bytes | None = None, effects_path: str | None = None) -> bytes:
    _, _, _, frame = build_plan(
        _backend(),
        argv=("echo", "hi"),
        cwd="/work",
        env_overrides={},
        workspace="/work",
        scratch="/tmp/scratch",
        stdin=stdin,
        effects_path=effects_path,
    )
    return frame


def test_frame_is_length_prefixed_json() -> None:
    frame = _plan()
    body = frame[8:]
    assert int.from_bytes(frame[:8], "big") == len(body)
    payload = json.loads(body.decode("utf-8"))
    assert payload["argv"] == ["echo", "hi"]
    assert payload["stdin_b64"] is None
    assert payload["effects_path"] is None


def test_optional_keys_framed_once() -> None:
    frame = _plan(stdin=b"hello", effects_path="/tmp/x.effects.json")
    payload = json.loads(frame[8:].decode("utf-8"))
    assert payload["stdin_b64"] == base64.b64encode(b"hello").decode("ascii")
    assert payload["effects_path"] == "/tmp/x.effects.json"
    assert int.from_bytes(frame[:8], "big") == len(frame[8:])


def test_single_build_matches_legacy_two_step_bytes() -> None:
    """Byte-identical to the old dumps/frame/loads/mutate/dumps/re-frame sequence.

    The expectation is spelled out by hand (legacy key order, legacy plain
    ``json.dumps`` params), never derived from the new code under test.
    """
    legacy_payload = {
        "policy": {
            "mode": "workspace-write",
            "network": False,
            "workspace": "/work",
            "writable_roots": ["/work"],
            "protected_paths": ["/etc/secrets"],
            "scratch": "/tmp/scratch",
        },
        "argv": ["echo", "hi"],
        "cwd": "/work",
        "env": {"PATH": "/usr/bin", "TERM": "xterm-256color", "TMPDIR": "/tmp/scratch", "HOME": "/work"},
        "stdin_b64": base64.b64encode(b"hello").decode("ascii"),
        "effects_path": "/tmp/x.effects.json",
    }
    legacy_body = json.dumps(legacy_payload).encode("utf-8")
    legacy_frame = len(legacy_body).to_bytes(8, "big") + legacy_body
    assert _plan(stdin=b"hello", effects_path="/tmp/x.effects.json") == legacy_frame
