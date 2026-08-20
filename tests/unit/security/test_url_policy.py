from __future__ import annotations

import pytest

from voidcode.security.url_policy import (
    ALLOWED_SCHEMES,
    BLOCKED_HOSTNAMES,
    UrlValidationResult,
    validate_redirect_target,
    validate_url,
)


def test_allowed_schemes_are_http_and_https() -> None:
    assert ALLOWED_SCHEMES == frozenset({"http", "https"})


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_validate_url_accepts_allowed_schemes(scheme: str) -> None:
    result = validate_url(f"{scheme}://example.com/path")
    assert result.scheme == scheme
    assert result.hostname == "example.com"


@pytest.mark.parametrize("scheme", ["ftp", "file", "ws", "gopher", ""])
def test_validate_url_rejects_disallowed_schemes(scheme: str) -> None:
    with pytest.raises(ValueError, match="start with http:// or https://"):
        validate_url(f"{scheme}://example.com")


def test_validate_url_rejects_missing_hostname() -> None:
    with pytest.raises(ValueError, match="include a hostname"):
        validate_url("https:///path")


@pytest.mark.parametrize(
    "url",
    [
        "http://user@example.com/",
        "http://:pass@example.com/",
        "http://user:pass@example.com/",
    ],
)
def test_validate_url_rejects_credentials(url: str) -> None:
    with pytest.raises(ValueError, match="must not include credentials"):
        validate_url(url)


@pytest.mark.parametrize("hostname", BLOCKED_HOSTNAMES)
def test_validate_url_rejects_blocked_hostnames(hostname: str) -> None:
    with pytest.raises(ValueError, match="blocked for security reasons"):
        validate_url(f"http://{hostname}/")


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.1.2.3",
        "10.0.0.5",
        "172.16.0.1",
        "192.168.1.100",
        "169.254.169.254",
        "0.0.0.0",
        "[::1]",
        "[::ffff:127.0.0.1]",
    ],
)
def test_validate_url_rejects_blocked_ip_targets(host: str) -> None:
    with pytest.raises(ValueError, match="blocked for security reasons"):
        validate_url(f"http://{host}/")


def test_validate_url_rejects_private_ip_from_dns_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force resolution of a public-looking hostname to a private address to
    # prove the DNS-resolution guard runs and blocks the outcome.
    import socket

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda hostname, port, *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.5", 0))],
    )
    with pytest.raises(ValueError, match="blocked for security reasons"):
        validate_url("http://example.com/")


def test_validate_url_accepts_public_ip() -> None:
    result = validate_url("http://8.8.8.8/")
    assert result.hostname == "8.8.8.8"
    assert result.port is None


def test_validate_url_records_explicit_port() -> None:
    result = validate_url("https://example.com:8443/path")
    assert result.port == 8443
    assert result.hostname == "example.com"
    assert isinstance(result, UrlValidationResult)


def test_validate_url_normalizes_hostname_case_and_dots() -> None:
    result = validate_url("https://EXAMPLE.COM./")
    assert result.hostname == "example.com"


def test_validate_url_rejects_blocked_hostname_with_case_variation() -> None:
    with pytest.raises(ValueError, match="blocked for security reasons"):
        validate_url("http://LocalHost/")


def test_validate_redirect_target_validates_joined_url() -> None:
    result = validate_redirect_target(base_url="https://example.com/a/b", location="/c")
    assert result.url == "https://example.com/c"
    assert result.hostname == "example.com"


def test_validate_redirect_target_rejects_redirect_to_blocked_host() -> None:
    with pytest.raises(ValueError, match="blocked for security reasons"):
        validate_redirect_target(base_url="https://example.com/a", location="http://localhost/x")
