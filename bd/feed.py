"""The signal feed: the newest stored signals across the whole list, grouped by what kind of thing happened.

Read-only and deterministic. Every row is one stored signal with its date, its summary as extracted from the source and the link to
that source; nothing is paraphrased. Groups, in the order a reader cares about them:

  leadership   a leadership signal (new executive, open executive search, transformation/AI/data leader)
  hiring       a hiring signal (posting clusters, senior or interim hires)
  press        any other signal reported in a press release or news article (company release, secondary report)
  events       any other signal from a filing, a notice or the company's own site (restructuring, deals, technology, procurement)
  background   filings and financial facts (risk factors, financial pressure): context, not the lead

Low-confidence signals (rumours) are stored but never shown here, as on every other page. Several rows for one event (same company, type
and date, found at more than one source) show once, at the most trustworthy source."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from bd.insights import HIRING, LEADERSHIP
from bd.labels import TYPE_LABELS

GROUPS = (("leadership", "Leadership changes"), ("hiring", "Hiring"), ("press", "Press and media"),
          ("events", "Company events and technology"), ("background", "Filings and financials"))
GROUP_LABELS = dict(GROUPS)
PRESS_SOURCES = frozenset({"company_release", "secondary_report", "rumor"})
BACKGROUND_TYPES = frozenset({"risk_factor", "financial_pressure"})
FEED_DAYS = 30
PER_GROUP = 6
_RANK = {"High": 0, "Medium": 1, "Low": 2}


def group_of(type_: str, source_type: str) -> str:
    if type_ in LEADERSHIP:
        return "leadership"
    if type_ in HIRING:
        return "hiring"
    if type_ in BACKGROUND_TYPES:
        return "background"
    return "press" if source_type in PRESS_SOURCES else "events"


@dataclass(frozen=True)
class FeedItem:
    id: int
    account_id: int
    account: str
    type: str
    group: str
    event_date: date
    detected_date: date
    summary: str
    url: str
    source_type: str
    confidence: str
    role_or_person: str | None
    service_lines: tuple[str, ...]
    band: str | None                  # the company's band on the latest scores, if it has one
    client: bool

    @property
    def label(self) -> str:
        return TYPE_LABELS.get(self.type, self.type)


@dataclass
class FeedGroup:
    key: str
    label: str
    total: int = 0                    # signals in the window, shown or not
    items: list[FeedItem] = field(default_factory=list)


@dataclass
class Feed:
    days: int
    groups: list[FeedGroup] = field(default_factory=list)
    total: int = 0
    companies: int = 0
    newest: date | None = None

    def group(self, key: str) -> FeedGroup:
        return next(g for g in self.groups if g.key == key)


def load_items(conn: sqlite3.Connection, today: date, days: int = FEED_DAYS, account_id: int | None = None) -> list[FeedItem]:
    """Stored signals dated in the last `days` days for companies that are scanned, newest first, one row per event."""
    since = (today - timedelta(days=days)).isoformat()
    latest = conn.execute("SELECT MAX(run_date) FROM scores").fetchone()[0]
    bands = {r["account_id"]: r["band"] for r in conn.execute("SELECT account_id, band FROM scores WHERE run_date = ?", (latest,))} if latest else {}
    clients = {r["account_id"] for r in conn.execute("SELECT account_id FROM lead_state WHERE client = 1")}
    sql = ("SELECT s.*, a.name AS account_name FROM signals s JOIN accounts a ON a.id = s.account_id "
           "WHERE a.status = 'active' AND s.confidence != 'Low' AND s.event_date >= ? AND s.event_date <= ?")
    args: list = [since, today.isoformat()]
    if account_id is not None:
        sql, args = sql + " AND s.account_id = ?", [*args, account_id]
    best: dict[tuple[int, str, str], sqlite3.Row] = {}
    for r in conn.execute(sql + " ORDER BY s.event_date DESC, s.id", args):
        key = (r["account_id"], r["type"], r["event_date"])
        if key not in best or (_RANK[r["confidence"]], r["id"]) < (_RANK[best[key]["confidence"]], best[key]["id"]):
            best[key] = r
    items = [FeedItem(r["id"], r["account_id"], r["account_name"], r["type"], group_of(r["type"], r["source_type"]),
                      date.fromisoformat(r["event_date"]), date.fromisoformat(r["detected_date"]), r["summary"], r["source_url"],
                      r["source_type"], r["confidence"], r["role_or_person"], tuple(json.loads(r["service_lines"] or "[]")),
                      bands.get(r["account_id"]), r["account_id"] in clients) for r in best.values()]
    items.sort(key=lambda i: (i.event_date, i.detected_date, i.id), reverse=True)
    return items


def build_feed(conn: sqlite3.Connection, today: date, days: int = FEED_DAYS, per_group: int = PER_GROUP) -> Feed:
    items = load_items(conn, today, days)
    feed = Feed(days=days, total=len(items), companies=len({i.account_id for i in items}), newest=items[0].event_date if items else None)
    for key, label in GROUPS:
        mine = [i for i in items if i.group == key]
        feed.groups.append(FeedGroup(key, label, len(mine), mine[:per_group]))
    return feed
