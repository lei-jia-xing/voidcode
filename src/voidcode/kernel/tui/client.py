"""HTTP client for the VoidCode kernel transport."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger(__name__)


class KernelClientError(Exception):
    def __init__(self, message: str, *, status_code: int = 0, code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class KernelHttpClient:
    """Thin stdlib-backed HTTP/SSE client for a running kernel transport."""

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
            raise KernelClientError(err_msg, status_code=exc.code, code=err_code) from exc
        except urllib.error.URLError as exc:
            raise KernelClientError(f"connection error: {exc.reason}") from exc

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
        client_capabilities: str = "",
    ) -> dict[str, Any]:
        caps = [c.strip() for c in client_capabilities.split(",") if c.strip()] if isinstance(client_capabilities, str) else list(client_capabilities)
        body: dict[str, Any] = {
            "arguments": arguments or {},
            # The revision the client actually displayed; never a default.
            "expected_revision": expected_revision,
            "client_capabilities": caps,
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
        client_capabilities: str = "",
    ) -> dict[str, Any]:
        caps = [c.strip() for c in client_capabilities.split(",") if c.strip()] if isinstance(client_capabilities, str) else list(client_capabilities)
        body: dict[str, Any] = {
            "request_id": request_id,
            "decision": decision,
            "expected_revision": expected_revision,
            "client_capabilities": caps,
        }
        return self._request(
            "POST",
            f"/api/sessions/{urllib.parse.quote(session_id)}/confirm",
            body=body,
            timeout=60.0,
        )

    def views(self, session_id: str, *, client_capabilities: str = "") -> tuple[str, list[dict[str, Any]]]:
        """The rendered views and the revision of the snapshot that rendered them."""
        res = self._request(
            "GET",
            f"/api/sessions/{urllib.parse.quote(session_id)}/views",
            query={"client_capabilities": client_capabilities} if client_capabilities else None,
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
                                except Exception:
                                    logger.warning("failed to decode SSE JSON payload: %r", data_str)
