"""Market view: where in the Chicago market are signals appearing, which sponsors are active, and which large companies are quiet.

Read-only and deterministic: aggregates the stored accounts, signals, scores and sponsor links. No model or network code. All
counts use the event date (from the source), not the day we found a signal."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from collections.abc import Sequence
from datetime import date, timedelta

UNKNOWN_INDUSTRY = "Industry not recorded"


@dataclass
class SegmentRow:
    industry: str
    companies: int = 0
    with_signal_90d: int = 0
    signals_30d: int = 0
    signals_prior_30d: int = 0
    hot: int = 0
    warm: int = 0
    watch: int = 0
    avg_score: float | None = None

    @property
    def trend(self) -> str:
        if self.signals_30d > self.signals_prior_30d:
            return "up"
        if self.signals_30d < self.signals_prior_30d:
            return "down"
        return "flat"


@dataclass
class SponsorRow:
    firm_id: int
    name: str
    companies: int = 0
    signals_90d: int = 0
    firm_signals_180d: int = 0


@dataclass
class QuietRow:
    account_id: int
    name: str
    industry: str
    revenue: float | None
    employees: int | None
    last_scanned: str | None
    signals_total: int


@dataclass
class Heat:
    signals: int = 0                  # signals of this type in the last 90 days, across the industry's ready companies
    companies: int = 0                # how many companies they belong to


@dataclass
class TrendPoint:
    run_date: str
    hot: int = 0
    warm: int = 0
    watch: int = 0
    other: int = 0                    # scored, but below Watch

    @property
    def hot_warm(self) -> int:
        return self.hot + self.warm


@dataclass
class Market:
    as_of: date
    score_date: str | None
    families: list[str] = field(default_factory=list)                   # heatmap columns
    heat: dict[str, dict[str, Heat]] = field(default_factory=dict)      # industry -> signal type -> counts, last 90 days
    heat_max: int = 0
    trend: list[TrendPoint] = field(default_factory=list)               # one point per scoring run, oldest first
    segments: list[SegmentRow] = field(default_factory=list)
    months: list[str] = field(default_factory=list)                     # 'YYYY-MM', oldest first
    family_by_month: dict[str, list[int]] = field(default_factory=dict)  # family -> counts per month
    sponsors: list[SponsorRow] = field(default_factory=list)
    quiet: list[QuietRow] = field(default_factory=list)
    quiet_total: int = 0


def _months_back(as_of: date, n: int) -> list[str]:
    y, m = as_of.year, as_of.month
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out[::-1]


def build_market(conn: sqlite3.Connection, as_of: date, months: int = 6, quiet_limit: int = 25, families: Sequence[str] | None = None,
                 runs: int = 12) -> Market:
    d30, d60, d90, d180 = (as_of - timedelta(days=n) for n in (30, 60, 90, 180))
    score_date = conn.execute("SELECT MAX(run_date) FROM scores").fetchone()[0]
    out = Market(as_of, score_date)

    accounts = conn.execute("SELECT id, name, industry, revenue, employees, last_scanned FROM accounts WHERE status = 'active'").fetchall()
    ids = {a["id"] for a in accounts}
    sig = defaultdict(list)                        # account -> [(event_date, family)]
    for r in conn.execute("SELECT account_id, event_date, family FROM signals"):
        if r["account_id"] in ids:
            sig[r["account_id"]].append((r["event_date"], r["family"]))
    scores = {r["account_id"]: (r["score"], r["band"]) for r in conn.execute(
        "SELECT account_id, score, band FROM scores WHERE run_date = ?", (score_date,))} if score_date else {}

    segs: dict[str, SegmentRow] = {}
    totals: dict[str, list[int]] = defaultdict(list)
    for a in accounts:
        name = (a["industry"] or "").strip() or UNKNOWN_INDUSTRY
        s = segs.setdefault(name, SegmentRow(name))
        s.companies += 1
        events = sig.get(a["id"], [])
        if any(e >= d90.isoformat() for e, _ in events):
            s.with_signal_90d += 1
        s.signals_30d += sum(1 for e, _ in events if d30.isoformat() <= e <= as_of.isoformat())
        s.signals_prior_30d += sum(1 for e, _ in events if d60.isoformat() <= e < d30.isoformat())
        sc = scores.get(a["id"])
        if sc:
            totals[name].append(sc[0])
            if sc[1] == "Hot":
                s.hot += 1
            elif sc[1] == "Warm":
                s.warm += 1
            elif sc[1] == "Watch":
                s.watch += 1
    for name, s in segs.items():
        if totals[name]:
            s.avg_score = sum(totals[name]) / len(totals[name])
    out.segments = sorted(segs.values(), key=lambda s: (-(s.hot * 3 + s.warm * 2 + s.watch), -s.signals_30d, -s.companies, s.industry))

    seen = sorted({f for events in sig.values() for _, f in events})
    out.families = list(families) if families is not None else sorted(seen)
    owner = {a["id"]: (a["industry"] or "").strip() or UNKNOWN_INDUSTRY for a in accounts}
    heat: dict[str, dict[str, Heat]] = {}
    for acc_id, events in sig.items():
        recent = [f for e, f in events if d90.isoformat() <= e <= as_of.isoformat()]
        for f in set(recent):
            h = heat.setdefault(owner[acc_id], {}).setdefault(f, Heat())
            h.signals += recent.count(f)
            h.companies += 1
    out.heat = {s.industry: heat.get(s.industry, {}) for s in out.segments}                 # same order as the industry table
    out.heat_max = max((h.signals for row in out.heat.values() for h in row.values()), default=0)

    by_run: dict[str, TrendPoint] = {}
    for r in conn.execute("SELECT run_date, band, COUNT(*) AS n FROM scores WHERE run_date <= ? GROUP BY run_date, band", (as_of.isoformat(),)):
        p = by_run.setdefault(r["run_date"], TrendPoint(r["run_date"]))
        if r["band"] == "Hot":
            p.hot += r["n"]
        elif r["band"] == "Warm":
            p.warm += r["n"]
        elif r["band"] == "Watch":
            p.watch += r["n"]
        else:
            p.other += r["n"]
    out.trend = [by_run[d] for d in sorted(by_run)[-runs:]]

    out.months = _months_back(as_of, months)
    idx = {m: i for i, m in enumerate(out.months)}
    fam: dict[str, list[int]] = defaultdict(lambda: [0] * len(out.months))
    for events in sig.values():
        for e, f in events:
            i = idx.get(e[:7])
            if i is not None and e <= as_of.isoformat():
                fam[f][i] += 1
    out.family_by_month = dict(sorted(fam.items(), key=lambda kv: -sum(kv[1])))

    for f in conn.execute("SELECT id, name FROM pe_firms WHERE status = 'active' ORDER BY name"):
        linked = [r["account_id"] for r in conn.execute(
            "SELECT account_id FROM portfolio_links WHERE firm_id = ? AND status = 'current' AND account_id IS NOT NULL", (f["id"],))]
        linked = [a for a in linked if a in ids]
        row = SponsorRow(f["id"], f["name"], len(linked))
        row.signals_90d = sum(1 for a in linked for e, _ in sig.get(a, []) if e >= d90.isoformat())
        row.firm_signals_180d = conn.execute("SELECT COUNT(*) FROM firm_signals WHERE firm_id = ? AND event_date >= ?",
                                             (f["id"], d180.isoformat())).fetchone()[0]
        if row.companies:
            out.sponsors.append(row)
    out.sponsors.sort(key=lambda r: (-(r.signals_90d + r.firm_signals_180d), -r.companies, r.name))

    quiet = []
    for a in accounts:
        events = sig.get(a["id"], [])
        if not any(e >= d180.isoformat() for e, _ in events):
            quiet.append(QuietRow(a["id"], a["name"], (a["industry"] or "").strip() or UNKNOWN_INDUSTRY, a["revenue"], a["employees"],
                                  a["last_scanned"], len(events)))
    quiet.sort(key=lambda q: (-(q.revenue or 0), -(q.employees or 0), q.name))
    out.quiet_total = len(quiet)
    out.quiet = quiet[:quiet_limit]
    return out
