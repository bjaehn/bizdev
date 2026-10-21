"""The private equity section of the Analysis page, as plain data: PE-backed companies worth a call, and what sponsors and their companies did lately.

Nothing here is new evidence. The call list is the existing evidence-based priority (bd.opportunity_model) for companies a source shows as currently or
pendingly owned by a sponsor; the feed lists dated, sourced rows already stored (firm signals, page-verified ledger evidence, company signals). A company whose
ownership is only "unclear" is a lead, not a holding, so it is counted but never listed as PE-backed. Coverage says how much of the sponsors' portfolios was read."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from bd.config import Config
from bd.labels import FIRM_TYPE_LABELS, TYPE_LABELS
from bd.opportunity_model import CATEGORY_ORDER, CONF_RANK, assess_company, kind_label
from bd.pe_ownership import company_geography
from bd.pe_view import source_dicts

FEED_DAYS = 90
FEED_LIMIT = 12
CALL_LIMIT = 6
CALL_CATEGORIES = ("Prospect now", "Validate first")
PE_SIGNAL_TYPES = ("pe_platform_or_sponsor_exec", "pe_addon")
# what a PE-backed company can do that a sponsor-minded reader wants to see: new leaders, restructuring, deals, hiring for leaders
BACKED_SIGNAL_TYPES = ("leadership_change", "leadership_transformation_ai_data", "open_exec_search", "hiring_leadership", "restructuring_layoffs", "ma_divestiture",
                       "cost_program", "financial_distress")
LEDGER_LABELS = {"announced": "Deal announced", "completed": "Deal completed", "exited": "Exit", "exit_announced": "Exit announced"}
LINK_LABELS = {"announced": "Add-on announced", "completed": "Add-on completed"}


@dataclass
class CallRow:
    name: str
    account_id: int | None
    company_id: int
    category: str
    score: int
    sponsors: list[str]
    latest_date: date | None
    latest_label: str
    latest_text: str
    latest_url: str
    possible_need: str
    geography: str


@dataclass
class FeedRow:
    date: date
    label: str
    who: str
    text: str
    url: str
    source_type: str
    account_id: int | None = None
    firm_id: int | None = None


@dataclass
class PeHome:
    empty: str = ""                                   # why there is nothing to show, in a sentence
    firms: int = 0
    backed: int = 0                                   # companies a source shows as currently or pendingly owned by a sponsor
    out_of_area: int = 0                              # of those, shown a place outside the configured area (not listed)
    unclear_leads: int = 0                            # account companies whose ownership is only "unclear"
    by_category: dict[str, int] = field(default_factory=dict)
    calls: list[CallRow] = field(default_factory=list)
    feed: list[FeedRow] = field(default_factory=list)
    feed_total: int = 0                               # dated rows inside the window, before the newest FEED_LIMIT are kept
    coverage: str = ""
    coverage_class: str = ""                          # ok | caution | fail, for the notice that shows the coverage


def _d(s: str | None) -> date | None:
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def _short(text: str, n: int = 170) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def coverage_line(conn: sqlite3.Connection, cfg: Config, today: date) -> tuple[str, str]:
    full = partial = unread = 0
    firms = conn.execute("SELECT id FROM pe_firms WHERE status = 'active'").fetchall()
    for f in firms:
        src = source_dicts(conn, f["id"], today, cfg)
        read = [s for s in src if s["status"] == "ok"]
        if not read:
            unread += 1
        elif len(read) == len(src) and all(s["coverage"] == "complete" and not s["stale"] for s in read):
            full += 1
        else:
            partial += 1
    if not firms:
        return "", ""
    text = f"Sponsor portfolios: {full} of {len(firms)} read in full, {partial} partly, {unread} not read yet."
    return text, ("ok" if full == len(firms) else ("fail" if full + partial == 0 else "caution"))


def pe_home(conn: sqlite3.Connection, cfg: Config, today: date) -> PeHome:
    h = PeHome()
    h.firms = conn.execute("SELECT COUNT(*) FROM pe_firms WHERE status = 'active'").fetchone()[0]
    if not h.firms:
        h.empty = "No sponsors are tracked yet. They appear here once the Update has found the private equity firms behind companies on your list."
        return h
    h.coverage, h.coverage_class = coverage_line(conn, cfg, today)
    h.unclear_leads = conn.execute(
        "SELECT COUNT(DISTINCT c.id) FROM pe_companies c JOIN pe_ownership o ON o.company_id = c.id WHERE c.account_id IS NOT NULL AND o.status = 'unclear' "
        "AND c.id NOT IN (SELECT company_id FROM pe_ownership WHERE status IN ('current', 'pending'))").fetchone()[0]
    backed = conn.execute("SELECT c.id, c.name, c.account_id FROM pe_companies c WHERE EXISTS (SELECT 1 FROM pe_ownership o WHERE o.company_id = c.id AND o.status IN ('current', 'pending')) "
                          "ORDER BY lower(c.name)").fetchall()
    sponsors: dict[int, list[str]] = {}
    for r in conn.execute("SELECT o.company_id, f.name FROM pe_ownership o JOIN pe_firms f ON f.id = o.firm_id WHERE o.status IN ('current', 'pending') AND f.status = 'active' "
                          "ORDER BY f.name"):
        sponsors.setdefault(r["company_id"], []).append(r["name"])
    rows: list[CallRow] = []
    for c in backed:
        geo = company_geography(conn, c["id"], cfg).status
        if geo == "out_of_scope":
            h.out_of_area += 1
            continue
        h.backed += 1
        a = assess_company(conn, cfg, c["id"], today)
        h.by_category[a.category] = h.by_category.get(a.category, 0) + 1
        if a.category not in CALL_CATEGORIES:
            continue
        dated = sorted((i for i in a.facts if i.event_date), key=lambda i: i.event_date, reverse=True)
        top = dated[0] if dated else (a.facts[0] if a.facts else None)
        rows.append(CallRow(c["name"], c["account_id"], c["id"], a.category, a.score, sponsors.get(c["id"], []), top.event_date if top else None,
                            kind_label(top.kind) if top else "", _short(top.summary) if top else "", top.url if top else "",
                            a.hypotheses[0].category if a.hypotheses else "", geo))
    rows.sort(key=lambda r: (CATEGORY_ORDER.index(r.category), -r.score, r.name.lower()))
    h.calls = rows[:CALL_LIMIT]
    h.feed, h.feed_total = _feed(conn, today, sponsors)
    return h


def _feed(conn: sqlite3.Connection, today: date, sponsors: dict[int, list[str]]) -> tuple[list[FeedRow], int]:
    since = (today - timedelta(days=FEED_DAYS)).isoformat()
    out: list[FeedRow] = []
    for r in conn.execute("SELECT s.*, f.name AS firm, f.id AS fid FROM firm_signals s JOIN pe_firms f ON f.id = s.firm_id "
                          "WHERE s.event_date >= ? AND s.confidence != 'Low' AND f.status = 'active'", (since,)):
        out.append(FeedRow(_d(r["event_date"]), "Sponsor: " + FIRM_TYPE_LABELS.get(r["type"], r["type"].replace("_", " ")), r["firm"], _short(r["summary"]),
                           r["source_url"], r["source_type"], None, r["fid"]))
    for r in conn.execute("SELECT e.event_date, e.claim, e.url, e.excerpt, e.source_type, f.name AS firm, f.id AS fid, c.name AS company, c.account_id AS aid "
                          "FROM pe_evidence e JOIN pe_ownership o ON e.subject_type = 'ownership' AND o.id = e.subject_id JOIN pe_firms f ON f.id = o.firm_id "
                          "JOIN pe_companies c ON c.id = o.company_id WHERE e.verification = 'page_verified' AND e.event_date >= ? "
                          "AND e.claim IN ('announced', 'completed', 'exited', 'exit_announced') AND f.status = 'active'", (since,)):
        out.append(FeedRow(_d(r["event_date"]), LEDGER_LABELS[r["claim"]], r["firm"], f"{r['company']}: " + _short(r["excerpt"] or "", 140), r["url"], r["source_type"], r["aid"], r["fid"]))
    for r in conn.execute("SELECT e.event_date, e.claim, e.url, e.excerpt, e.source_type, p.name AS parent, ch.name AS child, p.account_id AS aid "
                          "FROM pe_evidence e JOIN pe_company_links l ON e.subject_type = 'link' AND l.id = e.subject_id JOIN pe_companies p ON p.id = l.parent_company_id "
                          "JOIN pe_companies ch ON ch.id = l.child_company_id WHERE e.verification = 'page_verified' AND e.event_date >= ? AND e.claim IN ('announced', 'completed')",
                          (since,)):
        out.append(FeedRow(_d(r["event_date"]), LINK_LABELS[r["claim"]], r["parent"], f"{r['child']}: " + _short(r["excerpt"] or "", 140), r["url"], r["source_type"], r["aid"], None))
    backed_accounts = {r["account_id"]: r["id"] for r in conn.execute("SELECT id, account_id FROM pe_companies WHERE account_id IS NOT NULL AND EXISTS "
                                                                       "(SELECT 1 FROM pe_ownership o WHERE o.company_id = pe_companies.id AND o.status IN ('current', 'pending'))")}
    marks = ",".join("?" * len(BACKED_SIGNAL_TYPES))
    for r in conn.execute(f"SELECT s.*, a.name AS account FROM signals s JOIN accounts a ON a.id = s.account_id WHERE s.event_date >= ? AND s.confidence != 'Low' "
                          f"AND (s.type IN ({','.join('?' * len(PE_SIGNAL_TYPES))}) OR s.type IN ({marks}))", (since, *PE_SIGNAL_TYPES, *BACKED_SIGNAL_TYPES)):
        is_pe_type = r["type"] in PE_SIGNAL_TYPES
        if not is_pe_type and r["account_id"] not in backed_accounts:
            continue                                               # a leadership change is a sponsor story only at a company a source shows as sponsor-owned
        who = r["account"] + (f" ({', '.join(sponsors.get(backed_accounts.get(r['account_id']), []))})" if sponsors.get(backed_accounts.get(r["account_id"])) else "")
        out.append(FeedRow(_d(r["event_date"]), TYPE_LABELS[r["type"]], who, _short(r["summary"]), r["source_url"], r["source_type"], r["account_id"], None))
    seen: set[tuple[str, date]] = set()
    uniq: list[FeedRow] = []
    for r in sorted((x for x in out if x.date), key=lambda x: (x.date, x.label), reverse=True):
        if (r.url, r.date) in seen:
            continue
        seen.add((r.url, r.date))
        uniq.append(r)
    return uniq[:FEED_LIMIT], len(uniq)
