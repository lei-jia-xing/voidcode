"""HTTP credential/header ownership for the OpenAI Chat Completions wire.

The facade in :mod:`openai_native` owns SDK client construction so the existing
``openai_native.OpenAI`` monkeypatch seam remains effective. This module owns the
transport state, auth/header resolution, and resource closing.
"""

from __future__ import annotations

from collections.abc import Mapping
from threading import Lock

import httpx2
from openai import omit

from .openai_wire import DEFAULT_OPENAI_BASE_URL, OpenAIWireCodec


class OpenAIHttpTransport:
    """Shared HTTP state and credential/header policy for one provider."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_OPENAI_BASE_URL,
        api_key: str | None = None,
        organization: str | None = None,
        project: str | None = None,
        auth_header: str | None = None,
        auth_scheme: str = "bearer",
        ssl_verify: bool | None = None,
        http_client: httpx2.Client | None = None,
    ) -> None:
        self.base_url = OpenAIWireCodec.normalize_base_url(base_url)
        self.api_key = api_key
        self.organization = organization
        self.project = project
        self.auth_header = auth_header
        self.auth_scheme = auth_scheme
        self.ssl_verify = ssl_verify
        self.http_client = http_client
        self._client_lock = Lock()
        self._sdk_client: object | None = None

    def _auth_headers(self) -> dict[str, str]:
        """Resolve the configured auth scheme into the headers this wire carries.

        Mirrors the deleted LiteLLM contract and ``model_catalog._headers_for_discovery``:
        ``none`` (or a missing key) sends no credential at all, ``token`` sends the
        raw key, ``bearer`` sends ``Bearer <key>``, and ``auth_header`` only selects
        which header carries it.
        """
        if not self.api_key or self.auth_scheme == "none":
            return {}
        header_name = self.auth_header or "Authorization"
        if self.auth_scheme == "token":
            return {header_name: self.api_key}
        return {header_name: f"Bearer {self.api_key}"}

    def _request_headers(self, payload: Mapping[str, object]) -> dict[str, object]:
        """Return the SDK kwargs for a request, dropping the SDK's own auth default.

        The SDK always adds ``Authorization: Bearer <api_key>`` for whichever key it
        was constructed with (the configured key, or the placeholder when there is
        none). When the resolved scheme does not use that header the default has to be
        omitted per request: ``default_headers`` can add headers but cannot remove one
        the SDK owns. A per-request ``extra_headers`` override still wins.
        """
        kwargs: dict[str, object] = dict(payload)
        auth_headers = self._auth_headers()
        if any(name.lower() == "authorization" for name in auth_headers):
            return kwargs
        extra = kwargs.get("extra_headers")
        merged: dict[str, object] = dict(extra) if isinstance(extra, Mapping) else {}
        if not any(str(name).lower() == "authorization" for name in merged):
            merged["Authorization"] = omit
            kwargs["extra_headers"] = merged
        return kwargs

    def close(self) -> None:
        with self._client_lock:
            client, self._sdk_client = self._sdk_client, None
        if client is not None and self.http_client is None:
            closer = getattr(client, "close", None)
            if callable(closer):
                closer()


__all__ = ["OpenAIHttpTransport"]
