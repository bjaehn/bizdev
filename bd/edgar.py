"""Minimal SEC EDGAR client (public data, no key): recent 8-K filings that report Item 5.02.

Only two fixed hosts are ever contacted (data.sec.gov, www.sec.gov). Requests carry the declared
User-Agent the SEC requires, and are spaced out well under the published 10 requests/second.
Only the CIK (a public company identifier) and the filing identifiers leave this machine.

NOTE: the response shape below (data.sec.gov/submissions/CIK##########.json -> filings.recent
parallel arrays: accessionNumber, filingDate, reportDate, form, items, primaryDocument) is the
documented EDGAR layout as I know it; it could not be fetched from the build sandbox. Parsing is
defensive: a malformed entry is skipped and an unexpected top-level shape is reported as an
EdgarError, never silently treated as "no filings"."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser

import httpx2 as httpx

from bd.config import Config
from bd.sec_ids import TICKERS_URL

API_HOST = "https://data.sec.gov"
ARCHIVE_HOST = "https://www.sec.gov"
_ACCESSION = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_DOCNAME = re.compile(r"^[A-Za-z0-9_.\-]+$")
_FORMS = {"8-K", "8-K/A"}


class EdgarError(Exception):
    pass


@dataclass(frozen=True)
class Filing:
    cik: str
    accession: str
    filing_date: date
    report_date: date | None
    form: str
    items: tuple[str, ...]
    primary_document: str

    @property
    def event_date(self) -> date:
        """Date of the event the 8-K reports (reportDate); the filing date only if EDGAR gives none."""
        return self.report_date or self.filing_date

    @property
    def url(self) -> str:
        return (f"{ARCHIVE_HOST}/Archives/edgar/data/{int(self.cik)}/"
                f"{self.accession.replace('-', '')}/{self.primary_document}")


def submissions_url(cik: str) -> str:
    return f"{API_HOST}/submissions/CIK{int(cik):010d}.json"


def facts_url(cik: str) -> str:
    return f"{API_HOST}/api/xbrl/companyfacts/CIK{int(cik):010d}.json"


def _date(v: object) -> date | None:
    try:
        return date.fromisoformat(str(v)) if v else None
    except ValueError:
        return None


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    return re.sub(r"\s+", " ", " ".join(p.parts)).strip()


def excerpt(text: str, max_chars: int, item: str = "5.02") -> str:
    """The part of a filing around the given Item heading (or the start, if the heading is absent)."""
    m = re.search(r"item\s*" + re.escape(item).replace(r"\.", r"\."), text, re.I)
    start = m.start() if m else 0
    return text[start: start + max_chars]


_EXHIBIT = re.compile(r"^(ex|exhibit)[-_ ]?99([-_.]?0?1)?[-_a-z0-9]*\.html?$", re.I)


class EdgarClient:
    def __init__(self, cfg: Config, user_agent: str, client: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        self._cfg = cfg
        self._client = client or httpx.Client(timeout=cfg.edgar.timeout_seconds, follow_redirects=False)
        self._ua = user_agent
        self._sleep, self._clock = sleep, clock
        self._lock = threading.Lock()
        self._last = -1e9

    def _get(self, url: str) -> httpx.Response:
        with self._lock:   # one request at a time, spaced apart: shared by all worker threads
            wait = self._cfg.edgar.min_interval_seconds - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
            try:
                r = self._client.get(url, headers={"User-Agent": self._ua, "Accept-Encoding": "gzip, deflate"})
            except httpx.HTTPError as e:
                raise EdgarError(f"request failed ({type(e).__name__})") from e
            finally:
                self._last = self._clock()
        if r.status_code != 200:
            raise EdgarError(f"HTTP {r.status_code}")
        return r

    def submissions(self, cik: str) -> dict:
        if not cik.isdigit():
            raise EdgarError("bad cik")
        try:
            data = self._get(submissions_url(cik)).json()
        except ValueError as e:
            raise EdgarError("response was not JSON") from e
        if not isinstance(data, dict) or not isinstance(data.get("filings", {}).get("recent"), dict):
            raise EdgarError("unexpected submissions layout (no filings.recent)")
        return data

    def company_facts(self, cik: str) -> dict:
        """The SEC's structured financial data for a company (XBRL "company facts"): data.sec.gov/api/xbrl/companyfacts/CIK##########.json
        -> facts.<taxonomy>.<concept>.units.<unit> = list of {start, end, val, accn, fy, fp, form, filed}. Like the other layouts
        here it could not be fetched from the build sandbox; the parser in bd.financials is defensive."""
        if not cik.isdigit():
            raise EdgarError("bad cik")
        try:
            data = self._get(facts_url(cik)).json()
        except ValueError as e:
            raise EdgarError("response was not JSON") from e
        if not isinstance(data, dict) or not isinstance(data.get("facts"), dict):
            raise EdgarError("unexpected company-facts layout (no facts)")
        return data

    def company_tickers(self) -> object:
        """The SEC's list of every listed company (www.sec.gov/files/company_tickers.json; parsed by bd.sec_ids). One request, about a
        megabyte; callers fetch it once per run."""
        try:
            data = self._get(TICKERS_URL).json()
        except ValueError as e:
            raise EdgarError("response was not JSON") from e
        if not isinstance(data, (dict, list)):
            raise EdgarError("unexpected company list layout")
        return data

    def company(self, cik: str) -> tuple[str, list[str]]:
        """(SEC-registered name, tickers) for a CIK - used to let a human check a proposed CIK."""
        d = self.submissions(cik)
        return str(d.get("name") or ""), [str(t) for t in d.get("tickers") or []]

    def recent_item_502(self, cik: str, since: date, until: date, limit: int) -> list[Filing]:
        return self.recent_items(cik, since, until, limit, "5.02")

    def recent_items(self, cik: str, since: date, until: date, limit: int, item: str) -> list[Filing]:
        return self.recent_filings(cik, since, until, limit, {item})

    def recent_filings(self, cik: str, since: date, until: date, limit: int, items: set[str]) -> list[Filing]:
        """8-K filings (newest first) that report at least one of `items`, with an event date inside the window. One request."""
        recent = self.submissions(cik)["filings"]["recent"]
        cols = [recent.get(k) for k in ("accessionNumber", "filingDate", "reportDate", "form", "items", "primaryDocument")]
        if not all(isinstance(c, list) for c in cols[:2] + cols[3:]):
            raise EdgarError("unexpected filings.recent layout")
        reports = cols[2] if isinstance(cols[2], list) else []
        out: list[Filing] = []
        for i, (acc, fdate, form, listed, doc) in enumerate(zip(cols[0], cols[1], cols[3], cols[4], cols[5])):
            fd = _date(fdate)
            if form not in _FORMS or fd is None or not _ACCESSION.match(str(acc)) or not _DOCNAME.match(str(doc)):
                continue
            reported = [x.strip() for x in str(listed or "").split(",")]
            if not items & set(reported):
                continue
            f = Filing(cik=f"{int(cik):010d}", accession=str(acc), filing_date=fd,
                       report_date=_date(reports[i]) if i < len(reports) else None,
                       form=str(form), items=tuple(reported), primary_document=str(doc))
            if since <= f.event_date <= until:
                out.append(f)
        out.sort(key=lambda f: (f.filing_date, f.accession), reverse=True)
        return out[:limit]

    def filing_items(self, filing: Filing, items: set[str]) -> set[str]:
        """Which of `items` have their "Item x.xx" heading in the filing document itself (fetches the document once)."""
        text = html_to_text(self._get(filing.url).text)
        return {i for i in items if re.search(r"item\s*" + re.escape(i), text, re.I)}

    def filing_text(self, filing: Filing) -> str:
        return excerpt(html_to_text(self._get(filing.url).text), self._cfg.edgar.max_chars)

    def earnings_release(self, filing: Filing) -> tuple[str, str]:
        """(url, text) of the press release furnished with an Item 2.02 8-K: the exhibit 99 document listed in the
        filing's index if there is one, else the 8-K itself. The URL returned is one this client just fetched."""
        base = f"{ARCHIVE_HOST}/Archives/edgar/data/{int(filing.cik)}/{filing.accession.replace('-', '')}"
        url = filing.url
        try:
            items = self._get(f"{base}/index.json").json()["directory"]["item"]
            names = [str(i["name"]) for i in items if isinstance(i, dict) and _DOCNAME.match(str(i.get("name", "")))]
            exhibit = next((n for n in sorted(names) if _EXHIBIT.match(n)), None)
            if exhibit:
                url = f"{base}/{exhibit}"
        except (EdgarError, ValueError, KeyError, TypeError):
            pass                                  # no usable index: fall back to the 8-K document itself
        text = html_to_text(self._get(url).text)
        return url, text[: self._cfg.edgar.max_chars]
