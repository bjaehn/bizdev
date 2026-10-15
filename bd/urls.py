"""URL helpers shared by the verifier, scanners and eval harness."""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

_TRACKING = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid")


def hostname(url: str) -> str:
    return (urlparse(url.strip()).hostname or "").lower().removeprefix("www.")


def host_in(host: str, domains: Iterable[str]) -> bool:
    """True if host equals, or is a subdomain of, any listed domain."""
    host = host.lower().removeprefix("www.")
    for d in domains:
        d = d.lower().removeprefix("www.")
        if d and (host == d or host.endswith("." + d)):
            return True
    return False


def normalize_url(url: str) -> str:
    """Comparison key: scheme-insensitive, www-insensitive, no fragment, no tracking params,
    no trailing slash."""
    p = urlparse(url.strip())
    host = (p.hostname or "").lower().removeprefix("www.")
    query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(_TRACKING)))
    path = p.path.rstrip("/")
    return urlunparse(("", host, path, "", query, "")).lstrip("/") if host else url.strip()
