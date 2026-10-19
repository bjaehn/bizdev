"""Size, ownership and headquarters for a company that files with the SEC, read from the SEC's own structured data: no model.

Given a CIK, two public SEC documents say what a model would otherwise have to find on the web:
  * company facts (XBRL): the revenue of the latest fiscal year, taken from the company's own 10-K;
  * the submissions record: whether the company has a listed ticker (so it is public) and its business address.
Code reads them, so every value comes with the URL it was read from, and the fit gate's "revenue must come from an SEC filing" rule is
met honestly. Pure functions over already-fetched JSON; the network calls live in bd.edgar. The layouts are the documented SEC layouts
as I know them and could not be fetched from the build sandbox (SECURITY.md gap 22): anything that does not look right yields nothing."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from bd.financials import REVENUE_CONCEPTS, annual_values

MAX_FACT_AGE_DAYS = 548          # a fiscal year that ended more than ~18 months ago is not "latest" (a company that stopped filing)
_US_STATES = {"AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
              "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD",
              "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY"}


@dataclass
class SecFacts:
    revenue: float | None = None
    revenue_note: str = ""
    public: bool = False
    hq: str | None = None                                  # "Chicago, IL"
    notes: list[str] = field(default_factory=list)


def latest_revenue(facts: dict, as_of: date) -> tuple[float, date] | None:
    """(revenue, fiscal year end) for the newest annual figure under whichever revenue tag the company uses, or None."""
    best = None
    for concept in REVENUE_CONCEPTS:
        series = annual_values(facts, concept)
        if series and (best is None or series[0].end > best.end):
            best = series[0]
    if best is None or best.value <= 0 or (as_of - best.end).days > MAX_FACT_AGE_DAYS:
        return None
    return best.value, best.end


def is_public(submissions: dict) -> bool:
    return any(isinstance(t, str) and t.strip() for t in (submissions.get("tickers") or [])) or \
        any(isinstance(e, str) and e.strip() for e in (submissions.get("exchanges") or []))


def business_hq(submissions: dict) -> str | None:
    """'City, ST' from the SEC business address when it is a US address, else None."""
    addr = (submissions.get("addresses") or {}).get("business") or {}
    city, state = str(addr.get("city") or "").strip(), str(addr.get("stateOrCountry") or "").strip().upper()
    if not city or state not in _US_STATES:
        return None
    return f"{city.title()}, {state}"


def read(submissions: dict | None, facts: dict | None, as_of: date, need_revenue: bool = True) -> SecFacts:
    out = SecFacts()
    if submissions:
        out.public = is_public(submissions)
        out.hq = business_hq(submissions)
    if facts and need_revenue:
        r = latest_revenue(facts, as_of)
        if r:
            out.revenue = r[0]
            out.revenue_note = f"revenue FY{r[1].year} from SEC XBRL"
        else:
            out.notes.append("no recent annual revenue in the SEC data")
    return out
