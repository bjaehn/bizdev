"""Signals across a sponsor's portfolio, and the sponsors behind one company's signals, as plain data.

Read-only and deterministic: it joins the ownership ledger (bd.pe_ownership) to the signals and scores already stored for companies on the scan list. Nothing
here is new evidence and nothing is scored: a company's signals, band and theme are its own (bd.feed, bd.score_store, bd.insights), and a sponsor gets counts
and the companies' own lines, never a score.

Three rules keep the roll-up honest:
  * A relationship the ledger derives as current or pending is a *holding*. One that is only unclear (the researched workbook, a listing that disappeared, an
    announcement nobody confirmed closed) is a *lead*: counted and listed apart, never mixed into the holdings. A historical one is only counted.
  * A company not on the scan list has not been scanned, so no signal is shown for it and its silence is never read as quiet.
  * Sharing a sponsor is not evidence of shared needs. Another company's signal is shown beside a company only as "same sponsor", never as its own."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from bd.accounts import Account
from bd.config import Config
from bd.feed import FeedItem, load_items
from bd.insights import Insight, evidence_from_scored, insights_for
from bd.labels import FIRM_TYPE_LABELS
from bd.pe_home import LEDGER_LABELS, LINK_LABELS, _d, _short
from bd.pe_ownership import company_geography, is_peer_company
from bd.score_store import score_one

SIGNAL_DAYS = 90                  # how far back a company's signals count here (the same window as the signal coverage on Admin Home)
EVENT_DAYS = 365                  # how far back a firm's own events count
EVENT_LIMIT = 12
SIBLING_LIMIT = 5
CONFIRMED = ("current", "pending")
HOLD_ORDER = {"current": 0, "pending": 1, "unclear": 2, "historical": 3}


@dataclass
class Held:
    """One firm-to-company relationship from the ledger, with what is stored about the company's own signals."""
    company_id: int
    name: str
    domain: str | None
    ownership_id: int
    status: str                          # the ledger's derived status: current | pending | unclear | historical
    basis: str
    confidence: str
    geography: str                       # in_scope | out_of_scope | unknown (company_geography)
    account_id: int | None = None        # set only when the company is an active account, which is what puts it on the scan list
    scanned: bool = False                # the paid scan has looked at it at least once
    score: int | None = None
    band: str | None = None
    themes: list[Insight] = field(default_factory=list)
    signals: list[FeedItem] = field(default_factory=list)      # inside the window, newest first

    @property
    def on_list(self) -> bool:
        return self.account_id is not None

    @property
    def theme(self) -> Insight | None:
        return self.themes[0] if self.themes else None

    @property
    def latest(self) -> date | None:
        return self.signals[0].event_date if self.signals else None


@dataclass
class ThemeCount:
    name: str
    rule: str
    companies: list[Held] = field(default_factory=list)


@dataclass
class Group:
    """The holdings (or the leads) of one firm. `held` is what is listed; `outside` and `peers` are counted but not listed."""
    held: list[Held] = field(default_factory=list)
    outside: int = 0                     # a retrieved page shows the company outside the configured area
    peers: int = 0                       # a competitor of the client (fit_gate.competitors): shown as a peer elsewhere, never a prospect

    @property
    def total(self) -> int:
        return len(self.held)

    @property
    def on_list(self) -> int:
        return sum(h.on_list for h in self.held)

    @property
    def not_on_list(self) -> int:
        return self.total - self.on_list

    @property
    def scanned(self) -> int:
        return sum(h.scanned for h in self.held)

    @property
    def unscanned(self) -> int:
        return self.on_list - self.scanned

    @property
    def with_signals(self) -> list[Held]:
        return [h for h in self.held if h.signals]

    @property
    def quiet(self) -> list[Held]:
        """Scanned and nothing stored in the window. Quiet can mean nothing is happening or that the sources do not cover it."""
        return [h for h in self.held if h.scanned and not h.signals]

    @property
    def themes(self) -> list[ThemeCount]:
        """The strongest theme of each company that has one, counted by name. A theme is built from one company's own signals; counting it here is not a
        claim that the companies share it for a common reason."""
        by: dict[str, ThemeCount] = {}
        for h in self.held:
            if h.theme:
                by.setdefault(h.theme.key, ThemeCount(h.theme.name, h.theme.rule)).companies.append(h)
        return sorted(by.values(), key=lambda t: (-len(t.companies), t.name))

    @property
    def shared_themes(self) -> list[ThemeCount]:
        """Themes more than one company shows on its own signals: the only ones worth counting, since a theme with one company is already in the table."""
        return [t for t in self.themes if len(t.companies) > 1]


