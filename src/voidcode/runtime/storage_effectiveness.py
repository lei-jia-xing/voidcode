from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .effectiveness import (
    ToolEffectivenessEvent,
    ToolEffectivenessReport,
    project_tool_effectiveness,
)
from .events import EventEnvelope

if TYPE_CHECKING:
    from .storage_shared import _StorageMixinBase

    _MixinBase = _StorageMixinBase
else:
    _MixinBase = object


class _EffectivenessStorageMixin(_MixinBase):
    def tool_effectiveness_report(self, *, workspace: Path) -> ToolEffectivenessReport:
        """Project aggregate tool quality metrics from append-only session truth."""

        with self._connect(workspace) as connection:
            session_rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    """
                    SELECT session_id, metadata_json
                    FROM sessions
                    WHERE workspace_id = ?
                    ORDER BY session_id ASC
                    """,
                    (str(workspace),),
                ).fetchall(),
            )
            event_rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    """
                    SELECT session_id, sequence, event_type, source, payload_json
                    FROM session_events
                    WHERE workspace_id = ? AND event_type IN (
                        'runtime.tool_completed',
                        'runtime.request_received',
                        'runtime.context_compacted',
                        'runtime.approval_requested'
                    )
                    ORDER BY session_id ASC, sequence ASC
                    """,
                    (str(workspace),),
                ).fetchall(),
            )

        session_ids = tuple(cast(str, row["session_id"]) for row in session_rows)
        session_metadata = {
            cast(str, row["session_id"]): cast(dict[str, object], json.loads(cast(str, row["metadata_json"]))) for row in session_rows
        }
        events = tuple(
            ToolEffectivenessEvent(
                session_id=cast(str, row["session_id"]),
                event=EventEnvelope(
                    session_id=cast(str, row["session_id"]),
                    sequence=cast(int, row["sequence"]),
                    event_type=cast(str, row["event_type"]),
                    source=self._parse_event_source(cast(str, row["source"])),
                    payload=cast(dict[str, object], json.loads(cast(str, row["payload_json"]))),
                ),
            )
            for row in event_rows
        )
        return project_tool_effectiveness(
            workspace_id=str(workspace),
            session_ids=session_ids,
            session_metadata=session_metadata,
            events=events,
        )
