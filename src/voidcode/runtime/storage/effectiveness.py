from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, cast

from ..effectiveness import (
    ToolEffectivenessEvent,
    ToolEffectivenessReport,
    project_tool_effectiveness,
)

if TYPE_CHECKING:
    from .shared import _StorageMixinBase

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

        session_ids = tuple(row["session_id"] for row in session_rows)
        session_metadata = {row["session_id"]: json.loads(row["metadata_json"]) for row in session_rows}
        events = tuple(
            ToolEffectivenessEvent(
                session_id=row["session_id"],
                event=self._event_envelope_from_row(session_id=row["session_id"], row=row),
            )
            for row in event_rows
        )
        return project_tool_effectiveness(
            workspace_id=str(workspace),
            session_ids=session_ids,
            session_metadata=session_metadata,
            events=events,
        )