@dataclass
class FirmEvent:
    date: date
    label: str
    text: str
    url: str
    source_type: str
    company: str | None = None
    company_id: int | None = None
    account_id: int | None = None


@dataclass
class FirmRollup:
    firm_id: int
    days: int
    confirmed: Group = field(default_factory=Group)
    leads: Group = field(default_factory=Group)
    historical: int = 0
    scored_on: str | None = None         # the run date of the scores the bands come from


def _accounts(conn: sqlite3.Connection, ids: list[int]) -> dict[int, Account]:
    if not ids:
        return {}
    q = ",".join("?" * len(ids))
    return {r["id"]: Account(**{k: r[k] for k in Account.model_fields})
            for r in conn.execute(f"SELECT * FROM accounts WHERE status = 'active' AND id IN ({q})", ids)}


def firm_rollup(conn: sqlite3.Connection, cfg: Config, firm_id: int, today: date, days: int = SIGNAL_DAYS, themes: bool = True,
                items: list[FeedItem] | None = None) -> FirmRollup:
    """A firm's holdings and leads with each company's own band, theme and recent signals. `items` is the stored feed for `days`, passed in when several
    firms are rolled up in one request; `themes=False` skips the per-company scoring pass that themes need."""
    out = FirmRollup(firm_id, days)
    rows = conn.execute("SELECT o.id AS oid, o.status, o.basis, o.confidence, c.id AS cid, c.name, c.domain, c.account_id FROM pe_ownership o "
                        "JOIN pe_companies c ON c.id = o.company_id WHERE o.firm_id = ? ORDER BY lower(c.name)", (firm_id,)).fetchall()
    accounts = _accounts(conn, [r["account_id"] for r in rows if r["account_id"]])
    latest = conn.execute("SELECT MAX(run_date) FROM scores").fetchone()[0]
    stored = {r["account_id"]: r for r in conn.execute("SELECT account_id, score, band FROM scores WHERE run_date = ?", (latest,))} if latest and accounts else {}
    out.scored_on = latest if stored else None
    if items is None and accounts:
        items = load_items(conn, today, days)
    by_account: dict[int, list[FeedItem]] = {}
    for i in items or ():
        if i.account_id in accounts:
            by_account.setdefault(i.account_id, []).append(i)
    for r in rows:
        if r["status"] == "historical":
            out.historical += 1
            continue
        group = out.confirmed if r["status"] in CONFIRMED else out.leads
        if is_peer_company(conn, r["cid"], cfg):
            group.peers += 1
            continue
        geo = company_geography(conn, r["cid"], cfg)
        if geo.status == "out_of_scope":
            group.outside += 1
            continue
        h = Held(r["cid"], r["name"], r["domain"], r["oid"], r["status"], r["basis"], r["confidence"], geo.status)
        acc = accounts.get(r["account_id"]) if r["account_id"] else None
        if acc is not None:
            h.account_id, h.scanned = acc.id, bool(acc.last_scanned)
            h.signals = by_account.get(acc.id, [])
            s = stored.get(acc.id)
            if s is not None:
                h.score, h.band = s["score"], s["band"]
                if themes and s["score"]:
                    sa = score_one(conn, cfg, acc, date.fromisoformat(latest))
                    h.themes = insights_for(evidence_from_scored(sa.result, sa.rows), cfg.service_lines)
        group.held.append(h)
    for g in (out.confirmed, out.leads):
        g.held.sort(key=lambda h: (-(h.score or 0), -(h.latest.toordinal() if h.latest else 0), h.name.lower()))
    return out


