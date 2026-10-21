"""The data behind the private-equity network map and its table, as plain objects. No drawing here (bd.pe_graph draws) and no HTML.

Read-only, from the ownership ledger and the signals already stored. A firm-to-company line is drawn only as the ledger derives it: current is solid, pending is
dashed, an unclear one is a *lead* (dotted, labelled as unconfirmed) and a historical one appears only when asked for. Nothing here is a score of a firm.

What gets a dot is a rule, not a taste: a company is drawn when it is on the scan list, has a signal in the window, or a retrieved page (or the account's own
facts) puts it in the configured area; a company the researched workbook places in Chicago and nothing has confirmed is drawn as a dashed ring. Anything
else collapses into a per-firm count that links to a plain table, so a large portfolio never buries the picture."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date

from bd.accounts import Account
from bd.config import Config
from bd.feed import load_items
from bd.opportunity_model import CATEGORY_ORDER, assess_company
from bd.pe_ownership import company_geography, is_peer_company
from bd.pe_rollup import SIGNAL_DAYS, FirmEvent, company_themes, firm_events
from bd.pe_store import norm_name
from bd.pe_view import reading_sentence, source_dicts

LIVE = ("current", "pending", "unclear")
PER_FIRM_CAP = 30
CAT_KEYS = {"Prospect now": "now", "Validate first": "validate", "Nurture": "nurture", "Monitor": "monitor", "Insufficient evidence": "none"}
CAT_BY_KEY = {v: k for k, v in CAT_KEYS.items()}
NO_CATEGORY = "Insufficient evidence"


@dataclass
class CoNode:
    id: int
    name: str
    account_id: int | None
    category: str = NO_CATEGORY
    events: int = 0                      # distinct scored events behind the category (bd.opportunity_model)
    signals: int = 0                     # stored signals in the window (companies on the scan list only)
    on_list: bool = False
    placement: str = "unknown"           # confirmed (a page or the account's facts) | workbook (the researched workbook only) | unknown
    peer: bool = False
    firms: list[int] = field(default_factory=list)

    @property
    def href(self) -> str:
        return f"/companies/{self.account_id}" if self.account_id else f"/portfolio/{self.id}"

    @property
    def key(self) -> str:
        return CAT_KEYS[self.category]


@dataclass
class FirmNode:
    id: int
    name: str
    scope: str | None
    chicago: bool
    current: int = 0
    pending: int = 0
    leads: int = 0
    past: int = 0
    with_signals: int = 0                # live holdings and leads whose own stored signals are in the window
    drawn: int = 0
    collapsed: int = 0                   # live ties not drawn: counted, listed in the plain table

    @property
    def live(self) -> int:
        return self.current + self.pending + self.leads


@dataclass
class Tie:
    firm_id: int
    company_id: int
    status: str
    ownership_id: int


@dataclass
class AddOn:
    parent_id: int
    child_id: int
    status: str                          # announced | completed | unclear


@dataclass
class Universe:
    firms: list[FirmNode] = field(default_factory=list)              # firms on the map
    companies: dict[int, CoNode] = field(default_factory=dict)       # companies on the map
    ties: list[Tie] = field(default_factory=list)                    # firm-to-company lines, only between things on the map
    add_ons: list[AddOn] = field(default_factory=list)
    co_firms: list[FirmNode] = field(default_factory=list)           # one-firm view: other firms that also hold a drawn company
    all_firms: list[FirmNode] = field(default_factory=list)          # every active firm with its counts, drawn or not (the table)
    focus: int | None = None
    past: bool = False
    cat: str = ""
    scored_on: str | None = None


def _norm_cat(cat: str) -> str:
    return cat if cat in CAT_BY_KEY else ""


def build_universe(conn: sqlite3.Connection, cfg: Config, today: date, firm_id: int | None = None, past: bool = False, cat: str = "") -> Universe:
    """The map's nodes and lines. `firm_id` focuses one firm (its companies, plus the other firms that also hold them); `past` adds historical ties; `cat` keeps
    only companies of one priority category (a key from CAT_KEYS)."""
    cat = _norm_cat(cat)
    u = Universe(focus=firm_id, past=past, cat=cat)
    statuses = LIVE + (("historical",) if past else ())
    marks = ",".join("?" * len(statuses))
    firms = {f["id"]: f for f in conn.execute("SELECT id, name, scope FROM pe_firms WHERE status = 'active' ORDER BY lower(name)")}
    rows = conn.execute(f"SELECT o.id AS oid, o.firm_id, o.company_id, o.status, c.name, c.account_id FROM pe_ownership o JOIN pe_companies c ON c.id = o.company_id "
                        f"WHERE o.status IN ({marks}) ORDER BY o.id", statuses).fetchall()
    rows = [r for r in rows if r["firm_id"] in firms]
    if firm_id is not None:
        held = {r["company_id"] for r in rows if r["firm_id"] == firm_id}
        rows = [r for r in rows if r["company_id"] in held]
    latest = conn.execute("SELECT MAX(run_date) FROM scores").fetchone()[0]
    u.scored_on = latest
    accounts = {r["id"]: r for r in conn.execute("SELECT id, name, last_scanned FROM accounts WHERE status = 'active'")}
    sig_count: dict[int, int] = {}
    for i in load_items(conn, today, SIGNAL_DAYS):
        sig_count[i.account_id] = sig_count.get(i.account_id, 0) + 1
    # a company needs the full assessment only when something stored can speak for it
    assessable = {r[0] for r in conn.execute("SELECT id FROM pe_companies WHERE account_id IS NOT NULL")}
    assessable |= {r[0] for r in conn.execute("SELECT DISTINCT o.company_id FROM pe_ownership o JOIN pe_evidence e ON e.subject_type = 'ownership' AND e.subject_id = o.id "
                                              "WHERE e.verification = 'page_verified' AND e.claim IN ('announced', 'completed')")}
    assessable |= {r[0] for r in conn.execute("SELECT DISTINCT parent_company_id FROM pe_company_links")}
    workbook = {r[0] for r in conn.execute("SELECT DISTINCT subject_id FROM pe_evidence WHERE subject_type = 'company' AND claim = 'location' AND source_type = 'workbook'")}

    nodes: dict[int, CoNode] = {}
    for r in rows:
        if r["company_id"] in nodes:
            continue
        acc = accounts.get(r["account_id"]) if r["account_id"] else None
        n = CoNode(r["company_id"], r["name"], acc["id"] if acc else None, on_list=acc is not None, signals=sig_count.get(acc["id"], 0) if acc else 0)
        n.peer = is_peer_company(conn, n.id, cfg)
        g = company_geography(conn, n.id, cfg).status
        n.placement = "confirmed" if g == "in_scope" else ("out" if g == "out_of_scope" else ("workbook" if n.id in workbook else "unknown"))
        nodes[n.id] = n

    fnodes = {fid: FirmNode(fid, f["name"], f["scope"], f["scope"] == "chicago") for fid, f in firms.items()}
    by_firm: dict[int, list[Tie]] = {}
    for r in rows:
        f = fnodes[r["firm_id"]]
        k = {"current": "current", "pending": "pending", "unclear": "leads", "historical": "past"}[r["status"]]
        setattr(f, k, getattr(f, k) + 1)
        if r["status"] != "historical" and nodes[r["company_id"]].signals:
            f.with_signals += 1
        by_firm.setdefault(r["firm_id"], []).append(Tie(r["firm_id"], r["company_id"], r["status"], r["oid"]))

    def eligible(n: CoNode) -> bool:
        return n.placement != "out" and (n.on_list or n.signals > 0 or n.placement in ("confirmed", "workbook") or n.peer)

    for n in nodes.values():
        if eligible(n) and n.id in assessable and not n.peer:
            a = assess_company(conn, cfg, n.id, today)
            n.category, n.events = a.category, a.events
    drawn_ids: set[int] = set()
    for fid, ties in by_firm.items():
        if firm_id is not None and fid != firm_id:
            continue                                                  # in a one-firm view only the focus decides what is drawn; co-investors just join the lines
        cand = [t for t in ties if eligible(nodes[t.company_id])]
        cand.sort(key=lambda t: (CATEGORY_ORDER.index(nodes[t.company_id].category), -nodes[t.company_id].events, nodes[t.company_id].name.lower()))
        keep = [t for t in cand if not cat or nodes[t.company_id].key == cat][:PER_FIRM_CAP]
        drawn_ids |= {t.company_id for t in keep}
    u.companies = {i: nodes[i] for i in sorted(drawn_ids)}
    u.ties = [t for ts in by_firm.values() for t in ts if t.company_id in drawn_ids]
    for n in u.companies.values():
        n.firms = sorted({t.firm_id for t in u.ties if t.company_id == n.id})
    for fid, ties in by_firm.items():
        f = fnodes[fid]
        f.drawn = sum(t.company_id in drawn_ids for t in ties if t.status != "historical")
        f.collapsed = f.live - f.drawn                                # in a one-firm view a co-investor's counts cover only the focus firm's companies
    u.all_firms = [fnodes[i] for i in sorted(fnodes, key=lambda i: fnodes[i].name.lower())]
    if firm_id is not None:
        u.firms = [fnodes[firm_id]] if firm_id in fnodes else []
        u.co_firms = [fnodes[i] for i in sorted({t.firm_id for t in u.ties if t.firm_id != firm_id}, key=lambda i: fnodes[i].name.lower())]
    else:
        on_map = {t.firm_id for t in u.ties}
        u.firms = [f for f in u.all_firms if f.id in on_map or (f.live and not cat)]
    ids = ",".join("?" * len(drawn_ids))
    if drawn_ids:
        for lk in conn.execute(f"SELECT parent_company_id, child_company_id, status FROM pe_company_links WHERE parent_company_id IN ({ids}) AND child_company_id IN ({ids})",
                               (*drawn_ids, *drawn_ids)):
            u.add_ons.append(AddOn(lk["parent_company_id"], lk["child_company_id"], lk["status"]))
    return u


@dataclass
class CoLine:
    company: CoNode
    held_by: list[tuple[int, str, str]]        # (firm id, firm name, ledger status)


def company_lines(u: Universe) -> list[CoLine]:
    """The drawn companies as a plain list: the text twin of the picture, strongest first, each with who holds it and how the ledger derives that."""
    names = {f.id: f.name for f in u.all_firms}
    out = []
    for n in sorted(u.companies.values(), key=lambda n: (CATEGORY_ORDER.index(n.category), -n.events, n.name.lower())):
        held = sorted({(t.firm_id, names.get(t.firm_id, "a firm"), t.status) for t in u.ties if t.company_id == n.id},
                      key=lambda h: (["current", "pending", "unclear", "historical"].index(h[2]), h[1].lower()))
        out.append(CoLine(n, held))
    return out


# ---- the plain table ---------------------------------------------------------------------------------------------------------------------------------
@dataclass
class FirmLine:
    firm: FirmNode
    themes: list[tuple[str, int]] = field(default_factory=list)      # (theme name, companies showing it on their own signals), strongest first
    event: FirmEvent | None = None
    reading: str = ""
    reading_class: str = ""


def table_lines(conn: sqlite3.Connection, cfg: Config, today: date, u: Universe, limit_themes: int = 2) -> list[FirmLine]:
    """One line per firm that holds anything: counts from the ledger, the themes its companies show, its latest event and how complete its portfolio reading is."""
    latest = u.scored_on
    accounts = {r["id"]: r for r in conn.execute("SELECT * FROM accounts WHERE status = 'active'")}
    themed: dict[int, list] = {}
    out: list[FirmLine] = []
    for f in sorted((f for f in u.all_firms if f.live or f.past), key=lambda f: (-f.current - f.pending, -f.leads, f.name.lower())):
        ln = FirmLine(f)
        counts: dict[str, int] = {}
        for r in conn.execute("SELECT c.id, c.account_id FROM pe_ownership o JOIN pe_companies c ON c.id = o.company_id WHERE o.firm_id = ? AND o.status IN ('current', 'pending', 'unclear')",
                              (f.id,)):
            acc = accounts.get(r["account_id"]) if r["account_id"] else None
            if acc is None or not latest:
                continue
            if acc["id"] not in themed:
                themed[acc["id"]] = company_themes(conn, cfg, Account(**{k: acc[k] for k in Account.model_fields}), latest) if conn.execute(
                    "SELECT 1 FROM scores WHERE account_id = ? AND run_date = ? AND score > 0", (acc["id"], latest)).fetchone() else []
            if themed[acc["id"]]:
                counts[themed[acc["id"]][0].name] = counts.get(themed[acc["id"]][0].name, 0) + 1
        ln.themes = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit_themes]
        ev = firm_events(conn, f.id, today, limit=1)
        ln.event = ev[0] if ev else None
        ln.reading, ln.reading_class = reading_sentence(source_dicts(conn, f.id, today, cfg))
        out.append(ln)
    return out


# ---- the lookup box -----------------------------------------------------------------------------------------------------------------------------------
@dataclass
class LookupHit:
    company_id: int
    name: str
    account_id: int | None
    investors: list[tuple[int, str, str]]     # (firm id, firm name, ledger status)
    signals: int

    @property
    def href(self) -> str:
        return f"/companies/{self.account_id}" if self.account_id else f"/portfolio/{self.company_id}"


def lookup(conn: sqlite3.Connection, today: date, q: str, limit: int = 8) -> list[LookupHit]:
    """Companies whose name, or any other name they are known by, contains the text typed: who the ledger says owns each one. An empty list means the ledger has
    no record, which is not proof the company is not private-equity-backed."""
    q = " ".join((q or "").split())
    if len(q) < 2:
        return []
    like = "%" + q.lower().replace("%", "").replace("_", "") + "%"
    ids = [r[0] for r in conn.execute("SELECT id FROM pe_companies WHERE lower(name) LIKE ? UNION SELECT entity_id FROM pe_aliases WHERE entity_type = 'company' AND lower(alias) LIKE ? "
                                      "LIMIT ?", (like, like, limit * 3))]
    counts: dict[int, int] = {}
    for i in load_items(conn, today, SIGNAL_DAYS):
        counts[i.account_id] = counts.get(i.account_id, 0) + 1
    out: list[LookupHit] = []
    for cid in ids:
        c = conn.execute("SELECT id, name, account_id FROM pe_companies WHERE id = ?", (cid,)).fetchone()
        if c is None:
            continue
        inv = [(r["fid"], r["name"], r["status"]) for r in conn.execute(
            "SELECT f.id AS fid, f.name, o.status FROM pe_ownership o JOIN pe_firms f ON f.id = o.firm_id WHERE o.company_id = ? AND f.status = 'active' "
            "ORDER BY CASE o.status WHEN 'current' THEN 0 WHEN 'pending' THEN 1 WHEN 'unclear' THEN 2 ELSE 3 END, f.name", (cid,))]
        out.append(LookupHit(c["id"], c["name"], c["account_id"], inv, counts.get(c["account_id"], 0) if c["account_id"] else 0))
    out.sort(key=lambda h: (norm_name(h.name) != norm_name(q), h.name.lower()))
    return out[:limit]
