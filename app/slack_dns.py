"""Process-local DNS fallback for public web hosts.

Some local/router resolvers answer normal Slack API names but REFUSE
particular public domains. Keep the workaround process-local: only after the
system resolver raises, resolve public hostnames over Cloudflare DNS-over-HTTPS.
Local names, IP literals, and single-label hosts never leave the machine. TLS
still receives and verifies the original hostname.
"""

from __future__ import annotations

import logging
import ipaddress
import socket
import threading
import time
from typing import Any, Callable

import httpx

logger = logging.getLogger(__name__)
_CACHE_SECONDS = 300
_cache: dict[str, tuple[float, list[str]]] = {}
_lock = threading.Lock()


def install_slack_socket_dns_fallback() -> None:
    original = socket.getaddrinfo
    if getattr(original, "_slack_socket_dns_fallback", False):
        return

    def getaddrinfo(
        host: str | bytes | None, port: Any, family: int = 0, type: int = 0,
        proto: int = 0, flags: int = 0,
    ) -> list[tuple[Any, ...]]:
        try:
            return original(host, port, family, type, proto, flags)
        except socket.gaierror:
            hostname = host.decode() if isinstance(host, bytes) else str(host or "")
            if not _is_public_hostname(hostname):
                raise
            addresses = _resolve_a_records(hostname)
            if not addresses:
                raise
            logger.warning(
                "system DNS failed for %s; using process-local DoH fallback", hostname
            )
            results: list[tuple[Any, ...]] = []
            for address in addresses:
                results.extend(original(address, port, family, type, proto, flags))
            return results

    setattr(getaddrinfo, "_slack_socket_dns_fallback", True)
    socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]


def _resolve_a_records(hostname: str) -> list[str]:
    now = time.monotonic()
    with _lock:
        cached = _cache.get(hostname)
        if cached and cached[0] > now:
            return list(cached[1])
    try:
        response = httpx.get(
            "https://1.1.1.1/dns-query",
            params={"name": hostname, "type": "A"},
            headers={"accept": "application/dns-json"}, timeout=8,
        )
        response.raise_for_status()
        addresses = [
            str(item.get("data", ""))
            for item in response.json().get("Answer", [])
            if item.get("type") == 1 and item.get("data")
        ]
    except Exception as exc:
        logger.warning("DoH fallback failed for %s: %s", hostname, exc)
        return []
    with _lock:
        _cache[hostname] = (now + _CACHE_SECONDS, addresses)
    return addresses


def _is_public_hostname(hostname: str) -> bool:
    hostname = hostname.strip().lower().rstrip(".")
    if not hostname or "." not in hostname:
        return False
    if hostname in {"localhost", "localhost.localdomain"}:
        return False
    if hostname.endswith((".local", ".internal", ".localhost", ".lan", ".home")):
        return False
    try:
        ipaddress.ip_address(hostname)
        return False
    except ValueError:
        return True
