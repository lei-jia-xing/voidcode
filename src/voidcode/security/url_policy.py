from __future__ import annotations

import ipaddress
import socket
import urllib.parse
from dataclasses import dataclass

ALLOWED_SCHEMES = frozenset({"http", "https"})
BLOCKED_HOSTNAMES = ("localhost", "metadata", "metadata.google.internal")


@dataclass(frozen=True, slots=True)
class UrlValidationResult:
    url: str
    scheme: str
    hostname: str
    port: int | None


def _normalize_ip(address: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    parsed = ipaddress.ip_address(address)
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return parsed.ipv4_mapped
    return parsed


def _is_blocked_ip(address: str) -> bool:
    try:
        parsed = _normalize_ip(address)
    except ValueError:
        return False
    return bool(parsed.is_private or parsed.is_loopback or parsed.is_link_local or parsed.is_reserved or parsed.is_multicast or parsed.is_unspecified)


def _resolve_ips(hostname: str) -> tuple[str, ...]:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return ()
    addresses: list[str] = []
    for info in infos:
        if not info[4]:
            continue
        address = info[4][0]
        if not isinstance(address, str):
            continue
        addresses.append(address)
    return tuple(dict.fromkeys(addresses))


def validate_url(url_value: str) -> UrlValidationResult:
    parsed = urllib.parse.urlparse(url_value)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise ValueError("web_fetch url must start with http:// or https://")
    if not parsed.hostname:
        raise ValueError("web_fetch url must include a hostname")
    if parsed.username or parsed.password:
        raise ValueError("web_fetch url must not include credentials")

    hostname = parsed.hostname.lower().strip(".")
    if hostname in BLOCKED_HOSTNAMES:
        raise ValueError("web_fetch target host is blocked for security reasons")

    if _is_blocked_ip(hostname):
        raise ValueError("web_fetch target host is blocked for security reasons")

    for resolved in _resolve_ips(hostname):
        if _is_blocked_ip(resolved):
            raise ValueError("web_fetch target host is blocked for security reasons")

    return UrlValidationResult(
        url=url_value,
        scheme=parsed.scheme,
        hostname=hostname,
        port=parsed.port,
    )


def validate_redirect_target(*, base_url: str, location: str) -> UrlValidationResult:
    redirected_url = urllib.parse.urljoin(base_url, location)
    return validate_url(redirected_url)


__all__ = [
    "ALLOWED_SCHEMES",
    "BLOCKED_HOSTNAMES",
    "UrlValidationResult",
    "validate_redirect_target",
    "validate_url",
]
