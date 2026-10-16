"""Read a company's own job board directly from its applicant-tracking system: Greenhouse, Lever and Ashby publish
their boards as public JSON, so no model and no web search are needed. That makes it free, complete (every open
posting, not the few a search happens to surface) and fast enough to run daily.

Only three fixed hosts are ever contacted. The company's board id is learned from posting URLs the model scanner
already verified (`board_from_url`), never guessed from the company name.

NOTE: the response shapes below (Greenhouse boards-api `jobs[].{title, absolute_url, first_published}`, Lever
`[{text, hostedUrl, createdAt}]` with createdAt in epoch milliseconds, Ashby `jobs[].{title, jobUrl, publishedAt,
isListed}`) are the documented public layouts as I know them; they could not be fetched from the build sandbox.
Parsing is defensive: an entry without a usable date or URL is skipped (a posting with no date is never dated by us),
and an unexpected top-level shape is an AtsError, never "no postings"."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from urllib.parse import urlparse

import httpx2 as httpx

from bd.config import Config

PROVIDERS = ("greenhouse", "lever", "ashby")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
_API = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
    "lever": "https://api.lever.co/v0/postings/{token}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{token}",
}
MAX_BYTES = 8_000_000


class AtsError(Exception):
    pass


@dataclass(frozen=True)
class RawPosting:
    title: str
    url: str
    posted: date


def board_from_url(url: str) -> tuple[str, str] | None:
    """(provider, board id) if the URL is a posting or board page at a supported provider, else None."""
    u = urlparse((url or "").strip())
    host = (u.hostname or "").lower()
    parts = [p for p in u.path.split("/") if p]
    token = None
    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io") and parts:
        provider, token = "greenhouse", parts[0]
    elif host == "jobs.lever.co" and parts:
        provider, token = "lever", parts[0]
    elif host == "jobs.ashbyhq.com" and parts:
        provider, token = "ashby", parts[0]
    else:
        return None
    return (provider, token) if token and _TOKEN.match(token) else None


def _iso(v: object) -> date | None:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).date() if v else None
    except ValueError:
        return None


def _epoch_ms(v: object) -> date | None:
    try:
        return datetime.fromtimestamp(int(v) / 1000, tz=timezone.utc).date() if v else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


class AtsClient:
    def __init__(self, cfg: Config, user_agent: str, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(timeout=cfg.verify.timeout_seconds, follow_redirects=False)
        self._ua = user_agent

    def fetch(self, provider: str, token: str) -> list[RawPosting]:
        if provider not in PROVIDERS or not _TOKEN.match(token or ""):
            raise AtsError("bad provider or board id")
        try:
            r = self._client.get(_API[provider].format(token=token), headers={"User-Agent": self._ua, "Accept": "application/json"})
        except httpx.HTTPError as e:
            raise AtsError(f"request failed ({type(e).__name__})") from e
        if r.status_code != 200:
            raise AtsError(f"HTTP {r.status_code}")
        if len(r.content) > MAX_BYTES:
            raise AtsError("board response too large")
        try:
            data = r.json()
        except ValueError as e:
            raise AtsError("response was not JSON") from e
        return _parse(provider, data)


def _parse(provider: str, data: object) -> list[RawPosting]:
    out: list[RawPosting] = []
    if provider == "greenhouse":
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            raise AtsError("unexpected Greenhouse layout (no jobs list)")
        rows = [(j.get("title"), j.get("absolute_url"), _iso(j.get("first_published"))) for j in jobs if isinstance(j, dict)]
    elif provider == "lever":
        if not isinstance(data, list):
            raise AtsError("unexpected Lever layout (not a list)")
        rows = [(j.get("text"), j.get("hostedUrl"), _epoch_ms(j.get("createdAt"))) for j in data if isinstance(j, dict)]
    else:
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            raise AtsError("unexpected Ashby layout (no jobs list)")
        rows = [(j.get("title"), j.get("jobUrl"), _iso(j.get("publishedAt"))) for j in jobs
                if isinstance(j, dict) and j.get("isListed", True)]
    for title, url, posted in rows:
        title, url = " ".join(str(title or "").split()), str(url or "").strip()
        if title and posted and urlparse(url).scheme in ("http", "https") and urlparse(url).netloc:
            out.append(RawPosting(title, url, posted))
    return out


# ---- classification by title (deterministic; the keyword lists live in config.yaml) -----------------------------------
_SENIORITY = (
    ("c_suite", re.compile(r"\bchief\b|\bc[a-z]o\b|(?<!vice )\bpresident\b", re.I)),
    ("svp_vp", re.compile(r"\b(evp|svp|vp)\b|\bvice president\b", re.I)),
    ("director", re.compile(r"\bdirector\b|\bhead of\b", re.I)),
)


def seniority_of(title: str) -> str:
    for level, rx in _SENIORITY:
        if rx.search(title):
            return level
    return "other"


def _has(title: str, phrases: list[str]) -> bool:
    return any(re.search(r"\b" + re.escape(p.lower()) + r"\b", title.lower()) for p in phrases)


def classify(title: str, cfg: Config) -> tuple[str, str, bool] | None:
    """(theme, seniority, interim) for a relevant title, else None. Themes are tried in config order."""
    h = cfg.hiring
    for theme, phrases in h.theme_keywords.items():
        if _has(title, phrases) and (theme not in h.theme_title_gates or _has(title, h.theme_title_gates[theme])):
            return theme, seniority_of(title), _has(title, h.interim_words)
    return None
