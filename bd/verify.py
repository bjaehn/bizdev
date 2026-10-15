"""URL verifier (design rule 2): a source_url is accepted only if code can actually fetch it.

Redirects are followed by hand so every hop is checked against the blocked-domain list and
(optionally) for private/loopback addresses. Only the status line and headers are read; the body
is never downloaded.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx2 as httpx

from bd.config import Config
from bd.urls import host_in

Resolver = Callable[[str], list[str]]


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    reason: str  # "ok" or a short machine-readable reason
    status: int | None = None
    final_url: str | None = None


def _system_resolver(host: str) -> list[str]:
    return sorted({ai[4][0] for ai in socket.getaddrinfo(host, None)})


def _proxy_configured() -> bool:
    return any(os.environ.get(k) for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"))


class UrlVerifier:
    def __init__(self, cfg: Config, user_agent: str, client: httpx.Client | None = None,
                 resolver: Resolver | None = None) -> None:
        self._cfg = cfg
        self._client = client or httpx.Client(
            timeout=cfg.verify.timeout_seconds,
            follow_redirects=False,
            headers={"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
        )
        self._resolver = resolver or _system_resolver

    def _address_problem(self, host: str) -> str | None:
        if not self._cfg.verify.block_private_addresses:
            return None
        try:
            addrs = self._resolver(host)
        except OSError:
            # Behind an egress proxy the proxy resolves names (and enforces egress policy);
            # without one, an unresolvable host is simply unreachable.
            return None if _proxy_configured() else "dns_failure"
        if not addrs:
            return "dns_failure"
        for a in addrs:
            if not ipaddress.ip_address(a).is_global:
                return "private_address"
        return None

    def verify(self, url: str) -> VerifyResult:
        current = url.strip()
        for _ in range(self._cfg.verify.max_redirects + 1):
            p = urlparse(current)
            if p.scheme not in ("http", "https") or not p.hostname:
                return VerifyResult(False, "bad_url", final_url=current)
            if host_in(p.hostname, self._cfg.sources.blocked_domains):
                return VerifyResult(False, "blocked_domain", final_url=current)
            problem = self._address_problem(p.hostname)
            if problem:
                return VerifyResult(False, problem, final_url=current)
            try:
                with self._client.stream("GET", current) as r:
                    status, location = r.status_code, r.headers.get("location")
            except httpx.TimeoutException:
                return VerifyResult(False, "timeout", final_url=current)
            except httpx.HTTPError as e:
                return VerifyResult(False, f"connect_error:{type(e).__name__}", final_url=current)
            if status in (301, 302, 303, 307, 308) and location:
                current = urljoin(current, location)
                continue
            if 200 <= status < 300:
                return VerifyResult(True, "ok", status=status, final_url=current)
            return VerifyResult(False, f"http_{status}", status=status, final_url=current)
        return VerifyResult(False, "too_many_redirects", final_url=current)

    def close(self) -> None:
        self._client.close()
