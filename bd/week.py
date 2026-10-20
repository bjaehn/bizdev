"""The Analysis page's data: who ranks where, who is worth a call, and whether things are heating up.

Read-only and deterministic, in plain words. It composes the ranked opportunities (each named by its theme), the signals behind them, the
weekly trend and what is being tracked from what past runs stored; every sentence is built from counts and stored, sourced signals. Nothing is
paraphrased or invented, and the page says how much of the list it actually covers, so a thin week looks thin.

One build feeds the whole page, so the cards and the numbered list can never disagree. The list is everyone at Watch or above, strongest score
first, numbered by rank, existing clients tagged. The cards are the three highest-scoring companies that are new business (a client is expansion,
not a new logo), whatever the dates of their signals, each with the rank it holds in the list."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from bd.config import Config
from bd.feed import FeedItem, group_of, load_items
from bd.labels import FAMILY_LABELS, TYPE_LABELS
from bd.leadership import Coverage, coverage
from bd.opportunities import Opportunity, build_opportunities, score_dates
from bd.scoring import ScoreResult
from bd.shortlist import ShortlistError
from bd.trend import Trend, build_trend, score_history

CALLS_MAX = 3                # companies in "Worth a call"
CLOSEST_MAX = 5              # when nobody is at Watch yet: the strongest below it
EVIDENCE_MAX = 3             # signals shown on a call card
TRACK_DAYS = 14              # "what's new" at a tracked company looks back this far
TRACK_STAGES = ("reviewing", "pursuing")
FEED_DAYS = 7                # the collapsed list of everything found this week
KIND_OF_GROUP = {"leadership": "leadership", "hiring": "hiring", "press": "press", "events": "events", "background": "events"}


@dataclass
class NewAt:
    event_date: str
    type: str
    summary: str
    url: str


@dataclass
class Tracked:
    opp: Opportunity
    new: list[NewAt] = field(default_factory=list)


@dataclass(frozen=True)
class CallEvidence:
    event_date: date
    label: str
    summary: str
    url: str
    source_type: str
    kind: str                 # leadership | hiring | press | events: picks the dot colour
    role_or_person: str | None = None


@dataclass
class Call:
    opp: Opportunity
    evidence: list[CallEvidence] = field(default_factory=list)
    history: list[int] = field(default_factory=list)       # stored scores, oldest first, ending with the current one
    newest: date | None = None                             # the date of its newest signal


@dataclass
class Week:
    today: date
    coverage: Coverage
    trend: Trend = field(default_factory=Trend)
    run_date: str | None = None
    problem: str | None = None                       # why there is no ranked list, in plain words
    calls: list[Call] = field(default_factory=list)  # the top CALLS_MAX new-business companies by score
    ranked: list[Opportunity] = field(default_factory=list)    # everyone at Watch or above, strongest first (clients included, tagged)
    items: list[Opportunity] = field(default_factory=list)     # everyone scored, for "everything scanned"
    closest: list[Opportunity] = field(default_factory=list)   # when nobody is at Watch yet: the strongest below it
    tracked: list[Tracked] = field(default_factory=list)
    recent: list[FeedItem] = field(default_factory=list)       # signals dated in the last FEED_DAYS days
    stale: list[str] = field(default_factory=list)             # companies whose stored score no longer matches their signals
    prev_date: str | None = None                               # the run "since last run" changes are measured against


def signs_line(result: ScoreResult) -> str:
    """The plain-words version of 'how many signal types fired': '3 kinds of signs agree: leadership, hiring, company events'."""
    kinds = [FAMILY_LABELS.get(f, f).lower() for f in sorted(result.families, key=lambda f: -result.family_totals.get(f, 0.0))]      # strongest first
    if not kinds:
        return ""
    if len(kinds) == 1:
        return f"1 kind of sign: {kinds[0]}"
    return f"{len(kinds)} kinds of signs agree: {', '.join(kinds)}"


def newest_signal(o: Opportunity) -> date | None:
    dates = [date.fromisoformat(p.event_date) for p in o.result.signals]
    return max(dates) if dates else None


def call_evidence(o: Opportunity, limit: int = EVIDENCE_MAX) -> list[CallEvidence]:
    """The signals behind a company, newest first: those its main theme rests on and its strongest scored signals, each once."""
    seen: dict[int, CallEvidence] = {}
    pool = [(e.id, e.type, e.event_date, e.summary, e.url, e.source_type, e.role_or_person) for t in o.themes[:1] for e in t.evidence]
    pool += [(s.id, s.type, s.event_date, s.summary, s.url, s.source_type, s.role_or_person) for s in o.signals]
    for sid, type_, when, summary, url, source, who in pool:
        if sid not in seen:
            seen[sid] = CallEvidence(when, TYPE_LABELS.get(type_, type_), summary, url, source, KIND_OF_GROUP[group_of(type_, source)], who)
    return sorted(seen.values(), key=lambda e: e.event_date, reverse=True)[:limit]


def _tracked(conn: sqlite3.Connection, items: list[Opportunity], today: date) -> list[Tracked]:
    mine = [o for o in items if o.watching or o.stage in TRACK_STAGES]
    if not mine:
        return []
    since = (today - timedelta(days=TRACK_DAYS)).isoformat()
    ids = [o.account.id for o in mine]
    new: dict[int, list[NewAt]] = {}
    for r in conn.execute(f"SELECT account_id, event_date, type, summary, source_url FROM signals WHERE detected_date >= ? "
                          f"AND account_id IN ({','.join('?' * len(ids))}) ORDER BY event_date DESC, id DESC", (since, *ids)):
        new.setdefault(r["account_id"], []).append(NewAt(r["event_date"], r["type"], r["summary"], r["source_url"]))
    # companies with something new first, then by score
    return sorted((Tracked(o, new.get(o.account.id, [])) for o in mine), key=lambda t: (not t.new, t.opp.rank))


def choose_calls(ranked: list[Opportunity]) -> list[Opportunity]:
    """The top CALLS_MAX new-business companies of the ranked list, in rank order. Whatever the dates of their signals: the cards are the top of the list."""
    return [o for o in ranked if not o.client][:CALLS_MAX]


def build_week(conn: sqlite3.Connection, cfg: Config, today: date) -> Week:
    out = Week(today, coverage(conn, today))
    cov = out.coverage
    out.trend = build_trend(conn, today)
    out.recent = load_items(conn, today, days=FEED_DAYS)
    dates = score_dates(conn)
    if not dates:
        out.problem = "Nothing has been scored yet. An admin needs to run an update from the Admin area to scan for leadership changes, hiring and press, and rank what it finds."
        return out
    out.run_date = dates[0]
    cov.score_date, cov.score_age_days = dates[0], (today - date.fromisoformat(dates[0])).days
    try:
        ol = build_opportunities(conn, cfg, date.fromisoformat(dates[0]), include_tracked_only=True)
    except ShortlistError as e:
        out.problem = str(e)
        return out
    watch = cfg.scoring.bands.watch
    out.items, out.stale = ol.items, ol.stale
    out.prev_date = ol.prev_date.isoformat() if ol.prev_date else None
    out.ranked = [o for o in ol.items if o.result.score >= watch]
    cov.scored = len(out.ranked)
    cov.hot, cov.warm = sum(o.result.band == "Hot" for o in out.ranked), sum(o.result.band == "Warm" for o in out.ranked)

    chosen = choose_calls(out.ranked)
    history = score_history(conn, [o.account.id for o in chosen])
    out.calls = [Call(o, call_evidence(o), history.get(o.account.id, []), newest_signal(o)) for o in chosen]
    if not out.ranked:
        out.closest = [o for o in ol.items if not o.client][:CLOSEST_MAX]
    out.tracked = _tracked(conn, ol.items, today)
    return out
