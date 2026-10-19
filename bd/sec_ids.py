"""Which SEC filer is this company? A unique, exact-name match against the SEC's own list of listed companies: no model.

The SEC publishes one file of every company that has a stock ticker (www.sec.gov/files/company_tickers.json: a CIK, a ticker and the
registered name for each). A company's CIK is assigned from it only when exactly one registered name equals the company's name once
legal suffixes (Inc, Corp, Corporation, Co, Company, Ltd, PLC, LLC, Holdings), punctuation and case are ignored. "CME Group" matches
"CME Group Inc."; "Endeavor Health" does not match "Endeavor Group Holdings"; a name that fits two different filers is skipped, not
guessed. Nothing is inferred beyond that, and every match is written to the Change log with the SEC's own name so a person can
spot a wrong one.

Pure functions over already-fetched JSON; the fetch lives in bd.edgar. The layout ({"0": {"cik_str": 320193, "ticker": "AAPL",
"title": "Apple Inc."}, ...}) is the documented one as I know it and could not be fetched from the build sandbox (SECURITY.md gap 22):
anything that does not look right yields no entries, and an empty list matches nothing."""

from __future__ import annotations

import re
from dataclasses import dataclass

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# Words that say what legal form a company has, not which company it is. "Group", "Financial", "Health" and the like are kept:
# they are part of the name, and dropping them would make unrelated companies match.
_SUFFIXES = {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "plc", "llc", "holdings", "holding"}


@dataclass(frozen=True)
class Listing:
    cik: str          # 10 digits
    ticker: str
    title: str        # the name the SEC has on file

    @property
    def label(self) -> str:
        return f"{self.title} ({self.ticker})"


@dataclass(frozen=True)
class Match:
    listing: Listing | None
    reason: str = ""          # why there is no match: "no match" | "ambiguous (N different SEC filers)" | "empty name"


def normalize_name(name: str) -> str:
    text = re.sub(r"/[A-Za-z]{2,3}/", " ", str(name))                  # EDGAR's state-of-incorporation tag: "ABC CORP /DE/"
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]+", "", text.replace("-", " "))          # periods, commas and apostrophes vanish; hyphens split words
    words = text.split()
    if words and words[0] == "the":
        words = words[1:]
    while len(words) > 1 and words[-1] in _SUFFIXES:
        words.pop()
    if len(words) == 1 and words[0] in _SUFFIXES:
        return ""
    return " ".join(words)


class Index:
    """Registered names (normalised) -> the filers that carry them."""

    def __init__(self, listings: list[Listing]) -> None:
        self._by_name: dict[str, dict[str, Listing]] = {}
        for item in listings:
            key = normalize_name(item.title)
            if key:
                self._by_name.setdefault(key, {}).setdefault(item.cik, item)      # two share classes of one filer are one filer
        self.size = sum(len(v) for v in self._by_name.values())

    def lookup(self, name: str) -> Match:
        key = normalize_name(name)
        if not key:
            return Match(None, "empty name")
        found = self._by_name.get(key, {})
        if not found:
            return Match(None, "no match")
        if len(found) > 1:
            return Match(None, f"ambiguous ({len(found)} different SEC filers)")
        return Match(next(iter(found.values())))


def build_index(raw: object) -> Index:
    """Parse the SEC file defensively: a record that is not a CIK, a ticker and a name is skipped, and an unexpected top-level shape
    gives an empty index (which matches nothing) rather than an error."""
    records = list(raw.values()) if isinstance(raw, dict) else list(raw) if isinstance(raw, list) else []
    listings = []
    for r in records:
        if not isinstance(r, dict):
            continue
        cik, ticker, title = r.get("cik_str", r.get("cik")), r.get("ticker"), r.get("title", r.get("name"))
        digits = str(cik).strip() if isinstance(cik, (int, str)) and not isinstance(cik, bool) else ""
        if not digits.isdigit() or len(digits) > 10 or int(digits) == 0:
            continue
        if not isinstance(ticker, str) or not ticker.strip() or not isinstance(title, str) or not title.strip():
            continue
        listings.append(Listing(digits.zfill(10), ticker.strip().upper(), title.strip()))
    return Index(listings)
