from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, fields
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .contracts import (
    RuntimeRequest,
    RuntimeResponse,
    UnknownSessionError,
)
from .events import EventEnvelope
from .permission import PendingApproval
from .question import (
    PendingQuestion,
    PendingQuestionOption,
    PendingQuestionPrompt,
)
from .session import session_metadata_for_persistence
from .storage_shared import (
    _pending_operation_class,
    _pending_path_scope,
    _pending_permission_decision,
)

if TYPE_CHECKING:
    from .storage_shared import _StorageMixinBase

    _MixinBase = _StorageMixinBase
else:
    _MixinBase = object


class _ResumeStorageMixin(_MixinBase):
    @classmethod
    def _resume_checkpoint_base(
        cls,
        *,
        request: RuntimeRequest,
        response: RuntimeResponse,
        kind: str,
        last_event_sequence: int | None = None,
    ) -> dict[str, object]:
        snapshot_hash, snapshot_version, binding_snapshot = cls._checkpoint_skill_snapshot(response.session.metadata)
        return {
            "version": 1,
            "kind": kind,
            "prompt": request.prompt,
            "session_status": response.session.status,
            "session_metadata": session_metadata_for_persistence(
                response.session.metadata,
                events=response.events,
            ),
            "skill_snapshot_hash": snapshot_hash,
            "skill_snapshot_version": snapshot_version,
            "skill_binding_snapshot": binding_snapshot,
            "tool_results": cls._tool_results_from_events(response.events),
            "last_event_sequence": (cls._session_last_event_sequence(response.events) if last_event_sequence is None else last_event_sequence),
            "output": response.output,
        }

    @staticmethod
    def _decode_json_object_payload(
        payload: str,
        *,
        malformed_message: str,
        non_object_message: str,
    ) -> dict[str, object]:
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(malformed_message) from exc
        if not isinstance(decoded, dict):
            raise ValueError(non_object_message)
        return cast(dict[str, object], decoded)

    @classmethod
    def _decode_resume_checkpoint_payload(cls, payload: str) -> dict[str, object]:
        checkpoint = cls._decode_json_object_payload(
            payload,
            malformed_message="persisted resume checkpoint JSON is malformed",
            non_object_message="persisted resume checkpoint payload must decode to an object",
        )
        kind = checkpoint.get("kind")
        if not isinstance(kind, str) or kind not in cls._RESUME_CHECKPOINT_KINDS:
            raise ValueError(f"persisted resume checkpoint kind is invalid: {kind!r}")
        return checkpoint

    @staticmethod
    def _optional_string(value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise ValueError("persisted delegated reminder string fields must be non-empty strings")
        return value

    @staticmethod
    def _optional_int(value: object) -> int | None:
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("persisted delegated reminder timestamp fields must be integers")
        return value

    @staticmethod
    def _pending_question_payload(pending_question: PendingQuestion) -> dict[str, object]:
        return {
            "request_id": pending_question.request_id,
            "tool_name": pending_question.tool_name,
            "arguments": pending_question.arguments,
            "prompts": [
                {
                    "question": prompt.question,
                    "header": prompt.header,
                    "multiple": prompt.multiple,
                    "options": [{"label": option.label, "description": option.description} for option in prompt.options],
                }
                for prompt in pending_question.prompts
            ],
        }

    def save_pending_approval(
        self,
        *,
        workspace: Path,
        request: RuntimeRequest,
        response: RuntimeResponse,
        pending_approval: PendingApproval,
    ) -> None:
        with self._write_connect(workspace) as connection:
            persisted_last_sequence = self._max_persisted_event_sequence(
                connection=connection,
                workspace=workspace,
                session_id=response.session.session.id,
            )
            updated_at = self._write_session_snapshot(
                connection=connection,
                workspace=workspace,
                request=request,
                response=response,
                pending_approval_json=json.dumps(asdict(pending_approval), sort_keys=True),
                pending_question_json=None,
                resume_checkpoint=self._approval_wait_resume_checkpoint(
                    request=request,
                    response=response,
                    pending_approval=pending_approval,
                    last_event_sequence=persisted_last_sequence,
                ),
            )
            self._sync_background_task_durable_state(
                connection=connection,
                workspace=workspace,
                request=request,
                response=response,
                approval_request_id=pending_approval.request_id,
            )
            self._sync_notifications(
                connection=connection,
                workspace=workspace,
                request=request,
                response=response,
                pending_approval=pending_approval,
                notification_run_id=updated_at,
                last_event_sequence=persisted_last_sequence,
            )
            connection.commit()

    def load_pending_approval(self, *, workspace: Path, session_id: str) -> PendingApproval | None:
        with self._connect(workspace) as connection:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT pending_approval_json
                    FROM sessions
                    WHERE workspace_id = ? AND session_id = ?
                    """,
                    (str(workspace), session_id),
                ).fetchone(),
            )
        if row is None:
            raise UnknownSessionError(f"unknown session: {session_id}")
        payload = cast(str | None, row["pending_approval_json"])
        if payload is None:
            return None
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"persisted pending approval for session {session_id!r} is corrupt: "
                f"{exc}. Run `voidcode sessions debug {session_id}` to inspect, "
                "or `voidcode storage reset` to recover."
            ) from exc
        if not isinstance(decoded, dict):
            raise RuntimeError(f"persisted pending approval for session {session_id!r} is corrupt: payload must decode to an object.")
        data = cast(dict[str, object], decoded)
        required_fields = frozenset(field.name for field in fields(PendingApproval))
        missing_fields = sorted(required_fields - data.keys())
        if missing_fields:
            raise RuntimeError(
                f"persisted pending approval for session {session_id!r} is missing "
                f"required fields {missing_fields!r}; run `voidcode storage reset` to recover."
            )
        request_id = data["request_id"]
        tool_name = data["tool_name"]
        if not isinstance(request_id, str) or not isinstance(tool_name, str):
            raise RuntimeError(
                f"persisted pending approval for session {session_id!r} has invalid "
                "request_id/tool_name types; run `voidcode storage reset` to recover."
            )
        arguments = data["arguments"]
        target_summary = data["target_summary"]
        reason = data["reason"]
        if not isinstance(arguments, dict):
            raise RuntimeError("persisted pending approval arguments must be an object")
        if not isinstance(target_summary, str) or not isinstance(reason, str):
            raise RuntimeError("persisted pending approval summary and reason must be strings")
        raw_policy_mode = data["policy_mode"]
        try:
            policy_mode = _pending_permission_decision(raw_policy_mode)
        except ValueError as exc:
            raise RuntimeError(f"persisted pending approval for session {session_id!r} has invalid policy_mode {raw_policy_mode!r}: {exc}") from exc
        request_event_sequence = data["request_event_sequence"]
        if request_event_sequence is not None and (not isinstance(request_event_sequence, int) or isinstance(request_event_sequence, bool)):
            raise RuntimeError("persisted pending approval request_event_sequence must be an integer or null")
        nullable_string_fields = (
            "owner_session_id",
            "owner_parent_session_id",
            "delegated_task_id",
            "canonical_path",
            "matched_rule",
            "policy_surface",
        )
        for field_name in nullable_string_fields:
            value = data[field_name]
            if value is not None and not isinstance(value, str):
                raise RuntimeError(f"persisted pending approval {field_name} must be a string or null")
        path_scope = _pending_path_scope(data["path_scope"])
        if data["path_scope"] is not None and path_scope is None:
            raise RuntimeError("persisted pending approval path_scope is invalid")
        operation_class = _pending_operation_class(data["operation_class"])
        if data["operation_class"] is not None and operation_class is None:
            raise RuntimeError("persisted pending approval operation_class is invalid")
        return PendingApproval(
            request_id=request_id,
            tool_name=tool_name,
            arguments=cast(dict[str, object], arguments),
            target_summary=target_summary,
            reason=reason,
            policy_mode=policy_mode,
            request_event_sequence=request_event_sequence,
            owner_session_id=(data["owner_session_id"] if isinstance(data["owner_session_id"], str) else None),
            owner_parent_session_id=(data["owner_parent_session_id"] if isinstance(data["owner_parent_session_id"], str) else None),
            delegated_task_id=(data["delegated_task_id"] if isinstance(data["delegated_task_id"], str) else None),
            path_scope=path_scope,
            operation_class=operation_class,
            canonical_path=(data["canonical_path"] if isinstance(data["canonical_path"], str) else None),
            matched_rule=(data["matched_rule"] if isinstance(data["matched_rule"], str) else None),
            policy_surface=(data["policy_surface"] if isinstance(data["policy_surface"], str) else None),
        )

    def clear_pending_approval(self, *, workspace: Path, session_id: str) -> None:
        with self._write_connect(workspace) as connection:
            _ = connection.execute(
                "UPDATE sessions SET pending_approval_json = NULL WHERE workspace_id = ? AND session_id = ?",  # noqa: E501
                (str(workspace), session_id),
            )
            connection.commit()

    def save_pending_question(
        self,
        *,
        workspace: Path,
        request: RuntimeRequest,
        response: RuntimeResponse,
        pending_question: PendingQuestion,
    ) -> None:
        with self._write_connect(workspace) as connection:
            persisted_last_sequence = self._max_persisted_event_sequence(
                connection=connection,
                workspace=workspace,
                session_id=response.session.session.id,
            )
            updated_at = self._write_session_snapshot(
                connection=connection,
                workspace=workspace,
                request=request,
                response=response,
                pending_approval_json=None,
                pending_question_json=json.dumps(self._pending_question_payload(pending_question), sort_keys=True),
                resume_checkpoint=self._question_wait_resume_checkpoint(
                    request=request,
                    response=response,
                    pending_question=pending_question,
                    last_event_sequence=persisted_last_sequence,
                ),
            )
            self._sync_background_task_durable_state(
                connection=connection,
                workspace=workspace,
                request=request,
                response=response,
                question_request_id=pending_question.request_id,
            )
            self._sync_notifications(
                connection=connection,
                workspace=workspace,
                request=request,
                response=response,
                pending_approval=None,
                pending_question=pending_question,
                notification_run_id=updated_at,
                last_event_sequence=persisted_last_sequence,
            )
            connection.commit()

    def load_pending_question(self, *, workspace: Path, session_id: str) -> PendingQuestion | None:
        with self._connect(workspace) as connection:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT pending_question_json
                    FROM sessions
                    WHERE workspace_id = ? AND session_id = ?
                    """,
                    (str(workspace), session_id),
                ).fetchone(),
            )
        if row is None:
            raise UnknownSessionError(f"unknown session: {session_id}")
        payload = cast(str | None, row["pending_question_json"])
        if payload is None:
            return None
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"persisted pending question for session {session_id!r} is corrupt: "
                f"{exc}. Run `voidcode sessions debug {session_id}` to inspect, "
                "or `voidcode storage reset` to recover."
            ) from exc
        if not isinstance(decoded, dict):
            raise RuntimeError(f"persisted pending question for session {session_id!r} is corrupt: payload must decode to an object.")
        data = cast(dict[str, object], decoded)
        required_fields = {"request_id", "tool_name", "arguments", "prompts"}
        missing_fields = sorted(required_fields - data.keys())
        if missing_fields:
            raise RuntimeError(
                f"persisted pending question for session {session_id!r} is missing "
                f"required fields {missing_fields!r}; run `voidcode storage reset` to recover."
            )
        request_id = data["request_id"]
        if not isinstance(request_id, str):
            raise RuntimeError(
                f"persisted pending question for session {session_id!r} has invalid request_id type; run `voidcode storage reset` to recover."
            )
        raw_prompts = data["prompts"]
        if not isinstance(raw_prompts, list):
            raise RuntimeError(f"persisted pending question for session {session_id!r} has invalid prompts payload (must be a list).")
        prompts: list[PendingQuestionPrompt] = []
        for prompt_index, raw_prompt in enumerate(cast(list[object], raw_prompts)):
            if not isinstance(raw_prompt, dict):
                raise RuntimeError(f"persisted pending question for session {session_id!r} has invalid prompts[{prompt_index}] (must be an object).")
            prompt_payload = cast(dict[str, object], raw_prompt)
            if not {"question", "header", "multiple", "options"} <= prompt_payload.keys():
                raise RuntimeError(f"persisted pending question for session {session_id!r} has incomplete prompts[{prompt_index}] payload.")
            raw_options = prompt_payload["options"]
            if not isinstance(raw_options, list):
                raise RuntimeError(
                    f"persisted pending question for session {session_id!r} has invalid prompts[{prompt_index}].options (must be a list)."
                )
            options_list: list[PendingQuestionOption] = []
            for option_index, raw_option in enumerate(cast(list[object], raw_options)):
                if not isinstance(raw_option, dict):
                    raise RuntimeError(
                        f"persisted pending question for session {session_id!r} has "
                        f"invalid prompts[{prompt_index}].options[{option_index}] "
                        "(must be an object)."
                    )
                option_payload = cast(dict[str, object], raw_option)
                if set(option_payload) != {"label", "description"}:
                    raise RuntimeError(
                        f"persisted pending question for session {session_id!r} has "
                        f"incomplete prompts[{prompt_index}].options[{option_index}] payload."
                    )
                option_label = option_payload["label"]
                option_description = option_payload["description"]
                if not isinstance(option_label, str) or not isinstance(option_description, str):
                    raise RuntimeError(
                        f"persisted pending question for session {session_id!r} has invalid prompts[{prompt_index}].options[{option_index}] strings."
                    )
                options_list.append(
                    PendingQuestionOption(
                        label=option_label,
                        description=option_description,
                    )
                )
            prompt_question = prompt_payload["question"]
            prompt_header = prompt_payload["header"]
            if not isinstance(prompt_question, str) or not isinstance(prompt_header, str):
                raise RuntimeError(
                    f"persisted pending question for session {session_id!r} has invalid prompts[{prompt_index}].question/header (must be strings)."
                )
            raw_multiple = prompt_payload["multiple"]
            if not isinstance(raw_multiple, bool):
                raise RuntimeError(
                    f"persisted pending question for session {session_id!r} has invalid prompts[{prompt_index}].multiple (must be a boolean)."
                )
            prompts.append(
                PendingQuestionPrompt(
                    question=prompt_question,
                    header=prompt_header,
                    options=tuple(options_list),
                    multiple=raw_multiple,
                )
            )
        tool_name_value = data["tool_name"]
        if not isinstance(tool_name_value, str):
            raise RuntimeError("persisted pending question tool_name must be a string")
        arguments_value = data["arguments"]
        if not isinstance(arguments_value, dict):
            raise RuntimeError("persisted pending question arguments must be an object")
        return PendingQuestion(
            request_id=request_id,
            tool_name=tool_name_value,
            arguments=cast(dict[str, object], arguments_value),
            prompts=tuple(prompts),
        )

    def clear_pending_question(self, *, workspace: Path, session_id: str) -> None:
        with self._write_connect(workspace) as connection:
            _ = connection.execute(
                ("UPDATE sessions SET pending_question_json = NULL WHERE workspace_id = ? AND session_id = ?"),
                (str(workspace), session_id),
            )
            connection.commit()

    def load_resume_checkpoint(self, *, workspace: Path, session_id: str) -> dict[str, object] | None:
        with self._connect(workspace) as connection:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT resume_checkpoint_json
                    FROM sessions
                    WHERE workspace_id = ? AND session_id = ?
                    """,
                    (str(workspace), session_id),
                ).fetchone(),
            )
        if row is None:
            raise UnknownSessionError(f"unknown session: {session_id}")
        payload = cast(str | None, row["resume_checkpoint_json"])
        if payload is None:
            return None
        return self._decode_resume_checkpoint_payload(payload)

    def _read_pending_approval_json(self, *, connection: sqlite3.Connection, workspace: Path, session_id: str) -> str | None:
        row = cast(
            sqlite3.Row | None,
            connection.execute(
                ("SELECT pending_approval_json FROM sessions WHERE workspace_id = ? AND session_id = ?"),
                (str(workspace), session_id),
            ).fetchone(),
        )
        if row is None:
            return None
        return cast(str | None, row["pending_approval_json"])

    def _approval_wait_resume_checkpoint(
        self,
        *,
        request: RuntimeRequest,
        response: RuntimeResponse,
        pending_approval: PendingApproval,
        last_event_sequence: int | None = None,
    ) -> dict[str, object]:
        return {
            **self._resume_checkpoint_base(
                request=request,
                response=response,
                kind="approval_wait",
                last_event_sequence=last_event_sequence,
            ),
            "pending_approval_request_id": pending_approval.request_id,
            "pending_approval_tool_name": pending_approval.tool_name,
            "pending_approval_arguments": pending_approval.arguments,
            "pending_approval_request_event_sequence": pending_approval.request_event_sequence,
            "pending_approval_owner_session_id": pending_approval.owner_session_id,
            "pending_approval_owner_parent_session_id": pending_approval.owner_parent_session_id,
            "pending_approval_delegated_task_id": pending_approval.delegated_task_id,
        }

    def _question_wait_resume_checkpoint(
        self,
        *,
        request: RuntimeRequest,
        response: RuntimeResponse,
        pending_question: PendingQuestion,
        last_event_sequence: int | None = None,
    ) -> dict[str, object]:
        return {
            **self._resume_checkpoint_base(
                request=request,
                response=response,
                kind="question_wait",
                last_event_sequence=last_event_sequence,
            ),
            "pending_question_request_id": pending_question.request_id,
            "pending_question_tool_name": pending_question.tool_name,
            "pending_question_prompts": [
                {
                    "header": prompt.header,
                    "question": prompt.question,
                    "multiple": prompt.multiple,
                    "options": [
                        {
                            "label": option.label,
                            "description": option.description,
                        }
                        for option in prompt.options
                    ],
                }
                for prompt in pending_question.prompts
            ],
        }

    def _provider_failure_retryable_resume_checkpoint(
        self, *, request: RuntimeRequest, response: RuntimeResponse, failure_event: EventEnvelope, last_event_sequence: int | None = None
    ) -> dict[str, object]:
        payload = failure_event.payload
        last_tool: dict[str, object] = next(
            (
                event.payload
                for event in reversed(response.events)
                if event.event_type == "runtime.tool_completed" and event.payload.get("status") != "error"
            ),
            cast(dict[str, object], {}),
        )
        return {
            **self._resume_checkpoint_base(
                request=request,
                response=response,
                kind="provider_failure_retryable",
                last_event_sequence=last_event_sequence,
            ),
            "provider_error_kind": payload.get("provider_error_kind"),
            "provider": payload.get("provider"),
            "model": payload.get("model"),
            "fallback_exhausted": payload.get("fallback_exhausted"),
            "provider_error_details": payload.get("provider_error_details"),
            "failure_event_sequence": failure_event.sequence,
            "last_successful_tool": last_tool.get("tool"),
            "last_successful_tool_call_id": last_tool.get("tool_call_id"),
        }

    def _terminal_resume_checkpoint(
        self,
        *,
        request: RuntimeRequest,
        response: RuntimeResponse,
        last_event_sequence: int | None = None,
    ) -> dict[str, object]:
        return self._resume_checkpoint_base(
            request=request,
            response=response,
            kind="terminal",
            last_event_sequence=last_event_sequence,
        )

    def _run_resume_checkpoint(
        self,
        *,
        request: RuntimeRequest,
        response: RuntimeResponse,
        last_event_sequence: int | None = None,
    ) -> dict[str, object]:
        if response.session.status != "failed":
            return self._terminal_resume_checkpoint(
                request=request,
                response=response,
                last_event_sequence=last_event_sequence,
            )
        failure_event = next(
            (event for event in reversed(response.events) if event.event_type == "runtime.failed"),
            None,
        )
        if failure_event is None:
            return self._terminal_resume_checkpoint(
                request=request,
                response=response,
                last_event_sequence=last_event_sequence,
            )
        if failure_event.payload.get("provider_error_kind") != "transient_failure":
            return self._terminal_resume_checkpoint(
                request=request,
                response=response,
                last_event_sequence=last_event_sequence,
            )
        if not any(event.event_type == "runtime.tool_completed" and event.payload.get("status") != "error" for event in response.events):
            return self._terminal_resume_checkpoint(
                request=request,
                response=response,
                last_event_sequence=last_event_sequence,
            )
        return self._provider_failure_retryable_resume_checkpoint(
            request=request,
            response=response,
            failure_event=failure_event,
            last_event_sequence=last_event_sequence,
        )

    @classmethod
    def _interrupted_resume_checkpoint(
        cls,
        *,
        prompt: str,
        session_metadata: dict[str, object],
        tool_results: tuple[dict[str, object], ...],
        last_event_sequence: int,
        output: str | None,
    ) -> dict[str, object]:
        snapshot_hash, snapshot_version, binding_snapshot = cls._checkpoint_skill_snapshot(session_metadata)
        return {
            "version": 1,
            "kind": "interrupted",
            "prompt": prompt,
            "session_status": "interrupted",
            "session_metadata": session_metadata_for_persistence(session_metadata),
            "skill_snapshot_hash": snapshot_hash,
            "skill_snapshot_version": snapshot_version,
            "skill_binding_snapshot": binding_snapshot,
            "tool_results": list(tool_results),
            "last_event_sequence": last_event_sequence,
            "output": output,
        }
