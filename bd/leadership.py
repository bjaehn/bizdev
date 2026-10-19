"""Leadership view: one screen that answers "what should we act on, what changed, and what is the market doing?".

Read-only and deterministic: it composes the ranked opportunities, the market view, the pipeline and the existing-client list from
what past runs stored. No model, network or scanner code, and every sentence is built from counts and stored, sourced signals;
nothing is paraphrased or invented. The page reports how much of the list it actually covers, so a thin week looks thin."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from bd.config import Config
from bd.labels import FAMILY_LABELS
from bd.market import Market, SegmentRow, SponsorRow, build_market
from bd.opportunities import Opportunity, build_opportunities, score_dates
from bd.pipeline import STAGES, ClientRow, client_rows, stage_counts
from bd.shortlist import ShortlistError

ACT_COUNT = 8                  # companies in "Act on this week"
MIN_RISE = 5                   # points a score must gain to count as a riser
NEW_SIGNAL_DAYS = 7
MAX_NEW_SIGNALS = 10
STALE_SCORE_DAYS = 8


@dataclass
class Coverage:
    total: int = 0
    ready: int = 0
    waiting: int = 0
    with_signal_90d: int = 0
    scored: int = 0                    # companies at or above the Watch band on the latest scores
    hot: int = 0
    warm: int = 0
    clients: int = 0
    stages: dict[str, int] = field(default_factory=dict)
    last_run: str | None = None        # newest date any step ran
    score_date: str | None = None
    score_age_days: int | None = None


@dataclass
class NewSignal:
    account_id: int
    name: str
    type: str
    event_date: str
    summary: str
    url: str


@dataclass
class Changes:
    new_leads: list[Opportunity] = field(default_factory=list)     # Hot or Warm now, below Watch (or unscored) at the previous run
    risers: list[Opportunity] = field(default_factory=list)
    new_signals: list[NewSignal] = field(default_factory=list)
    new_signal_total: int = 0
    prev_date: str | None = None


@dataclass
class Pulse:
    segments: list[SegmentRow] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)                 # sentences built from the counts
    families: list[tuple[str, int]] = field(default_factory=list)  # (label, signals this month)
    sponsors: list[SponsorRow] = field(default_factory=list)
    quiet_total: int = 0


@dataclass
class Leadership:
    today: date
    coverage: Coverage
    act: list[Opportunity] = field(default_factory=list)
    changes: Changes = field(default_factory=Changes)
    pulse: Pulse = field(default_factory=Pulse)
    clients: list[ClientRow] = field(default_factory=list)
    problem: str | None = None         # why the ranked list is empty, in plain words


def _coverage(conn: sqlite3.Connection, today: date) -> Coverage:
    c = Coverage()
    by = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM accounts GROUP BY status")}
    c.total, c.ready, c.waiting = sum(by.values()), by.get("active", 0), by.get("hold_review", 0)
    c.with_signal_90d = conn.execute(
        "SELECT COUNT(DISTINCT s.account_id) FROM signals s JOIN accounts a ON a.id = s.account_id "
        "WHERE a.status = 'active' AND s.event_date >= ?", ((today - timedelta(days=90)).isoformat(),)).fetchone()[0]
    c.clients = conn.execute("SELECT COUNT(*) FROM lead_state WHERE client = 1").fetchone()[0]
    c.stages = {k: n for k, n in stage_counts(conn).items() if k in STAGES}
    c.last_run = conn.execute("SELECT MAX(run_date) FROM runs").fetchone()[0]
    return c


def _pulse(m: Market) -> Pulse:
    p = Pulse(quiet_total=m.quiet_total, sponsors=[s for s in m.sponsors if s.signals_90d or s.firm_signals_180d][:3])
    p.segments = [s for s in m.segments if s.signals_30d or s.hot or s.warm][:5]
    for s in p.segments:
        bits = [f"{s.signals_30d} signal{'' if s.signals_30d == 1 else 's'} in the last 30 days"]
        if s.signals_30d != s.signals_prior_30d:
            bits.append(f"{'up' if s.signals_30d > s.signals_prior_30d else 'down'} from {s.signals_prior_30d} in the 30 days before")
        hw = s.hot + s.warm
        if hw:
            bits.append(f"{hw} Hot or Warm compan{'y' if hw == 1 else 'ies'}")
        p.lines.append(f"{s.industry}: " + "; ".join(bits) + ".")
    if m.family_by_month:
        now = [(FAMILY_LABELS.get(f, f), counts[-1]) for f, counts in m.family_by_month.items() if counts and counts[-1]]
        p.families = sorted(now, key=lambda x: (-x[1], x[0]))[:4]
    return p


def build_leadership(conn: sqlite3.Connection, cfg: Config, today: date) -> Leadership:
    out = Leadership(today, _coverage(conn, today))
    cov = out.coverage
    dates = score_dates(conn)
    out.clients = [c for c in client_rows(conn, today) if c.signals_90d][:5]
    out.pulse = _pulse(build_market(conn, today))
    if not dates:
        out.problem = ("Nothing has been scored yet. Press Update my list on Home: it fills in company facts from the SEC, "
                       "scans for news and ranks what it finds.")
        return out
    cov.score_date = dates[0]
    cov.score_age_days = (today - date.fromisoformat(dates[0])).days
    try:
        ol = build_opportunities(conn, cfg, date.fromisoformat(dates[0]))
    except ShortlistError as e:
        out.problem = str(e)
        return out
    cov.scored = len(ol.items)
    cov.hot = sum(o.result.band == "Hot" for o in ol.items)
    cov.warm = sum(o.result.band == "Warm" for o in ol.items)
    out.act = ol.items[:ACT_COUNT]
    ch = out.changes
    ch.prev_date = ol.prev_date.isoformat() if ol.prev_date else None
    if ol.prev_date:
        ch.new_leads = [o for o in ol.items if o.is_new and o.result.band in ("Hot", "Warm")][:5]
        ch.risers = sorted((o for o in ol.items if o.delta is not None and o.delta >= MIN_RISE and o not in ch.new_leads),
                           key=lambda o: (-o.delta, o.rank))[:5]
    since = (today - timedelta(days=NEW_SIGNAL_DAYS)).isoformat()
    rows = conn.execute("SELECT s.account_id, a.name, s.type, s.event_date, s.summary, s.source_url FROM signals s "
                        "JOIN accounts a ON a.id = s.account_id WHERE a.status = 'active' AND s.detected_date >= ? "
                        "ORDER BY s.event_date DESC, s.id DESC", (since,)).fetchall()
    ch.new_signal_total = len(rows)
    ch.new_signals = [NewSignal(r["account_id"], r["name"], r["type"], r["event_date"], r["summary"], r["source_url"])
                      for r in rows[:MAX_NEW_SIGNALS]]
    return out
