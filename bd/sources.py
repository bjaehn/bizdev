"""Reading public pages politely: bounded, cached, rate-limited, and honest about what failed. No model, no scripts, no circumvention.

Every page that supports a claim is retrieved by this code (never taken from a search snippet or a model's report), hashed, and stored as a
snapshot of its visible text, so evidence can be checked against what was actually read and changes can be seen later.

What this refuses to do: bypass a login, a paywall or a challenge page (a challenge page is a failure, not an empty page), ignore robots.txt
(when configured to respect it), read a blocked domain or a private address, or follow redirects it has not checked."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.robotparser import RobotFileParser

import httpx2 as httpx

from bd.config import Config
from bd.htmltext import node_text, page_text, parse_html
from bd.urls import host_in, safe_urljoin, safe_urlparse
from bd.verify import UrlVerifier

OK_TYPES = ("text/html", "application/xhtml+xml", "application/json", "application/ld+json", "text/plain", "text/xml", "application/xml")
CHALLENGE = re.compile(r"(verify (that )?you are (a )?human|captcha|are you a robot|enable javascript and cookies to continue|"
                       r"attention required! \| cloudflare|checking your browser before accessing|access denied|request blocked)", re.I)


class FetchError(Exception):
    """A short machine-readable reason (timeout, http_403, robots_disallowed, challenge_page, ...). Never read as 'the page is empty'."""

    def __init__(self, reason: str, status: int | None = None) -> None:
        super().__init__(reason)
        self.reason, self.status = reason, status


@dataclass
class Fetched:
    url: str
    final_url: str
    status: int
    content_type: str
    body: str                      # the decoded response (HTML or JSON), capped
    truncated: bool
    content_hash: str              # sha256 of the capped bytes
    fetched_at: str
    headers: dict[str, str] = field(default_factory=dict)
    from_cache: bool = False


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def looks_like_challenge(text: str) -> bool:
    """A bot-check or block page served with a 200. Short pages only: a real page that mentions 'captcha' in passing is not one."""
    return len(text) < 4000 and bool(CHALLENGE.search(text))


class Fetcher:
    def __init__(self, cfg: Config, user_agent: str, client: httpx.Client | None = None, verifier: UrlVerifier | None = None,
                 sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.cfg, self.f = cfg, cfg.pe_portfolio.fetch
        self.ua = f"{user_agent} {self.f.user_agent_suffix}".strip()
        self._client = client or httpx.Client(timeout=self.f.timeout_seconds, follow_redirects=False,
                                              headers={"User-Agent": self.ua, "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.5"})
        self._verifier = verifier or UrlVerifier(cfg, self.ua, client=self._client)
        self._sleep, self._mono = sleep, monotonic
        self._last: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | str] = {}
        self.requests = 0

    # -- politeness --------------------------------------------------------------------------------------------------------------------------
    def _wait(self, host: str) -> None:
        gap = self.f.min_seconds_between_requests
        last = self._last.get(host)
        if last is not None:
            remaining = gap - (self._mono() - last)
            if remaining > 0:
                self._sleep(remaining)
        self._last[host] = self._mono()

    def _allowed(self, url: str) -> None:
        if not self.f.respect_robots:
            return
        p = safe_urlparse(url)
        if not p.scheme or not p.netloc:
            raise FetchError("bad_url")
        host = p.netloc.lower()
        if host not in self._robots:
            try:
                r = self._raw(f"{p.scheme}://{p.netloc}/robots.txt", robots=True)
                rp = RobotFileParser()
                rp.parse(r.body.splitlines())
                self._robots[host] = rp
            except FetchError as e:
                # 404 (and other 4xx) means there is no robots.txt: allowed. A server that cannot answer is not a licence to crawl.
                self._robots[host] = "allow" if e.status is not None and 400 <= e.status < 500 else "unavailable"
        rp = self._robots[host]
        if rp == "unavailable":
            raise FetchError("robots_unavailable")
        if isinstance(rp, RobotFileParser) and not (rp.can_fetch(self.f.user_agent_suffix, url) and rp.can_fetch("*", url)):
            raise FetchError("robots_disallowed")

    # -- one request, redirects checked by hand ------------------------------------------------------------------------------------------------
    def _raw(self, url: str, robots: bool = False) -> Fetched:
        current, hops = url.strip(), 0
        while True:
            p = safe_urlparse(current)
            if p.scheme not in ("http", "https") or not p.hostname:
                raise FetchError("bad_url")
            if host_in(p.hostname, self.cfg.sources.blocked_domains):
                raise FetchError("blocked_domain")
            problem = self._verifier.host_problem(p.hostname)
            if problem:
                raise FetchError(problem)
            self._wait(p.hostname)
            self.requests += 1
            try:
                with self._client.stream("GET", current) as r:
                    status, headers = r.status_code, {k.lower(): v for k, v in r.headers.items()}
                    if status in (301, 302, 303, 307, 308) and headers.get("location"):
                        hops += 1
                        if hops > self.cfg.verify.max_redirects:
                            raise FetchError("too_many_redirects")
                        current = safe_urljoin(current, headers["location"])
                        continue
                    ctype = headers.get("content-type", "").split(";")[0].strip().lower()
                    if not 200 <= status < 300:
                        raise FetchError(f"http_{status}", status)
                    if not robots and ctype and not any(ctype.startswith(t) for t in OK_TYPES):
                        raise FetchError(f"unsupported_content_type:{ctype}", status)
                    raw, truncated = bytearray(), False
                    for chunk in r.iter_bytes():
                        raw += chunk
                        if len(raw) >= self.f.max_bytes:
                            raw, truncated = raw[: self.f.max_bytes], True
                            break
            except httpx.TimeoutException:
                raise FetchError("timeout") from None
            except httpx.LocalProtocolError:
                raise FetchError("bad_request_header") from None
            except httpx.HTTPError as e:
                raise FetchError(f"connect_error:{type(e).__name__}") from None
            m = re.search(r"charset=([\w-]+)", headers.get("content-type", ""), re.I)
            try:
                body = bytes(raw).decode(m.group(1) if m else "utf-8", errors="replace")
            except LookupError:
                body = bytes(raw).decode("utf-8", errors="replace")
            return Fetched(url, current, status, ctype, body, truncated, hashlib.sha256(bytes(raw)).hexdigest(), now_iso(),
                           {k: headers[k] for k in ("x-wp-total", "x-wp-totalpages", "etag", "last-modified") if k in headers})

    def get(self, url: str, retries: int | None = None) -> Fetched:
        """Fetch one URL with retries on timeouts, connection errors, 429 and 5xx (never on other 4xx). Raises FetchError.
        `retries` overrides the configured count (0 for a URL that is only a guess: a host that does not exist is an answer)."""
        self._allowed(url)
        last: FetchError | None = None
        attempts = self.f.max_retries if retries is None else retries
        for attempt in range(attempts + 1):
            try:
                got = self._raw(url)
            except FetchError as e:
                last = e
                retryable = e.reason in ("timeout", "http_429") or e.reason.startswith("connect_error") or (e.status is not None and e.status >= 500)
                if retryable and attempt < attempts:
                    self._sleep(self.f.retry_backoff_seconds * (2 ** attempt))
                    continue
                raise
            if "html" in got.content_type and looks_like_challenge(page_text(parse_html(got.body))):
                raise FetchError("challenge_page", got.status)
            return got
        raise last or FetchError("unknown")

    def close(self) -> None:
        self._client.close()


# ---- snapshots ---------------------------------------------------------------------------------------------------------------------------------
def visible_text(f: Fetched, cap: int) -> tuple[str, bool]:
    """The text that evidence excerpts are checked against: for HTML, the visible text; for JSON, the decoded document re-serialised without
    escapes (so a name with an accent or an apostrophe appears as written); otherwise the body."""
    if "html" in f.content_type or not f.content_type:
        text = page_text(parse_html(f.body))
    elif "json" in f.content_type:
        try:
            text = json.dumps(json.loads(f.body), ensure_ascii=False, separators=(", ", ": "))
        except ValueError:
            text = f.body
    else:
        text = f.body
    return (text[:cap], True) if len(text) > cap else (text, f.truncated)


def store_snapshot(conn: sqlite3.Connection, cfg: Config, source_id: int | None, f: Fetched) -> int:
    """Store what was read. The same page content is stored once (its read time moves forward); a changed page is a new snapshot, and only the
    newest `snapshot_keep` per URL are kept. Caller owns the transaction."""
    text, truncated = visible_text(f, cfg.pe_portfolio.text_cap_bytes)
    title = parse_html(f.body).title if "html" in f.content_type else None
    cur = conn.execute("INSERT OR IGNORE INTO pe_snapshots(source_id, url, fetched_at, http_status, content_type, bytes, content_hash, title, text, truncated) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)", (source_id, f.url, f.fetched_at, f.status, f.content_type, len(f.body.encode("utf-8")),
                                                         f.content_hash, title, text, int(truncated)))
    if cur.rowcount:
        sid = cur.lastrowid
    else:
        row = conn.execute("SELECT id FROM pe_snapshots WHERE url = ? AND content_hash = ?", (f.url, f.content_hash)).fetchone()
        sid = row["id"]
        conn.execute("UPDATE pe_snapshots SET fetched_at = ?, source_id = COALESCE(source_id, ?) WHERE id = ? AND fetched_at < ?", (f.fetched_at, source_id, sid, f.fetched_at))
    old = conn.execute("SELECT id FROM pe_snapshots WHERE url = ? ORDER BY fetched_at DESC, id DESC LIMIT -1 OFFSET ?",
                       (f.url, cfg.pe_portfolio.snapshot_keep)).fetchall()
    for r in old:
        conn.execute("DELETE FROM pe_snapshots WHERE id = ?", (r["id"],))
    return sid


def latest_snapshot(conn: sqlite3.Connection, url: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM pe_snapshots WHERE url = ? ORDER BY fetched_at DESC, id DESC LIMIT 1", (url,)).fetchone()


def recently_read(conn: sqlite3.Connection, url: str, cfg: Config, now: datetime | None = None) -> sqlite3.Row | None:
    """The last snapshot of this URL if it was read within cache_hours (so a refresh need not fetch it again), else None."""
    snap = latest_snapshot(conn, url)
    if snap is None or snap["truncated"]:
        return None
    age = (now or datetime.now(timezone.utc)) - datetime.fromisoformat(snap["fetched_at"])
    return snap if age <= timedelta(hours=cfg.pe_portfolio.fetch.cache_hours) else None
