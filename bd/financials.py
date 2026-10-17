"""Financial pressure from a company's own annual figures (SEC XBRL "company facts").

Pure functions, no network and no model. From the latest 10-K and the fiscal year before it, work out revenue growth,
operating margin and whether the company lost money, and say which pressure flags (set in config.yaml `financials`) hold.
Everything returned comes from the facts the SEC published, with the date they were filed.

The layout parsed here (facts.us-gaap.<Concept>.units.USD = [{start, end, val, accn, fy, fp, form, filed}]) is the documented
SEC layout as I know it; it could not be fetched from the build sandbox. Entries that do not look right are skipped, and a
company whose figures cannot be compared year on year gets no assessment (never a guess)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from bd.config import Config

REVENUE_CONCEPTS = ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "RevenueFromContractWithCustomerIncludingAssessedTax",
                    "SalesRevenueNet", "SalesRevenueGoodsNet", "SalesRevenueServicesNet")
OPERATING_INCOME_CONCEPTS = ("OperatingIncomeLoss",)
_ANNUAL_FORMS = ("10-K", "10-K/A", "10-KT")


@dataclass(frozen=True)
class Annual:
    end: date
    value: float
    filed: date
    accession: str
    form: str


@dataclass(frozen=True)
class Assessment:
    fiscal_year_end: date
    filed: date
    accession: str
    form: str
    revenue: float
    prior_revenue: float
    revenue_growth_pct: float
    operating_income: float
    operating_margin_pct: float
    prior_operating_margin_pct: float | None
    margin_change_points: float | None
    flags: tuple[str, ...]


def _d(v: object) -> date | None:
    try:
        return date.fromisoformat(str(v)) if v else None
    except ValueError:
        return None


def annual_values(facts: dict, concept: str) -> list[Annual]:
    """One value per fiscal year for a concept, from annual (10-K) filings only: entries covering about a year, taking the
    most recently filed value where a year was restated."""
    try:
        entries = facts["facts"]["us-gaap"][concept]["units"]["USD"]
    except (KeyError, TypeError):
        return []
    if not isinstance(entries, list):
        return []
    best: dict[date, Annual] = {}
    for e in entries:
        if not isinstance(e, dict) or e.get("form") not in _ANNUAL_FORMS:
            continue
        start, end, filed = _d(e.get("start")), _d(e.get("end")), _d(e.get("filed"))
        val = e.get("val")
        if start is None or end is None or filed is None or not isinstance(val, (int, float)) or isinstance(val, bool):
            continue
        if not 350 <= (end - start).days <= 380:
            continue                                  # a quarter or a multi-year figure, not a fiscal year
        cur = best.get(end)
        if cur is None or filed > cur.filed:
            best[end] = Annual(end, float(val), filed, str(e.get("accn") or ""), str(e.get("form")))
    return sorted(best.values(), key=lambda a: a.end, reverse=True)


def _pair(series: list[Annual]) -> tuple[Annual, Annual] | None:
    """(latest year, the year before it) if both exist about a year apart."""
    if not series:
        return None
    latest = series[0]
    for a in series[1:]:
        if abs((latest.end - a.end).days - 365) <= 20:
            return latest, a
    return None


def _best_revenue(facts: dict) -> tuple[Annual, Annual] | None:
    """Revenue under whichever tag the company uses; a company that changed tags uses the one with the newest year, as long
    as it also has the year before."""
    pairs = [p for c in REVENUE_CONCEPTS if (p := _pair(annual_values(facts, c)))]
    return max(pairs, key=lambda p: p[0].end, default=None)


def assess(facts: dict, cfg: Config) -> Assessment | None:
    """The latest year's pressure flags, or None when the figures cannot be compared (no revenue pair, no operating income for
    the latest year)."""
    rev = _best_revenue(facts)
    if rev is None or rev[1].value <= 0 or rev[0].value <= 0:
        return None
    latest, prior = rev
    ops = {a.end: a for c in OPERATING_INCOME_CONCEPTS for a in annual_values(facts, c)}
    op = next((v for e, v in ops.items() if abs((e - latest.end).days) <= 7), None)
    if op is None:
        return None
    prior_op = next((v for e, v in ops.items() if abs((e - prior.end).days) <= 7), None)
    growth = (latest.value / prior.value - 1) * 100
    margin = op.value / latest.value * 100
    prior_margin = prior_op.value / prior.value * 100 if prior_op else None
    change = None if prior_margin is None else margin - prior_margin
    c = cfg.financials
    flags = []
    if growth <= -c.revenue_decline_pct:
        flags.append("revenue_decline")
    if change is not None and change <= -c.margin_drop_points:
        flags.append("margin_drop")
    if c.flag_operating_loss and op.value < 0:
        flags.append("operating_loss")
    return Assessment(latest.end, max(latest.filed, op.filed), latest.accession, latest.form, latest.value, prior.value, growth,
                      op.value, margin, prior_margin, change, tuple(flags))


def money_short(v: float) -> str:
    a = abs(v)
    for div, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"${a / div:.1f}{unit}"
    return f"${a:.0f}"


def summary(a: Assessment) -> str:
    """One neutral sentence (at most 25 words) built only from the figures."""
    parts = []
    if "revenue_decline" in a.flags:
        parts.append(f"revenue fell {abs(a.revenue_growth_pct):.0f}% to {money_short(a.revenue)}")
    if "margin_drop" in a.flags:
        parts.append(f"operating margin fell {abs(a.margin_change_points or 0):.1f} points to {a.operating_margin_pct:.1f}%")
    if "operating_loss" in a.flags:
        parts.append(f"operating loss of {money_short(a.operating_income)}")
    return f"Fiscal {a.fiscal_year_end.year} 10-K: " + ", ".join(parts) + "."


def window_start(as_of: date, days: int) -> date:
    return as_of - timedelta(days=days)
