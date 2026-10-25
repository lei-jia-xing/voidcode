"""HTTP client for the VoidCode core transport."""

from __future__ import annotations

import json
import logging
import threading
import urllib.parse
from collections.abc import Iterator
from typing import Any, NoReturn

import httpx

logger = logging.getLogger(__name__)


class CoreClientError(Exception):
    def __init__(self, message: str, *, status_code: int = 0, code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class StreamCancellation:
    """Cancels one owned event-stream connection from another thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._closed = False
        self._response: httpx.Response | None = None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            response, self._response = self._response, None
        if response is not None:
            response.close()

    def _attach(self, response: httpx.Response) -> None:
        with self._lock:
            if self._closed:
                stale = response
            else:
                stale, self._response = self._response, response
        if stale is not None:
            stale.close()

    def _detach(self) -> None:
        with self._lock:
            self._response = None


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
        err_msg = f"HTTP {exc.response.status_code}"
        err_code = None
        try:
            data = exc.response.json()
            if isinstance(data, dict):
                err_msg = str(data.get("error", err_msg))
                err_code = data.get("code")
        except Exception:
            pass
        raise CoreClientError(err_msg, status_code=exc.response.status_code, code=err_code) from exc

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

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/api/health")

    def command_catalog(self) -> tuple[str, list[dict[str, Any]]]:
        res = self._request("GET", "/api/commands")
        return str(res.get("revision_id", "")), list(res.get("commands", []))

    def sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        res = self._request("GET", "/api/sessions", query={"limit": limit})
        return list(res.get("sessions", []))

    list_sessions = sessions

    def create_session(self, session_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if session_id:
            payload["session_id"] = session_id
        return self._request("POST", "/api/sessions", body=payload)

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/sessions/{urllib.parse.quote(session_id)}")

    def show(self, session_id: str, events: bool = False) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/sessions/{urllib.parse.quote(session_id)}/show",
            query={"events": "true" if events else "false"},
        )

    def run(self, session_id: str, prompt: str, max_steps: int | None = None) -> dict[str, Any]:
        steps = 32 if max_steps is None else max_steps
        return self._request(
            "POST",
            f"/api/sessions/{urllib.parse.quote(session_id)}/run",
            body={"prompt": prompt, "session_id": session_id, "max_steps": steps},
            timeout=300.0,
        )

    def resume(
        self,
        session_id: str,
        approval_request_id: str | None = None,
        decision: str | None = None,
        max_steps: int | None = None,
    ) -> dict[str, Any]:
        steps = 32 if max_steps is None else max_steps
        body: dict[str, Any] = {"max_steps": steps}
        if approval_request_id is not None:
            body["approval_request_id"] = approval_request_id
        if decision is not None:
            body["decision"] = decision
        return self._request(
            "POST",
            f"/api/sessions/{urllib.parse.quote(session_id)}/resume",
            body=body,
            timeout=300.0,
        )

    def invoke_command(
        self,
        session_id: str,
        name: str,
        args: dict[str, Any] | None = None,
        expected_revision: str = "",
        *,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cmd_args = args if args is not None else (arguments or {})
        body: dict[str, Any] = {
            "arguments": cmd_args,
            "expected_revision": expected_revision,
        }
        return self._request(
            "POST",
            f"/api/sessions/{urllib.parse.quote(session_id)}/invoke/{urllib.parse.quote(name)}",
            body=body,
            timeout=120.0,
        )

    def confirm(
        self,
        session_id: str,
        request_id: str,
        decision: str,
        expected_revision: str = "",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "request_id": request_id,
            "decision": decision,
            "expected_revision": expected_revision,
        }
        return self._request(
            "POST",
            f"/api/sessions/{urllib.parse.quote(session_id)}/confirm",
            body=body,
            timeout=60.0,
        )

    def views(self, session_id: str) -> tuple[str, list[dict[str, Any]]]:
        res = self._request("GET", f"/api/sessions/{urllib.parse.quote(session_id)}/views")
        return str(res.get("revision_id", "")), list(res.get("views", []))

    def drain(self, session_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        res = self._request(
            "POST",
            f"/api/sessions/{urllib.parse.quote(session_id)}/continuations",
            body={"limit": limit},
            timeout=120.0,
        )
        return list(res.get("continuations", []))

    def usage(self, session_id: str, *, since_sequence: int = 0, until_sequence: int | None = None) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/sessions/{urllib.parse.quote(session_id)}/usage",
            query={"since_sequence": since_sequence, "until_sequence": until_sequence},
        )

    def cancel(self, session_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/sessions/{urllib.parse.quote(session_id)}/cancel")

    def stream_events(
        self,
        session_id: str,
        after_sequence: int = 0,
        *,
        follow: bool = True,
        cancel: StreamCancellation | None = None,
    ) -> Iterator[dict[str, Any]]:
        params = {
            "after_sequence": after_sequence,
            "follow": "true" if follow else "false",
        }
        headers = {
            "Accept": "text/event-stream",
            "Origin": self.origin,
        }
        url = f"/api/sessions/{urllib.parse.quote(session_id)}/events"
        try:
            with self._client.stream("GET", url, params=params, headers=headers, timeout=None) as response:
                if response.is_error:
                    response.read()
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        self._handle_status_error(exc)
                if cancel is not None:
                    cancel._attach(response)
                try:
                    for line in response.iter_lines():
                        if line.startswith("data:"):
                            data_str = line[5:].strip()
                            if data_str:
                                try:
                                    yield json.loads(data_str)
                                except json.JSONDecodeError as exc:
                                    raise CoreClientError(f"malformed SSE frame: {exc}") from exc
                finally:
                    if cancel is not None:
                        cancel._detach()
        except (httpx.StreamClosed, httpx.RequestError) as exc:
            if cancel is not None and cancel._closed:
                return
            raise CoreClientError(f"connection error: {exc}") from exc

    events_stream = stream_events
