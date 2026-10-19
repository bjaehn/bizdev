"""Is it heating up? Signals found per week, by kind, and a one-sentence takeaway built only from those counts.

Read-only and deterministic. A week is seven days ending today (the last bucket is the most recent seven days); a signal belongs to the week
its source dates it in, and the same rules as the feed apply (scanned companies only, no rumours, one event counted once). The takeaway
compares the last four weeks with the four before and names a kind only when it rose by at least MIN_RISE signals; it never says more than
the counts do. Also the score history behind each company's trend line, from the scores the scoring runs stored."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from bd.feed import group_of, load_items

WEEKS = 12
COMPARE_WEEKS = 4
MIN_RISE = 2          # signals a kind must gain over the earlier four weeks to be named in the takeaway
SPARK_POINTS = 8

# (feed group keys folded into the kind, label). Filings and financials are background and are not charted.
TREND_KINDS = (("leadership", ("leadership",), "Leadership changes"), ("hiring", ("hiring",), "Hiring"),
         ("press", ("press",), "Press and media"), ("events", ("events",), "Company events and technology"))
KIND_LABELS = {k: label for k, _, label in TREND_KINDS}


@dataclass
class Trend:
    weeks: list[date] = field(default_factory=list)           # the last day of each week, oldest first
    series: dict[str, list[int]] = field(default_factory=dict)   # kind -> signals per week
    recent: int = 0                                           # all kinds, last four weeks
    earlier: int = 0                                          # all kinds, the four weeks before
    pct: int | None = None                                    # change in percent, None when there was nothing earlier to compare with
    takeaway: str = ""
    found_this_week: int = 0                                  # signals first stored in the last seven days (whatever date the source gives)
    total: int = 0

    @property
    def max_week(self) -> int:
        return max((sum(self.series[k][i] for k in self.series) for i in range(len(self.weeks))), default=0)


def week_index(event: date, today: date, weeks: int = WEEKS) -> int | None:
    """0 = oldest week, weeks-1 = the most recent seven days; None when outside the window."""
    age = (today - event).days
    if age < 0 or age >= weeks * 7:
        return None
    return weeks - 1 - age // 7


def takeaway(series: dict[str, list[int]], labels: dict[str, str] = KIND_LABELS) -> tuple[str, int, int, int | None]:
    """(sentence, last four weeks, four weeks before, percent change or None)."""
    n = len(next(iter(series.values()), []))
    recent_of = {k: sum(v[n - COMPARE_WEEKS:]) for k, v in series.items()}
    earlier_of = {k: sum(v[n - 2 * COMPARE_WEEKS:n - COMPARE_WEEKS]) for k, v in series.items()}
    recent, earlier = sum(recent_of.values()), sum(earlier_of.values())
    pct = round((recent - earlier) * 100 / earlier) if earlier else None
    if recent == 0 and earlier == 0:
        return "No signals were found in the last eight weeks.", 0, 0, None
    if recent == 0:
        return f"No signals in the last four weeks, against {earlier} in the four weeks before.", 0, earlier, pct
    if earlier == 0:
        return f"{recent} signal{'' if recent == 1 else 's'} in the last four weeks, none in the four weeks before.", recent, 0, None
    rises = sorted(((recent_of[k] - earlier_of[k], k) for k in series if recent_of[k] - earlier_of[k] >= MIN_RISE), reverse=True)
    if rises:
        top = rises[0][1]
        return (f"More {labels[top].lower()}: {recent_of[top]} in the last four weeks against {earlier_of[top]} before "
                f"({recent} signals in all, against {earlier})."), recent, earlier, pct
    if recent > earlier * 1.2:
        return f"More signals than before: {recent} in the last four weeks against {earlier} in the four weeks before.", recent, earlier, pct
    if recent < earlier * 0.8:
        return f"Fewer signals than before: {recent} in the last four weeks against {earlier} in the four weeks before.", recent, earlier, pct
    return f"About the same as before: {recent} signals in the last four weeks against {earlier}.", recent, earlier, pct


def build_trend(conn: sqlite3.Connection, today: date, weeks: int = WEEKS) -> Trend:
    t = Trend(weeks=[today - timedelta(days=7 * (weeks - 1 - i)) for i in range(weeks)], series={k: [0] * weeks for k, _, _ in TREND_KINDS})
    folded = {g: k for k, groups, _ in TREND_KINDS for g in groups}
    for item in load_items(conn, today, days=weeks * 7):
        kind = folded.get(group_of(item.type, item.source_type))
        i = week_index(item.event_date, today, weeks)
        if kind is None or i is None:
            continue
        t.series[kind][i] += 1
        t.total += 1
    t.takeaway, t.recent, t.earlier, t.pct = takeaway(t.series)
    since = (today - timedelta(days=7)).isoformat()
    t.found_this_week = conn.execute(
        "SELECT COUNT(DISTINCT s.account_id || '|' || s.type || '|' || s.event_date) FROM signals s JOIN accounts a ON a.id = s.account_id "
        "WHERE a.status = 'active' AND s.confidence != 'Low' AND s.detected_date >= ?", (since,)).fetchone()[0]
    return t


def score_history(conn: sqlite3.Connection, account_ids: list[int], points: int = SPARK_POINTS) -> dict[int, list[int]]:
    """The last `points` stored scores per company, oldest first (from the scoring runs; a company never scored has none)."""
    if not account_ids:
        return {}
    out: dict[int, list[int]] = {}
    for r in conn.execute(f"SELECT account_id, score FROM scores WHERE account_id IN ({','.join('?' * len(account_ids))}) "
                          "ORDER BY run_date, account_id", account_ids):
        out.setdefault(r["account_id"], []).append(r["score"])
    return {i: v[-points:] for i, v in out.items()}
