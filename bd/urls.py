"""URL helpers shared by the verifier, scanners and eval harness."""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import ParseResult, parse_qsl, urlencode, urljoin, urlparse, urlunparse

_TRACKING = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid")


def safe_urlparse(url: str) -> ParseResult:
    """urlparse that never raises. A page can hold a template placeholder or a typo ("https://[tenant].example.com") that urlparse rejects with
    ValueError; such a URL parses as an empty one with no host, so it is dropped like any other bad link instead of stopping a whole run."""
    try:
        return urlparse((url or "").strip())
    except ValueError:
        return ParseResult("", "", "", "", "", "")


def safe_urljoin(base: str, href: str) -> str:
    """urljoin that never raises: "" when the link cannot be made into a URL."""
    try:
        return urljoin(base, href)
    except ValueError:
        return ""


def hostname(url: str) -> str:
    return (safe_urlparse(url).hostname or "").lower().removeprefix("www.")


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
    p = safe_urlparse(url)
    host = (p.hostname or "").lower().removeprefix("www.")
    query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(_TRACKING)))
    path = p.path.rstrip("/")
    return urlunparse(("", host, path, "", query, "")).lstrip("/") if host else url.strip()
