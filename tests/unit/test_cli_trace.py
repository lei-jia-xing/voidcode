from __future__ import annotations

from voidcode.cli.trace import TracePrinter
from voidcode.runtime.events import EventEnvelope


def test_trace_reads_model_turn_number_from_run_step(capsys) -> None:
    TracePrinter().handle_event(
        EventEnvelope(
            session_id="trace-test",
            sequence=1,
            event_type="core.model_turn",
            source="core",
            payload={"run_step": 3, "provider": "openai", "model": "gpt-test"},
        )
    )

    assert "Model turn 3: openai · gpt-test" in capsys.readouterr().out