def firm_events(conn: sqlite3.Connection, firm_id: int, today: date, days: int = EVENT_DAYS, limit: int = EVENT_LIMIT) -> list[FirmEvent]:
    """What the firm itself did lately, newest first: its own news (new fund, exit, operating-partner hire), deals and exits a fetched page states about its
    companies, and add-ons by those companies. Every row has a date and a link; a workbook lead is not an event."""
    since = (today - timedelta(days=days)).isoformat()
    out: list[FirmEvent] = []
    for r in conn.execute("SELECT * FROM firm_signals WHERE firm_id = ? AND confidence != 'Low' AND event_date >= ?", (firm_id, since)):
        label = FIRM_TYPE_LABELS.get(r["type"], r["type"].replace("_", " "))
        out.append(FirmEvent(_d(r["event_date"]), label[:1].upper() + label[1:], _short(r["summary"]), r["source_url"], r["source_type"]))
    for r in conn.execute("SELECT e.event_date, e.claim, e.url, e.excerpt, e.source_type, c.id AS cid, c.name AS company, c.account_id AS aid FROM pe_evidence e "
                          "JOIN pe_ownership o ON e.subject_type = 'ownership' AND o.id = e.subject_id JOIN pe_companies c ON c.id = o.company_id "
                          "WHERE o.firm_id = ? AND e.verification = 'page_verified' AND e.event_date >= ? "
                          "AND e.claim IN ('announced', 'completed', 'exited', 'exit_announced')", (firm_id, since)):
        out.append(FirmEvent(_d(r["event_date"]), LEDGER_LABELS[r["claim"]], _short(r["excerpt"] or "", 140), r["url"], r["source_type"], r["company"], r["cid"], r["aid"]))
    for r in conn.execute("SELECT e.event_date, e.claim, e.url, e.excerpt, e.source_type, p.id AS pid, p.name AS parent, ch.name AS child, p.account_id AS aid "
                          "FROM pe_evidence e JOIN pe_company_links l ON e.subject_type = 'link' AND l.id = e.subject_id "
                          "JOIN pe_companies p ON p.id = l.parent_company_id JOIN pe_companies ch ON ch.id = l.child_company_id "
                          "WHERE EXISTS (SELECT 1 FROM pe_ownership o WHERE o.company_id = p.id AND o.firm_id = ? AND o.status IN ('current', 'pending')) "
                          "AND e.verification = 'page_verified' AND e.event_date >= ? AND e.claim IN ('announced', 'completed')", (firm_id, since)):
        out.append(FirmEvent(_d(r["event_date"]), LINK_LABELS[r["claim"]], f"{r['child']}: " + _short(r["excerpt"] or "", 140), r["url"], r["source_type"], r["parent"], r["pid"], r["aid"]))
    seen: set[tuple[str, date]] = set()
    uniq: list[FirmEvent] = []
    for e in sorted((x for x in out if x.date), key=lambda x: (x.date, x.label), reverse=True):
        if (e.url, e.date) in seen:
            continue
        seen.add((e.url, e.date))
        uniq.append(e)
    return uniq[:limit]


# ---- the sponsors behind one company ----------------------------------------------------------------------------------------------------------------
@dataclass
class Investor:
    firm_id: int
    name: str
    ownership_id: int
    status: str
    basis: str
    confidence: str
    events: list[FirmEvent] = field(default_factory=list)
    siblings: list[Held] = field(default_factory=list)          # other companies this sponsor holds that have a signal in the window, strongest first
    siblings_total: int = 0                                      # the sponsor's other holdings counted (current and pending; or leads, for a lead)
    siblings_not_on_list: int = 0                                # of those, not on the scan list: no signal could have been found

    @property
    def lead(self) -> bool:
        return self.status == "unclear"


def company_investors(conn: sqlite3.Connection, cfg: Config, company_id: int, today: date, days: int = SIGNAL_DAYS,
                      items: list[FeedItem] | None = None) -> list[Investor]:
    """The sponsors the ledger records for a company, best-supported first: each one's own recent events, and the signals of the sponsor's other companies,
    labelled on the page as 'same sponsor, not evidence of shared needs'. A lead's siblings are the sponsor's other leads, not its confirmed holdings."""
    inv: list[Investor] = []
    rows = conn.execute("SELECT o.id, o.status, o.basis, o.confidence, f.id AS fid, f.name FROM pe_ownership o JOIN pe_firms f ON f.id = o.firm_id "
                        "WHERE o.company_id = ? AND f.status = 'active'", (company_id,)).fetchall()
    rows = sorted(rows, key=lambda r: (HOLD_ORDER.get(r["status"], 9), r["name"].lower()))
    if items is None and rows:
        items = load_items(conn, today, days)
    for r in rows:
        i = Investor(r["fid"], r["name"], r["id"], r["status"], r["basis"], r["confidence"], firm_events(conn, r["fid"], today))
        if r["status"] != "historical":
            roll = firm_rollup(conn, cfg, r["fid"], today, days, themes=False, items=items)
            same = roll.leads if r["status"] == "unclear" else roll.confirmed
            others = [h for h in same.held if h.company_id != company_id]
            i.siblings_total = len(others)
            i.siblings_not_on_list = sum(not h.on_list for h in others)
            i.siblings = [h for h in others if h.signals][:SIBLING_LIMIT]
        inv.append(i)
    return inv
