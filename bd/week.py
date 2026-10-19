"""The "This week" home: who to look at, what changed, existing clients with news, companies you track, and what the market is doing.

Read-only and deterministic, in plain words. It composes the ranked opportunities, the changes since the previous run, the pipeline state
and the market view from what past runs stored; every sentence is built from counts and stored, sourced signals. Nothing is paraphrased or
invented, and the page says how much of the list it actually covers, so a thin week looks thin."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from bd.config import Config
from bd.labels import FAMILY_LABELS
from bd.leadership import MIN_RISE, Changes, Coverage, Pulse, build_changes, coverage, pulse
from bd.market import build_market
from bd.opportunities import Opportunity, OpportunityList, build_opportunities, score_dates
from bd.scoring import ScoreResult
from bd.shortlist import ShortlistError

TARGETS_MAX = 8              # companies in "Who to look at"
TARGETS_MIN = 5              # when fewer are Hot or Warm, the strongest Watch companies fill the list up to this
CLIENTS_MAX = 3
TRACK_DAYS = 14              # "what's new" at a tracked company looks back this far
TRACK_STAGES = ("reviewing", "pursuing")


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


@dataclass
class Week:
    today: date
    coverage: Coverage
    run_date: str | None = None
    problem: str | None = None                       # why there is no list, in plain words
    targets: list[Opportunity] = field(default_factory=list)
    hot_warm: int = 0                                # new-business companies at Hot or Warm (the list may be longer or shorter)
    padded: bool = False                             # the list had to include Watch companies to reach TARGETS_MIN
    more: int = 0                                    # new-business companies at Watch or better that are not shown
    clients: list[Opportunity] = field(default_factory=list)
    tracked: list[Tracked] = field(default_factory=list)
    changes: Changes = field(default_factory=Changes)
    changed_line: str = ""
    pulse: Pulse = field(default_factory=Pulse)


def signs_line(result: ScoreResult) -> str:
    """The plain-words version of 'how many signal types fired': '3 kinds of signs agree: leadership, hiring, company events'."""
    kinds = [FAMILY_LABELS.get(f, f).lower() for f in sorted(result.families, key=lambda f: -result.family_totals.get(f, 0.0))]      # strongest first
    if not kinds:
        return ""
    if len(kinds) == 1:
        return f"1 kind of sign: {kinds[0]}"
    return f"{len(kinds)} kinds of signs agree: {', '.join(kinds)}"


def _changed_line(ol: OpportunityList, ch: Changes) -> str:
    if ol.prev_date is None:
        return "This is the first scoring run, so there is nothing to compare it with yet."
    new_hw = sum(1 for o in ol.items if o.is_new and o.result.band in ("Hot", "Warm"))
    risers = sum(1 for o in ol.items if o.delta is not None and o.delta >= MIN_RISE and not (o.is_new and o.result.band in ("Hot", "Warm")))
    bits = [f"{new_hw} compan{'y' if new_hw == 1 else 'ies'} became Hot or Warm",
            f"{risers} moved up by {MIN_RISE} points or more",
            f"{ch.new_signal_total} new signal{'' if ch.new_signal_total == 1 else 's'} {'was' if ch.new_signal_total == 1 else 'were'} found in the last 7 days"]
    return f"Since the run on {ol.prev_date.isoformat()}: " + ", ".join(bits[:2]) + ", and " + bits[2] + "."


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


def build_week(conn: sqlite3.Connection, cfg: Config, today: date) -> Week:
    out = Week(today, coverage(conn, today))
    cov = out.coverage
    out.pulse = pulse(build_market(conn, today))
    dates = score_dates(conn)
    if not dates:
        out.problem = "Nothing has been scored yet. An admin needs to run an update from the Admin area to fill in company facts, scan for news and rank what it finds."
        return out
    out.run_date = dates[0]
    cov.score_date, cov.score_age_days = dates[0], (today - date.fromisoformat(dates[0])).days
    try:
        ol = build_opportunities(conn, cfg, date.fromisoformat(dates[0]), include_tracked_only=True)
    except ShortlistError as e:
        out.problem = str(e)
        return out
    watch = cfg.scoring.bands.watch
    ranked = [o for o in ol.items if o.result.score >= watch]
    cov.scored = len(ranked)
    cov.hot, cov.warm = sum(o.result.band == "Hot" for o in ranked), sum(o.result.band == "Warm" for o in ranked)

    new_business = [o for o in ranked if not o.client]
    hot_warm = [o for o in new_business if o.result.band in ("Hot", "Warm")]
    out.hot_warm = len(hot_warm)
    shown = hot_warm[:TARGETS_MAX]
    if len(shown) < TARGETS_MIN:
        shown = new_business[:TARGETS_MIN]
        out.padded = len(shown) > len(hot_warm)
    out.targets = shown
    out.more = len(new_business) - len(shown)
    out.clients = [o for o in ranked if o.client][:CLIENTS_MAX]
    out.tracked = _tracked(conn, ol.items, today)
    out.changes = build_changes(conn, OpportunityList(ol.run_date, ol.prev_date, ranked), today)
    out.changed_line = _changed_line(OpportunityList(ol.run_date, ol.prev_date, ranked), out.changes)
    return out
