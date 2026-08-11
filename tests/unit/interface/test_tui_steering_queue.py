from voidcode.tui.app import RuntimeProtocol


def test_runtime_protocol_exposes_steering_queue() -> None:
    assert getattr(RuntimeProtocol, "queue_steering", None) is not None
