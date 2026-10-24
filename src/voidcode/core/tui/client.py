"""HTTP client for the VoidCode core transport."""

from __future__ import annotations

import errno
import json
import logging
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger(__name__)


class CoreClientError(Exception):
    def __init__(self, message: str, *, status_code: int = 0, code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class StreamCancellation:
    """Cancels one owned event-stream connection from another thread.

    A follow stream deliberately opens with ``timeout=None`` because idle is normal. A blocked
    ``readline`` on that response cannot be woken by closing an event or by the response object
    from another thread, so the response's public file descriptor is duplicated once and
    ``close`` shuts down the shared connection; the read then returns and the stream's own context
    manager still closes its response. ``close`` is idempotent, and attaching after a close shuts
    the connection down immediately (open/close race). Callers that stop the stream must call
    ``close`` before joining its worker.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._closed = False
        self._socket: socket.socket | None = None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            duplicate, self._socket = self._socket, None
        _shutdown_socket(duplicate)

    def _attach(self, response: Any) -> None:
        # Borrow the response's fd without taking ownership, duplicate it through the portable
        # public Socket API, then release the borrowed object in all outcomes.
        borrowed = socket.socket(fileno=response.fileno())
        try:
            duplicate = borrowed.dup()
        finally:
            borrowed.detach()
        with self._lock:
            if self._closed:
                stale, duplicate = duplicate, None
            else:
                stale, self._socket = self._socket, duplicate
                duplicate = None
        _shutdown_socket(stale)

    def _detach(self) -> None:
        with self._lock:
            duplicate, self._socket = self._socket, None
        if duplicate is not None:
            duplicate.close()


def _shutdown_socket(duplicate: socket.socket | None) -> None:
    if duplicate is None:
        return
    try:
        duplicate.shutdown(socket.SHUT_RDWR)
    except OSError as exc:
        # A stream may already be gone when quit/close races the peer; that is the expected case.
        expected = {errno.ENOTCONN, errno.EBADF, getattr(errno, "WSAENOTCONN", errno.ENOTCONN), getattr(errno, "WSAEBADF", errno.EBADF)}
        if exc.errno not in expected:
            raise
    finally:
        duplicate.close()


class CoreHttpClient:
    """Thin stdlib-backed HTTP/SSE client for a running core transport."""

    def __init__(self, base_url: str, *, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        parsed = urllib.parse.urlparse(self.base_url)
        self.origin = f"{parsed.scheme}://{parsed.netloc}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if query:
            clean_query = {k: v for k, v in query.items() if v is not None}
            if clean_query:
                url = f"{url}?{urllib.parse.urlencode(clean_query)}"

        headers = {
            "Accept": "application/json",
            "Origin": self.origin,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        data: bytes | None = None
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
            data = json.dumps(body, allow_nan=False).encode("utf-8")

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                content = resp.read()
                if not content:
                    return {}
                return json.loads(content.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", "replace")
            err_msg = f"HTTP {exc.code}"
            err_code = None
            try:
                parsed_json = json.loads(err_body)
                if isinstance(parsed_json, dict):
                    err_msg = str(parsed_json.get("error", err_msg))
                    err_code = parsed_json.get("code")
            except Exception:
                pass
            raise CoreClientError(err_msg, status_code=exc.code, code=err_code) from exc
        except urllib.error.URLError as exc:
            raise CoreClientError(f"connection error: {exc.reason}") from exc

    def health(self) -> dict[str, Any]:
        """Liveness/identity read (contracted /api/health shape; backend-owned schema)."""
        return self._request("GET", "/api/health")

    def command_catalog(self) -> tuple[str, list[dict[str, Any]]]:
        """The catalog and the capability revision it was captured under."""
        res = self._request("GET", "/api/commands")
        return str(res.get("revision_id", "")), list(res.get("commands", []))

    def list_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        res = self._request("GET", "/api/sessions", query={"limit": limit})
        return res.get("sessions", [])

    def create_session(self, session_id: str | None = None) -> dict[str, Any]:
        payload = {"session_id": session_id} if session_id else {}
        return self._request("POST", "/api/sessions", body=payload)

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/sessions/{urllib.parse.quote(session_id)}")

    def show(self, session_id: str, *, events: bool = False) -> dict[str, Any]:
        return self._request("GET", f"/api/sessions/{urllib.parse.quote(session_id)}/show", query={"events": "true" if events else "false"})

    def run(self, session_id: str, prompt: str, *, max_steps: int = 32) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/sessions/{urllib.parse.quote(session_id)}/run",
            body={"prompt": prompt, "session_id": session_id, "max_steps": max_steps},
            timeout=300.0,
        )

    def resume(
        self,
        session_id: str,
        *,
        approval_request_id: str | None = None,
        decision: str | None = None,
        max_steps: int = 32,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"max_steps": max_steps}
        if approval_request_id is not None:
            body["approval_request_id"] = approval_request_id
        if decision is not None:
            body["decision"] = decision
        return self._request("POST", f"/api/sessions/{urllib.parse.quote(session_id)}/resume", body=body, timeout=300.0)

    def invoke_command(
        self,
        session_id: str,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        expected_revision: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "arguments": arguments or {},
            # The revision the client actually displayed; never a default.
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
        *,
        expected_revision: str,
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
        """The rendered views and the revision of the snapshot that rendered them."""
        res = self._request(
            "GET",
            f"/api/sessions/{urllib.parse.quote(session_id)}/views",
        )
        return str(res.get("revision_id", "")), list(res.get("views", []))

    def drain(self, session_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        res = self._request("POST", f"/api/sessions/{urllib.parse.quote(session_id)}/continuations", body={"limit": limit}, timeout=120.0)
        return res.get("continuations", [])

    def usage(self, session_id: str, *, since_sequence: int = 0, until_sequence: int | None = None) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/sessions/{urllib.parse.quote(session_id)}/usage",
            query={"since_sequence": since_sequence, "until_sequence": until_sequence},
        )

    def cancel(self, session_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/sessions/{urllib.parse.quote(session_id)}/cancel")

    def events_stream(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
        follow: bool = True,
        cancel: StreamCancellation | None = None,
    ) -> Iterator[dict[str, Any]]:
        query = {
            "after_sequence": after_sequence,
            "follow": "true" if follow else "false",
        }
        url = f"{self.base_url}/api/sessions/{urllib.parse.quote(session_id)}/events?{urllib.parse.urlencode(query)}"
        headers = {
            "Accept": "text/event-stream",
            "Origin": self.origin,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=None) as resp:
            if cancel is not None:
                cancel._attach(resp)
            try:
                buffer = ""
                for raw_chunk in resp:
                    chunk = raw_chunk.decode("utf-8", "replace")
                    buffer += chunk
                    while "\n\n" in buffer:
                        raw_event, buffer = buffer.split("\n\n", 1)
                        raw_event = raw_event.strip()
                        if not raw_event:
                            continue
                        for line in raw_event.splitlines():
                            if line.startswith("data:"):
                                data_str = line[5:].strip()
                                if data_str:
                                    try:
                                        yield json.loads(data_str)
                                    except json.JSONDecodeError as exc:
                                        # A malformed frame fails the connection
                                        # visibly: skipping fact N and applying N+1
                                        # would advance the resume cursor past a
                                        # missed fact. The worker reconnects from
                                        # the own applied cursor.
                                        raise CoreClientError(f"malformed SSE frame: {exc}") from exc
            finally:
                if cancel is not None:
                    cancel._detach()
