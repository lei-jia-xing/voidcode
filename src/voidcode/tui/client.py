"""HTTP client for the VoidCode core transport."""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
from collections.abc import AsyncGenerator, Mapping
from typing import Any, Literal, NoReturn

import httpx

from voidcode.sdk.client import (
    CancelReceipt,
    CommandCatalog,
    CommandResult,
    ContinuationResult,
    CoreClientError,
    ErrorFrame,
    HealthStatus,
    ResumeSpec,
    RunResult,
    RunSpec,
    SessionRow,
    SessionUsageReport,
    SseFrame,
    StreamCancellation,
    ViewsSnapshot,
    parse_sse_frame,
)
from voidcode.sdk.transcript import ContextHistory, TranscriptPage, decode_context_history, decode_transcript_page
from voidcode.transport.projection import validated_session_id

logger = logging.getLogger(__name__)


def _checked_session_id(session_id: str) -> str:
    try:
        return validated_session_id(session_id)
    except ValueError as exc:
        raise CoreClientError(str(exc), status_code=400, code="invalid_params") from None


class CoreHttpClient:
    """HTTP client for a running core transport."""

    def __init__(self, base_url: str, *, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        parsed = urllib.parse.urlparse(self.base_url)
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        headers = {
            "Accept": "application/json",
            "Origin": self.origin,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self._client = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=30.0,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> CoreHttpClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _handle_status_error(self, exc: httpx.HTTPStatusError) -> NoReturn:
        try:
            body = exc.response.json()
        except Exception:
            body = None
        if isinstance(body, Mapping):
            raise CoreClientError.from_refusal(body, status_code=exc.response.status_code) from exc
        raise CoreClientError(f"HTTP {exc.response.status_code}", status_code=exc.response.status_code) from exc

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        params = {k: v for k, v in query.items() if v is not None} if query else None
        try:
            resp = self._client.request(
                method,
                path,
                json=body,
                params=params,
                timeout=timeout,
            )
            resp.raise_for_status()
            if not resp.content:
                return {}
            return resp.json()
        except httpx.HTTPStatusError as exc:
            self._handle_status_error(exc)
        except httpx.RequestError as exc:
            raise CoreClientError(f"connection error: {exc}") from exc

    def health(self) -> HealthStatus:
        return HealthStatus.from_payload(self._request("GET", "/api/health"))

    def command_catalog(self) -> CommandCatalog:
        return CommandCatalog.from_payload(self._request("GET", "/api/commands"))

    def list_sessions(self, limit: int = 100) -> list[SessionRow]:
        res = self._request("GET", "/api/sessions", query={"limit": limit})
        rows = res.get("sessions", [])
        if not isinstance(rows, list):
            raise CoreClientError("malformed sessions response")
        return [SessionRow.from_payload(item) for item in rows]

    def create_session(self, session_id: str | None = None) -> SessionRow:
        if session_id is not None and not session_id:
            raise CoreClientError("session_id must be a non-empty string", status_code=400, code="invalid_params")
        payload: dict[str, Any] = {}
        if session_id:
            payload["session_id"] = session_id
        return SessionRow.from_payload(self._request("POST", "/api/sessions", body=payload))

    def get_session(self, session_id: str) -> SessionRow:
        _checked_session_id(session_id)
        return SessionRow.from_payload(self._request("GET", f"/api/sessions/{urllib.parse.quote(session_id)}"))

    def show(self, session_id: str, events: bool = False) -> Mapping[str, Any]:
        _checked_session_id(session_id)
        return self._request(
            "GET",
            f"/api/sessions/{urllib.parse.quote(session_id)}/show",
            query={"events": "true" if events else "false"},
        )

    def run(self, session_id: str, spec: RunSpec) -> RunResult:
        _checked_session_id(session_id)
        return RunResult.from_payload(
            self._request(
                "POST",
                f"/api/sessions/{urllib.parse.quote(session_id)}/run",
                body={"prompt": spec.prompt, "session_id": session_id, "max_steps": spec.max_steps},
                timeout=300.0,
            )
        )

    def resume(
        self,
        session_id: str,
        spec: ResumeSpec | None = None,
    ) -> RunResult:
        _checked_session_id(session_id)
        active = spec or ResumeSpec()
        body: dict[str, Any] = {"max_steps": active.max_steps}
        if active.approval_request_id is not None:
            body["approval_request_id"] = active.approval_request_id
        if active.decision is not None:
            body["decision"] = active.decision
        return RunResult.from_payload(
            self._request(
                "POST",
                f"/api/sessions/{urllib.parse.quote(session_id)}/resume",
                body=body,
                timeout=300.0,
            )
        )

    def invoke_command(
        self,
        session_id: str,
        name: str,
        arguments: Mapping[str, object] | None = None,
        expected_revision: str = "",
    ) -> CommandResult:
        _checked_session_id(session_id)
        body: dict[str, Any] = {
            "arguments": dict(arguments or {}),
            "expected_revision": expected_revision,
        }
        return CommandResult.from_payload(
            self._request(
                "POST",
                f"/api/sessions/{urllib.parse.quote(session_id)}/invoke/{urllib.parse.quote(name)}",
                body=body,
                timeout=120.0,
            )
        )

    def confirm(
        self,
        session_id: str,
        request_id: str,
        decision: Literal["allow", "deny"],
        expected_revision: str = "",
    ) -> CommandResult:
        _checked_session_id(session_id)
        body: dict[str, Any] = {
            "request_id": request_id,
            "decision": decision,
            "expected_revision": expected_revision,
        }
        return CommandResult.from_payload(
            self._request(
                "POST",
                f"/api/sessions/{urllib.parse.quote(session_id)}/confirm",
                body=body,
                timeout=60.0,
            )
        )

    def views(self, session_id: str) -> ViewsSnapshot:
        _checked_session_id(session_id)
        return ViewsSnapshot.from_payload(self._request("GET", f"/api/sessions/{urllib.parse.quote(session_id)}/views"))

    def drain(self, session_id: str, *, limit: int = 10) -> list[ContinuationResult]:
        _checked_session_id(session_id)
        res = self._request(
            "POST",
            f"/api/sessions/{urllib.parse.quote(session_id)}/continuations",
            body={"limit": limit},
            timeout=120.0,
        )
        items = res.get("continuations", [])
        if not isinstance(items, list):
            raise CoreClientError("malformed continuations response")
        return [ContinuationResult.from_payload(item) for item in items]

    def usage(self, session_id: str, *, since_sequence: int = 0, until_sequence: int | None = None) -> SessionUsageReport:
        _checked_session_id(session_id)
        return SessionUsageReport.from_payload(
            self._request(
                "GET",
                f"/api/sessions/{urllib.parse.quote(session_id)}/usage",
                query={"since_sequence": since_sequence, "until_sequence": until_sequence},
            )
        )

    def cancel(self, session_id: str) -> CancelReceipt:
        _checked_session_id(session_id)
        return CancelReceipt.from_payload(self._request("POST", f"/api/sessions/{urllib.parse.quote(session_id)}/cancel"))

    def transcript(self, session_id: str, *, after_sequence: int = 0, limit: int = 100) -> TranscriptPage:
        _checked_session_id(session_id)
        return decode_transcript_page(
            self._request(
                "GET",
                f"/api/sessions/{urllib.parse.quote(session_id)}/transcript",
                query={"after_sequence": after_sequence, "limit": limit},
            )
        )

    def context_history(self, session_id: str, *, limit: int = 100) -> ContextHistory:
        _checked_session_id(session_id)
        return decode_context_history(self._request("GET", f"/api/sessions/{urllib.parse.quote(session_id)}/context", query={"limit": limit}))

    async def events_stream(
        self,
        session_id: str,
        after_sequence: int = 0,
        *,
        follow: bool = True,
        cancel: StreamCancellation | None = None,
    ) -> AsyncGenerator[SseFrame]:
        _checked_session_id(session_id)
        params = {
            "after_sequence": str(after_sequence),
            "follow": "true" if follow else "false",
        }
        headers = {
            "Accept": "text/event-stream",
            "Origin": self.origin,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        url = f"{self.base_url}/api/sessions/{urllib.parse.quote(session_id)}/events"

        if cancel is not None:
            task = asyncio.current_task()
            if task is not None:
                cancel.attach(asyncio.get_running_loop(), task)
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("GET", url, params=params, headers=headers) as response:
                    if response.is_error:
                        await response.aread()
                        try:
                            response.raise_for_status()
                        except httpx.HTTPStatusError as exc:
                            self._handle_status_error(exc)
                    async for line in response.aiter_lines():
                        if line.startswith("data:"):
                            data_str = line[5:].strip()
                            if data_str:
                                try:
                                    raw = json.loads(data_str)
                                except json.JSONDecodeError as exc:
                                    raise CoreClientError(f"malformed SSE frame: {exc}") from exc
                                try:
                                    frame = parse_sse_frame(raw)
                                except ValueError as exc:
                                    raise CoreClientError(f"malformed SSE frame: {exc}") from exc
                                yield frame
                                if isinstance(frame, ErrorFrame):
                                    return
        except asyncio.CancelledError:
            return
        except (httpx.StreamClosed, httpx.RequestError) as exc:
            if cancel is not None and cancel.is_closed:
                return
            raise CoreClientError(f"connection error: {exc}") from exc
        finally:
            if cancel is not None:
                cancel.detach()
